# LeetCode 直接获取研究

> 后续实现更新：本文保留接入前研究。现已接入中国站题源、解析、缓存及来源记录；当前命令和验收边界以 [中国站题源指南](LEETCODE_SOURCE.md) 为准。

日期：2026-10-16。本文记录接入前的匿名网络请求研究，以及当时的接入建议；后续实际命令以页首实现更新为准。

## 结论

**技术上可以直接获取。** 本次在开发机上成功读取了中国站的当前每日题、数字题号对应的 slug，以及公开题目的中英文题面和 Python3 模板。成功请求没有使用用户登录 Cookie、CSRF Token 或第三方 API Key。

国际站的每日题、按 slug 读取题面同样成功，但本次尝试的题库搜索接口要求登录。建议优先研究中国站 Provider，不把两站当作同一个接口实现。

当时的小样本研究不代表稳定公开 API、长期可用性或使用授权已确认。研究轮仅新增探针、测试及文档；当时主程序只有本地题库和手动日期登记，后续已完成中国站接入。

## 实测证据

环境：Windows、项目 Python 3.12 虚拟环境、httpx 0.28.1。保存的探针时间约为 2026-10-16 13:57–13:58（Asia/Shanghai）。客户端不加载项目 `.env`，不读取浏览器登录态。

| 能力 | 中国站 | 国际站 |
| --- | --- | --- |
| 地址 | `https://leetcode.cn/graphql/` | `https://leetcode.com/graphql/` |
| 每日题字段 | `todayRecord`，返回列表 | `activeDailyCodingChallengeQuestion`，返回对象 |
| 每日题 | 匿名 HTTP 200，无 GraphQL errors | 匿名 HTTP 200，无 GraphQL errors |
| 题号搜索字段 | `problemsetQuestionList` | `problemsetQuestionListV2` |
| 搜索 25 | 5 个候选中唯一精确匹配 25 | HTTP 200，但 GraphQL errors 要求注册或登录 |
| 第 25 题详情 | 中英文题面非空、Python3 模板非空 | 英文及 Python3 模板非空，中文内容为空 |

