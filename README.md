# OTA Canary

设备固件灰度发布与故障回滚系统：按批次推进升级、跟踪心跳与版本状态，在失败率超阈值时自动停止并回滚到上一个稳定版本。

## 范围

本仓库从零开始实现上述方向的可用工具，不依赖外部同类实现。

## 状态

初始基线：只有本说明，尚无实现。

## 用法

仅依赖 Python 3 标准库，通过 `python -m ota_canary` 调用。默认读写当前目录下的
`ota-canary-state.json`，可用 `--state <路径>` 指定其他状态文件（放在子命令前后均可）。

```bash
# 登记设备 / 更新心跳
python -m ota_canary device add --device-id d1 --version 1.0.0 --heartbeat-at 2026-10-01T08:00:00Z
python -m ota_canary device heartbeat --device-id d1 --version 1.0.0 --heartbeat-at 2026-10-01T09:00:00Z

# 建立并启动发布
python -m ota_canary release create --release-id R1 --version 2.0.0 \
    --previous-version 1.0.0 --batch-size 2 --max-failure-percent 50 \
    [--max-release-failure-percent 30] \
    [--heartbeat-timeout-seconds 900] [--stabilization-seconds 0] \
    [--target-device-id d1 --target-device-id d2]
python -m ota_canary release start --release-id R1

# 只读预览 pending 发布的分批计划（不创建批次、reports，不改变任何状态）
python -m ota_canary release plan --release-id R1

# 放量前调整 pending 发布的批次大小与定向目标（不建批次、保持 pending）
python -m ota_canary release update --release-id R1 [--batch-size 3] \
    [--target-device-id d1 --target-device-id d2 | --clear-target-device-ids]

# 当前批设备上报结果
python -m ota_canary device report --release-id R1 --device-id d1 \
    --result success --heartbeat-at 2026-10-01T10:00:00Z

# 按给定时刻收批心跳超时设备（仅处理 in_progress 发布）
python -m ota_canary release check --release-id R1 --at 2026-10-01T10:00:00Z

# 暂停 / 恢复发布
python -m ota_canary release pause --release-id R1
python -m ota_canary release resume --release-id R1

# 人工终止异常发布并回滚到稳定版本
python -m ota_canary release abort --release-id R1 \
    --reason "监控告警，人工止损" --at 2026-10-01T10:00:00Z

# 查看发布状态
python -m ota_canary status --release-id R1

# 只读查询发布审计时间线（分页）
python -m ota_canary release timeline --release-id R1 \
    [--after-sequence 0] [--limit 200]

# 只读汇总单台设备在 release 与 batch rollout 中的审计事件（分页）
python -m ota_canary device timeline --device-id d1 \
    [--offset 0] [--limit 200]

# 只读查看设备心跳与版本状态（全部设备 / 仅某发布批次）
python -m ota_canary fleet status \
    [--release-id R1] [--at 2026-10-01T10:00:00Z] [--heartbeat-timeout-seconds 900]

# 只读解释设备占用（全部设备 / 指定设备）
python -m ota_canary fleet occupancy \
    [--device-id d1 --device-id d2] \
    [--at 2026-10-01T10:00:00Z] [--heartbeat-timeout-seconds 900]

# 只读跨子系统风险总览（release 与 batch rollout 的活动发布）
python -m ota_canary fleet rollout-status [--at 2026-10-01T10:00:00Z]

# 集中执行活动发布/批次的定时收批（写命令，一次持久化）
python -m ota_canary fleet check --at 2026-10-01T10:00:00Z
```

行为约定：

- 版本号格式为 `MAJOR.MINOR.PATCH`，时间为 ISO 8601（支持 `Z` 后缀，无偏移按 UTC）。
- `release create` 可选 `--heartbeat-timeout-seconds`，为大于等于 1 的整数，缺省 `900`；
  发布视图含 `heartbeatTimeoutSeconds`，缺少该字段的旧发布按 `900` 计。
