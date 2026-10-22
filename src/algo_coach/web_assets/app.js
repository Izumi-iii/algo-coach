"use strict";

const $ = (id) => document.getElementById(id);
const storageKey = (key) => `algocoach.${location.host}.${key}`;
const remember = (key, value) => { try { value ? localStorage.setItem(storageKey(key), value) : localStorage.removeItem(storageKey(key)); } catch {} };
const recalled = (key) => { try { return localStorage.getItem(storageKey(key)); } catch { return null; } };
let boot = null;
let current = null;
let busy = false;
let uncertain = false;
let remoteBusy = false;
let platformConnected = false;
let platformRunning = false;
let platformTimer = null;
let platformGeneration = 0;
const statusNames = {not_run:"未执行", syntax_error:"语法错误", passed:"本地通过", wrong_answer:"答案错误", runtime_error:"运行错误", timeout:"执行超时", resource_limit:"资源超限", unsupported:"题型未支持", runner_error:"执行器错误", cancelled:"已取消"};

function notice(text, kind = "") {
  $("notice").textContent = text;
  $("notice").className = `notice ${kind}`;
}

async function api(path, body) {
  let response;
  try {
    response = await fetch(path, {cache:"no-store", ...(body ? {
      method:"POST", headers:{"Content-Type":"application/json", "X-AlgoCoach-Token":boot.csrf_token}, body:JSON.stringify(body),
    } : {})});
  } catch {
    throw new Error("连接中断。请刷新页面恢复任务状态，勿再次提交同一请求。");
  }
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.error || "操作未完成");
    error.status = response.status;
    throw error;
  }
  return data;
}

function controls() {
  const blocked = !boot || busy || uncertain || remoteBusy;
  $("new-session").querySelectorAll("button,input,select").forEach((node) => { node.disabled = blocked; });
  $("source").disabled = blocked;
  $("reference").hidden = $("source").value === "daily";
  for (const id of ["message","send","hint","answer","code","submit-code"]) $(id).disabled = blocked || !current;
  $("doctor").disabled = blocked;
  $("retry").disabled = blocked || !current?.last_error;
  $("retry").hidden = !current?.last_error;
  $("sessions").querySelectorAll("button").forEach((node) => { node.disabled = blocked; });
  const draft = current && $("code").value !== current.current_code;
  $("draft-report-note").hidden = !draft || !current?.current_code;
  $("test-code").disabled = blocked || !current?.current_code || draft;
  $("test-code").title = draft ? "请先提交当前编辑的代码，再验证该版本。" : "仅测试已提交版本，不调用教学模型。";
  $("draft-status").textContent = !current ? "等待选择题目" : !current.current_code ? "尚未提交" : draft ? "有未提交修改" : "已保存到会话";
  document.querySelectorAll("[data-browser-bind]").forEach(node => { node.disabled = blocked || !current?.current_code || draft; });
  $("submit-platform").disabled = blocked || platformRunning || !platformConnected || !current?.current_code || draft;
  document.querySelectorAll("[data-platform-control]").forEach(node => { node.disabled = blocked || platformRunning || !platformConnected; });
  const feedback = current?.browser_feedback;
  $("browser-binding").textContent = feedback ? `已匹配当前保存版本：提交 ${feedback.submission_id} · 浏览器回传报告 ${feedback.verdict}。${draft ? "编辑器有草稿修改，报告仅对应保存版本。" : "可在聊天中询问此结果；它不代表独立完成。"}` : "当前保存代码尚未匹配浏览器报告；其他会话或版本的结果不能用于当前教学。";
}