两个站点本次都返回日期 `2026-10-16`、展示题号 **2916**、slug `subarrays-distinct-element-sum-of-squares-ii`，中文题名“子数组不同元素数目的平方和 II”。[中国站原题页面](https://leetcode.cn/problems/subarrays-distinct-element-sum-of-squares-ii/)

随后读取该题中国站详情成功，中英文题面、样例输入、Python3 模板和参数元数据均非空。其内部 `questionId` 为 `3139`，展示题号 `questionFrontendId` 才是 `2916`，不能混用。

已保存结果：

- [中国站每日题、详情和题号搜索](evidence/leetcode-cn-2026-10-16.json)
- [国际站每日题、详情和登录错误](evidence/leetcode-com-2026-10-16.json)
- [中国站当日题目详情](evidence/leetcode-cn-daily-detail-2026-10-16.json)

报告包含请求时间、HTTP 状态、响应大小、响应哈希、必要元数据和字段统计。没有保存完整题面、响应 Cookie、账户信息或答案。哈希只是本次观测标识，不是签名；没有原始响应时不能凭摘要离线重建题面。

## 具体查询

### 1. 每日题

中国站最小示意：

```graphql
query {
  todayRecord {
    date
    question { questionFrontendId titleSlug translatedTitle isPaidOnly }
  }
}
```

国际站使用 `activeDailyCodingChallengeQuestion`，还返回相对 `link`。应保留实际响应日期，不直接用用户本地零点推断平台日期。中国站返回列表，未来解析时应按请求日期和唯一性校验，不能无条件取第一项。一次两站日期相同的观测不能证明更新时区或跨日边界相同，也不能证明支持历史日期查询。

### 2. 从题号定位到 slug

中国站本次成功查询：

```graphql
query($term: String!) {
  problemsetQuestionList(
    categorySlug: "", limit: 5, skip: 0,
    filters: {searchKeywords: $term}
  ) {
    total
    questions { frontendQuestionId title titleSlug paidOnly }
  }
}
```

`term="25"` 的返回题号顺序为 `1287`、`25`、`LCP 25`、`679`、`LCP 18`。**第一条不是第 25 题。** 必须精确比较 `frontendQuestionId == "25"` 且要求恰好一次命中，否则明确失败，不能用第一条结果或模型猜 slug。

列表字段叫 `frontendQuestionId`，详情字段却叫 `questionFrontendId`，Provider 需要统一。只验证了第 25 题前 5 条结果的精确命中；尚未证明任意题号都能首批命中，后续可设计有上限的分页。

初次尝试旧子字段 `totalNum` / `data` 返回 HTTP 400；改为 `total` / `questions` 成功。国际站旧根字段不存在，错误提示 V2；V2 当前匿名调用返回 HTTP 200 的登录错误。这说明只判断状态码 200 会产生误报。本次没有尝试绕过该登录要求。

### 3. 题面、样例和函数模板

通过 `question(titleSlug: $slug)` 读取以下字段：

| 字段 | 本次确认的含义 | 接入注意 |
| --- | --- | --- |
| `content` / `translatedContent` | 英文 / 中文 HTML | 需要清洗并保留列表、代码、图片来源等信息 |
| `codeSnippets { langSlug code }` | 语言函数模板 | `python3` 是模板，不是参考答案 |
| `metaData` | 函数名、参数、返回类型的 JSON 字符串 | 需解析和校验，不执行内容 |
| `sampleTestCase` | 样例输入 | 没有独立的预期输出列表，不能直接当完整测试集 |
| `isPaidOnly` | 付费标记 | 本次仅查询正常匿名可见的公开题目 |

第 25 题中文 HTML 为 1039 字符、英文为 1374 字符，并有两张图片；探针没有自动下载图片。第 2916 题中文 HTML 为 1461 字符。探针只统计字段、输入/输出/约束标记，不提供生产级 HTML 清洗或可靠的结构化题面拆分。

## 对 AlgoCoach 的接入建议

建议独立实现实验性 `LeetCodeCNProvider`，由用户命令显式触发，固定请求中国站域名。

```text
每日题：todayRecord → 校验站点/响应日期/唯一题目 → slug → 详情
指定题：有上限的搜索 → 精确匹配展示题号 → slug → 详情再核对题号
共同步骤：清洗和结构校验 → 独立来源快照/缓存 → 用户开始教学
```

具体还需完成：

1. **结构化题面**：现有 `Problem` 要求约束、输入/输出样例、签名。必须从真实内容解析，缺失则报错，不能编造占位字段。部分 LCP 题号有空格、slug 有大写，与现有正则不兼容；首版限定普通数字题号更容易验证。
2. **独立来源类型**：当前 `DailySelection` 只有个人计划和用户登记。远程获取应增加独立类型、实际响应日期和获取时间，不能伪装成用户登记或改写旧会话。
3. **缓存隔离**：不能直接用 `LocalProblems.import_file` 覆盖本地题库。远程缓存按站点、题号/slug、内容哈希保存，日期映射单独记录；旧日期缓存不能称作今日题。
4. **验证适配器**：`canonical_adapter()` 当前要求与内置教学摘要的完整题目哈希一致。远程第 25 题虽然是同一算法题，仍不会自动获得本地测试适配。后续应审核并绑定远程快照或版本，不能仅凭题号放松验证。第 2916 题目前没有测试适配器。
5. **错误处理**：区分超时、401/403/429、200 内的 GraphQL errors、空题面、字段改变；拒绝或限流时停止，不切换代理或反复试探。未知 Schema 明确失败，不交给模型补题。
6. **多日验收**：保存冻结响应用于普通单元测试，真实网络检查显式开启。继续验证多日更新、当前日期边界与更多题号。本次没有测量站点限流阈值、长期成功率、历史查询或更新 SLA。

这些是后续方案，不是已存在的 `fetch` 命令。研究结果不自动改变当前 `/daily` 行为。

## 复跑方式

使用现有虚拟环境；httpx 已在依赖快照里，不需另装：

```powershell
# 默认一次请求，只查中国站当前每日题
.\.venv\Scripts\python.exe scripts/probe_leetcode.py

# 最多三次请求：每日题、第 25 题详情、搜索定位
.\.venv\Scripts\python.exe scripts/probe_leetcode.py --site cn --probe all

# 已知 slug 的题目字段检查
.\.venv\Scripts\python.exe scripts/probe_leetcode.py --site cn --probe detail --slug reverse-nodes-in-k-group
```

`--output 新文件.json` 保存元数据摘要，不覆盖已有文件。国际站 `--site com --probe all` 本次因最后一步需要登录，退出码为 1；这不是主程序故障。

探针使用固定域名、不跟随重定向，连接超时 5 秒、读超时 15 秒，响应上限 256 KiB，不自动重试；客户端每次探针清空 Cookie。HTTP 401/403/429 或 GraphQL 登录错误会停止该站后续请求。它不调用模型、不写用户题库、不接收或使用浏览器凭据。合成响应测试不联网。

## 使用条件

已重新查阅[中国站服务条款](https://leetcode.cn/terms-c/) §3.1.6 / §3.3 和[国际站服务条款](https://leetcode.com/terms/) Use of Services。前者对未经授权的访问、复制、传播及平台外使用有限制，后者明确限制 crawling/scraping/spidering。

本次请求成功只说明技术可读，不构成第三方使用许可或稳定公开 API 承诺。持续自动读取、把完整题面发送模型、公开再分发的范围仍需分别明确；本记录不对所有个人使用情形作法律判断。现有 Git 归档继续默认采用自写摘要或来源指引，不自动公开完整远程题面。