- `release create` 可选 `--stabilization-seconds`，为大于等于 0 的整数，缺省 `0`；
  非整数或负数返回 `InvalidArgument`。发布视图含 `stabilizationSeconds` 与
  `stabilizationDeadline`；缺少这两个字段的旧发布分别按 `0` 与 `null` 计。
- `release create` 可选 `--max-release-failure-percent`，仅接受 `0` 到 `100` 的整数，
  非整数、负数或大于 `100` 返回 `InvalidArgument`；缺省（未提供）只判逐批失败率，
  提供后启用发布级累计失败预算。发布视图含 `maxReleaseFailurePercent`，
  未配置或旧状态缺该字段时为 `null`。
- 发布视图含 `reportedCount`、`failedCount` 与 `stopReason`：`reportedCount` 为已有
  结果数（含显式上报与超时补写结果），`failedCount` 为其中 `result=failure` 的数量；
  `stopReason` 在逐批回滚时为 `batch_failure_threshold`、累计回滚时为
  `release_failure_threshold`，未回滚或旧状态原因不明时为 `null`。
- 启动发布时，仅选择 `currentVersion == previousVersion` 且未被跨子系统占用的设备，
  按 `device-id` 升序分批；空匹配的发布直接 `completed`。跨子系统占用按统一口径计算：
  状态为 `in_progress`/`paused` 的 release，以及状态为 `in_progress`/`paused`/
  `failed_stopped` 的 batch rollout，都持续占用其已经纳入批次的目标设备；
  `pending`、`completed`、`rolled_back`、`rollback_failed` 不占用。
- `release plan --release-id <id>` 是 pending 发布的只读分批预览，沿用 `--state`、
  `release create` 的目标集合与 `release start` 的候选/分批语义（同一套计算），但
  不创建批次、reports，不改设备版本、心跳、占用关系或状态文件，重复调用结果稳定。
  成功输出字段固定为 `releaseId`、`targetDeviceIds`、`eligibleDeviceIds`、`batchSize`、
  `candidateCount`、`batches`：`targetDeviceIds` 显式定向时按 device-id 升序返回目标
  集合，否则为 `null`；`eligibleDeviceIds` 升序仅含当前版本等于 `previousVersion` 且
  未被跨子系统占用（`in_progress`/`paused` 的 release 或 `in_progress`/`paused`/
  `failed_stopped` 的 batch rollout 已纳入批次的设备）的设备；`candidateCount` 为其数量；
  `batches` 按 `batchSize` 切分，空候选为 `[]`。显式目标任一不存在返回 `DeviceNotFound`；其余
  目标版本不等于 `previousVersion` 或被跨子系统占用返回 `InvalidArgument`，
  且成功校验前不输出计划；存在性先于版本与占用校验。显式目标为空列表时
  `eligibleDeviceIds` 与 `batches` 均为空。发布不存在返回 `DeviceNotFound`；对非
  `pending` 发布执行返回 `InvalidState`。错误走 stderr JSON 且退出码非零；`status`
  保持现有输出，旧状态缺少 `targetDeviceIds` 时该字段为 `null`（视为未指定）。
