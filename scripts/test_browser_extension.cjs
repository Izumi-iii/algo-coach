/* Executes the actual injection script with controlled network fixtures, no npm. */
const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const source = fs.readFileSync("extensions/leetcode-bridge/read-submission.js","utf8");
const manifest = JSON.parse(fs.readFileSync("extensions/leetcode-bridge/manifest.json","utf8"));
assert.deepEqual(manifest.permissions,["activeTab","scripting","storage"]);
assert.deepEqual(manifest.host_permissions,["http://127.0.0.1/*"]);
assert(!source.includes("document.cookie"));
async function run(url, responses) {
  const calls=[];
  const result=await vm.runInNewContext(source,{URL,AbortController,setTimeout,clearTimeout,location:{href:url},fetch:async(path,options)=>{
    calls.push({path,options});
    assert.equal(options.credentials,"same-origin");
    assert.equal(options.redirect,"error");
    assert(!Object.keys(options.headers||{}).some(k=>k.toLowerCase()==="cookie"));
    const data=responses.shift();
    if(data instanceof Error) throw data;
    return {ok:true,text:async()=>JSON.stringify(data)};
  }});
  return {result:JSON.parse(JSON.stringify(result)),calls};
}
async function main(){
  const login={data:{userStatus:{isSignedIn:true}}};
  for(const url of ["https://leetcode.cn/submissions/detail/123/","https://leetcode.cn/problems/two-sum/submissions/123/?envType=daily-question"]){
    const {result,calls}=await run(url,[login,{state:"SUCCESS",submission_id:123,question_id:"1",status_code:10,status_runtime:"0 ms",LEETCODE_SESSION:"PRIVATE"}]);
    assert.equal(result.submission_id,"123");assert.equal(result.status_code,10);
    assert(!JSON.stringify(result).includes("PRIVATE"));
    assert.equal(calls.length,2);
    assert.equal(calls[1].path,"/submissions/detail/123/check/");
    assert.equal(calls[1].options.method,undefined);
    assert(!calls.some(c=>c.path.includes("/submit/")));
  }
  for(const url of ["https://evil.invalid/submissions/detail/123/","https://leetcode.cn/problems/two-sum/","https://leetcode.com/submissions/detail/123/"]){
    const {result,calls}=await run(url,[]);assert(result.error);assert.equal(calls.length,0);
  }
  for(const response of [{state:"PENDING"},{state:"SUCCESS",submission_id:9,status_code:10},{state:"SUCCESS",status_code:true}]){
    const {result}=await run("https://leetcode.cn/submissions/detail/123/",[login,response]);assert(result.error);
  }
  const signedOut=await run("https://leetcode.cn/submissions/detail/123/",[{data:{userStatus:{isSignedIn:false}}}]);
  assert(signedOut.result.error);assert.equal(signedOut.calls.length,1);
  const denied=await run("https://leetcode.cn/submissions/detail/123/",[new Error("PRIVATE")]);
  assert(denied.result.error);assert(!denied.result.error.includes("PRIVATE"));
  console.log("Extension script: 10 fixture flows passed; no cookie export or submission request.");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
