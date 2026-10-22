/* Use only with scripts/web_stream_fixture.py and an isolated debugging browser. */
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const base = process.argv[2] || "http://127.0.0.1:8768";
const debug = process.argv[3] || "http://127.0.0.1:9224";
const output = path.resolve(".algo-coach/web-stream-preview");
for (const address of [base,debug]) assert.equal(new URL(address).hostname,"127.0.0.1");
const delay = ms => new Promise(resolve => setTimeout(resolve,ms));

async function main() {
  fs.mkdirSync(output,{recursive:true});
  assert.equal((await (await fetch(`${base}/api/bootstrap`)).json()).mode,"demo","Refuses paid mode");
  const target = (await (await fetch(`${debug}/json/list`)).json()).find(item => item.type === "page");
  const socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise(resolve => socket.addEventListener("open",resolve,{once:true}));
  let serial = 0;
  let turnPosts = 0;
  const pending = new Map(), errors = [];
  socket.addEventListener("message",event => {
    const message = JSON.parse(event.data);
    if (message.id) {
      const item = pending.get(message.id); pending.delete(message.id);
      message.error ? item.reject(new Error(message.error.message)) : item.resolve(message.result);
    } else if (message.method === "Runtime.exceptionThrown") errors.push(message.params.exceptionDetails.text);
    else if (message.method === "Network.requestWillBeSent" && message.params.request.url.endsWith('/api/actions') && message.params.request.method === 'POST') {
      if (JSON.parse(message.params.request.postData || '{}').operation === 'turn') turnPosts++;
    }
  });
  const cdp = (method,params = {}) => new Promise((resolve,reject) => {
    const id = ++serial; pending.set(id,{resolve,reject}); socket.send(JSON.stringify({id,method,params}));
  });
  async function evaluate(expression) {
    const value = await cdp("Runtime.evaluate",{expression,returnByValue:true,awaitPromise:true});
    if (value.exceptionDetails) throw new Error(value.exceptionDetails.text);
    return value.result.value;
  }
  async function until(expression) {
    const end = Date.now()+20000;
    while (Date.now()<end) { if (await evaluate(expression)) return; await delay(80); }
    throw new Error("Timed out: "+expression);
  }
  async function shot(name) {
    const result = await cdp("Page.captureScreenshot",{format:"png",captureBeyondViewport:false});
    fs.writeFileSync(path.join(output,name),Buffer.from(result.data,"base64"));
  }
  try {
    await cdp("Runtime.enable"); await cdp("Page.enable"); await cdp("Network.enable");
    await cdp("Emulation.setDeviceMetricsOverride",{width:1440,height:1050,deviceScaleFactor:1,mobile:false});
    await cdp("Page.navigate",{url:base});
    await until(`document.querySelector('#mode')?.textContent === '离线演示'`);
    await evaluate(`document.querySelector('#reference').value='1';document.querySelector('#new-session').requestSubmit()`);
    await until(`document.querySelector('#problem-title').textContent.startsWith('1.') && !document.querySelector('#send').disabled`);
    const one = await evaluate(`current.session_id`);
    await evaluate(`document.querySelector('#reference').value='25';document.querySelector('#new-session').requestSubmit()`);
    await until(`document.querySelector('#problem-title').textContent.startsWith('25.') && !document.querySelector('#send').disabled`);
    const two = await evaluate(`current.session_id`);
    await evaluate(`restore('${one}')`);
    await evaluate(`document.querySelector('#message').value='先核对题意。';document.querySelector('#chat-form').requestSubmit()`);
    await until(`!!document.querySelector('[data-stream-placeholder]') && document.querySelector('#send').disabled`);
    assert(await evaluate(`!document.querySelector('#code').disabled && !document.querySelector('#message').disabled`));
    assert(await evaluate(`document.querySelector('#notice').hidden`));
    assert(await evaluate(`Array.from(document.querySelectorAll('.session')).every(button=>!button.disabled)`));
    await evaluate(`document.querySelector('#message').value='下一条消息草稿';document.querySelector('#code').value='# 生成期间的代码草稿';document.querySelector('#code').dispatchEvent(new Event('input'))`);
    await evaluate(`restore('${one}')`);
    assert(await evaluate(`document.querySelector('#message').value === '下一条消息草稿' && document.querySelector('#code').value === '# 生成期间的代码草稿'`),"Refreshing the active session must preserve drafts");
    await shot("stream-waiting.png");
    await until(`document.querySelector('[data-stream-placeholder] .message-content').textContent.includes('尚未结束时到达')`);
    assert(await evaluate(`!document.querySelector('[data-stream-placeholder] .message-content').textContent.includes('第二段已经完成')`));
    await shot("stream-first-paragraph.png");
    await evaluate(`restore('${two}')`);
    assert(await evaluate(`current.session_id === '${two}' && !document.querySelector('[data-stream-placeholder]')`));
    await until(`!busy`);
    assert(await evaluate(`current.session_id === '${two}'`),"Completion must not yank the reader back");
    await evaluate(`restore('${one}')`);
    assert(await evaluate(`document.querySelector('#message').value === '下一条消息草稿'`));
    assert(await evaluate(`document.querySelector('#code').value === '# 生成期间的代码草稿'`));
    assert(await evaluate(`document.querySelector('#messages').textContent.includes('第二段已经完成')`));
    assert.equal(await evaluate(`Array.from(document.querySelectorAll('.message.user')).filter(node=>node.textContent.includes('先核对题意。')).length`),1);
    await shot("stream-complete.png");
    await evaluate(`document.querySelector('#message').value='刷新恢复任务';document.querySelector('#chat-form').requestSubmit()`);
    await until(`busy && !!document.querySelector('[data-stream-placeholder]')`);
    await cdp("Page.reload",{ignoreCache:true});
    await until(`document.querySelector('#mode')?.textContent === '离线演示' && !!document.querySelector('[data-stream-placeholder]')`);
    await until(`!busy && !!current`);
    assert.equal(await evaluate(`current.history.filter(m=>m.role==='user' && m.content==='刷新恢复任务').length`),1);
    assert.equal(turnPosts,2,"Refresh must recover the original job without another POST");
    assert.deepEqual(errors,[]);
    const evidence={passed:true,real_model_calls:0,real_platform_submissions:0,turn_posts:turnPosts,checks:["live checked paragraph before completion","input and editor enabled","no global busy banner","history switching during generation","no session jump on completion","per-session drafts preserved","no duplicate user message","refresh recovers original job without resubmission"],screenshots:["stream-waiting.png","stream-first-paragraph.png","stream-complete.png"]};
    fs.writeFileSync(path.join(output,"browser-evidence.json"),JSON.stringify(evidence,null,2)+"\n");
    process.stdout.write(JSON.stringify(evidence));
  } finally { socket.close(); }
}
main().catch(error=>{process.stderr.write(String(error));process.exitCode=1;});