- `release update --release-id <id>` 在放量前调整 pending 发布的批次大小与定向目标，
  让后续 `release plan`/`release start` 按最新设备清单重排批次：可选 `--batch-size`
  （正整数，省略保留原值）、可重复的 `--target-device-id`（传入任一即以本次集合
  **整体替换**原目标，按 device-id 升序）与 `--clear-target-device-ids`（恢复为
  不限定，`targetDeviceIds` 写为 `null` 并全量选择）。`--target-device-id` 可放在
  子命令前后并混用，多次出现按出现顺序合并。显式目标与清空选项不得混用；没有任何
  调整项（三者均未提供）返回 `InvalidArgument`。`--batch-size` 非正整数、任一
  `--target-device-id` 修剪后为空、同一 device-id 重复（即使分散在子命令前后）均
  返回 `InvalidArgument`；显式目标任一设备未登记返回 `DeviceNotFound`。发布不存在
  返回 `DeviceNotFound`；对非 `pending` 发布执行返回 `InvalidState`。本命令只判
  设备存在性，不判版本与跨子系统占用——版本不等于 `previousVersion`、设备被占用等
  仍由 `release plan`/`release start` 按既有口径处理。全部校验通过后才一次原子
  写入：保持 `pending`，不建批次、不写 reports，不改设备版本、心跳、占用；失败
  命令不改变批次、报告、设备版本、心跳、占用或状态文件。成功返回与
  `status --release-id` 完全同结构的视图：pending 发布的 `batches` 为 `[]`、
  `devices` 为 `[]`、`reports` 为空（`reportedCount`/`failedCount` 为 0），
  `targetDeviceIds` 显式目标按 device-id 升序、清空后为 `null`，`batchSize` 为
  有效值（省略即原值）。成功后向 release timeline 追加一条 `policy_updated`
  事件：`occurredAt` 取调用时刻（UTC `Z`），`batchIndex`、`deviceId`、`result`、
  `phaseFrom`、`phaseTo`、`reason` 均为 `null`；失败不追加事件，沿用只增、严格
  递增与分页规则。旧状态缺少 `targetDeviceIds` 仍按不限定处理；update 成功只写
  本次给定的列表或 `null`，不回填历史字段；`release plan` 保持只读稳定，`status`
  兼容展示不变。既有 release、device、fleet、batch rollout 行为均不变，`--state`
  在子命令前后均可用。
- `release create` 可选、可重复的 `--target-device-id` 用于定向灰度：显式指定后，本次
  发布只升级给定设备，`release start` 不再自动吸收全部符合版本条件的设备。该参数可放在
  子命令前后并混用，多次出现按出现顺序合并。取值修剪后为空返回 `InvalidArgument`；
  同一 device-id 重复给出（即使分散在子命令前后）返回 `InvalidArgument`；两种情况都不创建
  发布。发布视图含 `targetDeviceIds`：显式指定时按 device-id 升序返回目标集合；未指定或
  读取缺少该字段的旧状态时为 `null`，旧发布继续采用全量选择行为，且不重写历史记录。
- 定向发布启动时，`release start` 只在目标集合内选择 `currentVersion == previousVersion`
  且未被跨子系统占用的设备，仍按 device-id 升序分批；目标集合本身已
  排序，故批次顺序确定。目标设备不存在返回 `DeviceNotFound`；目标设备存在但版本不等于
  `previousVersion`，或正被跨子系统占用（in_progress/paused 的 release，或
  in_progress/paused/failed_stopped 的 batch rollout 已纳入批次的设备），返回
  `InvalidArgument`。存在性先于版本与占用校验。这些启动失败都不修改发布状态、批次、报告、
  设备版本、心跳或占用关系（发布仍为 `pending`、`batches` 为空）。非空目标集合要求全部
  合法，因此校验通过即有成员；当目标集合为空（如状态中记录为空列表，目标合法性空真）时，
  发布直接 `completed`，与全量选择下的空匹配结果一致。
- 定向发布启动后，`device report`、`release check` 的超时判定、逐批失败率、可选的发布级
  累计失败率、`stabilization-seconds` 观察推进、`pause`/`resume`/`abort`、`status` 的
  既有字段与结果语义保持不变；迟到报告、设备占用解除、非零退出码、stderr JSON 错误以及
  失败命令不落盘也保持不变。未纳入目标集合的设备不受发布影响（版本不改变、不被占用）。
- `release check --at <时刻>` 仅处理 `in_progress` 发布：对当前批尚未显式上报结果的设备，
  将其 `heartbeatAt` 与 `at` 换算为 UTC 瞬间，当 `at` 严格晚于 `heartbeatAt + 超时秒数` 时，
  写入 `result=failure`、`reason=timeout`，`heartbeatAt` 保留设备原值；已有显式结果不覆盖。
  返回视图在顶层新增 `expiredDevices`，按 device-id 升序列出本次超时设备（无成员时为 `[]`）。
