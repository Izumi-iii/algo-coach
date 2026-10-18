# AlgoCoach

用交流引导解题、分析用户代码并保存学习过程的算法学习 Agent。

当前是 **本地 CLI 原型，已接入中国站题源、教学、两题验证和归档/Git 流程**，使用 LangChain 接入模型、LangGraph 编排每轮教学、SQLite 保存会话。支持本地摘要、JSON 导入及显式远程获取。

## 已实现与边界

- 题号、`leetcode25`、中国站链接解析；可显式获取中国站每日题和公开题面，带独立缓存及来源记录。
- H0～H3 分级提示；明确 `/answer` 才允许完整解答。
- 真实模型结构化教学回复、权限检查及有限重试；失败不切换为演示。
- 显式 `--demo` 离线脚本演示；它不理解用户思路，不评判算法正确性。
- 保存用户代码版本、SHA-256 和静态语法检查；显式 `/test` 验证内置及已审核远程版本的第 1、25 题。
- 动态验证包含样例、边界和固定种子随机用例；代码、执行器、测试集版本及失败反例写入报告。已运行真实容器测试，验收记录见 [实施进度](Docs/IMPLEMENTATION_STATUS.md)。
- 退出后恢复题目、阶段、提示等级、代码版本和用户反思。
- 预览并归档题目、原始代码、思路、反思、历次尝试、验证报告和独立复验工具；重复执行不覆盖旧归档。
- 可选模型题解整理草稿；本地提交和远端推送均需启用策略、预览后明确执行，默认只导出。
- 按日期登记个人练习计划或用户核对的每日题；到期复习查询、新会话复习和显式自评调度。

暂未实现通用判题、国际站 Provider 和后台每日提醒。已建立固定教学评估集和真实模型复跑工具，可靠性修正后完整 30 场景的自动契约全部通过，两份实际生成的参考代码通过独立本地测试；64 步教学质量及语义泄题仍待人工复核。结果和限制见 [教学可靠性](Docs/TEACHING_RELIABILITY.md)。

已对这 64 步完成独立标记的 AI 辅助复核，发现自动契约未捕获的内容问题；V2 已分批覆盖 12/12 场景，Docker 恢复后补跑 3 项自动契约通过；两批源码不同，不能视为同一版本完整通过，补跑发现的验证状态表述及展示代码对应关系已修正，2 个已知场景定向复验通过；仍有代码诊断语义问题。辅助结果不计作人工评分，详情见 [语义复核与首测](Docs/TEACHING_SEMANTIC_REVIEW.md)。

## 安装

在项目根目录使用 PowerShell。建议 Python 3.12；无需激活环境即可运行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
```

本次验证环境的依赖快照见 `requirements-lock.txt`，重建同版本环境可先执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe -m pip install -e . --no-deps
```

依赖快照不保证所有操作系统和 Python 版本的兼容性。

## 无密钥体验流程

```powershell
.\.venv\Scripts\python.exe -m algo_coach problems
.\.venv\Scripts\python.exe -m algo_coach --demo chat leetcode25
```

进入后可以依次输入：

```text
/hint
/hint
/status
/finish
/note 我需要先保存组后继，再修改节点连接。
/quit
```

`--demo` 只展示预置提示与参考答案，不调用模型。反复 `/hint` 最多到 H3；使用 `/answer` 才展示预置完整解答。所有 CLI 全局参数（例如 `--demo`、`--data-dir`）放在子命令前。

## 配置真实模型

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

在 `.env` 中填写：

```dotenv
ALGOCOACH_MODEL=你的模型名称
ALGOCOACH_API_KEY=你的密钥
ALGOCOACH_MODEL_PROVIDER=openai
# 使用兼容服务时填写；留空则使用对应供应商默认端点
# ALGOCOACH_BASE_URL=https://your-provider.example/v1
```

`openai` 表示当前已打包的 LangChain 接入适配器，也可连接支持工具调用的兼容服务；这不意味着任意兼容端点都已验证。其他供应商需要自行安装对应 LangChain 集成包。模型需要支持结构化工具调用输出，不支持时会明确失败。

```powershell
.\.venv\Scripts\python.exe -m algo_coach chat 25
```

模型模式会把题面、相关对话和提交的代码发送到所配置的服务。密钥不保存到学习状态。CLI 只加载当前工作目录的 `.env`，环境变量优先；不会自动寻找父目录配置。

### 使用 DeepSeek 官方接口

在 `.env` 中填写以下配置（已有 `.env` 时直接编辑，不要覆盖已有密钥）：

```dotenv
ALGOCOACH_MODEL=deepseek-flash
ALGOCOACH_API_KEY=你的DeepSeek_API_Key
ALGOCOACH_MODEL_PROVIDER=openai
ALGOCOACH_BASE_URL=https://api.deepseek.com
```

