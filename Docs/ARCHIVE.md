# 题解归档与 Git 操作

## 适用范围

本模块把会话中的代码和学习记录写入独立算法练习目录。归档、提交、推送各自有预览及执行步骤，操作记录存于原会话数据目录的 SQLite；恢复时应保留该目录。预览不写练习文件，但会保存计划；推送预览会联网查询远端分支。

归档接受未完成或未通过的代码，并如实记录状态，不会自动生成正确实现。`/summarize` 只整理已提交代码，模型输出标为待审阅草稿，不能作为正确性证明。没有填写的反思不会被补成虚构的学习经历。

## 本机配置与基本流程

当前用户选定的目录为 `D:\aaaWang\algo-practice`；已创建目录并在 `.env` 配置 `ALGOCOACH_PRACTICE_DIR`，Git 策略保持 `export_only`。修改前的配置保存在被忽略的 `.algo-coach/backups/`，其中可能包含密钥，应按私密文件保管。

其他环境请自行建立独立目录并配置：

```dotenv
ALGOCOACH_PRACTICE_DIR=D:/aaaWang/algo-practice
ALGOCOACH_GIT_POLICY=export_only
```

进入或恢复题目会话后：

```text
/code examples/submissions/two_sum.py
/test
/explain 遍历数组时，用哈希表查找已经出现的补数。
/note 先查询后存入，避免同一位置使用两次。
/complete hint_assisted
/archive
/archive show solution.py
/archive show explanation.md
/archive show metadata.json
/archive apply
```

`/complete` 是用户对完成程度的声明，不是自动认证。默认 `unfinished`；使用实际提示后不能声明 `independent`，展示完整答案后不能声明独立或仅提示辅助。`/finish` 只是进入复盘。新代码会重置完成状态并清除旧整理草稿；代码变化也会清除旧的用户思路。

可选 `/summarize` 请求模型整理思路、正确性推理、复杂度及限制，每次最多两次有界尝试并消耗供应商额度。没有模型配置或显式演示模式时不能整理；原有 `/explain` 和 `/note` 始终可用。归档本身不调用模型。

`/archive preview "D:\其他目录"` 可临时选择已存在的目录。目录必须独立于应用源码，不能是磁盘根目录或经过符号链接、Windows 重解析点的路径。

## 文件结构与版本

```text
problems/leetcode-cn/0001-two-sum/
  problem.md
  solution.py
  explanation.md
  reflection.md
  metadata.json
  archive-manifest.json
  attempts/attempt-001.py
  verification/report.json
  verification/attempt-001.json
  tests/cases.json
  tests/replay.py
  tests/runner.py
  tests/judging.py
  tests/sandbox_worker.py
  tests/Dockerfile
  tests/README.md
```

- `solution.py` 是用户最后提交的原文，历次尝试独立保留；不自动用参考答案替换。
- 题面仅自动导出项目自写的内置教学摘要。远程和导入题目保留来源指引，由用户自行填写有权使用的摘要；远程题有已审核测试适配器也不会因此导出完整题面。
- 元数据包含会话、代码哈希、题目快照哈希、提示使用和完成声明；代码来源只表示“用户提交”，不证明作者身份。
- 每次尝试保留最新及历史验证报告。未运行、失败和通过严格区分，平台结果始终记录为未验证。
- 不导出完整聊天记录、API 密钥或模型用量配置；用户在代码和笔记中自行写入的私密内容仍会原样保存，发布前应审阅。

首次归档写上述目录。同一内容重复执行返回已存在；不同内容或用户编辑过的旧文件不会覆盖，改写到其下 `reviews/<会话ID>-<内容哈希前12位>/`。归档后修改代码、反思、验证等会使旧计划过期，需要重新预览。

写入先在同一根目录的唯一临时目录中完成并校验，再重命名发布；文件锁避免本工具并发写入。普通写入失败会清理临时目录。若进程在发布后、日志落盘前中断，再次执行会核对既有文件并恢复；若在重命名前被强制结束，可能遗留 `.algo-coach-stage-*`，请在确认没有进程运行后检查并手动清理。本机制未提供断电持久性保证，也不用于对抗恶意本地文件系统竞争。

## 独立复验

进入某个归档目录，按照其 `tests/README.md` 的实际镜像标签运行：

```powershell
python tests/replay.py --check
docker build --file tests/Dockerfile --tag <tests/cases.json中的image值> tests
python tests/replay.py
```