- 整批仍缺结果时保持 `in_progress`，`pendingDevices` 只含未上报且未过期的当前批设备；
  整批集齐后先按 `failed * 100 > 设备数 * max-failure-percent` 判断当前批，
  超过则 `rolled_back`，`stopReason` 为 `batch_failure_threshold`；未超过再判全程
  （仅当配置了 `--max-release-failure-percent`）：当
  `failedCount * 100 > reportedCount * max-release-failure-percent` 时立即
  `rolled_back`，`stopReason` 为 `release_failure_threshold`；两者均未触发才推进
  下一批或 `completed`。累计回滚沿用逐批回滚口径：已纳入批次的设备恢复
  `previousVersion`，拒绝迟到报告，并随发布终止解除设备占用。
- 批次稳定观察：`stabilizationSeconds > 0` 时，整批集齐且失败率未超阈值不立即推进，
  保持 `in_progress` 与 `currentBatch`，`pendingDevices` 为 `[]`，并记录
  `stabilizationDeadline`（当前批报告中最晚 `heartbeatAt` 的 UTC 瞬间加观察秒数，
  ISO 8601；非观察期间为 `null`）。`release check --at` 仅在 `at` 严格晚于截止时刻时
  推进下一批或 `completed`，等于或更早不推进；同一次 check 补齐超时结果并越过截止
  时刻时直接推进。失败率超阈值仍立即 `rolled_back` 并回退版本，不进入观察。
  `stabilizationSeconds` 为 `0` 时保持原推进时机。观察期间设备仍被占用，
  `pause`/`resume` 照常可用，恢复后从原批次与截止时刻继续。
- `status` 保持只读，不补写超时结果，但 `devices[].report` 可读出 `reason=timeout`。
- `fleet status` 为只读视图，不修改状态文件：可选 `--release-id`、`--at`、
  `--heartbeat-timeout-seconds`。`--at` 按既有 ISO 8601 规则换算 UTC（支持 `Z`，
  无偏移按 UTC），缺省取调用时 UTC 并以 `Z` 输出；`--heartbeat-timeout-seconds`
  默认 `900`，只接受大于等于 `1` 的整数，非法值或非法 `--at` 返回 `InvalidArgument`。
  成功输出顶层固定为 `at`、`heartbeatTimeoutSeconds`、`summary`、`devices`：
  `devices` 按 device-id 升序，每项为 `deviceId`、`version`、`heartbeatAt`、
  `ageSeconds`、`heartbeatState`、`releaseId`、`report`。`ageSeconds` 为观察时刻减
  `heartbeatAt` 的向下取整秒，心跳等于或晚于观察时刻时为 `0`，心跳缺失为 `null`；
  `heartbeatAt` 严格早于观察时刻减 timeout 时 `heartbeatState` 为 `stale`，否则
  `fresh`，心跳缺失为 `unknown`。指定 `--release-id` 时只列该发布批次设备，
  `releaseId` 取该发布标识，`report` 取该设备已有报告或 `null`；发布不存在返回
  `DeviceNotFound`。未指定 `--release-id` 时列出全部设备：设备被 `in_progress` 或
  `paused` 发布占用则 `releaseId` 取占用发布标识，否则为 `null`，`report` 恒为
  `null`；同一设备被多个活动发布占用返回 `InvalidState`。`summary` 含
  `totalDevices`、`freshCount`、`staleCount`、`unknownCount`、`occupiedCount`。
  空状态返回零计数与空 `devices`；状态损坏返回 `InvalidState`。错误均走 stderr
  JSON、非零退出且不改状态；`status` 与 `release plan` 的既有输出保持不变。