function renderSessions() {
  $("sessions").replaceChildren();
  for (const item of boot.sessions) {
    const button = document.createElement("button");
    button.className = `session ${item.id === current?.session_id ? "active" : ""}`;
    button.dataset.session = item.id;
    const title = document.createElement("strong"); title.textContent = item.title;
    const date = document.createElement("small");
    date.textContent = new Date(item.created).toLocaleString("zh-CN", {month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"});
    button.append(title,date);
    button.addEventListener("click", () => restore(item.id));
    $("sessions").append(button);
  }
  if (!boot.sessions.length) {
    const empty = document.createElement("p"); empty.className = "quiet"; empty.textContent = "还没有练习记录。\n从上方选择一道题开始。"; $("sessions").append(empty);
  }
  $("session-count").textContent = `${boot.sessions.length} 个会话`;
}

async function refreshBoot() {
  boot = await api("/api/bootstrap");
  remoteBusy = boot.busy && !busy;
  $("mode").textContent = boot.mode === "demo" ? "离线演示" : "真实模型";
  $("model-notice").textContent = boot.mode === "demo" ? "演示教学使用预置回复，不理解你的思路；本地测试仍可单独执行。" : "对话、题目和提交的代码会发送给 .env 中配置的模型服务。";
  $("problem-options").replaceChildren();
  for (const problem of boot.problems) {
    const option = document.createElement("option"); option.value = problem.id; option.label = problem.title; $("problem-options").append(option);
  }
  renderSessions(); controls();
  if (remoteBusy) setTimeout(async () => { try { await refreshBoot(); } catch (error) { notice(error.message,"error"); } },1200);
}

function element(tag, text, className = "") {
  const node = document.createElement(tag); node.textContent = text; node.className = className; return node;
}

function setProblemExpanded(expanded) {
  $("problem-details").hidden = !expanded;
  $("problem-toggle").setAttribute("aria-expanded", String(expanded));
  $("problem-toggle").textContent = expanded ? "收起题面 ↑" : "展开题面 ↓";
}

function renderProblem(problem) {
  $("problem-title").textContent = `${problem.frontend_id}. ${problem.title.replace(/（本地教学摘要）$/, "")}`;
  $("empty-intro").hidden = true;
  $("problem-toggle").hidden = false;
  const body = $("problem-body"); body.replaceChildren(element("div",problem.statement,"problem-text"));
  body.append(element("h3","输入与约束","subheading"));
  const constraints = document.createElement("ul"); constraints.className = "constraints";
  problem.constraints.forEach((text) => constraints.append(element("li",text))); body.append(constraints);
  problem.examples.forEach((sample,index) => {
    body.append(element("h3",`示例 ${index + 1}`,"subheading"),element("div",`输入：${sample.input}\n输出：${sample.output}`,"example"));
  });
  body.append(element("h3","Python 方法签名","subheading"),element("pre",problem.signature,"signature"));
  const link = $("problem-link");
  link.hidden = !problem.url;
  if (problem.url) link.href = problem.url;
  const source = current.problem_source;
  $("provenance").textContent = source ? `来源：LeetCode 中国站 · 缓存：${source.cache_status}\n获取时间：${source.fetched_at || "—"}${source.warning ? `\n${source.warning}` : ""}` : problem.attribution;
}

function appendMessage(role, content) {
  const article = document.createElement("article"); article.className = `message ${role}`;
  const label = element("div","","message-label"); label.append(element("span",role === "user" ? "你" : "ac"), document.createTextNode(role === "user" ? "你的思路" : "AlgoCoach"));
  const body = document.createElement("div"); body.className = "message-content";
  // Render text and fenced code through textContent, never model-controlled HTML.
  const chunks = content.split(/```[^\n]*\n([\s\S]*?)```/g);
  chunks.forEach((text,index) => {
    if (!text) return;
    if (index % 2) { const pre = document.createElement("pre"); pre.append(element("code",text)); body.append(pre); }
    else body.append(element("p",text));
  });
  article.append(label,body); $("messages").append(article);
}

function starter(problem) {
  const signature = problem.signature.match(/\bdef\s+[^\n]+/);
  return signature ? `class Solution:\n    ${signature[0].replace(/:\s*$/,"")}:\n        pass\n` : "# 在这里编写你的 Python 实现。\n";
}

function lineNumbers() {
  $("line-numbers").textContent = Array.from({length:$("code").value.split("\n").length}, (_,i) => i+1).join("\n");
  $("line-numbers").scrollTop = $("code").scrollTop;
  controls();
}

function renderReport() {
  const report = current.verification;
  $("verdict").textContent = report ? statusNames[report.status] || report.status : "未提交";
  $("verdict").className = `verdict ${report?.status === "passed" ? "good" : report && !["not_run","unsupported"].includes(report.status) ? "bad" : "neutral"}`;
  $("verification-detail").textContent = report?.detail || "尚无用户代码提交。先在编辑器中写下实现，再提交分析。";
  $("verification-stats").replaceChildren();
  if (report) {
    for (const [title,value] of [["执行情况",report.executed ? "已执行" : "未执行"],["本地用例",`${report.passed_count} / ${report.case_count}`],["代码版本",report.code_hash.slice(0,10)]]) {
      const node = element("div",title,"stat"); node.append(element("strong",value)); $("verification-stats").append(node);
    }
  }
  $("failure").hidden = !report?.failure;
  $("failure").textContent = report?.failure ? JSON.stringify(report.failure,null,2) : "";
  $("reference-report").hidden = !current.answer_checks.length;
  $("reference-reports").replaceChildren();
  for (const check of current.answer_checks) $("reference-reports").append(element("div",`${statusNames[check.status] || check.status} · ${check.passed_count}/${check.case_count}\n${check.detail}\n代码：${check.code_hash}`,"reference-item"));
}

function renderState(next, discardDraft = false) {
  const previous = current;
  const oldDraft = $("code").value;
  const keepDraft = !discardDraft && previous?.session_id === next.session_id && oldDraft !== (previous.current_code || starter(previous.problem)) && oldDraft !== next.current_code;
  current = next;
  if (previous?.session_id !== current.session_id) {
    platformGeneration++; clearTimeout(platformTimer);
    $("platform-status").textContent = "正式提交会增加原站账号记录；模型不会自动提交。";
  }
  if (previous?.session_id !== current.session_id) $("message").value = "";
  remember("session",current.session_id);
  renderProblem(current.problem);
  if (previous?.session_id !== current.session_id) setProblemExpanded(recalled("problem-expanded") === "yes");
  $("hint-level").textContent = `H${current.hint_level} · ${current.answer_revealed ? "已查看完整答案" : "渐进提示"}`;
  $("messages").replaceChildren();
  if (current.history_total > current.history.length) $("messages").append(element("p",`展示最近 ${current.history.length} 条消息；更早内容仍保存在本地。`,"quiet"));
  const openingQuestion = "你目前对这道题有什么思路，或需要先澄清哪一点？";
  current.history.forEach((message, index) => {
    // Only shorten the application's initial greeting, never later discussion or
    // a truncated history. The full original message stays in session storage.
    const greeting = index === 0 && current.history_total === current.history.length && message.role === "assistant" && message.content.trimEnd().endsWith(openingQuestion);
    appendMessage(message.role, greeting ? openingQuestion : message.content);
  });
  if (current.last_error && current.response) appendMessage("assistant",current.response);
  $("messages").scrollTop = $("messages").scrollHeight;
  if (!keepDraft) $("code").value = current.current_code || starter(current.problem);
  $("attempts").textContent = `已保存 ${current.attempt_count} 次代码提交`;
  renderReport(); renderSessions(); lineNumbers(); controls();
  refreshPlatform().catch(() => {});
}

function hasUnsaved() {
  return current && ($("code").value !== (current.current_code || starter(current.problem)) || $("message").value.trim());
}

async function restore(sid, force = false) {
  if (!force && hasUnsaved() && !confirm("有未提交的代码或消息。切换会话会丢弃这些修改，是否继续？")) return;
  try {
    const state = await api(`/api/sessions/${sid}`); renderState(state,!force);
    const submissions = await refreshPlatform().catch(() => []);
    const pending = submissions?.find(item => ["prepared","checking","queued","judging"].includes(item.state) && !item.last_error);
    if (pending && platformConnected && !platformRunning) drivePlatform(pending.operation_id);
    notice(state.last_error ? "上次教学请求未完成，输入已保存，可以重试。" : "会话已恢复，可以继续推演。",state.last_error ? "error" : "");
  } catch (error) { notice(error.message,"error"); }
}

const delay = (ms) => new Promise((resolve) => setTimeout(resolve,ms));
async function waitForJob(id) {
  let errors = 0;
  while (true) {
    let job;
    try { job = await api(`/api/jobs/${id}`); errors = 0; }
    catch (error) {
      if (error.status || ++errors >= 3) throw error;
      notice("连接暂时中断，正在恢复任务状态，不会重新发送请求。","busy"); await delay(1200); continue;
    }
    if (job.status === "running") { await delay(700); continue; }
    remember("pending",null); uncertain = false;
    if (job.status === "failed") {
      if (current) await restore(current.session_id,true);
      throw new Error(job.error);
    }
    if (job.result.state) {
      renderState(job.result.state);
      notice(current.last_error ? "教学回复未完成，输入已保存。可用“重试上一请求”继续。" : "已保存。继续验证你的想法。",current.last_error ? "error" : "");
    }
    if (job.result.doctor) {
      const result = job.result.doctor;
      $("execution-environment").textContent = `${result.enabled ? "执行器已启用" : "执行器未启用"} · ${result.reason}`;
      notice(result.enabled && result.ready ? "Docker 引擎和镜像可用。代码测试仍需单独执行。" : result.reason, result.ready ? "" : "error");
    }
    return;
  }
}

async function act(operation, fields = {}) {
  if (busy || uncertain || remoteBusy) return false;
  let completed = false;
  busy = true; controls();
  notice(operation === "test" ? "正在运行已提交版本的本地测试…" : operation === "doctor" ? "正在检查 Docker 与判题镜像…" : "正在处理，请等待结果。输入提交后不会自动重发。","busy");
  const id = crypto.randomUUID().replaceAll("-","");
  remember("pending",id);
  try {
    const job = await api("/api/actions",{operation,request_id:id,...fields});
    await waitForJob(job.job_id);
    completed = true;
  } catch (error) {
    if (error.status && error.status < 500) remember("pending",null);
    uncertain = Boolean(recalled("pending"));
    notice(error.message,"error");
  } finally {
    busy = false;
    try { await refreshBoot(); } catch (error) { notice(error.message,"error"); }
    controls();
  }
  return completed;
}

async function recoverPending() {
  const id = recalled("pending");
  if (!id || busy) return;
  busy = true; uncertain = false; controls(); notice("正在恢复上一项任务的状态，不会重新提交…","busy");
  try { await waitForJob(id); }
  catch (error) {
    if (error.status === 404) remember("pending",null);
    uncertain = Boolean(recalled("pending")); notice(error.message,"error");
  } finally { busy = false; await refreshBoot(); controls(); }
}

$("new-session").addEventListener("submit",async (event) => {
  event.preventDefault();
  if (hasUnsaved() && !confirm("新练习会丢弃未提交的代码或消息，是否继续？")) return;
  await act("create",{source:$("source").value,reference:$("reference").value});
});
$("source").addEventListener("change",() => {
  $("source-help").textContent = {local:"本地题库无需联网获取题面。",remote:"点击开始后，从中国站获取公开题面。",daily:"点击开始后，获取中国站当前每日一题。"}[$("source").value]; controls();
});
$("chat-form").addEventListener("submit",async (event) => {
  event.preventDefault(); const text = $("message").value.trim(); if (!text || !current) return;
  const completed = await act("turn",{session_id:current.session_id,text});
  if (completed) $("message").value = "";
});
$("message").addEventListener("keydown",(event) => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); $("chat-form").requestSubmit(); } });
for (const [id,text] of [["hint","/hint"],["answer","/answer"],["retry","/retry"]]) $(id).addEventListener("click",() => act("turn",{session_id:current.session_id,text}));
$("submit-code").addEventListener("click",() => act("code",{session_id:current.session_id,code:$("code").value}));
$("test-code").addEventListener("click",() => act("test",{session_id:current.session_id,code_hash:current.code_hash}));
$("doctor").addEventListener("click",() => act("doctor"));
$("problem-toggle").addEventListener("click",() => {
  const expanded = $("problem-details").hidden;
  setProblemExpanded(expanded);
  remember("problem-expanded", expanded ? "yes" : "no");
});
$("code").addEventListener("input",lineNumbers);
$("code").addEventListener("scroll",() => { $("line-numbers").scrollTop = $("code").scrollTop; });
$("code").addEventListener("keydown",(event) => { if (event.key === "Tab") { event.preventDefault(); $("code").setRangeText("    ",$("code").selectionStart,$("code").selectionEnd,"end"); lineNumbers(); } });
$("refresh-sessions").addEventListener("click",async () => { try { await refreshBoot(); if (recalled("pending")) await recoverPending(); } catch (error) { notice(error.message,"error"); } });
$("date-label").textContent = new Date().toLocaleDateString("zh-CN",{timeZone:"Asia/Shanghai",month:"long",day:"numeric",weekday:"short"});

