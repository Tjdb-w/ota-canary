# 批次灰度推进与自动故障回滚（增量功能）

本文件描述在既有 OTA Canary 产品边界上增量补充的**批次灰度（batch rollout）**子系统。
既有命令（`device`、`release`、`status`、`fleet`）的请求/响应语义与状态结构保持不变；
本功能复用同一状态文件中的 `devices` 设备表与固件版本口径，新增独立状态键
`firmwares` 与 `batchRollouts`，与既有 `releases` 互不影响。

## 入口

仅依赖 Python 3 标准库，沿用 `python -m ota_canary` 与 `--state`。

```bash
# 登记固件版本（目标固件与稳定版本均须存在；设备当前运行某版本也视为该固件存在）
python -m ota_canary firmware register --version 2.0.0
python -m ota_canary firmware list

# 创建批次发布
python -m ota_canary batch create \
    --batch-id B1 --target-version 2.0.0 --stable-version 1.0.0 \
    --batch-size 2 --failure-threshold 0.5 --heartbeat-timeout-seconds 900 \
    --target-device-id d1 --target-device-id d2 --target-device-id d3

# 开始放量：仅把第一批设备置为 pending_upgrade
# 启动前可先做只读资格预检（不改变任何状态）
python -m ota_canary batch plan --batch-id B1 [--at 2026-10-01T08:00:00Z]
python -m ota_canary batch start --batch-id B1 [--at 2026-10-01T08:00:00Z]

# 设备回传心跳时间、当前固件版本与升级终态（result 可省，表示仅心跳）
python -m ota_canary batch report --batch-id B1 --device-id d1 \
    --version 2.0.0 --heartbeat-at 2026-10-01T09:00:00Z --result success
python -m ota_canary batch report --batch-id B1 --device-id d2 \
    --version 1.0.0 --heartbeat-at 2026-10-01T09:00:00Z            # 仅心跳=升级中

# 公开入口 device heartbeat 同样会喂给包含该设备的当前批（旁路，不改变其既有语义）
python -m ota_canary device heartbeat --device-id d2 --version 2.0.0 \
    --heartbeat-at 2026-10-01T09:05:00Z

# 按给定时刻收批：超过心跳超时仍未取得终态的设备记 failure(reason=timeout)
python -m ota_canary batch check --batch-id B1 --at 2026-10-01T10:00:00Z

# 人工止损：冻结后续批次、拒绝新的升级报告，并向已成功设备下发回滚任务
python -m ota_canary batch abort --batch-id B1 \
    --reason 人工止损 --at 2026-10-01T10:00:00Z

# 自动停止或人工止损后回传每台设备的回滚结果（回滚到 stable-version）
python -m ota_canary batch rollback-report --batch-id B1 --device-id d1 \
    --result success --version 1.0.0 --heartbeat-at 2026-10-01T10:10:00Z

# 只读状态查询
python -m ota_canary batch status --batch-id B1 [--at 2026-10-01T10:00:00Z]
```

## 创建校验（异常唯一）

按以下顺序校验，返回对应的唯一异常码（stderr JSON、非零退出、不落盘）：

1. 批次编号已存在 → `BatchConflict`
2. 目标固件或稳定版本不存在，或两者相同 → `FirmwareNotFound`
   （固件存在指已 `firmware register`，或已有设备当前运行该版本）
3. 设备集合为空（未提供 `--target-device-id`）→ `EmptyDeviceSet`
4. 每批数量不是正整数、失败率阈值不满足严格 `0 < x < 1`、
   心跳超时不大于 0 → `InvalidBatchPolicy`
5. 版本号格式非法、设备 ID 空/重复、设备未登记等参数问题沿用既有 `InvalidArgument`
   与 `DeviceNotFound`。

## 启动前资格预检（batch plan，只读）

