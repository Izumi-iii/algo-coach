# 中国站平台提交后端（实验性）

日常 Web 使用优先选择 [浏览器提交通道](WEB_PLATFORM_SUBMIT.md)：无需复制 Cookie，直接在 AlgoCoach 明确确认后提交并自动返回结果。本页保留可选的 CLI 登录态配置路径，二者的连接授权和执行入口独立。

本轮实现独立的 CLI 平台提交后端：只读登录检查、保存版本预览、显式提交、有限轮询、SQLite 记录与中断恢复。暂缓将本地判题适配器扩展到 10 题。

目前已做模拟 HTTP/真实 Coach 的工程验证，**尚未完成真实账号提交**。实现以已研究的内部接口为基础；真实环境的登录、风控、响应结构及平台使用条件仍需确认。研究来源见 [提交接入研究](LEETCODE_SUBMISSION_RESEARCH.md)。没有自动读取浏览器凭据、安装依赖或修改用户现有 `.env`。

## 1. 在本地准备登录态

在浏览器自行登录 `https://leetcode.cn`。将本人账号对应的 `LEETCODE_SESSION` 和 `csrftoken` Cookie 值配置到项目本地 `.env` 或当前终端环境；**不要发送到聊天，不要提交到 Git，不要写入题解**。

在已有 `.env` 中新增以下键，不覆盖原模型配置：

```dotenv
ALGOCOACH_LEETCODE_SESSION=在本地填写本人中国站会话Cookie
ALGOCOACH_LEETCODE_CSRF=在本地填写csrftoken的值
ALGOCOACH_PLATFORM_SUBMIT_ENABLED=false
```

Cookie 是账号凭据，服务端拒绝访问或要求验证时在原站完成处理；本工具不会绕过。真实用户验证时应先确认该接入方式适用于自己的账号和使用范围。

## 2. 只读确认登录

```powershell
.\.venv\Scripts\python.exe -m algo_coach platform doctor
```

此命令不需要模型密钥、不取得学习目录锁，也不会提交代码。成功返回 `signed_in=true` 和账号指纹，不显示用户名、Cookie 或 CSRF。缺少配置、登录失效、访问拒绝和查询失败使用受控错误说明，不输出原始响应。

## 3. 准备与审阅保存版本

使用现有会话或新建单独测试会话，先通过 CLI `/code 文件路径` 保存自己的 Python3 代码。已有 Web 会话需先正常停止 Web，再用同一数据目录操作，不能绕过目录锁。

```powershell
.\.venv\Scripts\python.exe -m algo_coach --data-dir .algo-coach/web sessions
.\.venv\Scripts\python.exe -m algo_coach --data-dir .algo-coach/web platform preview SESSION_ID --show-code
```

将 `SESSION_ID` 替换为完整会话 ID。其他数据目录应自行替换。预览会只读查询已登录账号、题目内部 ID、展示题号、slug 和 Python3 可用性，保存计划并返回 `operation_id`；不会创建平台提交。`--show-code` 显示实际待提交原文，应核对题目、语言、代码和哈希。

当前只支持中国站公开普通 Python3 题目。代码需要通过 Python 语法解析；这不是正确性认证。预览提交的是保存的用户版本，不自动用模型参考解替换，也不上传编辑器未提交草稿。

## 4. 明确提交一次

确认预览内容后，在本地将 `ALGOCOACH_PLATFORM_SUBMIT_ENABLED=true`，运行：

```powershell
.\.venv\Scripts\python.exe -m algo_coach --data-dir .algo-coach/web platform apply SESSION_ID OPERATION_ID
```

`OPERATION_ID` 替换为预览返回值。执行前重新核对当前保存代码、题面、登录账号和远程题目定位，再提交一次 POST。正式提交会增加平台账号记录。普通聊天、代码分析、模型纠正和页面刷新都不会触发此命令。

成功受理记录为 `queued`，保存 `submission_id`。相同操作 ID 再次 apply 只返回原记录，不重新提交。提交期间网络错误或响应没有任务 ID 时记为 `unknown`；硬中断可能保留 `sending`。两者均不得自动重发，应先在原站核对是否已经受理。

## 5. 查询、恢复与结果含义

```powershell
# 有限查询，默认最多 10 次，每次之间间隔 2 秒；到期仍可再次查询同一操作
.\.venv\Scripts\python.exe -m algo_coach --data-dir .algo-coach/web platform poll SESSION_ID OPERATION_ID

# 只读本地记录，无需登录态或网络
.\.venv\Scripts\python.exe -m algo_coach --data-dir .algo-coach/web platform show SESSION_ID OPERATION_ID
```

`queued` / `judging` 不代表通过。只有预期终态、任务 ID 对应以及可识别的正式判题状态才记录 `accepted`、`wrong_answer`、`compile_error` 等结论；HTTP 200 或 `state=SUCCESS` 本身不足以认定 AC。未知状态、任务 ID 缺失、题目 ID 不匹配都保留错误与未知结论；轮询读取失败可以再次查询，不重发提交。

结果保存在原学习数据目录 SQLite 的 `app_platform_submissions` 表，包含精确提交代码、哈希、题目与账号指纹、操作及提交 ID、时间和受控结果字段，不保存登录 Cookie。修改代码后，旧提交仍绑定原版本；不会转移给新代码。

## 当前接入边界

- 这是独立 CLI 后端，尚未添加 Web 提交按钮或聊天 `/submit` 命令。
- 平台报告与本地 `Verification` 分开保存，**尚未接入教学模型上下文和归档**；已有教学权限检查仍不允许声称平台已通过，不能用本地测试冒充 AC。
- 当前没有实现自定义样例的远程 `interpret_solution` 操作；先验收正式提交主链。模拟测试不会当作真实平台成功。
- 只查询本人登录状态，提交读取或写入均限固定中国站地址，不跟随重定向；没有自动 POST 重试。
- 单个学习目录由既有进程锁保护。这里的持久化与重复调用控制不能保证平台恰好执行一次，也没有在任务 ID 丢失后自动查找匹配历史提交。

## 开发验证

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_platform_judge.py -q
```

使用模拟 HTTP 和真实临时 SQLite/Coach，覆盖内部 ID 与展示题号不同、保存代码版本、账号及目标变化、默认禁用、明确提交、重复 apply、超时/中断恢复、结果绑定、排队、终态与脱敏。不调用 LeetCode 或真实模型。

本轮检查：平台后端与相关教学/Web 定向回归 **108 passed**（其中平台专用 35 项），原有归档与验证 CLI 回归 **4 passed**。Ruff 静态、格式、差异与本地文档链接检查通过。没有重跑完整工程集、Docker、模型或平台真实提交。开发初期的 30 项平台测试为前一轮中间结果，不与最终数量累加。
