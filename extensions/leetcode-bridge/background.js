"use strict";
importScripts("platform-page.js");
const running=new Set();
function localOrigin(value) {
  const u=new URL(value);
  if(u.protocol!=="http:" || u.hostname!=="127.0.0.1" || u.username || u.password) throw new Error("origin");
  return u.origin;
}
async function server(connection, action, fields) {
  const abort=new AbortController(); const timer=setTimeout(()=>abort.abort(),10000);
  try {
    const r=await fetch(connection.base+"/api/bridge/submit/step",{method:"POST",redirect:"error",signal:abort.signal,headers:{"Content-Type":"application/json","X-AlgoCoach-Bridge":connection.token},body:JSON.stringify({action,...fields})});
    const data=await r.json();
    if(!r.ok) throw new Error("local");
    return data;
  } finally {clearTimeout(timer);}
}
async function leetcodeTab(job) {
  const tabs=await chrome.tabs.query({url:"https://leetcode.cn/*"});
  let tab=tabs.find(t=>{try{return new URL(t.url).pathname.startsWith(`/problems/${job.slug}/`);}catch{return false;}});
  if(!tab) tab=await chrome.tabs.create({url:`https://leetcode.cn/problems/${job.slug}/`,active:false});
  for(let i=0;i<20;i++) {
    tab=await chrome.tabs.get(tab.id);
    if(tab.status==="complete") return tab;
    await new Promise(resolve=>setTimeout(resolve,500));
  }
  throw new Error("tab");
}
async function page(action, job) {
  const tab=await leetcodeTab(job);
  const [injection]=await chrome.scripting.executeScript({target:{tabId:tab.id},func:algoCoachPlatformPage,args:[action,job]});
  const result=injection?.result || {error:"extension"};
  if(["login","access","csrf"].includes(result.error)) await chrome.tabs.update(tab.id,{active:true});
  return result;
}
async function step(connection, operationId) {
  const key=`pending:${connection.base}:${operationId}`;
  let pending=(await chrome.storage.session.get(key))[key];
  if(pending) {
    const saved=await server(connection,pending.action,{operation_id:operationId,...pending.fields});
    await chrome.storage.session.remove(key);
    if(saved.state==="finished") return saved;
  }
  let job=await server(connection,"claim",{operation_id:operationId});
  async function deliver(action, fields) {
    await chrome.storage.session.set({[key]:{action,fields}});
    const saved=await server(connection,action,{operation_id:operationId,...fields});
    await chrome.storage.session.remove(key);
    return saved;
  }
  if(job.state==="checking") {
    const check=await page("preflight",job);
    if(check.error) return deliver("fail",{code:check.error});
    // The server commits 'sending' before returning this one-use authorization.
    await server(connection,"authorize",{operation_id:operationId,target:check.target});
    job={...job,target:check.target};
    const sent=await page("submit",job);
    if(sent.error) return deliver("fail",{code:sent.error});
    return deliver("accepted",{submission_id:sent.submission_id});
  }
  if(["queued","judging"].includes(job.state)) {
    const result=await page("poll",job);
    if(result.error) return deliver("fail",{code:result.error});
    return result.pending ? server(connection,"pending",{operation_id:operationId}) : deliver("finish",{receipt:result.receipt});
  }
  // 'sending'/'unknown' are never submitted again, even after worker/browser restart.
  const {source_code,problem_snapshot,...view}=job;
  return view;
}
chrome.runtime.onMessage.addListener((message,sender,sendResponse)=>{
  let base;
  try {base=localOrigin(sender.url);if(sender.frameId!==0 || !sender.tab) return false;} catch {return false;}
  if(message?.channel!=="algocoach-submit") return false;
  (async()=>{
    const storageKey="submit:"+base;
    if(message.action==="connect") {
      const connection=message.connection;
      if(!connection || localOrigin(connection.base)!==base || typeof connection.token!=="string" || connection.token.length>200 || !Number.isFinite(connection.expires)) throw new Error("connection");
      await chrome.storage.session.set({[storageKey]:connection});
      return {connected:true,version:chrome.runtime.getManifest().version};
    }
    const connection=(await chrome.storage.session.get(storageKey))[storageKey];
    if(!connection || connection.expires<=Date.now()) return {error:"connection"};
    if(message.action==="status") return {connected:true,version:chrome.runtime.getManifest().version};
    if(message.action!=="step" || !/^[a-f0-9]{32}$/.test(message.operation_id)) throw new Error("message");
    const key=base+":"+message.operation_id;
    if(running.has(key)) return {busy:true};
    running.add(key);
    try {return {submission:await step(connection,message.operation_id)};}
    finally {running.delete(key);}
  })().then(sendResponse,()=>sendResponse({error:"extension"}));
  return true;
});
