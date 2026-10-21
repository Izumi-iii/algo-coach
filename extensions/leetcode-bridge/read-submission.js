/* Runs only when the user invokes the popup on the active tab. No Cookie API. */
(async () => {
  const url = new URL(location.href);
  if (url.origin !== "https://leetcode.cn") return {error:"请切换到中国站提交详情页。"};
  const match = url.pathname.match(/^\/submissions\/detail\/([0-9]{1,30})\/?$/)
    || url.pathname.match(/^\/problems\/[a-z0-9-]+\/submissions\/([0-9]{1,30})\/?$/);
  if (!match) return {error:"请打开一条已有提交的详情页，不能只停留在题目页。"};
  async function request(path, options = {}) {
    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), 15000);
    try {
      const response = await fetch(path, {credentials:"same-origin",redirect:"error",signal:abort.signal,...options});
      if (!response.ok) throw new Error("denied");
      const text = await response.text();
      if (text.length > 262144) throw new Error("large");
      return JSON.parse(text);
    } finally { clearTimeout(timer); }
  }
  try {
    const login = await request("/graphql/", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({query:"query { userStatus { isSignedIn } }"})});
    if (login.errors || login.data?.userStatus?.isSignedIn !== true) return {error:"请先在中国站正常登录，再重新读取。"};
    const result = await request(`/submissions/detail/${match[1]}/check/`);
    if (result.state !== "SUCCESS") return {error:"该提交尚未取得终态结果，或接口结构不匹配；请稍后再读。"};
    if (result.submission_id != null && String(result.submission_id) !== match[1]) return {error:"返回的提交标识不一致，停止回传。"};
    if (!Number.isSafeInteger(result.status_code)) return {error:"未取得可识别的判题状态，停止回传。"};
    const qid = result.question_id == null ? null : String(result.question_id);
    if (qid !== null && !/^[0-9]{1,30}$/.test(qid)) return {error:"题目标识格式不匹配。"};
    return {submission_id:match[1],state:"SUCCESS",status_code:result.status_code,question_id:qid,runtime:String(result.status_runtime ?? "").slice(0,200),memory:String(result.status_memory ?? "").slice(0,200)};
  } catch { return {error:"读取失败：请在原站确认登录或访问验证。没有新建提交，也没有导出登录态。"}; }
})();
