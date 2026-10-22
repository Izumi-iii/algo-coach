/* Serialized into the LeetCode tab by the worker. Never exports login cookies. */
async function algoCoachPlatformPage(action, job) {
  if (location.origin !== "https://leetcode.cn") return {error:"target"};
  const hash = async value => Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)))).map(x=>x.toString(16).padStart(2,"0")).join("");
  const csrf = () => {
    const item = document.cookie.split(";").map(x=>x.trim()).find(x=>x.startsWith("csrftoken="));
    return item ? decodeURIComponent(item.slice(10)) : "";
  };
  let postStarted = false;
  async function request(path, options = {}) {
    const abort = new AbortController();
    const timer = setTimeout(()=>abort.abort(),15000);
    try {
      const response = await fetch(path,{credentials:"same-origin",redirect:"error",signal:abort.signal,...options});
      if (!response.ok) throw new Error(response.status===401 || response.status===403 ? "access" : "transport");
      const text = await response.text();
      if (text.length>524288) throw new Error("schema");
      return JSON.parse(text);
    } finally { clearTimeout(timer); }
  }
  async function query(query, variables={}) {
    const token=csrf();
    const data=await request("/graphql/",{method:"POST",headers:{"Content-Type":"application/json",...(token?{"X-CSRFToken":token}:{})},body:JSON.stringify({query,variables})});
    if(data.errors || !data.data) throw new Error("schema");
    return data.data;
  }
  async function account() {
    const status=(await query("query { userStatus { isSignedIn username } }")).userStatus;
    if(status?.isSignedIn!==true || typeof status.username!=="string" || !status.username) throw new Error("login");
    return hash(status.username);
  }
  async function preflight() {
    const accountHash=await account();
    const q=(await query("query($slug:String!) { question(titleSlug:$slug) { questionId questionFrontendId titleSlug isPaidOnly codeSnippets { langSlug } } }",{slug:job.slug})).question;
    if(!q || String(q.questionFrontendId)!==job.frontend_id || q.titleSlug!==job.slug || q.isPaidOnly!==false || !/^[0-9]{1,30}$/.test(String(q.questionId)) || !Array.isArray(q.codeSnippets) || !q.codeSnippets.some(s=>s?.langSlug==="python3")) throw new Error("target");
    return {account_hash:accountHash,question_id:String(q.questionId),frontend_id:job.frontend_id,slug:job.slug,language:"python3"};
  }
  try {
    if(!/^[a-z0-9][a-z0-9-]{0,119}$/.test(job.slug) || job.language!=="python3") throw new Error("target");
    if(!location.pathname.startsWith(`/problems/${job.slug}/`)) throw new Error("login");
    if(action==="preflight") {
      const token=csrf();
      if(!token || !/^[A-Za-z0-9._~+/%=-]{1,10000}$/.test(token)) throw new Error("csrf");
      return {target:await preflight()};
    }
    if(action==="submit") {
      if(typeof job.source_code!=="string" || await hash(job.source_code)!==job.code_hash) throw new Error("binding");
      const current=await preflight();
      if(Object.keys(current).some(key=>current[key]!==job.target?.[key])) throw new Error("account");
      const token=csrf();
      if(!token || !/^[A-Za-z0-9._~+/%=-]{1,10000}$/.test(token)) throw new Error("csrf");
      postStarted=true;
      const result=await request(`/problems/${job.slug}/submit/`,{method:"POST",headers:{"Content-Type":"application/json","X-CSRFToken":token},body:JSON.stringify({lang:"python3",question_id:current.question_id,typed_code:job.source_code})});
      const id=result.submission_id;
      if((typeof id!=="string" && !Number.isSafeInteger(id)) || !/^[0-9]{1,30}$/.test(String(id))) throw new Error("schema");
      return {submission_id:String(id)};
    }
    if(action!=="poll" || !/^[0-9]{1,30}$/.test(job.submission_id)) throw new Error("schema");
    if(await account()!==job.target.account_hash) throw new Error("account");
    const result=await request(`/submissions/detail/${job.submission_id}/check/`);
    if(["PENDING","STARTED"].includes(result.state)) return {pending:true};
    if(result.state!=="SUCCESS" || ![10,11,12,13,14,15,20].includes(result.status_code)) throw new Error("schema");
    if(result.submission_id!=null && String(result.submission_id)!==job.submission_id) throw new Error("binding");
    if(result.question_id!=null && String(result.question_id)!==job.target.question_id) throw new Error("binding");
    let d;
    try { d=(await query("query($id:ID!) { submissionDetail(submissionId:$id) { id code lang question { questionId questionFrontendId titleSlug } } }",{id:job.submission_id})).submissionDetail; }
    catch { throw new Error("detail"); }
    const q=d?.question;
    if(!d || !q || typeof d.code!=="string") throw new Error("detail");
    if(String(d.id)!==job.submission_id || String(q.questionId)!==job.target.question_id || String(q.questionFrontendId)!==job.frontend_id || q.titleSlug!==job.slug || !["Python3","python3"].includes(d.lang) || await hash(d.code)!==job.code_hash) throw new Error("binding");
    const failure={};
    for(const key of ["compile_error","runtime_error","last_testcase","expected_output","code_output"]) {
      const value=key==="compile_error" ? (result.full_compile_error ?? result.compile_error) : result[key];
      if(value!=null) failure[key]=String(value).slice(0,4000);
    }
    return {receipt:{submission_id:job.submission_id,state:"SUCCESS",status_code:result.status_code,question_id:job.target.question_id,runtime:String(result.status_runtime??"").slice(0,200),memory:String(result.status_memory??"").slice(0,200),failure_details:failure,detail:{submission_id:job.submission_id,question_id:job.target.question_id,frontend_id:job.frontend_id,slug:job.slug,language:d.lang,source_code:d.code}}};
  } catch(error) {
    const codes=["login","account","target","csrf","access","transport","schema","detail","binding"];
    return {error:codes.includes(error.message)?error.message:"transport",post_started:postStarted};
  }
}
