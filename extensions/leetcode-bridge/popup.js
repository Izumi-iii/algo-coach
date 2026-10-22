"use strict";
const status = document.getElementById("status");
const local = (value) => {
  const url = new URL(value);
  if (url.protocol !== "http:" || url.hostname !== "127.0.0.1" || url.username || url.password) throw new Error("请在 127.0.0.1 的 AlgoCoach 网页上连接。");
  return url.origin;
};
async function action(work) {
  document.querySelectorAll("button").forEach(b => b.disabled = true);
  try { await work(); }
  catch (error) { status.textContent = error.message === "Failed to fetch" ? "本地连接失败，请启动 AlgoCoach 并重新连接。" : "操作未完成，请检查当前页面和本地连接后重试。"; }
  finally { document.querySelectorAll("button").forEach(b => b.disabled = false); }
}
document.getElementById("connect").addEventListener("click", () => action(async () => {
  const [tab] = await chrome.tabs.query({active:true,currentWindow:true});
  let base;
  try { base = local(tab.url); } catch { status.textContent="请先切换到 http://127.0.0.1 的 AlgoCoach 网页，再点击连接。"; return; }
  const [injection] = await chrome.scripting.executeScript({target:{tabId:tab.id},func:async (extensionId) => {
    const boot = await (await fetch("/api/bootstrap",{cache:"no-store"})).json();
    const response = await fetch("/api/bridge/pair",{method:"POST",headers:{"Content-Type":"application/json","X-AlgoCoach-Token":boot.csrf_token},body:JSON.stringify({extension_id:extensionId})});
    return response.ok ? await response.json() : null;
  },args:[chrome.runtime.id]});
  if (!injection.result?.token) { status.textContent="本地服务尚未支持桥接，或连接未完成；请重启新版 Web 服务。"; return; }
  await chrome.storage.session.set({connection:{base,token:injection.result.token,expires:Date.now()+injection.result.expires_in*1000}});
  status.textContent="已连接。现在打开一条中国站已有提交，再点击读取。此连接用于回传一条结果，15 分钟内有效。";
}));
document.getElementById("read").addEventListener("click", () => action(async () => {
  const {connection} = await chrome.storage.session.get("connection");
  if (!connection || connection.expires <= Date.now()) { status.textContent="请先切换到本地 AlgoCoach 网页重新连接。"; return; }
  const base = local(connection.base);
  const [tab] = await chrome.tabs.query({active:true,currentWindow:true});
  if (new URL(tab.url).origin !== "https://leetcode.cn") { status.textContent="请切换到中国站已有提交详情页。"; return; }
  const [injection] = await chrome.scripting.executeScript({target:{tabId:tab.id},files:["read-submission.js"]});
  const result = injection.result;
  if (!result || result.error) { status.textContent=result?.error || "未能读取当前页，请确认是提交详情页。"; return; }
  const response = await fetch(base+"/api/bridge/results",{method:"POST",redirect:"error",headers:{"Content-Type":"application/json","X-AlgoCoach-Bridge":connection.token},body:JSON.stringify(result)});
  const receipt = await response.json();
  if (!response.ok) { status.textContent=receipt.error || "回传失败，请重新连接。"; return; }
  status.textContent=`已回传提交 ${receipt.submission_id}：${receipt.verdict}。回到 AlgoCoach 点击“刷新回传”。${receipt.detail ? "可选择“匹配当前保存代码”后用于教学。" : "缺少匹配字段，仅展示历史状态。"}`;
}));
