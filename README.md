# AlgoCoach

通过交流逐步解题、分析自己的代码、保存学习过程的算法学习 Agent。

AlgoCoach 使用 **LangChain** 接入模型、**LangGraph** 编排教学流程、**SQLite** 保存会话。当前提供本地 CLI 和 Web 测试界面，支持 LeetCode 中国站每日题与按题号选题。默认逐级提示，明确请求完整解答后才允许展示完整实现。

## 当前能做什么

| 能力 | 当前支持 |
| --- | --- |
| 获取题目 | 中国站每日题、数字题号、`leetcode25`、中国站题目链接；本地教学摘要与 JSON 导入 |
| 互动教学 | H0～H3 渐进提示、明确请求完整解答、模型失败后重试 |
| 分析代码 | 保存 Python 代码版本、静态语法检查、结合当前代码和测试证据交流 |
| 执行验证 | Docker 本地测试；仅支持内置第 1、25 题及已审核的对应远程题面快照 |
| 保存学习过程 | 会话恢复、用户思路与反思、完成程度声明、日期题单和复习调度 |
| 归档与 Git | 归档预览与写入、可选模型整理草稿、显式预览后提交或推送 |
| 网页测试 | 题面折叠、聊天、Python 编辑、提交分析、验证报告、会话恢复 |

当前是**个人本机使用的开发版本**。网页尚未提供归档、Git 和复习管理按钮，这些操作使用 CLI。没有通用判题、国际站题源、自动提交 LeetCode、后台每日提醒或多用户服务。

## 环境与安装

- Python **3.11+**，开发环境使用 Python 3.12。
- 真实教学需要模型 API Key；无密钥可使用 `--demo`。
- Docker 仅用于执行代码验证；讨论思路、获取题目和归档不要求 Docker。
- Git 仅在需要提交、推送练习仓库时使用。
- Web 使用项目自带静态页面，不需要 Node.js 或前端构建步骤。

以下示例使用 **Windows PowerShell**，均在仓库根目录执行；无需激活虚拟环境。先下载或克隆仓库并进入 `algo-coach` 目录，再运行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

只运行应用可将 `'.[dev]'` 换成 `.`。已有虚拟环境时无需重复创建。macOS/Linux 的环境命令为 `python3 -m venv .venv`，后续将 `.\.venv\Scripts\python.exe` 换为 `.venv/bin/python`；本文示例主要按 Windows 环境验证。

