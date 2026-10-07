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
    --stabilization-seconds 300 \
    --target-device-id d1 --target-device-id d2 --target-device-id d3

# 开始放量：仅把第一批设备置为 pending_upgrade
# 启动前可先做只读资格预检（不改变任何状态）
python -m ota_canary batch plan --batch-id B1 [--at 2026-10-01T08:00:00Z]
python -m ota_canary batch start --batch-id B1 [--at 2026-10-01T08:00:00Z]

# 放量前调整 pending 批次的灰度策略与设备集合（不建批次、保持 pending）
python -m ota_canary batch update --batch-id B1 --at 2026-10-01T07:00:00Z \
    --batch-size 3 --failure-threshold 0.25 \
    --target-device-id d2 --target-device-id d3 --target-device-id d4 \
    --canary-device-id d4        # 或 --clear-canary-device-ids 清空金丝雀

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

# 手动暂停 / 恢复：暂停后当前批已开始设备仍可完成，恢复后从尚未开始的设备继续
python -m ota_canary batch pause --batch-id B1 --at 2026-10-01T09:30:00Z
python -m ota_canary batch resume --batch-id B1 --at 2026-10-01T09:45:00Z

# 人工止损：冻结后续批次、拒绝新的升级报告，并向已成功设备下发回滚任务
# （in_progress 与 paused 批次均可中止）
python -m ota_canary batch abort --batch-id B1 \
    --reason 人工止损 --at 2026-10-01T10:00:00Z

# 自动停止或人工止损后回传每台设备的回滚结果（回滚到 stable-version）
python -m ota_canary batch rollback-report --batch-id B1 --device-id d1 \
    --result success --version 1.0.0 --heartbeat-at 2026-10-01T10:10:00Z

# 批次 rollback_failed 后，对最新回滚为 failure/timeout 的设备再试一次回滚
python -m ota_canary batch rollback-retry --batch-id B1 \
    --device-id d1 --device-id d2 \
    --reason 网络恢复后重试 --at 2026-10-01T11:00:00Z

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
   心跳超时不大于 0、`stabilization-seconds` 非整数或为负数、
   `rollback-timeout-seconds` 非整数或小于 1、金丝雀数量超过 `batch-size`
   → `InvalidBatchPolicy`
5. 版本号格式非法、设备 ID 空/重复、金丝雀为空/重复/不属于目标集合、
   设备未登记等参数问题沿用既有 `InvalidArgument` 与 `DeviceNotFound`；
   显式目标含隔离设备 → `InvalidArgument`。

`batch create --stabilization-seconds` 为可选参数，取大于等于 0 的整数，缺省 `0`，
与 `release create` 的同名参数互不影响；为 `0` 时保持既有推进时机（集齐终态即推进）。

`batch create --rollback-timeout-seconds` 为可选参数，取大于等于 1 的整数，缺省
`900`；非法值返回 `InvalidBatchPolicy` 且不写状态。该策略用于回滚收束时限（见下节）。

## 放量前调整（batch update，仅 pending）

`batch update --batch-id B1 --at <ISO 8601>` 在放量前调整灰度策略与设备集合，
仅对 `pending` 批次生效；`targetVersion` 与 `stableVersion` 不可修改。

- 可重复的 `--target-device-id`：传入任一即以去重后的完整集合**替换**原目标；
  可重复的 `--canary-device-id`：传入任一即替换原金丝雀；
  `--clear-canary-device-ids` 清空金丝雀。未传入时目标与金丝雀均保留原值。
- 策略参数 `--batch-size`、`--failure-threshold`、`--heartbeat-timeout-seconds`、
  `--stabilization-seconds`、`--rollback-timeout-seconds` 缺省保留原值，
  提供时取值范围与 `batch create` 完全相同。
- 目标替换后旧金丝雀不在新集合时，必须同时给出新金丝雀或
  `--clear-canary-device-ids`。
- 校验与异常（全部通过后才一次写入，失败不改状态文件）：批次不存在 →
  `DeviceNotFound`；非 pending → `InvalidState`；设备参数空/重复、金丝雀与
  清空选项同现、旧金丝雀脱离新目标、未提供任何修改项、新增显式目标含隔离
  设备 → `InvalidArgument`；未登记设备 → `DeviceNotFound`；空目标集合 →
  `EmptyDeviceSet`；金丝雀不属于
  目标或数量超过有效 `batch-size`、策略取值非法 → `InvalidBatchPolicy`。