- `fleet occupancy` 为只读视图，不建批次、不改状态文件，重复调用结果稳定：可选
  `--device-id`（可重复）、`--at`、`--heartbeat-timeout-seconds`。`--at` 按既有
  ISO 8601 规则换算 UTC，缺省取调用时 UTC 并以 `Z` 输出；
  `--heartbeat-timeout-seconds` 默认 `900`，只接受大于等于 `1` 的整数；非法
  `--at`、非法超时、`--device-id` 空值或重复均返回 `InvalidArgument`。省略
  `--device-id` 查询全部设备，指定时只查给定设备，输出均按 device-id 升序；
  指定设备不存在返回 `DeviceNotFound`。成功输出顶层固定为 `at`、
  `heartbeatTimeoutSeconds`、`summary`、`devices`：每项为 `deviceId`、`version`、
  `heartbeatAt`、`ageSeconds`、`heartbeatState`、`occupied`、`conflict`、
  `occupancies`。`ageSeconds` 为观察时刻减 `heartbeatAt` 的向下取整秒，非正为
  `0`，心跳缺失为 `null`；`heartbeatAt` 严格早于观察时刻减 timeout 时
  `heartbeatState` 为 `stale`，否则 `fresh`，心跳缺失为 `unknown`。`occupancies`
  只列活动占用：`in_progress`/`paused` 的 release 与 `in_progress`/`paused`/
  `failed_stopped` 的 batch rollout 已纳入批次的设备，每项为 `kind`
  （`release`/`batch`）、`id`、`status`、`batchIndex`（从 1 起的批次号），按
  kind/id 升序；`occupied` 为 `occupancies` 非空，`conflict` 为占用多于一项，
  冲突只标记不报错。`summary` 含 `totalDevices`、`selectedDevices`、
  `occupiedCount`、`conflictCount`、`freshCount`、`staleCount`、`unknownCount`。
  状态损坏返回 `InvalidState`；错误均走 stderr JSON、非零退出且不改状态文件。
- `fleet rollout-status` 为只读跨子系统风险总览，读 `releases` 与
  `batchRollouts`，不建批次、不补超时、不写状态文件，重复调用（同一 `--at`）
  结果稳定：可选 `--at`，按既有 ISO 8601 规则换算 UTC（支持 `Z`，无偏移按
  UTC），非法值返回 `InvalidArgument`；缺省取调用时 UTC 并以 `Z` 输出。沿用
  `--state`（放在子命令前后均可），既有命令的请求/响应均不变。成功输出顶层
  固定为 `at`、`summary`、`campaigns`。仅列活动发布：状态为 `in_progress`/
  `paused` 的 release，与状态为 `in_progress`/`paused`/`failed_stopped` 的
  batch rollout；`pending` 及各终态不列，无活动发布时 `campaigns` 为 `[]`。
  `campaigns` 按 `kind`（`batch` 在 `release` 前）、`id` 升序；每项为 `kind`、
  `id`、`status`、`versions`、`targetDeviceIds`、`progress`、`risk`、
  `deadline`、`stopReason`：
  - `versions` 为 `{target, rollback}`，口径同 `status`：release 取
    `version`/`previousVersion`，batch 取 `targetVersion`/`stableVersion`。
  - `targetDeviceIds` 口径同 `status`：release 未定向（含旧状态缺字段）为
    `null`；batch 为创建时目标集合按 device-id 升序（旧状态缺字段按 `[]`）。
  - `progress` 为 `{current, batchCount, reported, failed, rate}`：release 的
    `current` 为从 0 起的 `currentBatch`（`pending` 等无批次时为 0，活动发布
    下即当前批序号），batch 的 `current` 为其 `currentBatch`（从 0 起）；
    `batchCount` 为批次数；`reported`、`failed` 同 `status` 口径（release 为
    reports 总数与其中失败数，batch 为已取得升级终态数与其中失败数，均不含
    回滚尝试）；`rate = failed / reported`，保留 6 位小数，`reported` 为 0 时
    为 `null`。批次号、期限、`stopReason` 在状态中缺失或为 `null` 时输出
    `null`；设备列表缺失或为空按空数组处理。
  - `risk` 为 `{overdue, pending, failed}`，均为按 device-id 升序的设备数组；
    顶层 `summary.risk` 为三者设备数之和。`overdue` 按各子系统 `status` 的
    超时与暂停口径，只列观察时刻已逾期且尚未补写超时终态的设备：release 与
    `release check` 同判据（当前批无报告设备，`at` 严格晚于
    `heartbeatAt + heartbeat-timeout-seconds`；release 暂停不扣除墙钟时长），
    batch 与 `batch status` 的 `waiting_heartbeat` 同判据（扣除已闭合暂停
    时长；`paused`/`failed_stopped` 及终态不列逾期）。`pending`、`failed`
    只用于 batch：列最新一条回滚任务（设备当前 `rollback` 记录）为
    `pending`/`failure` 的设备；release 恒为 `[]`。被 `rollback-retry` 归档进
    `rollbackAttempts` 的历史尝试不计入，不区分重试次数。
  - `deadline` 取观察时刻尚未过去（截止时刻大于等于 `--at`）的
    `stabilizationDeadline` 与 `rollbackDeadline` 中最早者，输出
    `{type, at}`，`type` 为 `stabilization` 或 `rollback`，`at` 为 UTC `Z`；
    两者均缺失、均已过去时为 `null`（release 无回滚截止时刻）。
  - `stopReason` 取发布停止原因，缺失或为 `null` 时输出 `null`。
  - `summary` 含 `campaignCount`、`statusCounts`（按状态名聚合的活动发布数，
    键按字典序）、`risk`（`overdue`/`pending`/`failed` 设备数）。
  状态损坏或语义不明（非字典状态、未知状态、批次/报告/回滚记录结构损坏、
  时间戳非法等）返回 `InvalidState`；错误均走 stderr JSON、非零退出且不改
  状态文件。