- 对 `pending` 发布执行 `batch plan --batch-id B1 [--at 时间]` 做只读预检，
  输出 `batchId`、`targetVersion`、`stableVersion`、`batchSize`、`targetDeviceIds`、
  `eligibleDeviceIds`、`ineligibleDevices`、`candidateCount`、`batches`。
- `targetDeviceIds` 取创建时去重集合并按 device-id 升序。
- 合格条件：设备当前版本等于 `stableVersion`，且未被**其他**处于
  `in_progress` 或 `failed_stopped` 的发布占用；`completed`、`rolled_back`、
  `rollback_failed` 释放占用，`pending` 发布尚未建批也不占用。
- `ineligibleDevices` 按 device-id 升序给出 `{deviceId, reason}`，每台设备唯一原因：
  版本不符为 `VERSION_MISMATCH`，被占用为 `DEVICE_BUSY`；两者兼有时
  `VERSION_MISMATCH` 优先。
- `batches` 只含合格设备并按 `batchSize` 切分；没有合格设备时为 `[]`。
- `plan` 只读、不创建批次或设备阶段、不改设备版本/心跳/占用，重复调用结果一致；
  `--at` 仅做时间合法性校验，不影响预检结论。
- 错误：发布不存在返回 `DeviceNotFound`；发布非 `pending` 返回 `InvalidState`；
  非法 `--at` 返回 `InvalidArgument`。

## 推进与停止

- `batch start` 使用与 `plan` 完全相同的资格、排序与分批口径：任一目标设备不合格时
  非零退出，stderr JSON 返回 `InvalidBatchEligibility`，消息按 device-id 升序列出
  `设备=VERSION_MISMATCH|DEVICE_BUSY`，且**不启动、不创建批次或设备阶段、不落盘**。
- 全部合格时才启动：第一批设备置为 `pending_upgrade`，其余批次保持 `queued`。
- `start` 对不存在发布、非 `pending`、非法 `--at` 沿用 `plan` 的同一错误。
- 启动后每次只把**下一批**设备置为 `pending_upgrade`，其余批次保持 `queued`。
- 设备成功后记录目标版本；终态 `failure`，或等待终态期间超过心跳超时仍无有效心跳
  （`batch check --at` 收批，严格晚于 `最近有效心跳 + 超时`；从未心跳时锚点为本批
  开始时刻），计为本批失败。
- 每批全部取得终态后，失败率 = 本批失败设备 / 本批设备数；**严格大于阈值**时：
  批次立即 `failed_stopped`，`frozen=true`，不再下发后续批次；向本批及此前批次
  **已升级成功**的设备下发回滚到稳定版本的任务（`rolling_back` / 回滚记录 `pending`）。
- 所有回滚任务回报完毕：任一失败 → `rollback_failed`，全部成功 → `rolled_back`；
  没有需要回滚的设备时直接 `rolled_back`。未超阈值则推进下一批，全部完成 → `completed`。
- `batch abort` 是人工止损入口，仅接受 `in_progress` 批次：立即冻结后续批次、拒绝
  新的升级报告，`stopReason` 固定为 `manual_abort`，并向**已成功**设备下发回滚到
  `stableVersion` 的任务（失败、超时、排队和未取得终态设备不生成任务）。尚有回滚
  任务时先保持 `failed_stopped`；全部成功或无任务时最终 `rolled_back`，任一失败则
  `rollback_failed`，收束规则与自动停止完全一致。批次不存在返回 `DeviceNotFound`；
  对 `pending`、`failed_stopped`、`completed`、`rolled_back`、`rollback_failed`
  执行 abort 返回 `InvalidState`。

## 人工止损字段与校验

- 回滚期间 `batch status` 显示 `frozen=true`、`stopReason=manual_abort`，批次、
  报告、设备阶段与回滚计数均保留；顶层另返回 `abortReason`（去除首尾空白后的
  `--reason`）与 `abortedAt`（`--at` 换算为 UTC 后的 `Z` 时间）。