- 成功后保持 `pending`，不建批次、不改设备或占用，按 `--at` 的 UTC `Z` 时刻
  追加 `policy_updated` 审计事件，并输出与 `batch status` 同口径的视图；
  后续 `batch plan` / `batch start` 即按新目标、资格、金丝雀优先与分批规则计算。
- 非 pending 的后续状态保留全部既有语义；旧状态兼容显示且不补写历史。

## 金丝雀设备优先放量（--canary-device-id）

- `batch create` 增加可重复的 `--canary-device-id`：把关键设备确定为首批放量对象。
  值须非空且不重复，并属于同一命令的 `--target-device-id` 集合，数量不得超过
  `batch-size`。空值、重复或非目标设备返回 `InvalidArgument`，数量超限返回
  `InvalidBatchPolicy`，未登记设备返回 `DeviceNotFound`；这些失败均不创建批次、
  不改变任何状态。
- 未提供时 `canaryDeviceIds` 为 `[]`，仍按 device-id 升序切批（既有行为不变）。
- 指定后：金丝雀按 device-id 升序进入第一批，再用非金丝雀按同序补足第一批，
  其余目标按同序组成后续批次。`batch plan` 与 `batch start` 使用完全相同的
  资格、分组与排序口径。
- `batch create`、`batch plan`、`batch start` 成功视图返回 `canaryDeviceIds`；
  `batch status` 顶层增加 `canaryDeviceIds`，`devices` 每项增加 `canary`
  （仅标记金丝雀身份，不改变 phase、terminal、rollback、failureRate、占用与
  错误语义）。旧状态缺少该字段时显示 `[]` 与 `false`，只读不补写。

## 回滚收束时限（rollback-timeout-seconds）

- 自动停止或 `batch abort` 派发回滚后，仍有 pending 回滚任务时，以**停止时刻**加
  `rollback-timeout-seconds` 生成 `rollbackDeadline`（ISO 8601 UTC `Z`）；
  没有需要回滚的设备时 `rollbackDeadline` 为 `null`。
- `batch status` 新增 `rollbackDeadline` 与 `rollbackTimedOutCount`（因超时收束的
  回滚任务数）；回滚记录新增 `reason`：成功为 `null`、设备回传的失败为
  `reported`、超时收束为 `timeout`。
- `batch check --at` 仅在批次为 `failed_stopped` 且 `at` **严格晚于**
  `rollbackDeadline` 时，把 pending 回滚任务收束为 `failure(reason=timeout)`、
  设备阶段改为 `rollback_failed`，设备版本与 `heartbeatAt` 不变；响应按
  device-id 升序返回 `rollbackExpiredDeviceIds`，未超时时为空数组。每台超时设备
  以 `at` 追加一条 `type=rollback_timed_out`、`result=failure`、`reason=timeout`
  的时间线事件。重复 `check` 与查询不重复变更或追加。
- 回滚任务全部收束后：任一失败（含超时）→ `rollback_failed`，否则 `rolled_back`。
- `rollback-report` 首次终态优先：超时收束后重复上报 `failure` 幂等返回且不计数，
  上报 `success` 返回 `InvalidArgument` 且不改状态；`at` 等于 `rollbackDeadline`
  仍可正常上报，仅 `check` 的 `at` 严格晚于它才判定超时。
- 旧状态缺 `rollbackTimeoutSeconds` 按 `900` 解释；已有 `failed_stopped` 批次缺
  `rollbackDeadline` 时显示 `null`，不据此猜测超时。

## 回滚重试（batch rollback-retry）

- `batch rollback-retry --batch-id B1 --device-id d1 [--device-id d2 ...]
  --reason 原因 --at <ISO 8601>` 供 `rollback_failed` 批次中最新回滚为
  `failure`（设备回传）或 `timeout`（超时收束）且未成功的设备再试一次回滚；
  既有命令的请求/响应不变。
- 校验：`--device-id` 至少一个、不得重复，按 device-id 升序处理；设备须属于该
  批次，且最新回滚记录为 `failure`（`reason=reported` 或 `timeout`）。
  `--reason` 去除首尾空白后须为 1 到 200 个 Unicode 字符；`--at` 须为合法时间。
  批次或设备不存在返回 `DeviceNotFound`；批次非 `rollback_failed` 返回
  `InvalidState`；设备不属批次、无失败回滚、已成功、重复 id、reason 或 at 非法
  返回 `InvalidArgument`。所有校验通过前不写任何状态：错误走 stderr JSON、
  非零退出且不修改状态文件。