- `fleet check --at <时刻>` 集中执行活动发布/批次的定时收批（写命令，写结果
  一次持久化），沿用 `--state`，不改变 `release check`、`batch check`、
  `status`、`plan`、`timeline` 及既有写命令的单活动语义。`--at` 必须显式提供，
  是唯一观察时刻，非法时间返回 `InvalidArgument`。处理顺序：先按 releaseId
  升序处理 `in_progress` 的 release，再按 batchId 升序处理 `in_progress`、
  `failed_stopped` 的 batch rollout；`paused`、`pending`、`completed`、
  `rolled_back`、`rollback_failed` 对象只进入 `skipped`，不接受升级或回滚
  上报。入选对象沿用各自 `check` 的超时补写、稳定观察、失败阈值、自动停止、
  回滚任务与回滚超时规则，`at` 严格越过截止时刻才推进。成功输出顶层固定为
  `at`（UTC `Z`）、`campaigns`、`skipped`：`campaigns` 按上述顺序列出
  `kind`、`id`、`status`、`expiredDevices`、`rollbackExpiredDeviceIds`、
  `stopReason`（处理后口径；无事件时列表为空、原因为 `null`），`skipped`
  列出 `kind`、`id`、`status`。对象状态结构非法、设备表损坏或时间口径冲突
  时返回 `InvalidState`，退出码非零、stderr 输出 JSON 且不改状态文件；成功
  退出码为 `0`。旧状态缺少 `batchRollouts`、`firmwares` 或字段时按现有缺省
  读取，不回填。
- `release pause` 仅对 `in_progress` 发布生效：状态改为 `paused`，原样保留
  `batches`、`currentBatch`、`reports`、设备版本与心跳。`release resume` 仅对
  `paused` 发布生效：恢复为 `in_progress`，从同一批次继续，不重建批次或报告。
  暂停期间 `device report` 与 `release check` 对该发布返回 `InvalidState`，
  不写报告、不推进批次、不改变设备；`status` 仍可只读查看，`device add` 与
  `device heartbeat` 按既有规则工作。对 `pending`/`completed`/`rolled_back`
  发布执行 pause，或对非 `paused` 发布执行 resume，均返回 `InvalidState`；
  发布不存在返回 `DeviceNotFound`。
- 其他发布选择设备时尊重占用中的发布：`paused` 的 release、以及
  `in_progress`/`paused`/`failed_stopped` 的 batch rollout 已纳入批次的设备，都不会被新的
  `release plan`/`release start` 纳入候选（batch plan/batch start 使用同一跨子系统口径）。
  release 占用在进入 `completed` 或 `rolled_back` 后解除；batch rollout 占用在进入
  `completed`、`rolled_back` 或 `rollback_failed` 后解除；待其进入释放占用的终态后，
  其他子系统才可选择这些设备。
