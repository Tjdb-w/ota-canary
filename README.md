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
    [--heartbeat-timeout-seconds 900] [--stabilization-seconds 0]
python -m ota_canary release start --release-id R1

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
- 启动发布时，仅选择 `currentVersion == previousVersion` 且未被进行中发布占用的设备，
  按 `device-id` 升序分批；空匹配的发布直接 `completed`。
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
- `release pause` 仅对 `in_progress` 发布生效：状态改为 `paused`，原样保留
  `batches`、`currentBatch`、`reports`、设备版本与心跳。`release resume` 仅对
  `paused` 发布生效：恢复为 `in_progress`，从同一批次继续，不重建批次或报告。
  暂停期间 `device report` 与 `release check` 对该发布返回 `InvalidState`，
  不写报告、不推进批次、不改变设备；`status` 仍可只读查看，`device add` 与
  `device heartbeat` 按既有规则工作。对 `pending`/`completed`/`rolled_back`
  发布执行 pause，或对非 `paused` 发布执行 resume，均返回 `InvalidState`；
  发布不存在返回 `DeviceNotFound`。
- 其他发布选择设备时尊重暂停中的发布：`paused` 发布已占用的设备不会被新的
  `release start` 纳入候选；设备占用在发布进入 `completed` 或 `rolled_back` 后解除。
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
- 已超时设备或非当前批设备的迟到 `device report` 返回 `InvalidArgument`。
- 成功的命令输出 JSON 并持久化状态；`status` 为只读，不产生业务变化。
- 失败命令以非零退出码结束，向 stderr 输出 JSON 错误，且不修改状态文件。
  错误码：`DeviceNotFound`（设备或发布不存在）、`DeviceExists`（重复登记或重复发布）、
  `InvalidArgument`（参数非法、重复、非当前批或已超时报告、非法 `at`/`heartbeat-timeout-seconds`/`stabilization-seconds`/`max-release-failure-percent`、
  非法 `abort` 原因）、
  `InvalidState`（对 pending/completed/rolled_back 发布执行 check，或推进非进行中发布；
  对非 in_progress 发布 pause、对非 paused 发布 resume；暂停期间对 paused 发布执行
  `device report` 或 `release check`；对非 in_progress/paused 发布执行 `abort`）。

## 约定

- 公开行为以 README 与源码为准。
- 后续需求在此基线上增量实现。