- 成功后：每台设备阶段回到 `rolling_back`、回滚记录重置为 `pending`；批次回到
  `failed_stopped` 且 `frozen=true`；设备版本与心跳不变；`rollbackDeadline` 重算为
  `at + rollbackTimeoutSeconds`。每台设备以 `at` 追加一条
  `type=rollback_retry_started`（`phaseFrom=rollback_failed`、
  `phaseTo=rolling_back`、`reason` 为修剪后的 `--reason`）的时间线事件。
- `batch status` 新增 `retryDeviceIds`、`retryStartedAt`（最近一次重试的设备
  集合与时刻，缺省 `[]`、`null`）与 `rollbackAttempts`（全部被取代的失败尝试，
  依序保留 `state`、`result`、`reason`、`heartbeatAt`、`version`，只增不覆盖，
  缺省 `[]`）；`rollbackTimedOutCount` 累计各次尝试与当前记录中的 timeout。
- 重试后的结果仍走 `batch rollback-report`：既有幂等、冲突、首次终态、`check`
  与心跳规则不变；再次超时由 `batch check` 记 `failure(reason=timeout)` 并追加
  `rollback_timed_out` 事件。当前回滚记录全部 `success` 时批次收束为
  `rolled_back`，否则为 `rollback_failed` 并可再次 `rollback-retry`。

## 稳定观察期（stabilization-seconds > 0）

- 当前批设备全部取得终态后，先判失败率：失败率**严格大于** `failure-threshold`
  时立即 `failed_stopped`、冻结并下发回滚，与观察期配置无关；失败率未超阈值时
  **不**立即推进下一批：批次保持 `in_progress`、`currentBatch` 不变，
  并产生 `stabilizationDeadline`。
- `stabilizationDeadline` 取**本批有效心跳的最大时刻**加 `stabilization-seconds`；
  本批无任何有效心跳（含全部因超时收束的情形）时锚点取 `currentBatchStartedAt`；
  结果统一输出为 ISO 8601 UTC `Z` 时间。截止时刻在进入观察期时计算一次，之后不变。
- `batch check --at` 的顺序固定为：先补齐超过心跳超时仍无终态的设备
  `failure(reason=timeout)`，再判本批失败率——超阈值立即停止回滚；
  `stabilization-seconds` 为 `0` 时立即推进；大于 `0` 时仅当 `at`
  **严格晚于** `stabilizationDeadline` 才推进下一批，末批则 `completed`；
  `at` 等于或早于截止时刻时批次保持不变（既不推进也不重复记录）。
- 截止时刻过后但未执行 `batch check` 时**不**自动推进；一次 `check` 同时补齐
  超时结果并越过截止时刻即直接推进；越过截止时刻后重复 `check` 不重复推进。
- 推进时清空 `stabilizationDeadline`；阈值停止与 `batch abort` 在冻结时同样清空。
  观察期内设备仍属于本批，后续批次设备依旧被冻结，`report` 的幂等与冲突规则不变。

## 启动前资格预检（batch plan，只读）

- 对 `pending` 发布执行 `batch plan --batch-id B1 [--at 时间]` 做只读预检，
  输出 `batchId`、`targetVersion`、`stableVersion`、`batchSize`、`targetDeviceIds`、
  `canaryDeviceIds`、`eligibleDeviceIds`、`ineligibleDevices`、`candidateCount`、`batches`。
- `targetDeviceIds` 取创建时去重集合并按 device-id 升序；`canaryDeviceIds` 取创建时
  登记的金丝雀集合（升序），旧状态缺字段时显示 `[]`。
- 合格条件：设备当前版本等于 `stableVersion`，且未被跨子系统统一口径占用——
  状态为 `in_progress`/`paused` 的 release，或**其他**处于 `in_progress`、`paused`、
  `failed_stopped` 的 batch rollout，已纳入其批次的设备均占用（release plan/start
  使用同一口径）；`completed`、`rolled_back`、`rollback_failed` 释放占用，`pending`
  发布尚未建批也不占用。占用只统计已纳入批次（`batches`）的设备，未纳入批次的目标不占用。