如需按开发环境依赖快照重建：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe -m pip install -e . --no-deps
```

`requirements-lock.txt` 是依赖快照，不保证所有系统和 Python 版本都兼容。

## 快速体验：先打开网页

### 无密钥演示

```powershell
.\.venv\Scripts\python.exe -m algo_coach.web --demo --data-dir .algo-coach/web-demo --port 8766
```

浏览器打开 <http://127.0.0.1:8766>，选择“本地题库”，输入 `1` 或 `leetcode25`，点击“开始练习”。

`--demo` 使用预置教学回复，不理解你的思路，也不能证明模型教学质量。它只关闭模型调用：选择中国站或每日题仍可能联网获取题目，显式执行代码测试仍需要 Docker。

### 使用 DeepSeek 教学

编辑已有 `.env`，不要再次复制示例文件覆盖密钥。下面与项目 `.env.example` 一致，模型名称需以你账户当前可用的名称为准：

```dotenv
ALGOCOACH_MODEL=deepseek-flash
ALGOCOACH_API_KEY=填写你的密钥
ALGOCOACH_MODEL_PROVIDER=openai
ALGOCOACH_BASE_URL=https://api.deepseek.com
ALGOCOACH_DATA_DIR=.algo-coach
ALGOCOACH_RUNNER_ENABLED=false
ALGOCOACH_RUNNER_IMAGE=algo-coach-runner:0.1
ALGOCOACH_PRACTICE_DIR=
ALGOCOACH_GIT_POLICY=export_only
```

这里的 `openai` 表示 LangChain 兼容协议适配器，请求仍发往配置的 DeepSeek 地址。模型需要支持结构化工具调用输出。项目对 `api.deepseek.com` 显式关闭思考模式；其他兼容服务的参数和能力需分别确认。

```powershell
.\.venv\Scripts\python.exe -m algo_coach.web --data-dir .algo-coach/web --port 8765
```

打开 <http://127.0.0.1:8765>。修改 `.env` 后需要重启服务，终端按 **Ctrl+C** 停止。

### 一次网页练习

1. 选择“本地题库”并输入 `1`；也可选择“中国站每日一题”，点击开始后获取中国站当前每日题。
2. 顶部保留题号和标题，点击“展开题面”查看题意、约束、示例与来源；收起后为聊天腾出空间。
3. 在聊天中说明自己的思路，例如“我想用双重循环，如何判断是否用了同一个位置？”；需要时点击“一点提示”。
4. 在右侧编辑自己的 Python 实现，点击“提交并分析”。这一步保存代码、检查语法并调用教学模型，**不会自动执行用户代码**。
5. 配置好 Docker 后，点击“运行本地测试”。报告只对应已提交版本；修改编辑器内容后需重新提交再测试。
6. 通过左侧学习记录恢复会话；刷新页面也会恢复最近会话。完整答案需主动点击“查看完整解答”。

Enter 发送，Shift+Enter 换行。未提交的消息和代码草稿只存在当前页面，刷新或关闭可能丢失；聊天返回会保留当前未提交代码草稿。

网页只监听 `127.0.0.1`，适用于本机测试。更多说明见 [Web 使用指南](Docs/WEB_TESTING.md)。

## CLI 使用与例子

CLI 全局参数（`--demo`、`--data-dir`、`--practice-dir`）应放在子命令前；`--remote` 等题源参数放在对应子命令后。

### 选题与获取每日题

```powershell
# 查看内置题库；不调用模型
.\.venv\Scripts\python.exe -m algo_coach problems

# 从本地摘要开始教学；加 --demo 可无密钥体验
.\.venv\Scripts\python.exe -m algo_coach chat leetcode25
.\.venv\Scripts\python.exe -m algo_coach --demo chat 1

# 从中国站获取题面并进入真实教学
.\.venv\Scripts\python.exe -m algo_coach chat leetcode25 --remote
.\.venv\Scripts\python.exe -m algo_coach daily chat --remote

# 只获取、缓存并展示题目，不调用模型
.\.venv\Scripts\python.exe -m algo_coach fetch https://leetcode.cn/problems/two-sum/
.\.venv\Scripts\python.exe -m algo_coach daily fetch

# 只读取已缓存的远程题目；缓存不存在时明确报错
.\.venv\Scripts\python.exe -m algo_coach fetch 25 --offline
```

`chat 25` 使用本地题库，`chat 25 --remote` 使用中国站题源。远程读取无需 LeetCode Cookie，通过中国站网页内部 GraphQL 获取公开题目；接口变化可能导致失败。默认缓存有效期为 6 小时，合格的网络失败可返回明确标记的旧缓存；`--refresh` 强制刷新失败时不会悄悄退回缓存。详见 [中国站题源指南](Docs/LEETCODE_SOURCE.md)。

### 逐步解题与提交代码

进入 `chat` 后，可依次输入以下内容。这是用户操作示例，模型实际回复不固定：

```text
我知道需要分组反转，但不知道怎么保存前后节点的连接。
/hint
/status
/code
class Solution:
    def reverseKGroup(self, head, k):
        return head
