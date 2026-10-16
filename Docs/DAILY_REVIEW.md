# 日期题单与复习

## 当前能力

已实现按站点和日期保存题目快照、进入教学、保留来源，以及用户主动加入的复习队列。本文介绍本地登记；新增 `daily chat --remote` 和 `/daily fetch` 可显式读取中国站每日题，详见 [中国站题源指南](LEETCODE_SOURCE.md)。远程结果与本文的手动记录分开存储，不会覆盖。

日期默认采用 Asia/Shanghai 用户日历；记录中的日期由登记者明确指定。这个日期不代表程序已经核实了站点的更新边界。不同站点、不同日期独立存储，缺少当天记录时明确报错，不把昨天的题称作今日题。

## 安排个人每日练习

从项目根目录运行，不需要模型密钥：

```powershell
.\.venv\Scripts\python.exe -m algo_coach daily set 25
.\.venv\Scripts\python.exe -m algo_coach daily show
```

日期留空使用上海日历今天；也可以提前安排或查询某天：

```powershell
.\.venv\Scripts\python.exe -m algo_coach daily set 1 --date 2026-10-16
.\.venv\Scripts\python.exe -m algo_coach daily show --date 2026-10-16
```

界面标记为“个人练习计划（不是平台每日题）”。题目必须在本地题库；其他题目先使用 `import` 导入，当前内置 1、25 题。

## 登记在平台上看到的每日题

先在 LeetCode 网页核对题号和日期，再登记。例如以下命令是参数示例，**不表示已经核实 2026-10-15 的平台每日题就是第 25 题**：

```powershell
.\.venv\Scripts\python.exe -m algo_coach daily set 25 --date 2026-10-15 --platform --source-url https://leetcode.cn/problems/reverse-nodes-in-k-group/
```

这会保存“用户登记的平台每日题（未自动核实）”，不会变成系统验证的来源。来源要求与题目的站点、slug 匹配，不接受凭据、非 HTTPS、查询参数或另一个站点的链接。提供链接不会发起网络请求。

重复登记相同内容保留首次登记时间；相同日期已有不同题目或来源时，必须明确添加 `--replace`。替换只影响后续选择，已经创建的教学会话保留原题和原日期记录。

## 开始学习

```powershell
.\.venv\Scripts\python.exe -m algo_coach daily chat
# 不调用模型的命令演示：
.\.venv\Scripts\python.exe -m algo_coach --demo daily chat
```

在已有会话中使用 `/daily` 或 `/daily 2026-10-16`，会按当前题目的站点查找并创建新会话。原会话保留，可以恢复。后续 `/hint`、`/code`、`/test`、归档流程与普通选题一致。

归档元数据保留日期、来源类别、登记时间、来源链接和题目内容哈希，不把用户登记转换为平台证明，也不因日期选择而额外公开完整导入题面。

## 复习队列

在学习会话中输入：

```text
/review add
/review
```

首次加入安排明天复习；重复加入不会把原计划推迟。列表只展示题目标识、标题和到期日期，不展示旧代码、题解或反思。相同题号若已有不同题面快照，会拒绝自动合并进度；本版还没有删除或替换旧复习快照的命令。

打开或恢复聊天时，如有到期题目会显示数量；不创建后台提醒。

开始复习需使用完整标识：

```text
/review start leetcode_cn:25
```

这会建立新会话，代码、提示等级、答案展示状态、尝试和反思均从空状态开始。题面从加入队列时的快照读取，不自动复用原解法。新会话仍受提示阶梯约束，不会因过去完成过就视为已经掌握。

完成本次尝试后显式自评：

```text
/review rate again
# 或：
/review rate hard
/review rate good
```

每次复习会话只记录一次结果。重复同一评价返回原结果，不能通过重试不断推迟日期；改评需新开复习会话。多个复习会话并存时，先记录的结果生效，旧版本会话不能覆盖新计划。

初始间隔为 1 天，无提示情况下的连续 `good` 逐步安排到评价日起 3 / 7 / 14 天，之后保持 14 天。`hard`、`again` 安排 1 天后再练。`hard`、`good` 需先保存本次代码，尚未完成可以直接记录 `again`。

使用过提示或答案时，`good` 会保留原自评但按 `hard` 安排；当前代码已有语法错误、错误答案、运行错误、超时或资源限制证据时，按 `again` 安排。没有执行测试的 `good` 仍仅为用户自评，不生成正确性、独立完成或平台 Accepted 证明。返回结果同时显示原评价、有效评价、提示使用、代码哈希和验证状态。

上述间隔和调整只是初版产品启发式，尚未验证学习效果。也允许用户提前练习；日期按实际评价当天重新安排。

## 独立命令

查询和评价不消耗模型额度，学习仍按所选模型模式执行。全局参数放在子命令前：

```powershell
.\.venv\Scripts\python.exe -m algo_coach review list
.\.venv\Scripts\python.exe -m algo_coach review list --date 2026-10-30
.\.venv\Scripts\python.exe -m algo_coach review add 完整会话ID
.\.venv\Scripts\python.exe -m algo_coach review chat leetcode_cn:25
.\.venv\Scripts\python.exe -m algo_coach review rate 完整复习会话ID again
```

所有记录仍在原数据目录的 `checkpoints.sqlite`；演示和真实模型会话保持分开。不会自动修改练习仓库或执行 Git。