- `ineligibleDevices` 按 device-id 升序给出 `{deviceId, reason}`，每台设备唯一原因：
  版本不符为 `VERSION_MISMATCH`，被占用为 `DEVICE_BUSY`，被隔离为 `quarantined`；
  隔离判定优先，版本不符与占用兼有时 `VERSION_MISMATCH` 优先。
- `batches` 只含合格设备并按 `batchSize` 切分；没有合格设备时为 `[]`。
  指定金丝雀时合格的金丝雀按 device-id 升序进入第一批，非金丝雀按同序补足。
- `plan` 只读、不创建批次或设备阶段、不改设备版本/心跳/占用，重复调用结果一致；
  `--at` 仅做时间合法性校验，不影响预检结论。
- 错误：发布不存在返回 `DeviceNotFound`；发布非 `pending` 返回 `InvalidState`；
  非法 `--at` 返回 `InvalidArgument`。

## 推进与停止

- `batch start` 使用与 `plan` 完全相同的资格、排序与分批口径：任一目标设备因版本
  或占用不合格时非零退出，stderr JSON 返回 `InvalidBatchEligibility`，消息按
  device-id 升序列出 `设备=VERSION_MISMATCH|DEVICE_BUSY`，且**不启动、不创建批次
  或设备阶段、不落盘**。隔离设备（`quarantined`）不阻断启动，仅从放量中摘除：
  `batch start` 仅推进合格设备，全部目标被隔离时启动即 `completed`（不再建批）。
- 有合格设备时才开批：第一批设备置为 `pending_upgrade`，其余批次保持 `queued`。
- `start` 对不存在发布、非 `pending`、非法 `--at` 沿用 `plan` 的同一错误。
- 启动后每次只把**下一批**设备置为 `pending_upgrade`，其余批次保持 `queued`。
- 设备成功后记录目标版本；终态 `failure`，或等待终态期间超过心跳超时仍无有效心跳
  （`batch check --at` 收批，严格晚于 `最近有效心跳 + 超时`；从未心跳时锚点为本批
  开始时刻），计为本批失败。
- 每批全部取得终态后，失败率 = 本批失败设备 / 本批设备数；**严格大于阈值**时：
  批次立即 `failed_stopped`，`frozen=true`，不再下发后续批次；向本批及此前批次
  **已升级成功**的设备下发回滚到稳定版本的任务（`rolling_back` / 回滚记录 `pending`）。
- 所有回滚任务回报完毕：任一失败 → `rollback_failed`，全部成功 → `rolled_back`；
  没有需要回滚的设备时直接 `rolled_back`。未超阈值时：观察期为 0 立即推进下一批，
  全部完成 → `completed`；观察期大于 0 时先进入稳定观察期（见下节），
  由 `batch check --at` 严格晚于截止时刻后推进或完成。
- `batch abort` 是人工止损入口，接受 `in_progress`（含稳定观察期内）与 `paused`
  批次：立即冻结后续批次、拒绝新的升级报告，**成功时清空 `stabilizationDeadline`**，
  `stopReason` 固定为 `manual_abort`，并向**已成功**设备下发回滚到
  `stableVersion` 的任务（失败、超时、排队和未取得终态设备不生成任务）。尚有回滚
  任务时先保持 `failed_stopped`；全部成功或无任务时最终 `rolled_back`，任一失败则
  `rollback_failed`，收束规则与自动停止完全一致。批次不存在返回 `DeviceNotFound`；
  对 `pending`、`failed_stopped`、`completed`、`rolled_back`、`rollback_failed`
  执行 abort 返回 `InvalidState`。从 `paused` 止损时时间线 `aborted` 事件的
  `phaseFrom=paused`。

## 手动暂停与恢复（batch pause / batch resume）

- `batch pause --batch-id B1 --at <ISO 8601>` 仅接受 `in_progress` 批次：
  成功后立即 `status=paused`，记录 `pausedAt`（`--at` 换算 UTC 后的 `Z` 时间），
  并把 `resumedAt` 置空。`batch resume --batch-id B1 --at <ISO 8601>` 仅接受
  `paused` 批次：成功后回到 `in_progress`，记录 `resumedAt`。