这里的 `openai` 指兼容协议适配器；上述地址将请求发送到 DeepSeek。项目会对这个官方域名显式关闭思考模式，先验证结构化教学流程，不保存或回传推理内容。第三方中转域名不自动应用该参数。

模型名与接口根据 2026-10-15 查阅的 [DeepSeek 官方文档](https://api-docs.deepseek.com/) 设置；非思考模式配置参考 [Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)。接入参数有模拟请求测试，并已使用本地配置通过一次真实教学工作流验证；这不代表完整质量评估已完成。

运行：`.\.venv\Scripts\python.exe -m algo_coach chat 25`。

## 会话命令

| 命令 | 用途 |
| --- | --- |
| `/hint` | 请求更具体一级的提示，最高 H3 |
| `/answer` | 明确允许完整讲解与代码，记录答案已展示情况 |
| `/code path/to/solution.py` | 读取文件、保存原代码及静态语法检查结果 |
| `/code` | 粘贴多行代码，以独立一行 `.end` 结束，`.cancel` 取消 |
| `/test` | 使用 Docker 验证当前保存代码，不调用模型；环境不可用时明确返回未运行 |
| `/status` | 查看当前阶段、尝试次数和验证结果 |
| `/finish` | 进入复盘，不自动标记独立完成 |
| `/note 内容` | 保存用户自己的反思，再次输入会替换现有反思 |
| `/explain 内容` | 保存当前代码对应的用户思路 |
| `/summarize` | 显式调用模型整理现有代码，生成待审阅草稿；消耗模型额度 |
| `/complete 完成程度` | `independent`、`hint_assisted`、`answer_assisted` 或 `unfinished`；默认未完成 |
| `/archive`、`/archive show 文件名` | 生成归档预览、查看文件内容 |
| `/archive apply` | 写入此前预览，数据变化后需重新预览 |
| `/commit`、`/commit apply` | 预览、执行本地提交；需要配置策略 |
| `/push`、`/push apply` | 查询远端并预览、明确推送；需要配置策略 |
| `/daily [YYYY-MM-DD]` | 学习当前站点指定日期已登记的题目，缺少记录时明确报错 |
| `/fetch leetcode25` | 从中国站获取或读缓存，开始新会话 |
| `/daily fetch` | 获取中国站每日题并开始新会话 |
| `/review`、`/review add` | 查询到期题目、把当前题加入复习 |
| `/review start leetcode_cn:25` | 开始全新复习会话，默认隐藏旧解法 |
| `/review rate again` | 记录本次复习自评；支持 `again`、`hard`、`good` |
| `/problem 1` | 创建另一道题的新会话，旧会话保留 |
| `/retry` | 重试上次失败的教学请求，不额外提高提示级别 |
| `/help`、`/quit` | 帮助与退出 |

自然语言可直接讨论思路；目前确定性识别 `给一点提示`、`再具体一点`、`直接给答案` 等明确表达。对含糊的答案请求请使用 `/answer`，避免误授权。

明确表达“不知道怎么做”“没有思路”等求助时，初始 H0 可提升到 H1，允许一个轻量引导问题；明确只讨论题意或不要提示时仍保留 H0。该规则不会自动给出算法或完整答案。模型失败时会区分认证、余额、限流、格式和教学权限问题；输入 `/retry` 可重试已保存的请求。

`/answer` 现在检查是否实际交付完整实现；已启用 Docker 且题型有适配器时，模型参考代码先经独立本地验证，失败会有限纠正，仍失败则不交付。参考代码报告与用户代码报告分开；执行器禁用或题型未支持时会如实标注未验证。`/retry` 保留失败的答案请求类型，`/code` 不自动执行用户代码。边界见 [教学可靠性](Docs/TEACHING_RELIABILITY.md)。

输入 `/code` 后的内容只做 AST 语法分析，语法成功的结果是 `not_run`，不代表逻辑正确。模型提出的错误及反例属于未执行的分析推断。

若随后显式 `/test` 并获得有效报告，模型可使用实际失败证据或描述本地测试通过；不能声称平台已通过。修改代码会使当前验证恢复为 `not_run`。

## 配置动态验证

先运行环境检查，不需要模型密钥：

```powershell
.\.venv\Scripts\python.exe -m algo_coach doctor
```

执行器默认禁用，不会自动安装 Docker 或拉取镜像。准备 Linux Docker Engine、构建镜像及在现有 `.env` 开启验证的步骤见 [代码验证指南](Docs/RUNNER.md)。没有容器环境时仍可正常讨论思路，系统不会用宿主 Python 执行用户代码。

环境配置后，可在会话中使用 `/code` → `/test`，也可独立运行：

```powershell
.\.venv\Scripts\python.exe -m algo_coach test 1 examples/submissions/two_sum.py
```

## 归档到算法练习目录

在 `.env` 中配置已存在的独立目录，路径不能位于 AlgoCoach 源码仓库中：

```dotenv
ALGOCOACH_PRACTICE_DIR=D:/aaaWang/algo-practice
ALGOCOACH_GIT_POLICY=export_only
```

在保存代码后，可在当前会话输入：

```text
/test
/explain 我使用哈希表保存已经访问过的元素及下标。
/note 查询补数要在保存当前元素之前，避免重复使用同一位置。
/archive
/archive show explanation.md
/archive apply
```

需要模型帮助整理时，在归档预览前输入 `/summarize`。思路和反思未填写时会如实留空说明；本地测试通过不会自动标记独立完成或 LeetCode Accepted。

归档无需 Git 仓库或模型密钥。Git 初始化、远程仓库创建、身份及认证需自行准备；提交和推送的策略与限制见 [归档与 Git 指南](Docs/ARCHIVE.md)。

## 日期题单与复习

可以先安排个人今天的练习，再开始学习：

```powershell
.\.venv\Scripts\python.exe -m algo_coach daily set 25
.\.venv\Scripts\python.exe -m algo_coach daily show
.\.venv\Scripts\python.exe -m algo_coach daily chat
.\.venv\Scripts\python.exe -m algo_coach review list
```

以上选题是**个人练习计划**，不代表已获取 LeetCode 官方每日题。默认日期采用 Asia/Shanghai 用户日历；也可通过 `--date YYYY-MM-DD` 指定。日期记录及复习无需模型密钥；进入真实模型教学仍需原模型配置。

操作、评价规则和边界见 [日期题单与复习](Docs/DAILY_REVIEW.md)。直接获取的接入状态与使用条件见 [题源接入核查](Docs/SOURCE_FEASIBILITY.md)。

中国站获取已接入教学主程序：

```powershell
.\.venv\Scripts\python.exe -m algo_coach daily chat --remote
.\.venv\Scripts\python.exe -m algo_coach chat leetcode25 --remote
# 只获取、缓存和展示，不调用模型：
.\.venv\Scripts\python.exe -m algo_coach fetch leetcode25
```

默认本地命令仍保留。远程读取支持 `--offline` 和 `--refresh`；缓存、来源、解析范围和日期校验见 [中国站题源指南](Docs/LEETCODE_SOURCE.md)。

## 恢复与导入

```powershell
.\.venv\Scripts\python.exe -m algo_coach sessions
.\.venv\Scripts\python.exe -m algo_coach --demo resume 完整会话ID
.\.venv\Scripts\python.exe -m algo_coach status 完整会话ID
.\.venv\Scripts\python.exe -m algo_coach import examples/problems/sum-local.json
.\.venv\Scripts\python.exe -m algo_coach --demo chat sum-demo --site local
```

真实模型会话恢复时不要加 `--demo`。演示和模型会话不能互相转换。本地题目格式参见 [示例](examples/problems/sum-local.json)，导入会覆盖同站点、同题号的题库条目；已存在会话保留自己的题面快照。

默认数据在当前目录 `.algo-coach/`，含题库、SQLite 检查点、代码和对话，应按私密内容保管。使用 `--data-dir` 或 `ALGOCOACH_DATA_DIR` 可改变位置；恢复时需指向原目录。同一数据目录同时只允许一个进程。

## 开发验证

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
```

自动化测试覆盖状态恢复、提示权限、输入校验、代码版本、故障降级与 CLI 跨进程恢复；使用脚本或模拟模型，不消耗真实模型额度。测试通过不能作为真实教学质量评估结论。

真实模型评估包含 30 个多轮场景（20 dev、10 holdout）。在现有模型配置下运行：

```powershell
.\.venv\Scripts\python.exe -m evaluation.runner validate
.\.venv\Scripts\python.exe -m evaluation.runner run --profile smoke
```

第二条命令会调用真实模型，默认最多 12 次调用，报告保存在独立的 `.algo-coach/evaluations/` 目录。完整场景、Docker 前置条件、人工评分和两次运行比较见 [教学评估指南](Docs/TEACHING_EVALUATION.md)。

完整目标、后续里程碑和技术来源见 [设计文档](Docs/PROJECT_DESIGN.md)。

重复值误诊修正及新增 V3 场景复验见 [2026-10-18 记录](Docs/DUPLICATE_REASONING_2026-10-18.md)；自动契约通过仍不能代替内容复核。

针对换代码后沿用旧诊断、忽略提前 return 的修正见 [控制流证据与复验](Docs/CONTROL_FLOW_RELIABILITY_2026-10-18.md)，仅覆盖严格匹配的字典扫描模板。