.end
/test
请结合刚才的报告，帮我定位第一个失败用例的问题，先不要给完整代码。
/quit
```

示例中的代码故意未实现反转，用于体验提交与报告；`/test` 需要已启用的 Docker 执行器。也可用 `/code "D:/your-path/solution.py"` 读取自己的文件。

普通聊天默认只逐步引导，`/hint` 最高提升到 H3；明确 `/answer` 才允许完整讲解与实现。模型回复仍可能出现推理、代码诊断或反例错误，遇到争议应结合代码和实际测试核对。

### 常用会话命令

| 命令 | 用途 |
| --- | --- |
| `/hint`、`/answer` | 逐级提示、明确请求完整解答 |
| `/code 文件路径` | 读取并保存自己的 Python 代码 |
| `/code` | 粘贴多行代码；独立一行 `.end` 结束，`.cancel` 取消 |
| `/test` | 测试当前保存代码，不调用模型 |
| `/status`、`/retry` | 查看阶段和验证状态、重试失败的教学请求 |
| `/finish` | 进入复盘，不自动标记独立完成 |
| `/explain 内容`、`/note 内容` | 保存当前代码的思路、个人反思；再次填写会替换对应内容 |
| `/complete 完成程度` | `independent` / `hint_assisted` / `answer_assisted` / `unfinished` |
| `/summarize` | 调用模型整理已提交代码，生成待审阅题解草稿 |
| `/archive`、`/archive show explanation.md`、`/archive apply` | 预览归档、审阅文件、明确写入 |
| `/commit`、`/commit apply` | 预览与执行本地提交，需要启用对应策略 |
| `/push`、`/push apply` | 预览与明确推送，需要启用对应策略和远端认证 |
| `/problem 1`、`/fetch leetcode25`、`/daily fetch` | 新建本地题、远程题、远程每日题会话 |
| `/review`、`/review add` | 查看到期复习、将当前题加入复习 |
| `/review start leetcode_cn:25`、`/review rate good` | 开始新复习会话、自评 `again` / `hard` / `good` |
| `/help`、`/quit` | 完整命令说明、保存退出 |

这些是 CLI 交互命令；网页使用对应按钮提供部分能力，不支持全部 CLI 管理命令。

### 恢复会话与导入题目

```powershell
.\.venv\Scripts\python.exe -m algo_coach sessions
.\.venv\Scripts\python.exe -m algo_coach resume SESSION_ID
.\.venv\Scripts\python.exe -m algo_coach status SESSION_ID

# 导入项目提供的本地题目示例
.\.venv\Scripts\python.exe -m algo_coach import examples/problems/sum-local.json
.\.venv\Scripts\python.exe -m algo_coach --demo chat sum-demo --site local
```

`SESSION_ID` 替换为列表中的完整 ID。演示会话恢复需加 `--demo`，真实模型会话不加；两种模式不能转换。JSON 导入会替换同站点、同题号的题库条目，既有会话保留原题面快照。

## 启用 Docker 代码验证（可选）

准备可用的 Linux Docker Engine，Windows 可使用 Docker Desktop。应用不会自动安装或启动 Docker，也不会改用宿主 Python 执行用户提交代码。

```powershell
# 在项目根目录构建专用镜像
docker build --file sandbox/Dockerfile --tag algo-coach-runner:0.1 .
```

将现有 `.env` 中的开关改为以下值，然后重启 CLI/Web：

```dotenv
ALGOCOACH_RUNNER_ENABLED=true
ALGOCOACH_RUNNER_IMAGE=algo-coach-runner:0.1
```

```powershell
.\.venv\Scripts\python.exe -m algo_coach doctor

