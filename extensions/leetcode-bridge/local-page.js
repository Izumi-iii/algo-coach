"use strict";
// This script runs only on loopback. The page receives status, never the capability token.
let connecting=null;
let initialized=false;
async function connect() {
  if(connecting) return connecting;
  connecting=(async()=>{
    const boot=await (await fetch("/api/bootstrap",{cache:"no-store"})).json();
    const r=await fetch("/api/bridge/pair",{method:"POST",headers:{"Content-Type":"application/json","X-AlgoCoach-Token":boot.csrf_token},body:JSON.stringify({extension_id:chrome.runtime.id,purpose:"submit"})});
    if(!r.ok) throw new Error("connect");
    const pair=await r.json();
    const result=await chrome.runtime.sendMessage({channel:"algocoach-submit",action:"connect",connection:{base:location.origin,token:pair.token,expires:Date.now()+pair.expires_in*1000}});
    initialized=Boolean(result?.connected);
    return result;
  })();
  try {return await connecting;} finally {connecting=null;}
}
window.addEventListener("message",async event=>{
  const m=event.data;
  if(event.source!==window || event.origin!==location.origin || m?.channel!=="algocoach-page" || !/^[a-f0-9]{32}$/.test(m.id) || !["status","step","connect"].includes(m.action)) return;
  try {
    let result;
    if(m.action==="connect" || (m.action==="status" && !initialized)) result=await connect();
    else {
      result=await chrome.runtime.sendMessage({channel:"algocoach-submit",action:m.action,operation_id:m.operation_id});
      if(result?.error==="connection") {
        await connect();
        result=await chrome.runtime.sendMessage({channel:"algocoach-submit",action:m.action,operation_id:m.operation_id});
      }
    }
    window.postMessage({channel:"algocoach-extension",id:m.id,result},location.origin);
  } catch {window.postMessage({channel:"algocoach-extension",id:m.id,result:{error:"extension"}},location.origin);}
});