需要 Python 3.11+ 和 Linux Docker Engine，不需要安装 AlgoCoach、LangChain 或提供模型密钥。`--check` 只核对清单哈希；执行验证仍将候选代码放入受限 Docker 容器，不在宿主运行代码。代码改动后原哈希检查会失败，应重新归档。哈希清单不是签名或作者认证。

只有当前内置 1、25 题有测试适配器；导入题目会导出空测试集并明确无法动态验证。Dockerfile 的基础镜像尚未固定 digest，跨时间重建不保证完全相同环境；实际验证报告包含执行器证据。当前执行器边界见 [RUNNER.md](RUNNER.md)。

## 本地提交

先自行初始化或克隆练习仓库、配置 `user.name` 和 `user.email`。本工具不会自动初始化仓库或创建 GitHub 仓库。练习目录必须是该 Git 仓库根目录，当前不支持 `.git` 为文件的 worktree。

将 `.env` 中策略改为：

```dotenv
ALGOCOACH_GIT_POLICY=commit_on_request
```

重新运行会话，确保当前记录已归档且未修改：

```text
/commit
/commit apply
```

预览包含根目录、分支、HEAD、准确文件清单和提交信息；执行只暂存该归档清单中的文件。已有暂存、分支变化、归档文件变化、合并或变基中间状态、detached HEAD 均会阻止操作并保留现场。未暂存的无关文件不会被一起提交。最初空仓库可以创建第一次归档提交。

提交前有有限的疑似密钥检查，不能保证识别所有敏感信息。每次 Git 调用禁用仓库 hooks，提交调用禁用 GPG 签名，避免隐式执行附加流程或等待交互；不修改用户全局 Git 配置。依赖 hooks 或签名的仓库请自行提交。

执行结果写入操作记录。提交成功后即使日志更新失败，重试会以父提交、预期树和提交信息核对恢复，避免重复提交；无法确认时停止并要求人工检查，不自动 reset 或清理暂存区。

## 显式推送

先自行配置 `origin` 和 GitHub 认证。将策略改为：

```dotenv
ALGOCOACH_GIT_POLICY=push_on_request
```

该策略允许按请求提交和推送，但不自动执行。输入：

```text
/push
/push apply
```

推送使用预览绑定的提交 OID 到 `origin` 的当前同名分支，不 force、不自动 fetch/merge/rebase。预览后 HEAD、分支、origin 地址或远端分支变化都会要求重新检查。认证必须已经可用，本工具禁用终端认证提示；失败时不会把原始 URL 或供应商错误写进会话。

待推送历史必须全部是本数据目录操作记录中本工具为该练习仓库创建的提交。若 GitHub 仓库已有初始化 README，或本地含手工提交，请先自行审阅、同步并推送这些历史，再使用本工具；不会为了满足限制改写历史。

网络断开时可能出现“远端成功但本地未收到结果”，状态记为 `unknown`。重试先查询远端 OID，已达到预期则恢复成功，不重复创建提交。网络和远端并发状态仍可能变化，本工具不保证 GitHub 永远可达。本轮只使用临时本地 bare 仓库测试推送协议，未向真实 GitHub 推送。

## 不进入聊天也可操作

以下命令不需要模型密钥，`SESSION_ID` 替换为 `sessions` 中的完整标识。全局参数放在子命令前；数据目录必须是原会话目录。

```powershell
.\.venv\Scripts\python.exe -m algo_coach sessions
.\.venv\Scripts\python.exe -m algo_coach archive SESSION_ID D:\aaaWang\algo-practice
.\.venv\Scripts\python.exe -m algo_coach archive SESSION_ID D:\aaaWang\algo-practice --show explanation.md
.\.venv\Scripts\python.exe -m algo_coach archive SESSION_ID D:\aaaWang\algo-practice --apply PLAN_ID
.\.venv\Scripts\python.exe -m algo_coach commit SESSION_ID
.\.venv\Scripts\python.exe -m algo_coach commit SESSION_ID --apply OPERATION_ID
.\.venv\Scripts\python.exe -m algo_coach push SESSION_ID
.\.venv\Scripts\python.exe -m algo_coach push SESSION_ID --apply OPERATION_ID
```

`PLAN_ID` 与 `OPERATION_ID` 使用各次预览返回的完整值，不互换。默认 `export_only` 时提交、推送执行会被拒绝。