- **暂停冻结范围有限**：进入暂停后不再为尚未开始的设备（后续批次及当前批中
  尚未派发的设备）发起升级；当前批已经开始（`pending_upgrade`/`upgrading`）的
  设备允许完成——暂停期间 `batch report` 对当前批设备仍然有效，心跳、版本与
  终态按既有规则记录；`device heartbeat` 的既有结果不变，同样只更新公开设备
  与当前批心跳/版本。终态照常计入本批失败率。
- **自动停止优先于暂停**：暂停期间本批集齐终态且失败率**严格大于**
  `failure-threshold` 时，自动停止与回滚规则优先执行——批次立即进入既有
  `failed_stopped` 流程（时间线 `stopped` 事件的 `phaseFrom=paused`），冻结、
  回滚派发、`rollbackDeadline`、回滚上报与收束口径与非暂停时完全一致。此后
  再执行 resume 返回 `ConflictState`，再执行 pause 同样返回 `ConflictState`。
- **暂停不推进**：失败率未越阈时，暂停期间不开下一批——`stabilization-seconds`
  为 `0` 时本应立即发生的推进也顺延到恢复时；大于 `0` 且暂停前尚未进入观察期
  时，观察截止时刻不在暂停中起算，在恢复时统一计算（已扣除闭合暂停区间）；
  暂停前已在观察期内的，`resume` 时按本段暂停与窗口的重叠顺延截止时刻。
  `batch check` 对 `paused` 批次仍返回 `InvalidState`：不补超时、不推进；
  观察期推进仍须恢复后由显式 `batch check --at` 严格晚于新截止时刻完成。
- **恢复继续原策略**：`resume` 成功后从当前批尚未取得终态的设备继续等待/推进，
  已成功设备不重复处理，已失败或已回滚设备沿用现有处置规则；若本批在暂停期间
  已集齐终态且未越阈，恢复时刻按原策略收束——`stabilization-seconds=0` 立即
  开下一批（末批完成 → `completed`），大于 `0` 时进入/继续稳定观察期。
- **暂停时长不计入心跳超时**：恢复后 `batch check` 的超时判定按
  `at - 暂停重叠时长 > 最近有效心跳 + heartbeat-timeout-seconds` 计算，
  只读 `status` 的 `waiting_heartbeat` 判定同口径；暂停期间 `check` 被拒绝，
  不会补超时。
- `batch abort` 接受 `paused`，仍按 `manual_abort` 派发回滚并沿用
  `rollback_failed`/`rolled_back` 收束（从 paused 止损时时间线 `aborted`
  事件的 `phaseFrom=paused`）。
- 批次划分（`batches`）、`currentBatch`、终态报告、设备版本与心跳、回滚数据、
  阈值/超时/观察策略全部原样保留；新增状态转换只限制手动暂停与恢复，不绕过
  失败率阈值，也不扩大或缩小参与统计的设备范围。
- `pause`/`resume` 成功时输出带暂停字段的 `batch status` 视图（含
  `processedCount`、`pendingCount`、`lastStatusChangedAt`），并返回
  `effective=true`；状态冲突不生效时 stderr 返回 `ConflictState`（非零退出、
  不落盘），不返回视图、不追加事件。
- 时间线：成功时追加 `type=paused` / `type=resumed`（`phaseFrom`/`phaseTo`
  分别为 `in_progress→paused`、`paused→in_progress`）；失败不追加。
- 错误：批次不存在返回 `DeviceNotFound`；`--at` 非法返回 `InvalidArgument`；
  **状态冲突统一返回 `ConflictState`**——批次尚未启动（`pending`）、已经停止
  （`failed_stopped`）、正在回滚（`failed_stopped`）、已经回滚完成
  （`rolled_back`/`rollback_failed`）、已经完成（`completed`）或已经暂停
  （`paused`）时执行 `pause`；非 `paused` 状态执行 `resume`。所有校验通过后
  才写状态：错误走 stderr JSON、非零退出且不修改状态文件。