- `release abort` 是人工止损入口，仅接受 `in_progress` 或 `paused` 发布：状态改为
  `rolled_back`，`stopReason` 为 `manual_abort`，`abortReason` 为去除首尾空白后的
  `--reason`，`abortedAt` 为 `--at` 按 UTC 规范输出的 `Z` 时间；所有批次内设备
  （不论已报告、当前批或未开始）`version` 均恢复 `previousVersion`，`reports`、
  `heartbeatAt`、`batches` 保持原值，`stabilizationDeadline` 清空并解除设备占用。
  之后 `device report` 与 `release check` 对该发布返回 `InvalidState`，`status`
  可正常查询。`--reason` 修剪后为空或超过 200 个 Unicode 字符、`--at` 非法返回
  `InvalidArgument`；发布不存在返回 `DeviceNotFound`；对 `pending`/`completed`/
  `rolled_back` 发布执行返回 `InvalidState`。发布视图含 `abortReason` 与
  `abortedAt`，旧状态缺少这两个字段时显示 `null`，不补写历史；`manual_abort`
  不改变 `batch_failure_threshold` 与 `release_failure_threshold` 的既有口径。
- `release timeline --release-id <id> [--after-sequence N] [--limit M]` 是发布的
  只读审计时间线，字段结构、UTC `Z` 时刻口径与 `batch timeline` 完全一致（独立存储，
  互不影响）。输出固定为 `releaseId`、`nextSequence`、`events`：`events` 按
  `sequence` 升序，且仅含严格大于 `--after-sequence`（缺省 0，须为非负整数）的事件，
  取最早的 `--limit` 条（缺省 200，须为 1 到 1000 的整数）；`nextSequence` 为下一
  条事件序号（无事件为 1，否则为最大 `sequence` 加 1），与分页参数无关。每条事件
  字段为 `sequence`（从 1 起严格递增）、`type`、`occurredAt`、`batchIndex`、
  `deviceId`、`result`、`phaseFrom`、`phaseTo`、`reason`，未涉及字段为 `null`。
  事件类型与取值：
  - `create` 成功记 `created`；`start` 记 `started`，空匹配直接完成时再补一条
    `finished`（同一时刻）。
  - `release update` 成功记 `policy_updated`（`occurredAt` 取调用时刻；
    `batchIndex`、`deviceId`、`result`、`phaseFrom`、`phaseTo`、`reason` 均为
    `null`）；更新失败不追加。
  - `device report` 成功记 `reported`（`batchIndex` 为当前批、`deviceId` 为上报
    设备，`result` 为 `success`/`failure`，`reason` 为 `null`）。
  - `release check` 先对本次每台超时设备记 `timeout_recorded`
    （`result=failure`、`reason=timeout`、`deviceId` 为该设备），再做收批判定。
  - 收批（由 report 触发或 check 越过观察期）后：推进非末批记 `batch_advanced`
    （`batchIndex` 为新批次号）；末批完成记 `finished`；逐批失败率超阈值记
    `stopped` 后回滚再记 `rolled_back`，发布级累计失败率超阈值同理，
    `reason` 分别为 `batch_failure_threshold` / `release_failure_threshold`。
  - `pause`/`resume` 成功记 `paused`/`resumed`（`phaseFrom`/`phaseTo` 为
    `in_progress→paused`、`paused→in_progress`）；`abort` 先记 `aborted`
    （`result=failure`、`reason` 为修剪后的人工 `--reason`，从 `paused` 止损时
    `phaseFrom=paused`），回滚后再记 `rolled_back`（`result=success`、
    `reason=manual_abort`）。
  - `result`/`reason` 汇总：`reported` 为 `(success|failure, null)`，
    `timeout_recorded` 为 `(failure, timeout)`，`stopped` 为
    `(failure, batch_failure_threshold|release_failure_threshold)`，`aborted` 为
    `(failure, 人工原因)`，`rolled_back` 为 `(success, 阈值|manual_abort)`，
    其余事件为 `(null, null)`。
  - `occurredAt` 取显式 `--at`（check/abort）、上报的 `heartbeatAt`（report 触发的
    收批事件同取该心跳时刻）或调用时刻（create/update/start/pause/resume）。
  - 仅状态变化成功时追加：命令失败、只读命令、幂等重复、迟到/冲突上报均不追加，
    历史事件只增不改。观察期内未越过截止时刻且未补出超时的 `check` 不产生事件。
  - 发布不存在返回 `DeviceNotFound`；`--after-sequence` 不是非负整数、`--limit`
    不在 1 到 1000 返回 `InvalidArgument`；状态或时间线结构语义不明（`events`
    存在但非列表、事件非对象或 `sequence` 非整数）返回 `InvalidState`。错误均走
    stderr JSON、非零退出且不改状态。查询无副作用；旧状态缺时间线时输出
    `events=[]`、`nextSequence=1`，不回填，后续成功写命令从 1 起追加。