async function refreshBrowserReceipts() {
  const result = await api("/api/bridge/results");
  const panel = $("browser-receipts"); panel.replaceChildren();
  if (!result.receipts.length) panel.textContent = "暂无回传。加载 extensions/leetcode-bridge 扩展后连接本页。";
  for (const receipt of result.receipts) {
    const scope = receipt.detail ? "浏览器历史记录；是否用于当前教学，以上方代码匹配状态为准。" : receipt.scope;
    const row = element("p",`提交 ${receipt.submission_id} · 浏览器报告：${receipt.verdict}\n${scope} `);
    const link = element("a","查看原站记录 ↗");
    link.href = `https://leetcode.cn/submissions/detail/${encodeURIComponent(receipt.submission_id)}/`;
    link.target = "_blank"; link.rel = "noopener noreferrer"; row.append(link); panel.append(row);
    if (receipt.detail) {
      row.append(element("span",` · 题号 ${receipt.detail.frontend_id} · ${receipt.detail.language} `));
      const button = element("button","匹配当前保存代码","text-button"); button.dataset.browserBind = receipt.receipt_id;
      button.addEventListener("click",async () => {
        if (!current || $("code").value !== current.current_code) return;
        if (await act("bind_browser",{session_id:current.session_id,receipt_id:receipt.receipt_id,code_hash:current.code_hash})) notice("匹配成功。可在聊天中询问这条浏览器报告；未运行本地测试，也未自动标记完成。");
      });
      row.append(button);
    } else row.append(element("span"," · 缺少匹配字段，请更新扩展后重新读取。"));
  }
  controls();
}
$("refresh-browser").addEventListener("click",async () => { try { await refreshBrowserReceipts(); } catch(error) { notice(error.message,"error"); } });

async function init() {
  controls();
  try {
    await refreshBoot();
    await detectPlatform();
    if (!boot.busy) {
      try { await refreshBrowserReceipts(); }
      catch { $("browser-receipts").textContent = "回传服务暂不可用；若刚更新代码，请重启 Web 服务。"; }
    }
    $("execution-environment").textContent = boot.runner_enabled ? "执行器配置已启用。可检查环境后运行已提交版本。" : "执行器尚未启用，测试会如实返回未执行。配置见 Docs/RUNNER.md。";
    const sid = recalled("session");
    if (sid && boot.sessions.some((s) => s.id === sid) && !boot.busy) await restore(sid,true);
    else notice(boot.busy ? "另一页面的操作正在进行，请等待完成。" : "本地工作区已就绪。选择题目开始，或恢复左侧会话。",boot.busy ? "busy" : "");
    if (recalled("pending")) await recoverPending();
  } catch (error) { notice(error.message,"error"); }
}
window.addEventListener("DOMContentLoaded", init);
