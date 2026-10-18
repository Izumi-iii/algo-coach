/* Browser smoke using an existing Chromium DevTools endpoint; no npm dependencies.
 * Use an isolated browser profile and a --demo Web server. Optional --docker executes
 * the submitted fixture exclusively through the application's Docker runner.
 */
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const base = process.argv[2] || "http://127.0.0.1:8766";
const debug = process.argv[3] || "http://127.0.0.1:9223";
for (const address of [base,debug]) assert.equal(new URL(address).hostname,"127.0.0.1");
const docker = process.argv.includes("--docker");
const output = path.resolve(".algo-coach/web-preview");
fs.mkdirSync(output,{recursive:true});
const delay = (ms) => new Promise((resolve) => setTimeout(resolve,ms));

async function main() {
  const targets = await (await fetch(`${debug}/json/list`)).json();
  const target = targets.find((value) => value.type === "page");
  assert(target,"No page in the isolated debugging browser");
  const socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve,reject) => { socket.addEventListener("open",resolve,{once:true});socket.addEventListener("error",reject,{once:true}); });
  let counter = 0;
  const pending = new Map();
  const errors = [];
  socket.addEventListener("message",(event) => {
    const message = JSON.parse(event.data);
    if (message.id) {
      const item = pending.get(message.id);
      if (item) { pending.delete(message.id); message.error ? item.reject(new Error(message.error.message)) : item.resolve(message.result); }
    } else if (message.method === "Runtime.exceptionThrown") errors.push(message.params.exceptionDetails.text);
  });
  function cdp(method,params = {}) {
    return new Promise((resolve,reject) => { const id = ++counter;pending.set(id,{resolve,reject});socket.send(JSON.stringify({id,method,params})); });
  }
  async function evaluate(expression) {
    const response = await cdp("Runtime.evaluate",{expression,returnByValue:true,awaitPromise:true});
    if (response.exceptionDetails) throw new Error(response.exceptionDetails.text+" "+expression.slice(0,100));
    return response.result.value;
  }
  async function until(expression,timeout = 30000) {
    const deadline = Date.now()+timeout;
    while (Date.now()<deadline) { if (await evaluate(expression)) return;await delay(150); }
    throw new Error("Timed out: "+expression);
  }
  async function screenshot(name) {
    const image = await cdp("Page.captureScreenshot",{format:"png",captureBeyondViewport:false});
    const file = path.join(output,name);fs.writeFileSync(file,Buffer.from(image.data,"base64"));return file;
  }
  try {
    await cdp("Runtime.enable");await cdp("Page.enable");
    await cdp("Emulation.setDeviceMetricsOverride",{width:1440,height:1050,deviceScaleFactor:1,mobile:false});
    await cdp("Page.navigate",{url:base});
    await until(`document.querySelector('#mode')?.textContent === '离线演示'`);
    assert.equal(await evaluate(`(async () => (await (await fetch('/api/bootstrap')).json()).mode)()`),"demo","Browser smoke refuses paid model mode");
    await evaluate(`localStorage.removeItem('algocoach.'+location.host+'.problem-expanded')`);
    await evaluate(`document.querySelector('#reference').value='1';document.querySelector('#new-session').requestSubmit();`);
    await until(`document.querySelector('#problem-title').textContent.startsWith('1.') && !document.querySelector('#send').disabled`);
    const sid = await evaluate(`document.querySelector('.session.active').dataset.session`);
    assert(await evaluate(`document.querySelector('#problem-details').hidden`),"Problem starts folded");
    assert.equal(await evaluate(`document.querySelector('.message.assistant .message-content').textContent`),"你目前对这道题有什么思路，或需要先澄清哪一点？");
    const foldedHeight = await evaluate(`document.querySelector('#messages').clientHeight`);
    await evaluate(`document.querySelector('#problem-toggle').click()`);
    assert(await evaluate(`!document.querySelector('#problem-details').hidden && document.querySelector('#problem-body').textContent.length > 50`));
    assert(await evaluate(`document.querySelector('#messages').clientHeight < ${foldedHeight} - 100`),"Folding frees conversation space");
    const expanded = await screenshot("desktop-expanded.png");
    await evaluate(`document.querySelector('#message').value=${JSON.stringify("我想先理解题意。<img src=x onerror=window.__xss=1>")};document.querySelector('#chat-form').requestSubmit();`);
    await until(`!document.querySelector('#send').disabled && document.querySelector('#message').value === ''`);
    assert.equal(await evaluate(`document.querySelectorAll('.messages img').length`),0);
    assert.equal(await evaluate(`window.__xss || 0`),0);
    await evaluate(`document.querySelector('#hint').click()`);
    await until(`document.querySelector('#hint-level').textContent.startsWith('H1') && !document.querySelector('#send').disabled`);
    assert.equal(await evaluate(`document.querySelector('#problem-toggle').getAttribute('aria-expanded')`),"true","Chat preserves expanded preference");
    const code = "class Solution:\n    def twoSum(self, nums, target):\n        return [0, 0]\n";
    await evaluate(`document.querySelector('#code').value=${JSON.stringify(code)};document.querySelector('#code').dispatchEvent(new Event('input'));`);
    assert(await evaluate(`document.querySelector('#test-code').disabled`));
    await evaluate(`document.querySelector('#submit-code').click()`);
    await until(`document.querySelector('#attempts').textContent.includes('1 次') && !document.querySelector('#send').disabled`);
    assert.equal(await evaluate(`document.querySelector('#verdict').textContent`),"未执行");
    if (docker) {
      await evaluate(`document.querySelector('#test-code').click()`);
      await until(`!document.querySelector('#send').disabled && document.querySelector('#verdict').textContent === '答案错误'`,45000);
      assert(await evaluate(`!document.querySelector('#failure').hidden`));
    }
    await cdp("Page.reload");
    await until(`document.querySelector('.session.active')?.dataset.session === ${JSON.stringify(sid)} && !document.querySelector('#send').disabled`);
    assert.equal(await evaluate(`document.querySelector('#code').value`),code);
    assert.equal(await evaluate(`document.querySelector('#problem-toggle').getAttribute('aria-expanded')`),"true","Reload restores expanded preference");
    await evaluate(`document.querySelector('#problem-toggle').click()`);
    await cdp("Page.reload");
    await until(`document.querySelector('.session.active')?.dataset.session === ${JSON.stringify(sid)} && !document.querySelector('#send').disabled`);
    assert(await evaluate(`document.querySelector('#problem-details').hidden`),"Reload restores folded preference");
    await evaluate(`document.querySelector('#message').value='你目前对这道题有什么思路，或需要先澄清哪一点？';document.querySelector('#chat-form').requestSubmit();`);
    await until(`!document.querySelector('#send').disabled && document.querySelector('#message').value === ''`);
    assert.equal(await evaluate(`Array.from(document.querySelectorAll('.message.user .message-content')).at(-1).textContent`),"你目前对这道题有什么思路，或需要先澄清哪一点？","Later messages are not filtered");
    const draft = code+"# unsaved draft remains while chatting\n";
    await evaluate(`document.querySelector('#code').value=${JSON.stringify(draft)};document.querySelector('#code').dispatchEvent(new Event('input'));document.querySelector('#message').value='请直接核对题目返回下标还是数值。';document.querySelector('#chat-form').requestSubmit();`);
    await until(`!document.querySelector('#send').disabled && document.querySelector('#message').value === ''`);
    assert.equal(await evaluate(`document.querySelector('#code').value`),draft);
    assert(await evaluate(`document.querySelector('#test-code').disabled`));
    const desktop = await screenshot("desktop.png");
    assert(await evaluate(`document.documentElement.scrollWidth <= 1440`),"Desktop layout overflows horizontally");
    await cdp("Emulation.setDeviceMetricsOverride",{width:390,height:844,deviceScaleFactor:1,mobile:true});
    await delay(300);
    assert(await evaluate(`document.documentElement.scrollWidth <= 390`),"Mobile layout overflows horizontally");
    const mobile = await screenshot("mobile.png");
    await evaluate(`document.querySelector('#problem-toggle').click();document.querySelector('.problem-card').scrollIntoView()`);
    assert(await evaluate(`!document.querySelector('#problem-details').hidden && document.documentElement.scrollWidth <= 390`));
    const mobileExpanded = await screenshot("mobile-expanded.png");
    assert.deepEqual(errors,[],"Uncaught browser exceptions");
    console.log(JSON.stringify({desktop,expanded,mobile,mobileExpanded,docker,checks:["create","initial greeting shortened","later messages retained","problem fold and expand","fold frees chat space","fold preference survives chat and reload","safe text rendering","hint","code submission","report","reload restore","draft preserved","stale draft cannot execute","desktop and mobile layout","no browser exceptions"]},null,2));
  } finally { socket.close(); }
}
main().catch((error) => { console.error(error.message);process.exitCode=1; });