- 旧状态缺少 `abortReason`、`abortedAt` 时分别显示 `null`，不补写历史。
- `--reason` 修剪后为空或超过 200 个 Unicode 字符、`--at` 为非法时间 →
  `InvalidArgument`。所有校验失败（含不存在、状态不符）均发生在任何状态写入前：
  错误走 stderr JSON、非零退出且不修改状态文件。
- 人工止损不改变阈值停止、设备版本、心跳、升级报告与回滚结果的既有行为。

## 设备阶段（status 可见九种）

`queued`、`pending_upgrade`、`upgrading`、`success`、`failed`、`waiting_heartbeat`、
`rolling_back`、`rollback_succeeded`、`rollback_failed`。

`batch status` 顶层还返回：`completedCount`、`failedCount`、`reportedCount`、
`currentBatchReportedCount`、`failureRate`（当前推进批次已取得终态设备中的失败占比，
无终态时为 `null`）、`currentBatch`、`frozen`（后续批次是否已冻结）、`phaseCounts`、
回滚计数（`rollbackTotal/Pending/Succeeded/Failed`）与逐设备 `devices[]`。
`status` 只读，不补写超时结果；超过心跳超时但尚未 `check` 的设备显示
`waiting_heartbeat`。

## 幂等与冻结

- 同一设备同一阶段重复上报相同终态：原样返回（`report.idempotent=true`），不重复计数，
  重复成功不增加失败率。
- 终态以首次为准：失败后再报成功等冲突/晚到终态返回 `InvalidArgument`，
  已失败设备不会被晚到心跳或终态改回成功。
- 自动停止后 `frozen=true`：后续批次及未取得终态的设备再上报返回 `InvalidState`，
  不再接收升级任务。
- 回滚结果同样以首次为准：`pending` 之外重复相同结果幂等返回，冲突结果 `InvalidArgument`。

## 审计时间线（batch timeline，只读）

`batch create`、`start`、`report`、`check`、`abort`、`rollback-report` 成功后向批次
追加严格递增的审计事件；命令失败、幂等重复、冲突/迟到终态均不追加，历史事件只增不改。
事件字段：`sequence`（从 1 起严格递增）、`type`、`occurredAt`、`batchIndex`、
`deviceId`、`result`、`phaseFrom`、`phaseTo`、`reason`，未涉及的字段为 `null`。

- `type` 取值：`created`、`started`、`batch_opened`、`upgrade_reported`、
  `timeout_recorded`、`batch_advanced`、`stopped`、`rollback_started`、
  `rollback_reported`、`aborted`、`finished`。
- `occurredAt` 取显式 `--at`、调用时刻或心跳时刻，统一为 ISO 8601 UTC `Z` 后缀。
- 自动停止事件 `stopped` 的 `reason=failure_threshold`；人工中止事件 `aborted`
  的 `reason` 为去除首尾空白后的 `--reason`。

```bash
python -m ota_canary batch timeline --batch-id B1 [--after-sequence N] [--limit M]
```

- 输出 `batchId`、`nextSequence`、`events`；`events` 按 `sequence` 升序，
  且均严格大于 `--after-sequence`（默认 0，须为非负整数）。
- `--limit` 默认 200，须为 1 到 1000 的整数，取最早的条数。
- `nextSequence` 为下一条事件的序号：无事件时为 1，否则为最大 `sequence` 加 1，
  与 `--after-sequence`、`--limit` 无关。
- 各批次状态均可查询，重复查询结果稳定；旧状态不补历史（`events` 为空、
  `nextSequence=1`），后续成功命令从 1 起继续追加。
- 批次不存在返回 `DeviceNotFound`；`--after-sequence`、`--limit` 非法返回
  `InvalidArgument`；错误走 stderr JSON、非零退出且不修改状态文件。
- 时间线独立于 `releases`，`--state` 用法与既有命令一致。
