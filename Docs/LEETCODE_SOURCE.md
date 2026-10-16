# 中国站题源、缓存与来源记录

已接入中国站网页内部 GraphQL。使用用户明确触发的命令读取公开题目，不需要填写 LeetCode Cookie 或第三方 API Key。国际站、付费题、带空格的特殊题号、设计类多方法题暂不支持。

## 直接开始学习

从项目根目录运行，使用现有 DeepSeek 配置：

```powershell
# 获取中国站当前每日题，或读取该日期的有效缓存，进入教学
.\.venv\Scripts\python.exe -m algo_coach daily chat --remote

# 按展示题号取得完整题目并开始教学
.\.venv\Scripts\python.exe -m algo_coach chat leetcode25 --remote
```

`--remote` 明确选择远程题源及其独立缓存。原来的 `chat 25` 仍读本地教学摘要；`daily set/show/chat` 不带 `--remote` 时仍使用原来的手动日期登记，不混淆个人计划和平台返回记录。

会话中的入口：

```text
/fetch leetcode25
/fetch https://leetcode.cn/problems/two-sum/
/daily fetch
```

这些命令创建新会话，原会话保留。远程题面的约束和样例来自实际响应，模板仅作 Python 签名解析，不执行模板。后续 `/hint`、`/code`、`/test`、思路、反思和归档命令保持一致。

`--demo` 仅关闭模型调用，**不等于题源离线**；例如 `--demo daily chat --remote` 仍可请求 LeetCode。真正不请求题源应使用 `--offline`。模型模式仍会把题目和相关对话发送给你配置的模型服务。

## 只获取题目，不调用模型

```powershell
.\.venv\Scripts\python.exe -m algo_coach fetch leetcode25
.\.venv\Scripts\python.exe -m algo_coach fetch https://leetcode.cn/problems/reverse-nodes-in-k-group/
.\.venv\Scripts\python.exe -m algo_coach daily fetch
.\.venv\Scripts\python.exe -m algo_coach daily show --remote
```

这些命令不要求模型密钥，也不创建教学会话。数字题号搜索采用精确匹配，最多读取 3 页、共 60 个候选；找不到唯一题号则明确失败，可以改用中国站原题链接。不取模糊搜索第一条，不让模型猜题。

只支持 HTTPS 中国站原题链接，可带 `/description/` 路径；拒绝其他站点、凭据、非标准端口、查询参数和片段。输入链接仅用来提取 slug，网络请求始终发往固定的 `https://leetcode.cn/graphql/`，不跟随重定向或加载题面中的链接、图片。

## 缓存策略

缓存与本地题库分开，位于同一私密数据目录 `checkpoints.sqlite` 的独立表中：

- `app_remote_snapshots`：解析后的题面及来源快照，只追加；不存完整原始响应。
- `app_remote_heads`：每个远程展示题号的当前快照指针。
- `app_remote_daily`：接口返回日期对应的每日题及完整快照。
- `app_remote_network`：本地请求间隔及限流冷却时间，重启后仍有效。

默认有效期 **6 小时**，有效缓存直接读取。过期缓存会尝试更新；连接失败、HTTP 错误、拒绝访问或限流时，可以退回请求的同一道题/同一日期旧缓存，并明确显示 `stale_cache`、原获取时间和“未重新核实”提示。结构变化、题号不一致、日期不一致、解析失败不会被旧缓存掩盖。

显式模式：

```powershell
# 只读缓存：缺失时明确报错，绝不请求题源
.\.venv\Scripts\python.exe -m algo_coach fetch 25 --offline
.\.venv\Scripts\python.exe -m algo_coach chat 25 --remote --offline
.\.venv\Scripts\python.exe -m algo_coach daily chat --remote --offline

# 强制联网更新：失败时报告错误，旧缓存保留，不悄悄退回
.\.venv\Scripts\python.exe -m algo_coach fetch 25 --refresh
.\.venv\Scripts\python.exe -m algo_coach daily fetch --refresh
```

`--offline` 和 `--refresh` 互斥；聊天及 `daily show/chat` 使用它们时要同时指定 `--remote`。会话命令也支持 `/fetch --offline 25`、`/fetch --refresh 25`、`/daily fetch --offline` 等形式。