# 可选：独立验证项目的完整参考实现，不调用模型或创建会话
.\.venv\Scripts\python.exe -m algo_coach test 1 examples/submissions/two_sum.py
.\.venv\Scripts\python.exe -m algo_coach test 25 examples/submissions/reverse_k_group.py
```

检查 `doctor` 的 `enabled` 和 `ready` 是否都为 `true`。`examples/submissions/` 包含完整答案，想独立练习时可先跳过。

报告区分 `not_run`、`syntax_error`、`passed`、`wrong_answer`、`timeout`、`unsupported` 等状态：语法成功不代表逻辑正确，本地测试通过不等于 LeetCode Accepted。适配器按题面快照绑定，不能只凭题号复用测试。详细步骤、资源限制和判定方式见 [代码验证指南](Docs/RUNNER.md)。

已提供实验性中国站 CLI 提交后端，支持登录检查、代码版本预览、显式提交和结果查询，目前尚未完成真实账号验收。暂缓扩展到 10 个本地适配器，优先验证平台判题。使用步骤见 [平台提交指南](Docs/PLATFORM_JUDGE.md)，技术证据和边界见 [接入研究](Docs/LEETCODE_SUBMISSION_RESEARCH.md)。

不想复制 Cookie 时，可使用 [浏览器回传原型](Docs/BROWSER_BRIDGE.md)：手动加载 `extensions/leetcode-bridge`，在原站登录并读取一条已有提交，回传本地网页。当前仅收集未绑定代码的历史记录，不新建提交；用户已手动验证一条真实编译错误记录的读取回传；尚未验证新建平台提交。

## 归档到算法练习仓库

先准备独立于 AlgoCoach 源码的现有目录，在 `.env` 中配置，例如：

```dotenv
ALGOCOACH_PRACTICE_DIR=D:/aaaWang/algo-practice
ALGOCOACH_GIT_POLICY=export_only
```

在 CLI 中完成代码提交后，记录思路并预览归档：

```text
/explain 我使用哈希表查找已经出现的补数。
/note 查询补数要在保存当前元素之前，避免重复使用同一位置。
/finish
/complete hint_assisted
/archive
/archive show explanation.md
/archive show reflection.md
/archive apply
```

完成程度应按实际学习过程填写，默认 `unfinished`；不能把看过答案的练习声明为独立完成。`/summarize` 可选，会调用模型生成待审阅草稿；未填写的思路和反思不会伪造成个人学习经历。

典型归档结构（第 1 题）：

```text
algo-practice/
└── problems/leetcode-cn/0001-two-sum/
    ├── problem.md
    ├── solution.py
    ├── explanation.md
    ├── reflection.md
    ├── metadata.json
    ├── archive-manifest.json
    ├── attempts/
    ├── verification/
    └── tests/
```

`solution.py` 保留用户最后提交的代码，可能尚未正确，不自动替换为模型参考解。远程题面归档保留来源指引，不自动复制完整题面；同一内容重复归档不覆盖，不同内容保存在新版本目录。

默认 `export_only` 只导出。需要本地提交时配置 `commit_on_request`，再 `/commit` → `/commit apply`；需要工具推送时配置 `push_on_request`，再 `/push` → `/push apply`。不会自动初始化 Git 或创建 GitHub 仓库，推送也有提交来源限制。推荐先自行审阅归档，再手动 push。详细规则见 [归档与 Git 指南](Docs/ARCHIVE.md)。

网页会话也可退出 Web 服务后，用 CLI 指向同一数据目录恢复并归档：

```powershell
.\.venv\Scripts\python.exe -m algo_coach --data-dir .algo-coach/web sessions
.\.venv\Scripts\python.exe -m algo_coach --data-dir .algo-coach/web resume SESSION_ID
```

## 个人题单与复习

```powershell
.\.venv\Scripts\python.exe -m algo_coach daily set 25
.\.venv\Scripts\python.exe -m algo_coach daily show
.\.venv\Scripts\python.exe -m algo_coach daily chat
.\.venv\Scripts\python.exe -m algo_coach review list
```

不带 `--remote` 的日期选题是个人练习计划，不代表已获取平台每日题；默认日期按 Asia/Shanghai，支持 `--date YYYY-MM-DD`。加入复习后需主动查询、自评，没有后台提醒。详见 [日期题单与复习](Docs/DAILY_REVIEW.md)。

## 配置、数据与常见问题

`.env` 只从当前工作目录加载，已有环境变量优先。模型模式会把题面、相关对话和提交代码发送到配置的服务；打开页面、恢复会话、查看报告和单独测试本身不调用模型。代码提交分析、聊天、模型整理及失败重试会产生模型请求；完整答案可能触发独立验证和有限纠正请求。

默认数据目录是 `.algo-coach/`，Web 快速启动示例分别使用 `.algo-coach/web` 与 `.algo-coach/web-demo`。目录保存会话、代码、对话和题源缓存；不同目录不会自动共享记录。`.env`、`.venv` 和 `.algo-coach/` 已被 Git 忽略，发布代码和笔记前仍需自行检查私密内容。

| 遇到的问题 | 检查方式 |
| --- | --- |
| 学习目录被占用 | 同一目录只允许一个进程；先正常退出对应 CLI/Web，或用 `--data-dir` 指定独立目录 |
| 网页没有 CLI 历史 | 确认两者使用同一数据目录；先停止原进程，再切换入口 |
| 网页打不开或端口被占用 | 确认服务终端仍在运行，或用 `--port 8767` 更换端口并打开对应地址 |
| 模型认证、额度或格式失败 | 核对 `.env` 的密钥、模型名、地址及结构化工具调用支持；修改后重启，使用重试功能继续 |
| `not_run` 或无法测试 | 检查是否仅完成静态分析；运行 `doctor`，确认 Docker、镜像和开关 |
| `unsupported` | 当前题面没有匹配的测试适配器，不代表代码错误 |
| 中国站获取失败 | 看错误与缓存来源；已有缓存可用 CLI `--offline`；不自动猜题或换题 |
| 新代码仍看到旧报告 | 网页会标明报告属于已保存版本；先提交修改后的代码，再执行测试 |

## 项目结构与开发

```text
src/algo_coach/       应用代码、CLI、Web、教学与验证流程
src/algo_coach/data/  内置题目及已审核远程适配记录
sandbox/             Docker 执行镜像
examples/            导入题目和完整参考代码
tests/               自动化回归测试
evaluation/          教学场景、真实模型评估工具
scripts/             题源探针、浏览器验收等工具
Docs/                设计、操作指南、评估与证据
```

安装开发依赖后可运行：

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
```