- 并发暂停、恢复或推进同一批次时，状态变更串行生效，先完成的合法操作为准，
  后续冲突返回 `ConflictState`。可重复暂停/恢复（非并发的串行交替）；每次
  成功追加一对事件。旧状态缺少暂停字段时按从未暂停读取（`paused=false`、
  `pausedAt=null`、`resumedAt=null`），时间线沿用 `nextSequence` 继续追加。

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
`processedCount`（已取得升级终态的目标设备数，与 `reportedCount` 同口径；
回滚处置不改变该计数）、`pendingCount`（待处理设备数 = 目标设备总数
-`processedCount`，含当前批未取得终态与尚未开始的后续批次设备，启动前为
全部目标）、`lastStatusChangedAt`（最近一次批次 `status` 变更的 UTC `Z`
时刻；同状态内的批次推进不更新，旧状态显示 `null`，只读不补写）、
`currentBatchReportedCount`、`failureRate`（当前推进批次已取得终态设备中的失败占比，
无终态时为 `null`）、`currentBatch`、`frozen`（后续批次是否已冻结）、
`paused`（当前是否处于暂停）、`pausedAt`/`resumedAt`（最近一次暂停/恢复时刻，
从未暂停或对应动作未发生时为 `null`；再次暂停会把 `resumedAt` 重新置空）、
`stabilizationSeconds`（策略值，旧状态显示 `0`）、`stabilizationDeadline`
（仅稳定观察期截止时刻非空；观察期外与旧状态均为 `null`，只读查询不补写）、
`rollbackTimeoutSeconds`（回滚收束时限策略值，旧状态按 `900` 显示）、
`rollbackDeadline`（停止或中止派发回滚且仍有 pending 任务时，为停止时刻加
`rollback-timeout-seconds` 的 UTC `Z` 时刻；无回滚任务、未停止及缺该字段的旧状态
均为 `null`，不据此猜测超时）、`rollbackTimedOutCount`（因超时收束的回滚任务数，含重试归档尝试中的 timeout）、
`retryDeviceIds`/`retryStartedAt`（最近一次回滚重试的设备集合与时刻，缺省
`[]`/`null`）、`rollbackAttempts`（被重试取代的失败回滚尝试，依序保留
`state`/`result`/`reason`/`heartbeatAt`/`version`，只增不覆盖，缺省 `[]`）、
`phaseCounts`、
回滚计数（`rollbackTotal/Pending/Succeeded/Failed`）与逐设备 `devices[]`。
每条回滚记录含 `state`（`pending`/`success`/`failure`）、`heartbeatAt`、`version`
与 `reason`（成功为 `null`、设备回传的失败为 `reported`、超时收束为 `timeout`；
旧记录缺 `reason` 时按状态推导显示，只读不补写）。
`status` 只读，不补写超时结果；超过心跳超时但尚未 `check` 的设备显示
`waiting_heartbeat`。

## 幂等与冻结

- 同一设备同一阶段重复上报相同终态：原样返回（`report.idempotent=true`），不重复计数，
  重复成功不增加失败率。
- 终态以首次为准：失败后再报成功等冲突/晚到终态返回 `InvalidArgument`，
  已失败设备不会被晚到心跳或终态改回成功。
- 自动停止后 `frozen=true`：后续批次及未取得终态的设备再上报返回 `InvalidState`，
  不再接收升级任务。
- 回滚结果同样以首次为准：`pending` 之外重复相同结果幂等返回，冲突结果
  `InvalidArgument`；超时收束（`reason=timeout`）后重复上报 `failure` 幂等且
  不计数，上报 `success` 返回 `InvalidArgument` 且不改状态。

## 审计时间线（batch timeline，只读）

`batch create`、`update`、`start`、`report`、`check`、`abort`、`rollback-report`、
`rollback-retry`、`pause`、`resume` 成功后向批次追加严格递增的审计事件；命令失败、幂等重复、冲突/迟到终态
均不追加，历史事件只增不改。
事件字段：`sequence`（从 1 起严格递增）、`type`、`occurredAt`、`batchIndex`、
`deviceId`、`result`、`phaseFrom`、`phaseTo`、`reason`，未涉及的字段为 `null`。

- `type` 取值：`created`、`policy_updated`、`started`、`batch_opened`、`upgrade_reported`、
  `timeout_recorded`、`batch_advanced`、`stopped`、`rollback_started`、
  `rollback_reported`、`rollback_timed_out`、`rollback_retry_started`、
  `aborted`、`paused`、`resumed`、`finished`。
- `occurredAt` 取显式 `--at`、调用时刻或心跳时刻，统一为 ISO 8601 UTC `Z` 后缀。
- `batch check` 越过观察期截止时刻推进下一批时追加 `batch_advanced`，末批完成时
  追加 `finished`，`occurredAt` 取该次 `--at`；观察期内未越过截止时刻、且未补出
  超时结果的 `check` 不产生任何事件。
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