每次刷新形成新的观察快照，不覆盖旧快照或已存在会话。缓存同时绑定解析器版本，解析器升级后的旧格式缓存不会被当成当前可用结果。尚无缓存清理/容量管理命令，适用于个人串行使用。

## 日期与来源

默认查询日期采用 Asia/Shanghai 用户日历，远程每日题还必须与接口 `date` 精确一致。中国站返回列表时，要求该日期唯一命中；响应不是请求日期、出现多个匹配或目标不可见时明确失败，不用昨天的题冒充今天。

当前接口没有提供本实现可用的任意历史日期查询能力。历史或未来日期只读已经获取过的对应日期缓存；没有缓存则在发请求前报错。例如：

```powershell
.\.venv\Scripts\python.exe -m algo_coach daily fetch --date 2026-10-16 --offline
```

远程日期记录的类别为 `provider_fetched`，与 `personal_plan`（个人计划）、`user_reported_platform`（用户登记）分开。手动登记不会被远程获取覆盖。

会话 `/status` 及归档元数据保存：固定来源类型/端点、快照 ID、UTC 获取时间、规范化题面哈希、原始字段投影哈希、解析器版本、题面语言，以及创建会话时的缓存状态。每日题另保留响应日期和核对时间。这些哈希提供本地一致性核对，不是作者身份或平台签名证明。

复习入队保留对应题面和来源；新复习会话显示 `review_snapshot`，提示使用入队时快照，不联网替换题面。已有复习记录采用新增可空 `source` 列迁移，旧记录和进度保留。

## 解析范围与动态验证

解析器保留段落、列表、上下标、样例说明和进阶要求；丢弃脚本、样式、嵌入框架、控制字符和不安全链接。图片仅保留允许域名的 HTTPS 来源地址，不自动请求。样例输入、输出和约束从实际 HTML 提取；缺失或无法识别时失败，不编造占位内容。模板仅通过 AST 分析函数签名，并核对 `metaData` 中的函数名和参数，不执行其中任何代码。

这仍是保守的首版解析器，不保证支持所有 LeetCode 排版和题型。中文内容存在时优先解析中文；中文缺失时可读英文，结构不满足契约则明确报错。

动态验证只开放内置摘要及**经过审核的第 1、25 题远程快照**。允许列表在 `data/remote_adapters.json` 中，绑定完整规范化题目哈希；修改题意、约束、样例、签名或其他题目字段后都会失配，不会仅凭题号复用测试。第 2916 题等其他题目可教学和归档，`/test` 会如实返回 `unsupported`。

允许动态测试不等于允许公开完整题面。归档仅对项目自写内置摘要导出全文；远程题目即使有适配器，`problem.md` 也只导出来源指引，代码、用户思路、反思、验证报告、用例和来源元数据照常保留。

## 网络与验收边界

请求间隔至少 1 秒；连接超时 5 秒、读超时 15 秒，解压后的响应上限 512 KiB。HTTP 200 中的 GraphQL 错误也算失败。HTTP 401/403、登录错误和限流会停止请求并记录冷却；429 默认至少 60 秒，可采纳有界的数字 `Retry-After`。不自动重试、不保存原始服务错误、不读取用户 Cookie。响应结构异常或解析失败不会更新题目指针。

普通测试使用自写合成响应，不联网或调用付费模型。真实网络与 Docker 串联验收需要显式开启：

```powershell
$env:ALGOCOACH_TEST_LEETCODE='1'
$env:ALGOCOACH_TEST_DOCKER='1'
.\.venv\Scripts\python.exe -m pytest tests/test_remote_live.py -q
```

该验收读取真实第 1、25 题与当前每日题，核对解析、缓存、教学会话、来源及归档，再用 Docker 验证两题参考实现；不会向 LeetCode 提交代码，也不会自动推送 Git。

内部接口可能变动，小样本连通不构成长期稳定或题目使用范围的承诺。既有技术研究和条款来源见 [直接获取研究](LEETCODE_DIRECT_RESEARCH.md)。
