"use strict";
// Shared UI globals/functions are defined by app.js. All external writes start with a click.
const extensionRequests = new Map();
window.addEventListener("message",event=>{
  if(event.source!==window || event.origin!==location.origin || event.data?.channel!=="algocoach-extension") return;
  const pending=extensionRequests.get(event.data.id);
  if(!pending) return;
  clearTimeout(pending.timer); extensionRequests.delete(event.data.id); pending.resolve(event.data.result);
});
function extensionRequest(action, operationId) {
  const id=crypto.randomUUID().replaceAll("-","");
  return new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>{extensionRequests.delete(id);reject(new Error("未收到扩展响应。请重新加载扩展并刷新本页；已发送操作不会自动重发。"));},action==="step"?120000:3000);
    extensionRequests.set(id,{resolve,timer});
    window.postMessage({channel:"algocoach-page",id,action,operation_id:operationId},location.origin);
  });
}
async function detectPlatform(action="status") {
  try {
    const result=await extensionRequest(action);
    platformConnected=Boolean(result?.connected);
    $("platform-connection").textContent=platformConnected?`扩展 ${result.version} 已连接。提交使用浏览器中已登录的中国站账号。`:"扩展未连接。请加载 v0.3.0 或更新版本，刷新本页后点击连接扩展。";
  } catch {
    platformConnected=false;
    $("platform-connection").textContent="未检测到提交扩展。请在扩展管理页加载/重新加载 extensions/leetcode-bridge，再刷新本页。";
  }
  controls();
}
const platformStates={prepared:"等待扩展处理",checking:"检查登录与提交目标",sending:"发送受理情况待确认",unknown:"受理情况不明，禁止重发",queued:"已受理，等待判题",judging:"正在判题",finished:"判题完成",blocked:"提交前检查未完成",cancelled:"代码变化，提交已取消"};
const platformVerdicts={accepted:"通过（Accepted）",wrong_answer:"答案错误",compile_error:"编译错误",runtime_error:"运行错误",time_limit:"超出时间限制",memory_limit:"超出内存限制",output_limit:"超出输出限制"};
async function refreshPlatform() {
  if(!current) return;
  const sid=current.session_id;
  const data=await api(`/api/platform/session/${sid}`);
  if(current?.session_id!==sid) return;
  const panel=$("platform-submissions");panel.replaceChildren();
  for(const item of data.submissions) {
    const active=current.browser_feedback?.receipt_id===item.receipt_id && item.receipt_id;
    const text=`${platformStates[item.state]||item.state}${item.verdict?` · ${platformVerdicts[item.verdict]||item.verdict}`:""} · 代码 ${item.code_hash.slice(0,8)}${item.state==="finished"?(active?" · 已用于当前教学":" · 历史版本结果"):""}`;
    const row=element("div",text,"platform-result");
    if(item.last_error) row.append(element("p",item.last_error));
    if(item.runtime || item.memory) row.append(element("p",`用时：${item.runtime||"未返回"} · 内存：${item.memory||"未返回"}`));
    if(Object.keys(item.failure_details||{}).length) {
      const details=document.createElement("details");details.append(element("summary","原站判题详情"));
      details.append(element("pre",Object.entries(item.failure_details).map(([key,value])=>`${key}:\n${value}`).join("\n\n"),"failure"));
      row.append(details);
    }
    if(item.submission_id) {
      const link=element("a",`提交 ${item.submission_id} ↗`);link.href=`https://leetcode.cn/submissions/detail/${encodeURIComponent(item.submission_id)}/`;link.target="_blank";link.rel="noopener noreferrer";row.append(link);
    }
    if(["prepared","checking","queued","judging"].includes(item.state)) {
      const resume=element("button","继续处理 / 查询","text-button");resume.dataset.platformControl="true";resume.disabled=platformRunning;
      resume.addEventListener("click",()=>drivePlatform(item.operation_id));row.append(resume);
    }
    if(["sending","unknown"].includes(item.state) || (item.recovered && ["queued","judging"].includes(item.state))) {
      row.append(element("p","先到原站确认是否已受理。填写已有提交 ID 后只查询与核对，绝不重新提交。"));
      const input=element("input","");input.placeholder="原站已有提交 ID";input.inputMode="numeric";input.setAttribute("aria-label","恢复原站提交 ID");
      const recover=element("button","核对已有提交","text-button");recover.dataset.platformControl="true";
      recover.addEventListener("click",async()=>{
        if(!/^[0-9]{1,30}$/.test(input.value.trim()) || current?.session_id!==sid) return;
        try {await api("/api/platform/recover",{session_id:sid,operation_id:item.operation_id,submission_id:input.value.trim()});await drivePlatform(item.operation_id);}
        catch(error){notice(error.message,"error");}
      });row.append(input,recover);
    }
    panel.append(row);
  }
  controls();
  return data.submissions;
}
async function drivePlatform(operationId, count=0) {
  if(platformRunning || !current) return;
  clearTimeout(platformTimer);
  const generation=platformGeneration, sid=current.session_id;
  platformRunning=true;controls();
  $("platform-status").textContent="正在通过浏览器处理提交与判题。请保持本页和浏览器开启。";
  let next=null;
  try {
    const reply=await extensionRequest("step",operationId);
    if(reply?.error) throw new Error("扩展处理未完成。请确认原站登录/访问验证，再连接扩展并继续查询；不会自动重发已发送提交。");
    const record=reply.submission;
    if(reply.busy) next=operationId;
    else if(record) {
      if(generation!==platformGeneration || current?.session_id!==sid) return;
      $("platform-status").textContent=`${platformStates[record.state]||record.state}${record.verdict?`：${platformVerdicts[record.verdict]||record.verdict}`:""}${record.last_error?`。${record.last_error}`:""}`;
      if(["prepared","checking","queued","judging"].includes(record.state) && !record.last_error && count<30) next=operationId;
      if(record.state==="finished") {
        const state=await api(`/api/sessions/${sid}`);renderState(state);
        await refreshBrowserReceipts();
        notice(state.browser_feedback?.receipt_id===record.receipt_id?"LeetCode 结果已自动匹配保存版本，可以继续向教练提问。":"判题完成；代码版本已变化，结果仅保留为历史记录。");
      }
    }
    await refreshPlatform();
  } catch(error) {$("platform-status").textContent=error.message;await refreshPlatform().catch(()=>{});}
  finally {
    platformRunning=false;controls();
    if(next && generation===platformGeneration && current?.session_id===sid) platformTimer=setTimeout(()=>drivePlatform(next,count+1),2000);
  }
}
$("connect-platform").addEventListener("click",()=>detectPlatform("connect"));
$("submit-platform").addEventListener("click",async()=>{
  if(!current?.current_code || $("code").value!==current.current_code || platformRunning || !platformConnected) return;
  const sid=current.session_id, hash=current.code_hash;
  if(!confirm(`提交题目 ${current.problem.frontend_id}（${current.problem.title}）的当前保存版本到 LeetCode？\n语言：Python3；代码版本：${hash.slice(0,12)}。\n这会增加当前浏览器登录账号的正式提交记录。${boot.mode==="demo"?"\n当前虽为演示教学模式，此次平台提交仍是真实提交。":""}`)) return;
  platformRunning=true;controls();
  try {
    const record=await api("/api/platform/prepare",{session_id:sid,operation_id:crypto.randomUUID().replaceAll("-",""),code_hash:hash});
    platformRunning=false;
    await drivePlatform(record.operation_id);
  } catch(error){notice(error.message,"error");}
  finally{platformRunning=false;controls();}
});