- `device timeline --device-id <id> [--offset N] [--limit M]` 是单台设备的只读
  跨子系统审计时间线汇总，不写审计记录、不改变写命令的输出、幂等与落盘行为，
  重复调用结果稳定。事件只取 `releases` 与 `batchRollouts` 中 `deviceId` 等于
  指定设备的原事件，排除总体事件（如 `created`/`started`/`batch_opened`）与任何
  虚构心跳；两个来源桶、单个来源或其 `events` 缺失时按空集读取，不回填。
  成功输出顶层固定为 `device`、`total`、`offset`、`limit`、`nextOffset`、`events`：
  - `device` 为 `{deviceId, version, heartbeatAt}`，取设备当前记录，字段缺失为 `null`。
  - 每条事件固定为 `sourceType`（`release` 或 `batch`）、`sourceId`（releaseId/
    batchId）、`sourceSequence`（来源事件的 `sequence`）、`type`、`occurredAt`、
    `batchIndex`、`result`、`phaseFrom`、`phaseTo`、`reason`；除前三个来源字段外
    其余沿用来源事件原值，缺失为 `null`。
  - 排序按 `occurredAt` 升序；同一时刻 `release` 先于 `batch`；再按 `sourceId`
    码点升序；最后按 `sourceSequence` 升序。
  - `--offset` 为非负整数（缺省 0）；`--limit` 缺省 200，为 1 到 1000 的整数。
    `total` 为过滤后事件总数；`events` 为 `offset` 之后的至多 `limit` 条；
    `nextOffset` 为 `offset` 加本次返回条数，取完（或无更多事件）时为 `null`。
  - 设备不存在返回 `DeviceNotFound`；`--offset`/`--limit` 非法返回 `InvalidArgument`；
    状态文件不可读、整体非对象，或任一时间线结构损坏（来源桶非对象、来源非对象、
    `events` 存在但非列表、事件非对象、`sequence` 非整数或 `occurredAt` 非法）返回
    `InvalidState`，总体事件与其他设备事件中的结构损坏同样判为损坏。错误均走 stderr
    JSON、非零退出且不改状态。无匹配事件时 `events` 为 `[]`、`total` 为 0、
    `nextOffset` 为 `null`。
- 已超时设备或非当前批设备的迟到 `device report` 返回 `InvalidArgument`。
- 成功的写命令输出 JSON 并持久化状态；`status` 与 `release plan` 为只读，不产生业务变化。
- 失败命令以非零退出码结束，向 stderr 输出 JSON 错误，且不修改状态文件。
  错误码：`DeviceNotFound`（设备或发布不存在）、`DeviceExists`（重复登记或重复发布）、
  `InvalidArgument`（参数非法、重复、非当前批或已超时报告、非法 `at`/`heartbeat-timeout-seconds`/`stabilization-seconds`/`max-release-failure-percent`、
  非法 `abort` 原因）、
  `InvalidState`（对 pending/completed/rolled_back 发布执行 check，或推进非进行中发布；
  对非 in_progress 发布 pause、对非 paused 发布 resume；暂停期间对 paused 发布执行
  `device report` 或 `release check`；对非 in_progress/paused 发布执行 `abort`；
  `fleet status` 发现状态损坏或同一设备被多个活动发布占用）。

## 约定

- 公开行为以 README 与源码为准。
- 后续需求在此基线上增量实现。