普通回归使用演示/模拟模型，不消耗真实模型额度；真实 Docker 与题源网络集成有各自的显式开关。真实模型评估需另行运行：

```powershell
# 仅校验场景
.\.venv\Scripts\python.exe -m evaluation.runner validate

# 会调用模型并消耗额度；默认最多 12 次调用
.\.venv\Scripts\python.exe -m evaluation.runner run --profile smoke
```

自动契约、模型内容复核与本地代码测试是不同证据。已有真实模型评估和定向修正，但仍记录有历史引用、代码诊断及反例语义问题，不能把自动契约通过当作教学准确率。各版本结果以对应记录为准，不合并成当前版本的全面通过声明。

新增教学证据检查：结构化样例可独立核对输入约束与预期，历史引文核对具体消息和代码版本；失败进入有限重试。它不自动执行用户代码，也不保证自由文本推理正确。真实模型小样本仍有被拦截后未完成的步骤，见 [教学证据检查与失败记录](Docs/TEACHING_EVIDENCE_2026-10-21.md)。

| 文档 | 内容 |
| --- | --- |
| [项目设计](Docs/PROJECT_DESIGN.md) / [实施进度](Docs/IMPLEMENTATION_STATUS.md) | 架构、里程碑与当前实现 |
| [Web 使用指南](Docs/WEB_TESTING.md) | 页面流程、会话恢复与浏览器验证 |
| [中国站题源](Docs/LEETCODE_SOURCE.md) | 解析、缓存、来源和支持范围 |
| [代码验证](Docs/RUNNER.md) / [归档与 Git](Docs/ARCHIVE.md) | Docker 配置、报告、归档和 Git 规则 |
| [日期题单与复习](Docs/DAILY_REVIEW.md) | 日期登记、复习与自评 |
| [教学评估](Docs/TEACHING_EVALUATION.md) / [可靠性](Docs/TEACHING_RELIABILITY.md) | 场景复跑、契约和验证边界 |
| [语义复核](Docs/TEACHING_SEMANTIC_REVIEW.md) | 自动检查未捕获的问题 |
| [控制流复验](Docs/CONTROL_FLOW_RELIABILITY_2026-10-18.md) / [响应复验](Docs/RESPONSE_FOCUS_2026-10-18.md) | 定向修正结果及尚存限制 |
