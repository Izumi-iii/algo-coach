# 本地代码验证：配置、使用与验证边界

更新日期：2026-10-15。

## 当前交付与环境状态

已实现 Docker 执行接口、宿主侧判定器、两道题的测试集，以及会话中的 `/test` 和独立 CLI `test` 命令。

当前开发机已安装 WSL 3.0.3 和 Docker Desktop，Linux 引擎可用，执行镜像 `algo-coach-runner:0.1` 已构建。6 项真实容器集成测试已通过，包括两题参考解、错误下标、网络与文件权限探针、死循环和大量输出。项目本地配置已备份到被 Git 忽略的 `.algo-coach/backups/`，并开启动态验证；`doctor` 返回 `enabled=true`、`ready=true`。独立 CLI 已确认第 1 题参考解通过 47/47 个本地用例。

普通测试仍包含模拟观测与进程探针；真实容器测试默认需显式开启，以便无 Docker 的其他开发环境运行基础测试。这里的通过结果只覆盖现有探针，不能证明绝对隔离或所有资源限制均已验证。

应用不会自动安装 Docker、启动系统服务、拉取执行镜像，或改用宿主 Python 运行提交代码。`/code` 始终只保存和静态检查，`/test` 才是明确的动态执行请求。

## 环境准备

需要可用的 Docker CLI、运行中的 Linux Docker Engine，以及在本地构建的专用镜像。Windows 可使用已配置 Linux 容器的 Docker Desktop；其安装和系统配置应单独确认后进行。

先运行检查（不需要模型密钥，不受学习会话锁影响）：

```powershell
.\.venv\Scripts\python.exe -m algo_coach doctor
```

`ready` 仅表示 Linux 引擎和镜像存在，不代表隔离测试已经通过；`enabled` 表示应用是否允许发起验证。两个字段分别判断。

在 Docker 环境已配置的前提下，从项目根目录构建镜像：

```powershell
docker build --file sandbox/Dockerfile --tag algo-coach-runner:0.1 .
```

构建会获取 `python:3.12-slim` 基础镜像。构建上下文通过 `.dockerignore` 限制，Dockerfile 只复制执行 worker，不复制 `.env`、仓库凭据、学习数据库或用户项目目录。当前基础镜像采用版本标签；正式发布应进一步固定基础镜像 digest。每次运行都会解析并记录本地执行镜像的不可变 ID，并以该 ID 启动。

**直接编辑现有 `.env`，增加以下配置，不要复制示例文件覆盖已有密钥：**

```dotenv
ALGOCOACH_RUNNER_ENABLED=true
ALGOCOACH_RUNNER_IMAGE=algo-coach-runner:0.1
```

然后再次执行 `doctor`。在旧 CLI 会话中修改环境配置不会自动生效，需要退出并重新启动。

## 使用方式

### 在教学会话中验证自己的代码

```powershell
.\.venv\Scripts\python.exe -m algo_coach chat 1
```

进入后输入：

```text
/code 你的代码文件.py
/test
```

也可以用 `/code` 粘贴代码，以独立一行 `.end` 结束，然后输入 `/test`。验证本身不调用模型；后续可以询问“请引导我分析这个失败用例”，模型会看到当前代码和报告。

修改代码后重新 `/code`，新的版本恢复为静态检查结果，必须重新 `/test`。旧版本的测试证据仍保存在对应尝试中，不转移到新版本。

### 不启动教学会话，直接检查文件

```powershell
.\.venv\Scripts\python.exe -m algo_coach test 1 examples/submissions/two_sum.py
.\.venv\Scripts\python.exe -m algo_coach test 25 examples/submissions/reverse_k_group.py
```

这些文件是开发参考示例，包含完整解法，不会默认放入模型的教学上下文。独立命令不调用模型、不创建学习会话；通过返回退出码 0，未运行、未通过或发生错误返回 1。

## 支持范围与测试证据

支持内置 `leetcode_cn:1`、`leetcode_cn:25` 快照，以及 `data/remote_adapters.json` 中已审核的两题远程快照。全部按完整规范化题目哈希绑定；题面或元数据变化后返回 `unsupported`，避免只看题号误用测试。远程接入与缓存见 [中国站题源指南](LEETCODE_SOURCE.md)。

