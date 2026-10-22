/* Real worker and page-task code, with a controlled LeetCode network and Chrome facade. */
const fs=require("node:fs"), vm=require("node:vm"), assert=require("node:assert/strict");
const {webcrypto}=require("node:crypto");
const input=JSON.parse(fs.readFileSync(0,"utf8"));
const store={};
let listener, posts=0, polls=0, source="", lost=false, csrfChecks=0;
const nativeFetch=global.fetch;
const target={questionId:"999",questionFrontendId:"1",titleSlug:"two-sum",isPaidOnly:false,codeSnippets:[{langSlug:"python3"}]};
async function leetcode(path,options) {
  assert.equal(options.credentials,"same-origin");assert.equal(options.redirect,"error");
  assert(!Object.keys(options.headers||{}).some(k=>k.toLowerCase()==="cookie"));
  let data;
  if(path==="/graphql/") {
    const body=JSON.parse(options.body);
    if(body.query.includes("userStatus")) data={data:{userStatus:{isSignedIn:input.mode!=="login",username:input.mode==="account-switch" && posts>0?"other-account":"fixture-account"}}};
    else if(body.query.includes("submissionDetail")) data={data:{submissionDetail:{id:"5678",code:source,lang:"Python3",question:target}}};
    else data={data:{question:target}};
  } else if(path.endsWith("/submit/")) {
    posts++;assert.equal(posts,1,"duplicate platform POST");
    assert.equal(options.headers["X-CSRFToken"],"fixture-csrf");csrfChecks++;
    const body=JSON.parse(options.body);assert.equal(body.question_id,"999");assert.equal(body.lang,"python3");source=body.typed_code;
    if(input.mode==="submit-timeout") throw new Error("PRIVATE transport details");
    data={submission_id:5678};
  } else if(path==="/submissions/detail/5678/check/") {
    polls++;
    data=polls===1?{state:"PENDING"}:{state:"SUCCESS",status_code:input.mode==="accepted"?10:input.mode==="compile"?20:11,submission_id:5678,question_id:999,last_testcase:"[2,7]\n9",expected_output:"[0,1]",code_output:"[0,0]",full_compile_error:"fixture compile diagnostic",status_runtime:"0 ms"};
  } else throw new Error("Unexpected fixture path: "+path);
  return {ok:true,text:async()=>JSON.stringify(data)};
}
async function localFetch(url, options) {
  options.headers={...options.headers,Origin:input.origin};
  const body=JSON.parse(options.body);
  const response=await nativeFetch(url,options);
  if(!lost && ((input.mode==="lost-authorization" && body.action==="authorize") || (input.mode==="lost-ack" && body.action==="accepted"))) {
    lost=true;throw new TypeError("simulated lost local response");
  }
  return response;
}
function worker() {
  let context;
  const chrome={
    runtime:{onMessage:{addListener:f=>{listener=f;}},getManifest:()=>({version:"0.3.0"})},
    storage:{session:{
      get:async key=>({[key]:store[key]}),set:async value=>Object.assign(store,value),remove:async key=>{delete store[key];},
    }},
    tabs:{query:async()=>[{id:7,url:"https://leetcode.cn/problems/two-sum/",status:"complete"}],get:async()=>({id:7,url:"https://leetcode.cn/problems/two-sum/",status:"complete"}),update:async()=>({}),create:async()=>{throw new Error("unexpected tab creation");}},
    scripting:{executeScript:async ({func,args})=>{
      const page={location:{origin:"https://leetcode.cn",pathname:"/problems/two-sum/"},document:{cookie:input.mode==="csrf"?"":"csrftoken=fixture-csrf; harmless=value"},crypto:webcrypto,TextEncoder,Uint8Array,AbortController,setTimeout,clearTimeout,fetch:leetcode};
      return [{result:await vm.runInNewContext(`(${func.toString()})(...args)`,{...page,args})}];
    }},
  };
  context=vm.createContext({chrome,URL,AbortController,setTimeout,clearTimeout,fetch:localFetch,importScripts:name=>vm.runInContext(fs.readFileSync("extensions/leetcode-bridge/"+name,"utf8"),context)});
  vm.runInContext(fs.readFileSync("extensions/leetcode-bridge/background.js","utf8"),context);
}
function message(action,extra={}) {
  return new Promise(resolve=>{
    const keep=listener({channel:"algocoach-submit",action,...extra},{url:input.base+"/",frameId:0,tab:{id:1}},resolve);
    assert.equal(keep,true);
  });
}
async function main() {
  worker();
  await message("connect",{connection:{base:input.base,token:input.token,expires:Date.now()+600000}});
  let response=await message("step",{operation_id:input.operation_id});
  if(["login","csrf","submit-timeout"].includes(input.mode)) {
    const record=response.submission;
    assert(record);
    assert.equal(record.state,input.mode==="submit-timeout"?"unknown":"blocked");
    assert.equal(posts,input.mode==="submit-timeout"?1:0);
    worker(); // storage.session and Python SQLite survive service-worker recreation.
    response=await message("step",{operation_id:input.operation_id});
    assert.equal(response.submission.state,record.state);assert.equal(posts,input.mode==="submit-timeout"?1:0);
  } else if(input.mode==="lost-authorization") {
    assert(response.error);worker();
    response=await message("step",{operation_id:input.operation_id});
    assert.equal(response.submission.state,"sending");assert.equal(posts,0);
  } else if(input.mode==="account-switch") {
    response=await message("step",{operation_id:input.operation_id});
    assert.equal(response.submission.state,"queued");assert.equal(response.submission.error_code,"account");assert.equal(posts,1);assert.equal(polls,0);
  } else {
    if(input.mode==="lost-ack") {assert(response.error);worker();}
    for(let i=0;i<5 && response.submission?.state!=="finished";i++) response=await message("step",{operation_id:input.operation_id});
    assert.equal(response.submission.state,"finished");assert.equal(posts,1);assert.equal(csrfChecks,1);
    assert.equal(response.submission.verdict,input.mode==="accepted"?"accepted":input.mode==="compile"?"compile_error":"wrong_answer");
    assert.equal(Object.keys(store).filter(k=>k.startsWith("pending:")).length,0);
  }
  const serialized=JSON.stringify(response);
  assert(!serialized.includes("fixture-csrf"));assert(!serialized.includes("fixture-account"));assert(!serialized.includes("PRIVATE"));assert(!serialized.includes('class Solution'));
  console.log(JSON.stringify({mode:input.mode,state:response.submission?.state,posts,polls}));
}
main().catch(error=>{console.error(error);process.exitCode=1;});
