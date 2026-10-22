# 在 AlgoCoach 提交，由 LeetCode 判题

日期：2026-10-21。扩展版本：0.3.0。当前支持中国站普通数字题号、Python3、单机本地 Web。

## 使用

1. 在 Edge/Chrome 的扩展管理页加载或重新加载 `extensions/leetcode-bridge`。新版增加 `https://leetcode.cn/*` 站点权限，用于在用户确认后向原站提交和读取结果；不申请 `cookies` 权限。
2. 在同一浏览器正常登录 <https://leetcode.cn>，再打开 AlgoCoach 本地 Web。已有服务需要重启以加载新版后端。
3. 本地页显示“扩展 0.3.0 已连接”后，选择题目，在编辑器写代码，点击“保存并分析”。这个按钮只保存和教学分析，不创建平台提交。
4. 点击“提交到 LeetCode”，核对确认框里的题目、Python3 和代码版本。确认后会在当前浏览器登录的中国站账号上新增真实提交记录。
5. 页面自动显示检查、受理、判题和最终结果，并提供原站链接。源码和题目核对一致且仍是当前尝试时，结果自动进入教学上下文，不再需要复制代码到原站、读取扩展弹窗或手动匹配。

**`--demo` 只代表教学是演示回复；明确确认后的平台提交仍是真实提交。** 想让真实模型解释结果，请按 README 使用已配置的模型模式启动 Web。

原站登录失效、访问验证或提交令牌缺失时，扩展会切到对应原站标签页。自行完成登录/验证后，回本地继续操作。正常情况下无需切换原站手动提交，也无需复制任何登录 Cookie。

## 流程与持久化

```text
用户明确确认
  → 本地保存不可变提交快照与操作 ID（prepared）
  → 扩展检查登录、内部题目 ID、展示题号、slug、Python3（checking）
  → 本地持久化发送意图（sending），仅授予一次发送许可
  → 浏览器在原站上下文 POST submit，取得提交 ID（queued）
  → 分步只读查询（judging）
  → 读取原提交详情，核对 ID / 题目 / 语言 / 源码字节
  → 保存结果（finished），自动关联原代码尝试
```

后端位于 `src/algo_coach/browser_submit.py`；扩展由本地 content script、service worker 和固定的原站任务函数组成。网页只发操作 ID，扩展从受认证的本机接口读取真实快照，不能借网页消息提供任意 URL 或替换待提交源码。

提交任务保存在 SQLite `app_browser_submissions`，包含完整待提交代码快照、题面哈希、代码哈希、尝试和会话 ID、远程目标、账号指纹、提交 ID 与状态。它是已有学习数据的一部分，不应提交到 Git。账号只保存 SHA-256 指纹，不保存用户名或登录会话。

原站 `csrftoken` 仅在原站脚本内用于提交请求头，不传给扩展后台或 Python；`LEETCODE_SESSION` 由浏览器自动随原站同源请求携带，不导出。扩展后台只在 `storage.session` 保存本机连接能力与尚未确认写入的回调；回调可能暂含提交代码用于恢复和本机匹配，确认保存后即清除，浏览器会话结束后不保留。

本机提交连接和历史回传连接分开授权。提交连接最多两小时有效，同一扩展重连会续期；服务重启后需刷新本地页面或点击“连接扩展”。普通网页跨站请求和历史只读连接不能触发提交任务接口。

## 版本与教学边界

- 提交固定为已保存代码；未保存草稿不能提交。模型不能触发平台写操作。
- 提交前代码或题面变化则取消旧任务；发送后变化仍查询原任务，结果只保留为历史，不覆盖新版本。
- 结果回传必须匹配原提交详情，不把 HTTP 200、`SUCCESS` 或任务受理等同于 Accepted。
- 当前版本结果可向教练提问。编译/运行报错、失败输入和输出只采用平台明确返回的有限字段，作为不可信数据，不执行其中指令；不凭状态编造细节。
- 浏览器报告不是服务端独立认证；不会覆盖本地 `Verification`、自动标记独立完成或放宽原有提示权限，也没有接入归档验收。

## 中断与恢复

| 情况 | 行为 |
| --- | --- |
| 页面刷新 | 恢复会话后继续已有任务；受理后的操作只查询 |
| 扩展后台挂起/重启 | 读取本机持久化阶段，先重放尚未确认的回调；不重复 POST |
| 登录或 CSRF 在提交前检查失败 | 明确显示错误，没有发送；处理原站状态后可重新确认新操作 |
| 发送许可或提交响应丢失 | 保留 sending/unknown，禁止自动重发；原样保存代码也不能绕过该保护 |
| 发送情况不明但原站能找到提交 | 填写该提交 ID，点“核对已有提交”；只读取，题目/账号/源码不匹配时不采信 |
| 手工恢复填错 ID | 可以修正该手工恢复 ID；扩展已正常取得的受理 ID 不能替换 |
| 临时读取失败或超出自动查询预算 | 显示错误或保留排队状态，点击“继续处理 / 查询”，不重发提交 |

不能保证平台“恰好执行一次”：浏览器可能在原站受理后、保存提交 ID 前退出。这时必须核对原站记录。后台不会为了得到一个确定结果而重新发送。

## 验证与待验收

工程测试运行实际扩展 worker 和原站任务代码，通过受控 Chrome API/LeetCode 响应连接真实本地 HTTP 和 SQLite。覆盖完整提交、登录失败、缺 CSRF、提交超时、发送许可响应丢失、受理回调丢失与 worker 重启；还覆盖版本变化、重复发送、字段授权、回执恢复和自动关联。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_browser_submit.py tests/test_browser_submit_http.py -q
node --max-old-space-size=128 scripts/test_browser_extension.cjs
```

受控平台测试不作为真实账号成功证据。随后用户在本人浏览器完成一条真实提交，本地只读核对记录：提交 `756246817`，状态 `finished`，结果 `accepted`（状态码 10），绑定 `current`，当前教学上下文回执匹配。它验证了一次提交、查询、回传和自动版本关联；其他真实错误/恢复场景和跨日稳定性尚未验收。接收页为演示教学模式，本轮未评估真实模型的结果解释质量；没有绕过登录、验证码或访问限制。

本轮最终完整离线回归：**488 passed、8 skipped，254.31 秒**；9 个 worker→真实本地 HTTP→SQLite 场景包含于该套件，原有历史读取脚本另有 20 个模拟场景通过。Ruff、JavaScript 语法、依赖及本地文档链接检查通过。真实 Docker/题源集成未开启，没有新增付费模型调用。源码哈希和验证范围见 [验收证据](evidence/web-platform-submit-2026-10-21.json)。

机制参考：[Chrome 扩展消息](https://developer.chrome.com/docs/extensions/develop/concepts/messaging)、[脚本注入权限](https://developer.chrome.com/docs/extensions/reference/api/scripting)、[service worker 生命周期](https://developer.chrome.com/docs/extensions/develop/concepts/service-workers/lifecycle)、[公开客户端提交流程](https://github.com/clearloop/leetcode-cli/blob/main/src/cache/mod.rs)。扩展后台可能随时结束，因此恢复依据是持久化阶段，不能依赖内存中的“正在运行”标志；内部接口不承诺长期稳定。