| 题目 | 调用约定 | 当前测试 | 判定方式 |
| --- | --- | --- | --- |
| 两数之和 | `Solution().twoSum(nums, target)` | 7 个人工用例 + 40 个固定种子的随机小规模用例 | 暴力枚举核实唯一解，检查两个不同且合法的下标，允许答案顺序不同 |
| K 个一组翻转链表 | `Solution().reverseKGroup(head, k)` | 6 个人工用例 + 40 个固定种子的随机小规模用例 | 比较预期值序列和原节点身份顺序，识别环、替代节点、只交换值和不足 k 个节点的处理错误 |

worker 提供 `ListNode`、`Optional` 和 `List`；每个用例创建新的输入、命名空间和 `Solution` 实例。用例在同一个容器进程内依次运行，并非逐用例独立进程，因此恶意修改模块或内建对象可能影响后续用例。

随机种子固定为 `20261015`，适配器和测试集版本写入报告。容器只获得代码、用例 ID 和输入；预期输出与正确性判定留在宿主。首个失败用例返回输入、预期、实际和原因；运行异常尽量提供受控异常类型与提交代码中的行号，不记录原始异常消息。

测试规模用于验证功能及边界，没有覆盖平台全部约束或性能上界。`passed` 只表示本地这组测试通过，不表示平台 Accepted、算法绝对正确或复杂度已被证明。

## 执行限制与故障状态

Docker 命令由程序固定生成，使用参数数组调用，不接受模型提供的 shell 命令：

- Linux 非 root 用户 `65534:65534`，只读根文件系统。
- 不联网、不挂载宿主目录或 Docker socket，不传入 API Key 和 Git 凭据。
- 移除 capabilities，禁止提权，禁用容器日志持久化。
- 内存 256 MiB、交换加内存总量 256 MiB、1 CPU、64 个进程、64 个文件描述符。
- `/tmp` 使用最大 16 MiB 的 tmpfs，禁止执行和设备文件。
- 每个任务包含整组用例；Docker 启动与执行合计墙钟上限 10 秒，stdout 与 stderr 合计上限 64 KiB。
- 由宿主监督 Docker CLI；超时、取消和输出超限后，尝试强制删除本次唯一命名的容器。无法确认清理时返回 `runner_error` 并给出本次容器名，不能报告成功。

Docker CLI 的限制参数参考 [官方 docker run 文档](https://docs.docker.com/reference/cli/docker/container/run/)。实际限制是否生效必须在目标机器运行下面的集成测试验证。

| 状态 | 含义 |
| --- | --- |
| `not_run` | 未启用执行器，或引擎/镜像缺失，没有执行证据 |
| `syntax_error` | 静态语法解析失败，未启动执行 |
| `unsupported` | 题目没有匹配的可信适配器 |
| `passed` / `wrong_answer` | 收到完整合法观测后，在宿主完成判定 |
| `runtime_error` | worker 观察到候选代码异常 |
| `timeout` / `resource_limit` | 本地时间、输出或内存限制；不等价于 LeetCode TLE/MLE |
| `runner_error` | 协议异常、Docker 退出、结果缺失或清理未确认，不判用户算法错误 |
| `cancelled` | 用户取消任务 |

容器启动超时或协议失败时不能确认代码实际执行了多少，因此报告不会虚构已运行用例数；`executed=false` 不应被解读为已证明代码完全没有启动。

## 在真实 Docker 环境中验收

镜像构建后，显式开启容器集成测试：

```powershell
$env:ALGOCOACH_TEST_DOCKER = "1"
.\.venv\Scripts\python.exe -m pytest tests/test_docker_integration.py -q
Remove-Item Env:ALGOCOACH_TEST_DOCKER
```

该组测试执行两道题的参考解、错误下标解、无限循环、大量输出，并探测非 root、只读根文件系统、无法访问 Docker socket、凭据缺失及外网访问失败。它们不会调用模型。显式开启后若引擎或镜像不可用，测试失败，不会跳过或改为宿主执行。

容器共用宿主内核，不是绝对安全边界；候选代码也与 worker 同进程，执行协议不防恶意伪造。本版本面向个人非对抗练习，不能作为公开多人在线判题器。公开服务需要独立工作节点、更强隔离和独立防篡改协议设计。

## 下一步验收缺口

1. 已完成本机引擎检查、镜像构建和现有 6 项真实容器集成测试；执行器代码更新后需要重建镜像并重新运行。
2. 补齐并实际运行内存与进程数限制探针，检查 Windows / Docker Desktop 的限制效果。
3. 验证真实模型能根据本地失败证据进行教学，而不会直接给完整代码。
4. 扩展经核实的题目与测试适配器，再进入解题目录归档阶段。
