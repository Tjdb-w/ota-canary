"""批次灰度推进与自动故障回滚子系统。

本模块在既有产品边界上增量实现一套可独立运行的"批次发布（batch rollout）"能力：

- 沿用同一状态文件中的设备表（``devices``）作为公开设备/固件入口，新增
  ``firmwares`` 与 ``batchRollouts`` 两个状态键，不触碰 ``releases`` 的任何语义；
- 新增 ``firmware register|list`` 与 ``batch create|plan|start|report|check|
  pause|resume|rollback-report|rollback-retry|abort|status`` 命令，不改变既有命令的请求/响应；
- ``batch plan`` 对 pending 发布做只读启动资格预检，``batch start`` 复用同一口径：
  仅当前版本等于 stableVersion 且未被跨子系统统一口径占用的设备合格——
  in_progress/paused 的 release，或其他 in_progress/paused/failed_stopped
  的 batch rollout 已纳入批次的设备均占用（release plan/start 使用同一口径）；
  版本/占用不合格即拒绝启动；隔离设备标记 reason=quarantined，不阻断启动，
  仅从放量中摘除（batch start 仅推进合格设备，全部目标被隔离时启动即完成）；
  合格设备放量时仅第一批置为 pending_upgrade；
- ``batch create --canary-device-id``（可重复）指定金丝雀设备：须非空、不重复、
  属于同一命令的 target-device-id 且数量不超过 batch-size；金丝雀按 device-id
  升序进入第一批，非金丝雀按同序补足，其余目标按同序组成后续批次；未指定时
  canaryDeviceIds 为 []，仍按 device-id 升序切批；
- 批次逐批放量，设备通过公开入口回传心跳、当前固件版本与升级终态；
- stabilization-seconds > 0 时本批集齐终态且失败率未超阈值后进入稳定观察期：
  保持 in_progress 并产生 stabilizationDeadline（本批有效心跳最大值加观察秒数，
  无有效心跳时锚点为本批开始时刻），仅 batch check --at 严格晚于截止时刻才推进；
- 本批失败率严格大于阈值时自动 FAILED_STOPPED、冻结后续批次，并向已成功升级的
  设备下发回滚到稳定版本的任务，逐台记录 PENDING/成功/失败，最终 ROLLED_BACK
  或 ROLLBACK_FAILED；
- 人工止损 ``batch abort`` 对 IN_PROGRESS 与 PAUSED 均可用：立即冻结后续批次、
  拒绝新的升级报告，并向已成功设备下发同样的回滚任务，收束口径与自动停止一致。
- 手动暂停/恢复 ``batch pause``/``batch resume``：仅 in_progress 可暂停为 paused，
  仅 paused 可恢复回 in_progress；状态冲突返回 ConflictState。暂停立即冻结对
  尚未开始设备的升级派发，但当前批已开始（pending_upgrade/upgrading）的设备仍可
  上报心跳与终态，终态按现有规则记录并照常计算本批失败率——暂停期间失败率越过
  阈值时自动停止/回滚规则优先执行（批次进入既有 failed_stopped 流程）；心跳与
  版本在暂停期间持续接收。未越阈时暂停不推进下一批（含 stabilization-seconds
  为 0 本应立即推进的情形），推进顺延到恢复后由 batch check 跨越；观察期截止
  时刻在恢复时顺延暂停时长。恢复只对 paused 生效，从尚未开始的设备继续推进，
  不重复处理已成功设备；已失败或已回滚设备沿用现有处置规则。
- 回滚收束时限：``batch create --rollback-timeout-seconds``（>=1 的整数，缺省 900）；
  停止或 abort 派发回滚后仍有 pending 任务时，以停止时刻加策略秒数生成
  ``rollbackDeadline``（无任务为 null）。``batch check --at`` 在 failed_stopped 且
  at 严格晚于截止时刻时，把 pending 回滚任务收束为 failure(reason=timeout)、设备
  阶段置为 rollback_failed（版本与心跳不变），逐台追加 rollback_timed_out 事件并
  按 device-id 升序返回 rollbackExpiredDeviceIds；重复 check 不重复变更。
- 回滚重试 ``batch rollback-retry``：对 rollback_failed 批次中最新回滚为
  failure（设备回传）或 timeout（超时收束）且未成功的设备再试一次；成功后设备
  回到 rolling_back、批次回到 failed_stopped 并保持冻结，版本与心跳不改，
  rollbackDeadline 按重试时刻加 rollbackTimeoutSeconds 重算；被取代的失败尝试
  依序归档进 rollbackAttempts（state/result/reason/heartbeatAt/version，只增不
  覆盖），rollbackTimedOutCount 累计各次尝试中的 timeout；重试结果仍走
  batch rollback-report，再次超时仍由 batch check 记 failure(reason=timeout)，
  当前记录全 success 收束为 rolled_back，否则 rollback_failed 并可再次重试。
- 放量前调整 ``batch update``：仅对 pending 批次生效，可替换目标设备集合
  （去重后整体替换）、替换或清空金丝雀、调整 batch-size/failure-threshold/
  heartbeat-timeout-seconds/stabilization-seconds/rollback-timeout-seconds；
  targetVersion 与 stableVersion 不可改，未提供的策略项保留原值。目标替换后
  旧金丝雀不在新集合时必须同时给出新金丝雀或 --clear-canary-device-ids。
  全部校验通过后才一次写入，保持 pending、不建批次、不改设备或占用，按
  --at 追加 policy_updated 审计事件；后续 batch plan/start 即按新集合与
  策略计算资格、金丝雀优先与分批。
- 人工放行门禁：``batch create --approval-required`` 开启（``batch update`` 可在
  pending 阶段用 --approval-required/--no-approval-required 切换，两开关同现
  InvalidArgument，缺省保留原策略）。开启后 batch start 仍开始第一批；当前批
  集齐终态且未越失败阈值、沿用 stabilization-seconds 与 batch check --at 的
  观察规则到达可推进边界且仍有下一批时，不直接推进：批次保持 in_progress 并
  进入 awaitingApproval 等待（追加 approval_requested 事件，batchIndex 为下一
  批次号），尚未开始设备保持冻结；``batch approve --batch-id --at`` 仅在该状态
  生效，记录放行（approvedBatchIndexes 追加下一批次号、lastApprovedAt）并打开
  下一批（追加 approval_granted），末批无下一批可等待、直接 completed。等待
  期间 check 不推进，pause 后不能批准，resume 回到等待，abort 按既有规则冻结
  并回滚，迟到或冲突上报不能绕过门禁。
- 审计时间线：create/update/start/report/check/abort/rollback-report/pause/resume/
  rollback-retry/approve 成功后
  向批次追加严格递增事件（失败、幂等重复、冲突/迟到不追加，历史只增不改），
  ``batch timeline`` 只读查询，支持 --after-sequence 与 --limit 分页。

仅使用 Python 3 标准库。
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction

from . import cli

# ---------------------------------------------------------------------------
# 新增异常类型（仅覆盖本需求明确给出的四个唯一异常；其余沿用既有异常）
# ---------------------------------------------------------------------------


class BatchConflict(cli.OtaError):
    code = "BatchConflict"


class ConflictState(cli.OtaError):
    """手动暂停/恢复与批次当前状态冲突（暂停只接受 in_progress，恢复只接受 paused）。"""

    code = "ConflictState"


class FirmwareNotFound(cli.OtaError):
    code = "FirmwareNotFound"


class EmptyDeviceSet(cli.OtaError):
    code = "EmptyDeviceSet"


class InvalidBatchPolicy(cli.OtaError):
    code = "InvalidBatchPolicy"


class InvalidBatchEligibility(cli.OtaError):
    code = "InvalidBatchEligibility"


# 启动前资格预检的不合格原因
REASON_VERSION_MISMATCH = "VERSION_MISMATCH"
REASON_DEVICE_BUSY = "DEVICE_BUSY"
# 设备处于隔离状态：batch plan 标记该原因，batch start 跳过（仅推进合格设备）
REASON_QUARANTINED = "quarantined"


# 批次发布状态
STATUS_PENDING = "pending"
STATUS_IN_PROGRESS = "in_progress"
STATUS_PAUSED = "paused"
STATUS_COMPLETED = "completed"
STATUS_FAILED_STOPPED = "failed_stopped"
STATUS_ROLLED_BACK = "rolled_back"
STATUS_ROLLBACK_FAILED = "rollback_failed"

# 设备阶段（外部可见的九种状态）
PHASE_QUEUED = "queued"                              # 排队
PHASE_PENDING_UPGRADE = "pending_upgrade"            # 待升级
PHASE_UPGRADING = "upgrading"                        # 升级中
PHASE_SUCCESS = "success"                            # 成功
PHASE_FAILED = "failed"                              # 失败
PHASE_WAITING_HEARTBEAT = "waiting_heartbeat"        # 等待心跳
PHASE_ROLLING_BACK = "rolling_back"                  # 回滚中
PHASE_ROLLBACK_SUCCEEDED = "rollback_succeeded"      # 回滚成功
PHASE_ROLLBACK_FAILED = "rollback_failed"            # 回滚失败

TERMINAL_PHASES = (PHASE_SUCCESS, PHASE_FAILED)

# 审计时间线事件类型
EVENT_CREATED = "created"
EVENT_STARTED = "started"
EVENT_BATCH_OPENED = "batch_opened"
EVENT_UPGRADE_REPORTED = "upgrade_reported"
EVENT_TIMEOUT_RECORDED = "timeout_recorded"
EVENT_BATCH_ADVANCED = "batch_advanced"
EVENT_STOPPED = "stopped"
EVENT_ROLLBACK_STARTED = "rollback_started"
EVENT_ROLLBACK_REPORTED = "rollback_reported"
EVENT_ABORTED = "aborted"
EVENT_PAUSED = "paused"
EVENT_RESUMED = "resumed"
EVENT_FINISHED = "finished"
EVENT_ROLLBACK_TIMED_OUT = "rollback_timed_out"
EVENT_ROLLBACK_RETRY_STARTED = "rollback_retry_started"
EVENT_POLICY_UPDATED = "policy_updated"
EVENT_APPROVAL_REQUESTED = "approval_requested"
EVENT_APPROVAL_GRANTED = "approval_granted"

# 回滚收束时限缺省秒数（旧状态缺 rollbackTimeoutSeconds 时按此解释）
DEFAULT_ROLLBACK_TIMEOUT_SECONDS = 900

# 自动停止事件的 reason（人工中止事件取修剪后的 --reason）
STOP_REASON_FAILURE_THRESHOLD = "failure_threshold"

# timeline 查询的默认与上限
TIMELINE_DEFAULT_LIMIT = 200
TIMELINE_MAX_LIMIT = 1000


# ---------------------------------------------------------------------------
# 审计时间线：成功命令追加严格递增事件，只增不改
# ---------------------------------------------------------------------------

def append_event(rollout, event_type, occurred_at, batch_index=None, device_id=None,
                 result=None, phase_from=None, phase_to=None, reason=None):
    """向批次追加一条审计事件；sequence 从 1 起严格递增，未涉及字段为 None。

    仅在命令成功路径调用；失败、幂等重复、冲突/晚到均不追加。occurred_at 为
    已校验的 UTC 瞬间（显式 --at、调用时刻或心跳时刻），统一格式化为 Z 后缀。
    """
    events = rollout.get("events")
    if not isinstance(events, list):
        events = []
        rollout["events"] = events
    events.append({
        "sequence": len(events) + 1,
        "type": event_type,
        "occurredAt": cli.format_instant(occurred_at),
        "batchIndex": batch_index,
        "deviceId": device_id,
        "result": result,
        "phaseFrom": phase_from,
        "phaseTo": phase_to,
        "reason": reason,
    })


# ---------------------------------------------------------------------------
# 状态辅助
# ---------------------------------------------------------------------------

def rollouts(state):
    bucket = state.get("batchRollouts")
    if not isinstance(bucket, dict):
        bucket = {}
        state["batchRollouts"] = bucket
    return bucket


def firmwares(state):
    bucket = state.get("firmwares")
    if not isinstance(bucket, dict):
        bucket = {}
        state["firmwares"] = bucket
    return bucket


def firmware_exists(state, version):
    """固件存在：已在固件登记表中，或已有任一设备当前运行该版本。"""
    if version in firmwares(state):
        return True
    for device in state.get("devices", {}).values():
        if isinstance(device, dict) and device.get("version") == version:
            return True
    return False


def get_rollout(state, batch_id):
    rollout = rollouts(state).get(batch_id)
    if rollout is None:
        # 沿用既有约定：标识不存在统一返回 DeviceNotFound（release 不存在亦然）。
        raise cli.DeviceNotFound("batch not found: %s" % batch_id)
    return rollout


def mark_status_changed(rollout, at_instant):
    """记录最近一次批次状态（status）变更时刻；同状态内的批次推进不更新。"""
    rollout["lastStatusChangedAt"] = cli.format_instant(at_instant)


def require_policy_batch_size(value):
    try:
        size = int(str(value).strip())
    except (TypeError, ValueError):
        raise InvalidBatchPolicy("batch-size must be a positive integer: %r" % (value,))
    if size < 1:
        raise InvalidBatchPolicy("batch-size must be a positive integer: %r" % (value,))
    return size


def require_failure_threshold(value):
    """失败率阈值：严格大于 0 且小于 1 的小数。"""
    if value is None:
        raise InvalidBatchPolicy("failure-threshold is required")
    text = str(value).strip()
    try:
        threshold = Decimal(text)
    except InvalidOperation:
        raise InvalidBatchPolicy(
            "failure-threshold must be a decimal strictly between 0 and 1: %r" % (value,)
        )
    if not threshold.is_finite() or not (Decimal(0) < threshold < Decimal(1)):
        raise InvalidBatchPolicy(
            "failure-threshold must be strictly greater than 0 and less than 1: %r" % (value,)
        )
    return text, threshold


def require_policy_timeout(value):
    try:
        seconds = int(str(value).strip())
    except (TypeError, ValueError):
        raise InvalidBatchPolicy(
            "heartbeat-timeout-seconds must be a positive integer: %r" % (value,)
        )
    if seconds < 1:
        raise InvalidBatchPolicy(
            "heartbeat-timeout-seconds must be greater than 0: %r" % (value,)
        )
    return seconds


def require_policy_stabilization(value):
    """稳定观察期秒数：大于等于 0 的整数，缺省 0；非整数或负数 InvalidBatchPolicy。"""
    if value is None:
        return 0
    try:
        seconds = int(str(value).strip())
    except (TypeError, ValueError):
        raise InvalidBatchPolicy(
            "stabilization-seconds must be a non-negative integer: %r" % (value,)
        )
    if seconds < 0:
        raise InvalidBatchPolicy(
            "stabilization-seconds must be greater than or equal to 0: %r" % (value,)
        )
    return seconds


def require_rollback_timeout(value):
    """回滚收束时限秒数：大于等于 1 的整数，缺省 900；否则 InvalidBatchPolicy。"""
    if value is None:
        return DEFAULT_ROLLBACK_TIMEOUT_SECONDS
    try:
        seconds = int(str(value).strip())
    except (TypeError, ValueError):
        raise InvalidBatchPolicy(
            "rollback-timeout-seconds must be a positive integer: %r" % (value,)
        )
    if seconds < 1:
        raise InvalidBatchPolicy(
            "rollback-timeout-seconds must be greater than or equal to 1: %r" % (value,)
        )
    return seconds


def require_batch_device_ids(values):
    """目标设备集合：未提供/空列表 -> EmptyDeviceSet；空值/重复 -> InvalidArgument。"""
    if not values:
        raise EmptyDeviceSet("target device set must be non-empty")
    seen = set()
    ids = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise cli.InvalidArgument("target-device-id must be a non-empty string: %r" % (value,))
        device_id = value.strip()
        if device_id in seen:
            raise cli.InvalidArgument("duplicate target-device-id: %s" % device_id)
        seen.add(device_id)
        ids.append(device_id)
    return sorted(ids)


def require_canary_device_ids(values, target_ids, batch_size):
    """金丝雀设备集合：未提供 -> []；空值/重复/非目标设备 -> InvalidArgument；
    数量超过 batch-size -> InvalidBatchPolicy。按 device-id 升序返回。

    金丝雀必须属于同一命令的 target-device-id 集合；登记存在性由目标设备的
    统一存在性校验覆盖（金丝雀是目标的子集）。
    """
    if not values:
        return []
    seen = set()
    ids = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise cli.InvalidArgument("canary-device-id must be a non-empty string: %r" % (value,))
        device_id = value.strip()
        if device_id in seen:
            raise cli.InvalidArgument("duplicate canary-device-id: %s" % device_id)
        seen.add(device_id)
        ids.append(device_id)
    target_set = set(target_ids)
    for device_id in ids:
        if device_id not in target_set:
            raise cli.InvalidArgument(
                "canary-device-id must be one of the target-device-id values: %s" % device_id
            )
    if len(ids) > batch_size:
        raise InvalidBatchPolicy(
            "canary device count %d exceeds batch-size %d" % (len(ids), batch_size)
        )
    return sorted(ids)


def require_retry_device_ids(values):
    """重试设备集合：至少一个；空值/重复 -> InvalidArgument；按 device-id 升序返回。"""
    if not values:
        raise cli.InvalidArgument("at least one --device-id is required")
    seen = set()
    ids = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise cli.InvalidArgument("device-id must be a non-empty string: %r" % (value,))
        device_id = value.strip()
        if device_id in seen:
            raise cli.InvalidArgument("duplicate device-id: %s" % device_id)
        seen.add(device_id)
        ids.append(device_id)
    return sorted(ids)


# ---------------------------------------------------------------------------
# 固件入口
# ---------------------------------------------------------------------------

def cmd_firmware_register(state, args):
    version = cli.require_version(args.version)
    bucket = firmwares(state)
    if version in bucket:
        raise cli.DeviceExists("firmware already registered: %s" % version)
    record = {"version": version}
    bucket[version] = record
    return dict(record)


def cmd_firmware_list(state, args):
    return {"firmwares": [{"version": version} for version in sorted(firmwares(state))]}


# ---------------------------------------------------------------------------
# 批次创建 / 启动 / 放量
# ---------------------------------------------------------------------------

def cmd_batch_create(state, args):
    batch_id = cli.require_id(args.batch_id, "batch-id")
    target_version = args.target_version
    stable_version = args.stable_version

    # 1) 批次编号唯一
    if batch_id in rollouts(state):
        raise BatchConflict("batch already exists: %s" % batch_id)

    # 2) 目标固件与稳定版本都存在且不同（版本号格式本身不合法按 InvalidArgument）
    cli.require_version(target_version)
    cli.require_version(stable_version)
    if not firmware_exists(state, target_version):
        raise FirmwareNotFound("target firmware not found: %s" % target_version)
    if not firmware_exists(state, stable_version):
        raise FirmwareNotFound("stable firmware not found: %s" % stable_version)
    if target_version == stable_version:
        raise FirmwareNotFound(
            "target firmware and stable firmware must differ: %s" % target_version
        )

    # 3) 设备集合非空
    device_ids = require_batch_device_ids(getattr(args, "target_device_id", None))

    # 4) 每批数量为正整数、失败率阈值严格 (0,1)、心跳超时大于 0、观察期为非负整数、
    #    回滚收束时限为大于等于 1 的整数
    batch_size = require_policy_batch_size(args.batch_size)
    _, threshold = require_failure_threshold(args.failure_threshold)
    heartbeat_timeout = require_policy_timeout(args.heartbeat_timeout_seconds)
    stabilization_seconds = require_policy_stabilization(args.stabilization_seconds)
    rollback_timeout = require_rollback_timeout(getattr(args, "rollback_timeout_seconds", None))

    # 5) 金丝雀设备：非空不重复、属于目标集合、数量不超过 batch-size；
    #    任一不满足即失败，不创建批次或改状态
    canary_ids = require_canary_device_ids(
        getattr(args, "canary_device_id", None), device_ids, batch_size
    )

    # 人工放行门禁：--approval-required 开启；缺省不开启（旧状态缺字段按未开启）。
    approval_required = bool(getattr(args, "approval_required", False))

    # 其余沿用既有入口约定：设备必须存在（金丝雀是目标子集，一并覆盖）；
    # 显式目标不得包含隔离设备（InvalidArgument）。
    for device_id in device_ids:
        cli.get_device(state, device_id)
        if cli.is_device_quarantined(state, device_id):
            raise cli.InvalidArgument("target device %s is quarantined" % device_id)

    rollout = {
        "batchId": batch_id,
        "targetVersion": target_version,
        "stableVersion": stable_version,
        "batchSize": batch_size,
        "failureThreshold": str(threshold),
        "heartbeatTimeoutSeconds": heartbeat_timeout,
        "stabilizationSeconds": stabilization_seconds,
        "stabilizationDeadline": None,
        "rollbackTimeoutSeconds": rollback_timeout,
        "rollbackDeadline": None,
        "targetDeviceIds": device_ids,
        "canaryDeviceIds": canary_ids,
        "approvalRequired": approval_required,
        "awaitingApproval": False,
        "approvedBatchIndexes": [],
        "lastApprovedAt": None,
        "status": STATUS_PENDING,
        "frozen": False,
        "stopReason": None,
        "abortReason": None,
        "abortedAt": None,
        "pausedAt": None,
        "resumedAt": None,
        "pauses": [],
        "lastStatusChangedAt": None,
        "batches": [],
        "currentBatch": None,
        "currentBatchStartedAt": None,
        "devices": {},
        "events": [],
    }
    rollouts(state)[batch_id] = rollout
    # 创建成功：追加 created 事件（无显式时刻，取调用时刻）。
    created_at = datetime.now(timezone.utc)
    append_event(rollout, EVENT_CREATED, created_at)
    mark_status_changed(rollout, created_at)
    return batch_view(state, rollout, at_instant=None)


# ---------------------------------------------------------------------------
# 放量前调整（仅 pending：替换目标/金丝雀集合与灰度策略，不建批次）
# ---------------------------------------------------------------------------

def require_update_canary_ids(values):
    """batch update 的金丝雀参数语法校验：空值/重复 -> InvalidArgument；升序返回。

    成员归属与数量上限按 update 口径单独判定（InvalidBatchPolicy），与
    batch create 的 require_canary_device_ids 异常口径不同，故不复用。
    """
    seen = set()
    ids = []
    for value in values or []:
        if not isinstance(value, str) or not value.strip():
            raise cli.InvalidArgument("canary-device-id must be a non-empty string: %r" % (value,))
        device_id = value.strip()
        if device_id in seen:
            raise cli.InvalidArgument("duplicate canary-device-id: %s" % device_id)
        seen.add(device_id)
        ids.append(device_id)
    return sorted(ids)


def cmd_batch_update(state, args):
    """放量前调整 pending 批次的灰度策略与设备集合。

    仅 pending 可更新；targetVersion/stableVersion 不变，未提供的策略项保留
    原值。传入任一 --target-device-id 时以去重后的完整集合替换原目标，传入
    任一 --canary-device-id 时替换原金丝雀，--clear-canary-device-ids 清空
    金丝雀。全部校验通过后才一次写入：保持 pending、不建批次、不改设备或
    占用，按 --at 追加 policy_updated 审计事件，返回 batch status 同口径视图。
    """
    batch_id = cli.require_id(args.batch_id, "batch-id")
    _, at_instant = cli.require_time(args.at)
    rollout = get_rollout(state, batch_id)
    if rollout["status"] != STATUS_PENDING:
        raise cli.InvalidState(
            "batch %s cannot be updated (status: %s)" % (batch_id, rollout["status"])
        )

    target_values = getattr(args, "target_device_id", None)
    canary_values = getattr(args, "canary_device_id", None)
    clear_canary = bool(getattr(args, "clear_canary_device_ids", False))

    # 设备参数语法校验：空值/重复 InvalidArgument；显式空目标集合 EmptyDeviceSet。
    new_targets = None
    if target_values is not None:
        new_targets = require_batch_device_ids(target_values)
    new_canary = None
    if canary_values is not None:
        new_canary = require_update_canary_ids(canary_values)
    if new_canary is not None and clear_canary:
        raise cli.InvalidArgument(
            "canary-device-id and clear-canary-device-ids cannot be used together"
        )

    # 人工放行门禁开关：--approval-required 开启、--no-approval-required 关闭；
    # 两开关同现 InvalidArgument，均未提供时保留原策略（旧状态缺字段按未开启）。
    set_approval = bool(getattr(args, "approval_required", False))
    clear_approval = bool(getattr(args, "no_approval_required", False))
    if set_approval and clear_approval:
        raise cli.InvalidArgument(
            "approval-required and no-approval-required cannot be used together"
        )

    # 未提供任何修改项（目标/金丝雀/清空/策略/门禁开关）时拒绝。
    policy_given = any((
        args.batch_size is not None,
        args.failure_threshold is not None,
        args.heartbeat_timeout_seconds is not None,
        args.stabilization_seconds is not None,
        args.rollback_timeout_seconds is not None,
    ))
    if new_targets is None and new_canary is None and not clear_canary \
            and not policy_given and not (set_approval or clear_approval):
        raise cli.InvalidArgument("batch update requires at least one change")

    # 策略参数：未提供保留原值；提供时沿用 batch create 的取值范围，
    # 非法取值 InvalidBatchPolicy。
    if args.batch_size is None:
        batch_size = rollout["batchSize"]
    else:
        batch_size = require_policy_batch_size(args.batch_size)
    if args.failure_threshold is None:
        failure_threshold = rollout["failureThreshold"]
    else:
        _, threshold = require_failure_threshold(args.failure_threshold)
        failure_threshold = str(threshold)
    if args.heartbeat_timeout_seconds is None:
        heartbeat_timeout = rollout["heartbeatTimeoutSeconds"]
    else:
        heartbeat_timeout = require_policy_timeout(args.heartbeat_timeout_seconds)
    if args.stabilization_seconds is None:
        stabilization_seconds = rollout.get("stabilizationSeconds", 0)
    else:
        stabilization_seconds = require_policy_stabilization(args.stabilization_seconds)
    if args.rollback_timeout_seconds is None:
        rollback_timeout = rollout.get(
            "rollbackTimeoutSeconds", DEFAULT_ROLLBACK_TIMEOUT_SECONDS
        )
    else:
        rollback_timeout = require_rollback_timeout(args.rollback_timeout_seconds)
    if set_approval:
        approval_required = True
    elif clear_approval:
        approval_required = False
    else:
        approval_required = bool(rollout.get("approvalRequired", False))

    # 目标替换：去重后的完整集合整体替换；每台设备必须已登记（DeviceNotFound）
    # 且未隔离（InvalidArgument）。未替换目标时不校验隔离（隔离时已纳入的
    # 目标继续按既有流程处理）。
    if new_targets is None:
        new_targets = list(rollout["targetDeviceIds"])
    else:
        for device_id in new_targets:
            cli.get_device(state, device_id)
            if cli.is_device_quarantined(state, device_id):
                raise cli.InvalidArgument(
                    "target device %s is quarantined" % device_id
                )

    # 金丝雀解析：显式替换 > 清空 > 保留旧值。目标替换后旧金丝雀不在新集合
    # 且未给出新金丝雀或清空选项时拒绝（InvalidArgument）。
    if clear_canary:
        effective_canary = []
    elif new_canary is not None:
        effective_canary = new_canary
    else:
        effective_canary = sorted(rollout.get("canaryDeviceIds") or [])
        if target_values is not None \
                and any(device_id not in set(new_targets) for device_id in effective_canary):
            raise cli.InvalidArgument(
                "existing canary devices are no longer in the target set; "
                "provide --canary-device-id or --clear-canary-device-ids"
            )
    # 金丝雀越界：须属于（更新后的）目标集合且数量不超过有效 batch-size。
    target_set = set(new_targets)
    for device_id in effective_canary:
        if device_id not in target_set:
            raise InvalidBatchPolicy(
                "canary-device-id must be one of the target-device-id values: %s"
                % device_id
            )
    if len(effective_canary) > batch_size:
        raise InvalidBatchPolicy(
            "canary device count %d exceeds batch-size %d"
            % (len(effective_canary), batch_size)
        )

    # 全部校验通过，一次写入：保持 pending，不建批次、不改设备或占用。
    if target_values is not None:
        rollout["targetDeviceIds"] = new_targets
    if clear_canary or new_canary is not None:
        rollout["canaryDeviceIds"] = effective_canary
    rollout["batchSize"] = batch_size
    rollout["failureThreshold"] = failure_threshold
    rollout["heartbeatTimeoutSeconds"] = heartbeat_timeout
    rollout["stabilizationSeconds"] = stabilization_seconds
    rollout["rollbackTimeoutSeconds"] = rollback_timeout
    rollout["approvalRequired"] = approval_required
    append_event(rollout, EVENT_POLICY_UPDATED, at_instant)
    return batch_view(state, rollout, at_instant=at_instant)


# ---------------------------------------------------------------------------
# 启动前资格预检（batch plan 与 batch start 共用同一只读口径）
# ---------------------------------------------------------------------------

def occupied_device_ids(state, rollout):
    """跨子系统统一占用口径下、被其他发布占用的设备集合。

    release 状态为 in_progress/paused，或 batch rollout 状态为
    in_progress/paused/failed_stopped 时，持续占用其已经纳入批次（batches）
    的目标设备；completed、rolled_back、rollback_failed 释放占用，pending
    发布尚未建批也不占用。本函数委托 cli.unified_occupied_device_ids 计算并
    剔除当前批次自身（pending 预检时自身尚未建批、本不在集合中）。
    """
    occupied = cli.unified_occupied_device_ids(state)
    occupied.difference_update(cli.rollout_batch_device_ids(rollout))
    return occupied


def evaluate_eligibility(state, rollout):
    """计算目标设备的启动资格（只读）。

    返回 (target_ids, eligible_ids, ineligible, batches)：
    - target_ids 为创建时去重集合，按 device-id 升序；
    - 当前版本等于 stableVersion 且未被跨子系统统一口径占用者合格：
      in_progress/paused 的 release 或 in_progress/paused/failed_stopped 的
      其他 batch rollout 已纳入批次的设备均占用；
    - 隔离设备一律不合格（reason=quarantined），隔离判定优先于版本与占用；
      版本不符与占用兼一时 VERSION_MISMATCH 优先；
    - batches 只含合格设备并按 batchSize 切分，无合格设备时为 []；
    - 金丝雀（创建时登记的 canaryDeviceIds）按 device-id 升序进入第一批，
      再用非金丝雀按同序补足，其余合格设备按同序组成后续批次；
      未指定金丝雀（含旧状态缺字段）时保持纯 device-id 升序切批。
    """
    target_ids = sorted(rollout["targetDeviceIds"])
    # 显式目标先确认仍然存在（DeviceNotFound），再判隔离、版本与占用资格。
    for device_id in target_ids:
        cli.get_device(state, device_id)
    stable_version = rollout["stableVersion"]
    busy = occupied_device_ids(state, rollout)
    eligible_ids = []
    ineligible = []
    for device_id in target_ids:
        device = state["devices"].get(device_id)
        version = device.get("version") if isinstance(device, dict) else None
        if cli.is_device_quarantined(state, device_id):
            ineligible.append({"deviceId": device_id, "reason": REASON_QUARANTINED})
        elif version != stable_version:
            ineligible.append({"deviceId": device_id, "reason": REASON_VERSION_MISMATCH})
        elif device_id in busy:
            ineligible.append({"deviceId": device_id, "reason": REASON_DEVICE_BUSY})
        else:
            eligible_ids.append(device_id)
    size = rollout["batchSize"]
    # 金丝雀优先：eligible_ids 已按 device-id 升序，金丝雀子集与非金丝雀子集各自
    # 保持升序；拼接后按 batchSize 切分即“金丝雀进第一批、非金丝雀补足”。
    canary = set(rollout.get("canaryDeviceIds") or [])
    ordered = ([d for d in eligible_ids if d in canary]
               + [d for d in eligible_ids if d not in canary])
    batches = [ordered[i:i + size] for i in range(0, len(ordered), size)]
    return target_ids, eligible_ids, ineligible, batches


def ineligible_message(ineligible):
    """升序列出不合格设备及其唯一原因。"""
    parts = [
        "%s=%s" % (item["deviceId"], item["reason"])
        for item in sorted(ineligible, key=lambda item: item["deviceId"])
    ]
    return "ineligible devices: " + ", ".join(parts)


def cmd_batch_plan(state, args):
    """对 pending 发布做只读启动预检：不改变状态，重复调用结果一致。"""
    batch_id = cli.require_id(args.batch_id, "batch-id")
    rollout = get_rollout(state, batch_id)
    if rollout["status"] != STATUS_PENDING:
        raise cli.InvalidState(
            "batch %s is not pending (status: %s)" % (batch_id, rollout["status"])
        )
    if args.at is not None:
        # 仅校验时刻合法性；预检结论本身与时刻无关。
        cli.require_time(args.at)
    target_ids, eligible_ids, ineligible, batches = evaluate_eligibility(state, rollout)
    return {
        "batchId": rollout["batchId"],
        "targetVersion": rollout["targetVersion"],
        "stableVersion": rollout["stableVersion"],
        "batchSize": rollout["batchSize"],
        "targetDeviceIds": target_ids,
        "canaryDeviceIds": list(rollout.get("canaryDeviceIds") or []),
        # 人工放行门禁与等待状态：旧状态缺字段时按未开启、不等待显示。
        "approvalRequired": bool(rollout.get("approvalRequired", False)),
        "awaitingApproval": bool(rollout.get("awaitingApproval", False)),
        "eligibleDeviceIds": eligible_ids,
        "ineligibleDevices": ineligible,
        "candidateCount": len(eligible_ids),
        "batches": batches,
    }


def cmd_batch_start(state, args):
    batch_id = cli.require_id(args.batch_id, "batch-id")
    rollout = get_rollout(state, batch_id)
    if rollout["status"] != STATUS_PENDING:
        raise cli.InvalidState(
            "batch %s is not pending (status: %s)" % (batch_id, rollout["status"])
        )
    if args.at is None:
        at_instant = datetime.now(timezone.utc)
    else:
        _, at_instant = cli.require_time(args.at)
    # 与 plan 完全相同的资格、排序与分批口径：版本/占用不合格即拒绝启动，
    # 不创建批次或设备阶段，不发生任何状态写入；隔离设备（reason=quarantined）
    # 不阻断启动，仅从放量中摘除——仅推进合格设备。
    ids, eligible_ids, ineligible, planned_batches = evaluate_eligibility(state, rollout)
    blocking = [item for item in ineligible if item["reason"] != REASON_QUARANTINED]
    if blocking:
        raise InvalidBatchEligibility(ineligible_message(ineligible))
    rollout["batches"] = planned_batches
    entries = rollout["devices"]
    for index, group in enumerate(rollout["batches"]):
        for device_id in group:
            entries[device_id] = {
                "batchIndex": index,
                "phase": PHASE_QUEUED,
                "lastHeartbeatAt": None,
                "lastVersion": None,
                "terminal": None,
                "rollback": None,
            }
    if rollout["batches"]:
        rollout["status"] = STATUS_IN_PROGRESS
        promote_batch(rollout, 0, at_instant)
        append_event(rollout, EVENT_STARTED, at_instant,
                     phase_from=STATUS_PENDING, phase_to=STATUS_IN_PROGRESS)
        append_event(rollout, EVENT_BATCH_OPENED, at_instant, batch_index=0)
    else:
        # 目标设备均被隔离：无合格设备可推进，启动后直接 completed（名义生命周期
        # 与 release start 空匹配一致：pending→in_progress→completed）。
        rollout["status"] = STATUS_COMPLETED
        append_event(rollout, EVENT_STARTED, at_instant,
                     phase_from=STATUS_PENDING, phase_to=STATUS_IN_PROGRESS)
        append_event(rollout, EVENT_FINISHED, at_instant,
                     phase_from=STATUS_IN_PROGRESS, phase_to=STATUS_COMPLETED)
    mark_status_changed(rollout, at_instant)
    return batch_view(state, rollout, at_instant=at_instant)


def promote_batch(rollout, index, at_instant):
    """把第 index 批设备置为待升级；每次只推进一批。"""
    rollout["currentBatch"] = index
    rollout["currentBatchStartedAt"] = cli.format_instant(at_instant)
    for device_id in rollout["batches"][index]:
        entry = rollout["devices"][device_id]
        entry["phase"] = PHASE_PENDING_UPGRADE


# ---------------------------------------------------------------------------
# 设备回传：心跳时间、当前固件版本、升级终态
# ---------------------------------------------------------------------------

def heartbeat_anchor(rollout, entry):
    """等待终态期间的计时锚点：最近一次有效心跳；尚未收到则取本批开始时刻。"""
    text = entry.get("lastHeartbeatAt") or rollout.get("currentBatchStartedAt")
    return cli.parse_time(text) if text else None


def pause_intervals(rollout):
    """已闭合的暂停区间列表 ``[(paused_at, resumed_at), ...]``，按开始时刻升序。

    区间为内部计时依据：resume 成功时闭合；当前仍处于暂停（或在暂停中被止损）
    的未闭合区间不参与计时扣除——暂停期间没有任何入口会推进或补超时。
    旧状态缺少 ``pauses`` 时视为从未暂停。
    """
    intervals = []
    for item in rollout.get("pauses") or []:
        if not isinstance(item, dict):
            continue
        start_text = item.get("pausedAt")
        end_text = item.get("resumedAt")
        if not start_text or not end_text:
            continue
        try:
            intervals.append((cli.parse_time(start_text), cli.parse_time(end_text)))
        except cli.InvalidArgument:
            continue
    intervals.sort(key=lambda pair: pair[0])
    return intervals


def paused_duration_between(rollout, start, end):
    """[start, end] 与已闭合暂停区间的重叠总时长；end 不晚于 start 时为 0。"""
    if end <= start:
        return timedelta(0)
    total = timedelta(0)
    for pause_start, pause_end in pause_intervals(rollout):
        overlap_start = max(start, pause_start)
        overlap_end = min(end, pause_end)
        if overlap_end > overlap_start:
            total += overlap_end - overlap_start
    return total


def record_heartbeat(state, rollout, device_id, version, heartbeat_text, at_instant):
    """登记一次有效心跳：同步公开设备入口的版本/心跳，并推进待升级->升级中。

    暂停期间继续接收心跳与版本：当前批已派发（pending_upgrade）的设备收到
    心跳即视为升级已开始（upgrading），与是否暂停无关；暂停冻结的只是对尚未
    开始设备（后续批次）的升级派发与批次推进。
    """
    device = state["devices"].get(device_id)
    if device is not None:
        device["heartbeatAt"] = heartbeat_text
        device["version"] = version
    entry = rollout["devices"].get(device_id)
    if entry is not None:
        entry["lastHeartbeatAt"] = heartbeat_text
        entry["lastVersion"] = version
        if entry["phase"] == PHASE_PENDING_UPGRADE \
                and rollout["status"] in (STATUS_IN_PROGRESS, STATUS_PAUSED):
            entry["phase"] = PHASE_UPGRADING


def cmd_batch_report(state, args):
    batch_id = cli.require_id(args.batch_id, "batch-id")
    device_id = cli.require_id(args.device_id, "device-id")
    version = cli.require_version(args.version)
    heartbeat_text, at_instant = cli.require_time(args.heartbeat_at)
    result = args.result
    if result is not None:
        cli.require_result(result)
    rollout = get_rollout(state, batch_id)
    cli.get_device(state, device_id)  # 设备必须经公开入口登记
    entry = rollout["devices"].get(device_id)

    # 仅进行中或手动暂停的批次接收当前批上报：暂停只冻结对尚未开始设备的升级
    # 派发，当前批已开始设备的心跳、版本与终态仍按既有规则接收并计入失败率；
    # 自动停止（failed_stopped）后依旧冻结，后续批次及未取得终态设备不再接收。
    if rollout["status"] not in (STATUS_IN_PROGRESS, STATUS_PAUSED):
        raise cli.InvalidState(
            "batch %s is not accepting upgrade reports (status: %s)"
            % (batch_id, rollout["status"])
        )
    if entry is None:
        raise cli.InvalidArgument("device %s is not part of batch %s" % (device_id, batch_id))

    terminal = entry["terminal"]
    if terminal is not None:
        # 幂等：同一设备同一阶段（含此前已集齐批次的设备）重复上报同一终态，原样返回，
        # 不重复计数；晚到/冲突终态（含失败后又报成功）一律拒绝，已失败设备不得改回成功。
        if result is not None and terminal["result"] != result:
            raise cli.InvalidArgument(
                "device %s already reported %s for batch %s; late result %s rejected"
                % (device_id, terminal["result"], batch_id, result)
            )
        return batch_view(state, rollout, at_instant=at_instant, reported_device=device_id,
                          result=result or terminal["result"], heartbeat_at=heartbeat_text,
                          version=version, idempotent=True)

    # 尚未取得终态的设备必须属于当前已推进批次；后续批次设备被冻结，不接收升级任务。
    if entry["batchIndex"] != rollout["currentBatch"]:
        raise cli.InvalidState(
            "device %s is in a frozen later batch of batch %s" % (device_id, batch_id)
        )

    # 心跳（无论是否携带终态）始终有效，但终态以首次为准。
    phase_before = entry["phase"]
    record_heartbeat(state, rollout, device_id, version, heartbeat_text, at_instant)

    if result is None:
        # 仅心跳：保持升级中，等待终态。
        append_event(rollout, EVENT_UPGRADE_REPORTED, at_instant,
                     batch_index=entry["batchIndex"], device_id=device_id,
                     phase_from=phase_before, phase_to=entry["phase"])
        return batch_view(state, rollout, at_instant=at_instant, reported_device=device_id,
                          result=None, heartbeat_at=heartbeat_text, version=version)

    if result == "success":
        # 设备完成升级：记录目标版本。
        state["devices"][device_id]["version"] = rollout["targetVersion"]
        entry["phase"] = PHASE_SUCCESS
    else:
        entry["phase"] = PHASE_FAILED
    entry["terminal"] = {
        "result": result,
        "reason": "reported",
        "heartbeatAt": heartbeat_text,
        "version": version,
    }
    append_event(rollout, EVENT_UPGRADE_REPORTED, at_instant,
                 batch_index=entry["batchIndex"], device_id=device_id, result=result,
                 phase_from=phase_before, phase_to=entry["phase"])
    finalize_batch_if_ready(state, rollout, at_instant)
    return batch_view(state, rollout, at_instant=at_instant, reported_device=device_id,
                      result=result, heartbeat_at=heartbeat_text, version=version)


def cmd_batch_check(state, args):
    batch_id = cli.require_id(args.batch_id, "batch-id")
    rollout = get_rollout(state, batch_id)
    _, at_instant = cli.require_time(args.at)
    if rollout["status"] == STATUS_PAUSED:
        # 暂停冻结：不补超时、不推进，观察期截止时刻在恢复时才顺延。
        raise cli.InvalidState(
            "batch %s is paused and cannot be checked (status: %s)"
            % (batch_id, rollout["status"])
        )
    if rollout["status"] not in (STATUS_IN_PROGRESS, STATUS_FAILED_STOPPED):
        raise cli.InvalidState(
            "batch %s is not in progress (status: %s)" % (batch_id, rollout["status"])
        )
    expired, rollback_expired = run_batch_check(state, rollout, at_instant)
    view = batch_view(state, rollout, at_instant=at_instant)
    view["expiredDevices"] = expired
    view["rollbackExpiredDeviceIds"] = rollback_expired
    return view


def run_batch_check(state, rollout, at_instant):
    """batch check 的收批核心，返回 (expiredDevices, rollbackExpiredDeviceIds)。

    仅处理 in_progress 与 failed_stopped 批次（状态前置校验由调用方完成）；
    batch check 与 fleet check 共用本函数，判定口径完全一致。

    failed_stopped：回滚收束时限 at 严格晚于 rollbackDeadline 时把 pending 回滚
    任务收束为 failure(reason=timeout)；无回滚任务的停止态在显式收批入口再次
    到达时收束为 rolled_back；仍有 pending 回滚任务时保持 failed_stopped。

    in_progress：先补写当前批心跳超时（显式结果永不覆盖），再收批判定
    （失败阈值自动停止/回滚任务），最后判定观察期推进——at 严格晚于
    stabilizationDeadline 才推进，等于或更早保持不变。
    """
    if rollout["status"] == STATUS_FAILED_STOPPED:
        rollback_expired = apply_rollback_timeouts(rollout, at_instant)
        finalize_rollback_if_done(rollout, converge_empty=True, at_instant=at_instant)
        return [], rollback_expired
    timeout = timedelta(seconds=rollout["heartbeatTimeoutSeconds"])
    current = rollout["currentBatch"]
    expired = []
    for device_id in rollout["batches"][current]:
        entry = rollout["devices"][device_id]
        if entry["terminal"] is not None:
            continue
        anchor = heartbeat_anchor(rollout, entry)
        if anchor is None:
            continue
        # 暂停时长不计入心跳超时：有效流逝 = 观察时刻 - 锚点 - 区间内暂停重叠。
        paused_elapsed = paused_duration_between(rollout, anchor, at_instant)
        if at_instant - paused_elapsed > anchor + timeout:
            # 等待升级终态期间超过心跳超时仍未收到有效心跳 -> 本批失败。
            # heartbeatAt 保留设备原值，显式结果永不覆盖。
            phase_before = entry["phase"]
            entry["phase"] = PHASE_FAILED
            entry["terminal"] = {
                "result": "failure",
                "reason": "timeout",
                "heartbeatAt": entry.get("lastHeartbeatAt"),
                "version": entry.get("lastVersion"),
            }
            append_event(rollout, EVENT_TIMEOUT_RECORDED, at_instant,
                         batch_index=current, device_id=device_id, result="failure",
                         phase_from=phase_before, phase_to=PHASE_FAILED,
                         reason="timeout")
            expired.append(device_id)
    expired.sort()
    finalize_batch_if_ready(state, rollout, at_instant)
    # 观察期仅由显式收批入口跨越：at 严格晚于 stabilizationDeadline 才推进，
    # 等于或更早保持不变；deadline 过后未 check 不自动推进。
    advance_stabilized_batch_if_due(rollout, at_instant)
    return expired, []


def apply_rollback_timeouts(rollout, at_instant):
    """failed_stopped 且 at 严格晚于 rollbackDeadline 时收束 pending 回滚任务。

    每台超时设备：回滚记录置为 failure(reason=timeout)，设备阶段改为
    rollback_failed，版本与 heartbeatAt 保留原值，并以 at 追加一条
    rollback_timed_out 审计事件。返回按 device-id 升序的超时设备列表；
    未超时（无截止时刻、at 不晚于截止时刻或没有 pending 任务）时为空数组，
    重复 check 不重复变更或追加事件。
    """
    deadline_text = rollout.get("rollbackDeadline")
    if deadline_text is None:
        return []
    if at_instant <= cli.parse_time(deadline_text):
        return []
    expired = []
    for device_id in sorted(rollout["devices"]):
        entry = rollout["devices"][device_id]
        record = entry.get("rollback")
        if record is None or record.get("state") != "pending":
            continue
        record["state"] = "failure"
        record["reason"] = "timeout"
        entry["phase"] = PHASE_ROLLBACK_FAILED
        append_event(rollout, EVENT_ROLLBACK_TIMED_OUT, at_instant,
                     batch_index=entry["batchIndex"], device_id=device_id,
                     result="failure",
                     phase_from=PHASE_ROLLING_BACK, phase_to=PHASE_ROLLBACK_FAILED,
                     reason="timeout")
        expired.append(device_id)
    return expired


def finalize_batch_if_ready(state, rollout, at_instant):
    """本批全部取得终态后：判失败率 -> 自动停止回滚 / 进入观察 / 立即推进。

    report 路径在本批集齐终态时调用本函数：观察期大于 0 时只产生
    stabilizationDeadline 并保持 in_progress、不开下一批，不自动推进
    （deadline 过后未经 batch check 不推进）；是否越过截止时刻由 check 路径
    另行调用 advance_stabilized_batch_if_due 判定。

    手动暂停期间同样执行失败率判定：暂停不冻结终态记录与统计，本批集齐终态
    且失败率严格大于阈值时自动停止/回滚规则优先（批次转入既有
    failed_stopped 流程）。未越阈时暂停不推进下一批、也不开启观察计时：
    stabilization-seconds 为 0 时本应立即发生的推进顺延到恢复时（由 resume
    再次调用本函数），大于 0 时观察截止时刻在恢复时统一计算（暂停期间
    stabilizationDeadline 保持 null；若观察期在暂停前已开始，则保留原截止并
    由 resume 顺延）。
    """
    current = rollout["currentBatch"]
    group = rollout["batches"][current]
    entries = rollout["devices"]
    if any(entries[d]["terminal"] is None for d in group):
        return
    # 人工放行门禁等待中：批次保持 in_progress，report/check/resume 均不推进、
    # 不重开观察窗口、不重复追加事件；仅 batch approve 放行下一批。
    if rollout.get("awaitingApproval"):
        return
    failed = sum(1 for d in group if entries[d]["terminal"]["result"] == "failure")
    rate = Fraction(failed, len(group))
    threshold = Fraction(Decimal(rollout["failureThreshold"]))
    if rate > threshold:
        trigger_failure_stop(state, rollout, at_instant)
        return
    # 暂停态：失败率未越阈即冻结在此——不推进、不新开观察窗口；恢复时由
    # cmd_batch_resume 再次调用本函数继续原策略。
    if rollout["status"] == STATUS_PAUSED:
        return
    if rollout.get("stabilizationSeconds", 0) > 0:
        # 失败率未超阈值：进入稳定观察期，截止时刻只计算一次（终态与有效心跳此后冻结）。
        if rollout.get("stabilizationDeadline") is None:
            rollout["stabilizationDeadline"] = compute_stabilization_deadline(rollout)
        return
    advance_or_finish_batch(rollout, at_instant)


def stabilization_base(rollout):
    """观察窗口锚点：本批有效心跳最大值；无有效心跳时取本批开始时刻。"""
    current = rollout["currentBatch"]
    heartbeats = []
    for device_id in rollout["batches"][current]:
        heartbeat_text = rollout["devices"][device_id].get("lastHeartbeatAt")
        if heartbeat_text:
            heartbeats.append(cli.parse_time(heartbeat_text))
    if heartbeats:
        return max(heartbeats)
    return cli.parse_time(rollout["currentBatchStartedAt"])


def shift_deadline_for_interval(deadline, base, interval_start, interval_end):
    """恢复时把一段暂停顺延到当前观察截止时刻上。

    仅计算暂停从进入观察窗口（``base``）起的部分：暂停早于窗口开始时只顺延
    窗内重叠；暂停跨越原截止时刻时整段剩余暂停都顺延（窗口在暂停期间冻结，
    剩余观察时长恢复后补满）。暂停开始于当前截止时刻之后则不顺延。
    """
    overlap_start = max(interval_start, base)
    if overlap_start < deadline and interval_end > overlap_start:
        deadline += interval_end - overlap_start
    return deadline


def extend_deadline_for_pauses(rollout, base, deadline):
    """按有效（非暂停）时间重算从 base 起、窗口长度 deadline-base 的截止时刻。

    逐段模拟：窗口只在非暂停区间消耗；暂停开始时结算已消耗时长并跳过整段
    暂停，窗口在某段暂停开始前已闭合则不再顺延。
    """
    cur = base
    remaining = deadline - base
    for pause_start, pause_end in pause_intervals(rollout):
        if pause_end <= cur:
            continue
        interval_start = max(pause_start, cur)
        if interval_start >= cur + remaining:
            break
        remaining -= interval_start - cur
        cur = pause_end
    return cur + remaining


def compute_stabilization_deadline(rollout):
    """观察期截止时刻：本批有效心跳最大值 + stabilization-seconds（扣除暂停时长）。

    本批无任何有效心跳时锚点取 currentBatchStartedAt；暂停区间与观察窗口的
    重叠部分顺延截止；输出统一为 UTC Z。
    """
    base = stabilization_base(rollout)
    deadline = base + timedelta(seconds=rollout["stabilizationSeconds"])
    deadline = extend_deadline_for_pauses(rollout, base, deadline)
    return cli.format_instant(deadline)


def advance_stabilized_batch_if_due(rollout, at_instant):
    """check 路径专用：观察期内 at 严格晚于截止时刻才推进下一批或 completed。

    重复 check 不会重复推进：推进后 currentBatch 已切换或状态已为 completed，
    stabilizationDeadline 同时清空。
    """
    deadline_text = rollout.get("stabilizationDeadline")
    if deadline_text is None or rollout["status"] != STATUS_IN_PROGRESS:
        return
    if at_instant <= cli.parse_time(deadline_text):
        return
    rollout["stabilizationDeadline"] = None
    advance_or_finish_batch(rollout, at_instant)


def advance_or_finish_batch(rollout, at_instant):
    """推进下一批（batch_opened 已在 start 记录，此处记 batch_advanced）或完成。

    开启人工放行门禁（approvalRequired）且仍有下一批时，到达可推进边界不直接
    推进：批次保持 in_progress，进入 awaitingApproval 等待并冻结尚未开始的
    设备，追加 approval_requested 事件（batchIndex 为下一批次号），由
    batch approve 放行；末批无下一批可等待，直接 completed。
    """
    current = rollout["currentBatch"]
    rollout["stabilizationDeadline"] = None
    if current + 1 == len(rollout["batches"]):
        rollout["status"] = STATUS_COMPLETED
        rollout["frozen"] = False
        rollout["currentBatchStartedAt"] = None
        append_event(rollout, EVENT_FINISHED, at_instant,
                     phase_from=STATUS_IN_PROGRESS, phase_to=STATUS_COMPLETED)
        mark_status_changed(rollout, at_instant)
    elif rollout.get("approvalRequired"):
        rollout["awaitingApproval"] = True
        append_event(rollout, EVENT_APPROVAL_REQUESTED, at_instant,
                     batch_index=current + 1)
    else:
        promote_batch(rollout, current + 1, at_instant)
        append_event(rollout, EVENT_BATCH_ADVANCED, at_instant, batch_index=current + 1)


def freeze_and_dispatch_rollbacks(rollout, stop_reason, at_instant):
    """冻结后续批次并向已成功设备下发回滚到 stableVersion 的任务。

    失败、超时、排队和未取得终态的设备不生成任务；设备阶段与终态报告保留现场。
    每台收到回滚任务的设备追加一条 rollback_started 事件。
    """
    rollout["status"] = STATUS_FAILED_STOPPED
    rollout["frozen"] = True
    rollout["stopReason"] = stop_reason
    rollout["currentBatchStartedAt"] = None
    mark_status_changed(rollout, at_instant)
    # 冻结即退出观察期：清空观察截止时刻（自动阈值停止与人工 abort 共用本路径）；
    # 同时退出人工放行等待（若正处于 awaitingApproval）。
    rollout["stabilizationDeadline"] = None
    rollout["awaitingApproval"] = False
    ordered = sorted(rollout["devices"].items(),
                     key=lambda kv: (kv[1]["batchIndex"], kv[0]))
    dispatched = False
    for device_id, entry in ordered:
        if entry["phase"] == PHASE_SUCCESS:
            entry["phase"] = PHASE_ROLLING_BACK
            entry["rollback"] = {
                "state": "pending", "heartbeatAt": None, "version": None, "reason": None,
            }
            append_event(rollout, EVENT_ROLLBACK_STARTED, at_instant,
                         batch_index=entry["batchIndex"], device_id=device_id,
                         phase_from=PHASE_SUCCESS, phase_to=PHASE_ROLLING_BACK)
            dispatched = True
    # 回滚收束时限：有 pending 任务时以停止时刻加策略秒数生成截止时刻（UTC Z），
    # 无任务为 null；旧状态缺 rollbackTimeoutSeconds 按 900 解释。
    if dispatched:
        timeout_seconds = rollout.get(
            "rollbackTimeoutSeconds", DEFAULT_ROLLBACK_TIMEOUT_SECONDS
        )
        rollout["rollbackDeadline"] = cli.format_instant(
            at_instant + timedelta(seconds=timeout_seconds)
        )
    else:
        rollout["rollbackDeadline"] = None


def trigger_failure_stop(state, rollout, at_instant):
    """失败率超阈值：FAILED_STOPPED、冻结后续批次、向已成功设备下发回滚任务。

    立即置为 failed_stopped；若存在回滚任务，由各设备 rollback-report 收束为
    rolled_back/rollback_failed；若没有需要回滚的设备，保持 failed_stopped 直到
    下一次 batch check（显式收批入口）收束为 rolled_back，保证停止状态外部可见。

    手动暂停期间本批集齐终态且越阈时同样走本路径（自动停止优先于暂停），
    此时 stopped 事件的 phaseFrom 为 paused。
    """
    status_before = rollout["status"]
    append_event(rollout, EVENT_STOPPED, at_instant,
                 phase_from=status_before, phase_to=STATUS_FAILED_STOPPED,
                 reason=STOP_REASON_FAILURE_THRESHOLD)
    freeze_and_dispatch_rollbacks(rollout, "batch_failure_threshold", at_instant)


def cmd_batch_pause(state, args):
    """手动暂停：仅 in_progress 可暂停。

    立即进入 paused，不再为尚未开始的设备（后续批次及当前批尚未派发者）
    发起升级；当前批已开始设备的心跳、版本与终态在暂停期间继续按既有规则
    接收并计算失败率，越阈时自动停止/回滚优先。批次未启动、已停止、正在
    回滚、已回滚完成或已暂停时返回 ConflictState。校验全过后才写状态。
    """
    batch_id = cli.require_id(args.batch_id, "batch-id")
    _, at_instant = cli.require_time(args.at)
    rollout = get_rollout(state, batch_id)
    if rollout["status"] != STATUS_IN_PROGRESS:
        raise ConflictState(
            "batch %s cannot be paused (status: %s)" % (batch_id, rollout["status"])
        )
    at_text = cli.format_instant(at_instant)
    rollout["status"] = STATUS_PAUSED
    rollout["pausedAt"] = at_text
    rollout["resumedAt"] = None
    rollout.setdefault("pauses", []).append({"pausedAt": at_text, "resumedAt": None})
    append_event(rollout, EVENT_PAUSED, at_instant,
                 phase_from=STATUS_IN_PROGRESS, phase_to=STATUS_PAUSED)
    mark_status_changed(rollout, at_instant)
    view = batch_view(state, rollout, at_instant=at_instant)
    view["effective"] = True
    return view


def cmd_batch_resume(state, args):
    """从暂停恢复：仅 paused 可恢复，从尚未开始的设备继续，不重复处理已终态设备。

    恢复后沿用原批次与策略：已成功设备不重复处理，已失败/已回滚设备沿用
    既有处置规则。暂停期间本批已集齐终态且失败率未越阈时，在此刻继续原
    策略——stabilization-seconds 为 0 立即推进下一批或完成，大于 0 时按
    扣除暂停时长后的观察截止时刻等待 batch check。其他状态返回 ConflictState。
    """
    batch_id = cli.require_id(args.batch_id, "batch-id")
    _, at_instant = cli.require_time(args.at)
    rollout = get_rollout(state, batch_id)
    if rollout["status"] != STATUS_PAUSED:
        raise ConflictState(
            "batch %s cannot be resumed (status: %s)" % (batch_id, rollout["status"])
        )
    at_text = cli.format_instant(at_instant)
    intervals = rollout.setdefault("pauses", [])
    current = intervals[-1] if intervals else None
    if isinstance(current, dict) and current.get("resumedAt") is None and current.get("pausedAt"):
        pause_start_text = current["pausedAt"]
        current["resumedAt"] = at_text
    else:
        # 兼容旧状态：只有 pausedAt 而无区间记录时补一个闭合区间。
        pause_start_text = rollout.get("pausedAt")
        intervals.append({"pausedAt": pause_start_text, "resumedAt": at_text})
    rollout["status"] = STATUS_IN_PROGRESS
    rollout["resumedAt"] = at_text
    # 已在观察期内（暂停发生在集齐终态、进入观察之后）：截止时刻顺延本段
    # 暂停与观察窗口的重叠时长；暂停期间才集齐终态（deadline 为 null）时，
    # 由下方 finalize 进入观察期时统一按闭合暂停区间扣除。
    deadline_text = rollout.get("stabilizationDeadline")
    if deadline_text is not None and pause_start_text:
        deadline = shift_deadline_for_interval(
            cli.parse_time(deadline_text),
            stabilization_base(rollout),
            cli.parse_time(pause_start_text), at_instant,
        )
        rollout["stabilizationDeadline"] = cli.format_instant(deadline)
    append_event(rollout, EVENT_RESUMED, at_instant,
                 phase_from=STATUS_PAUSED, phase_to=STATUS_IN_PROGRESS)
    mark_status_changed(rollout, at_instant)
    # 暂停期间本批可能已集齐终态：此刻继续原判定——越阈已在暂停时停止
    # （状态会是 failed_stopped，不会走到 resume）；未越阈时 stabilization=0
    # 立即推进/完成，>0 时进入观察期（截止时刻已扣除本段暂停）。
    finalize_batch_if_ready(state, rollout, at_instant)
    view = batch_view(state, rollout, at_instant=at_instant)
    view["effective"] = True
    return view


def cmd_batch_approve(state, args):
    """人工放行：仅 awaitingApproval 的进行中批次可批准，打开下一批。

    记录放行（approvedBatchIndexes 追加下一批次号、lastApprovedAt 记为 --at
    换算 UTC 后的 Z 时刻），清空等待标记，把下一批合格未隔离设备置为
    pending_upgrade，并追加 approval_granted 事件（batchIndex 为下一批次号）；
    批次、设备、状态与事件一次原子更新，不改变升级、超时、回滚和占用结果。
    批次不存在 DeviceNotFound；--at 非法 InvalidArgument；非等待放行状态
    （含已暂停、未开启门禁、末批已完成等）InvalidState。失败与重复动作不写
    状态、不追加事件。
    """
    batch_id = cli.require_id(args.batch_id, "batch-id")
    _, at_instant = cli.require_time(args.at)
    rollout = get_rollout(state, batch_id)
    if rollout["status"] != STATUS_IN_PROGRESS or not rollout.get("awaitingApproval"):
        raise cli.InvalidState(
            "batch %s is not awaiting approval (status: %s)"
            % (batch_id, rollout["status"])
        )
    next_index = rollout["currentBatch"] + 1
    rollout["awaitingApproval"] = False
    rollout.setdefault("approvedBatchIndexes", []).append(next_index)
    rollout["lastApprovedAt"] = cli.format_instant(at_instant)
    promote_batch(rollout, next_index, at_instant)
    append_event(rollout, EVENT_APPROVAL_GRANTED, at_instant, batch_index=next_index)
    return batch_view(state, rollout, at_instant=at_instant)


def cmd_batch_abort(state, args):
    """人工止损：进行中或已暂停的批次可中止，固定 manual_abort。

    所有参数与状态校验（含批次存在性与当前状态）通过前不写入任何状态。
    """
    batch_id = cli.require_id(args.batch_id, "batch-id")
    reason = cli.require_abort_reason(args.reason)
    _, at_instant = cli.require_time(args.at)
    rollout = get_rollout(state, batch_id)
    if rollout["status"] not in (STATUS_IN_PROGRESS, STATUS_PAUSED):
        raise cli.InvalidState(
            "batch %s cannot be aborted (status: %s)" % (batch_id, rollout["status"])
        )
    status_before = rollout["status"]
    append_event(rollout, EVENT_ABORTED, at_instant,
                 phase_from=status_before, phase_to=STATUS_FAILED_STOPPED,
                 reason=reason)
    freeze_and_dispatch_rollbacks(rollout, "manual_abort", at_instant)
    rollout["abortReason"] = reason
    rollout["abortedAt"] = cli.format_instant(at_instant)
    # 尚有回滚任务时先保持 failed_stopped，由 rollback-report 收束；无任务时直接
    # 收束为 rolled_back（abort 响应本身已保证停止状态外部可见）。
    finalize_rollback_if_done(rollout, converge_empty=True, at_instant=at_instant)
    return batch_view(state, rollout, at_instant=at_instant)


def finalize_rollback_if_done(rollout, converge_empty=False, at_instant=None):
    """回滚任务全部收束后决定最终状态。

    converge_empty 为 True（显式收批入口）时，没有任何回滚任务也收束为
    rolled_back；否则空集合保留 failed_stopped，保证停止状态外部可见。
    收束为终态（rolled_back/rollback_failed）时追加 finished 审计事件。
    """
    if rollout["status"] != STATUS_FAILED_STOPPED:
        return
    records = [e["rollback"] for e in rollout["devices"].values() if e["rollback"] is not None]
    if not records:
        if converge_empty:
            rollout["status"] = STATUS_ROLLED_BACK
            append_event(rollout, EVENT_FINISHED, at_instant,
                         phase_from=STATUS_FAILED_STOPPED, phase_to=STATUS_ROLLED_BACK)
            if at_instant is not None:
                mark_status_changed(rollout, at_instant)
        return
    if any(r["state"] == "pending" for r in records):
        return
    rollout["status"] = (
        STATUS_ROLLBACK_FAILED
        if any(r["state"] == "failure" for r in records)
        else STATUS_ROLLED_BACK
    )
    append_event(rollout, EVENT_FINISHED, at_instant,
                 phase_from=STATUS_FAILED_STOPPED, phase_to=rollout["status"])
    if at_instant is not None:
        mark_status_changed(rollout, at_instant)


# ---------------------------------------------------------------------------
# 回滚任务结果回传
# ---------------------------------------------------------------------------

def cmd_batch_rollback_report(state, args):
    batch_id = cli.require_id(args.batch_id, "batch-id")
    device_id = cli.require_id(args.device_id, "device-id")
    result = cli.require_result(args.result)
    heartbeat_text, at_instant = cli.require_time(args.heartbeat_at)
    version = cli.require_version(args.version) if args.version else None
    rollout = get_rollout(state, batch_id)
    cli.get_device(state, device_id)
    entry = rollout["devices"].get(device_id)
    if entry is None or entry.get("rollback") is None:
        raise cli.InvalidState(
            "device %s has no pending rollback task in batch %s" % (device_id, batch_id)
        )
    record = entry["rollback"]
    if record["state"] != "pending":
        # 幂等：同一回滚任务重复上报同一结果直接返回；冲突结果拒绝。
        if record["state"] != result:
            raise cli.InvalidArgument(
                "device %s rollback already %s in batch %s; late result %s rejected"
                % (device_id, record["state"], batch_id, result)
            )
        view = batch_view(state, rollout, at_instant=at_instant)
        view["report"] = {"deviceId": device_id, "result": result,
                          "heartbeatAt": record["heartbeatAt"], "idempotent": True}
        return view

    record["state"] = result
    record["heartbeatAt"] = heartbeat_text
    record["version"] = version
    # 回滚记录的 reason：成功为 None，设备回传的失败为 reported；
    # 超时收束（reason=timeout）由 batch check 写入，首次终态优先，此处不会覆盖。
    record["reason"] = None if result == "success" else "reported"
    device = state["devices"].get(device_id)
    if device is not None:
        device["heartbeatAt"] = heartbeat_text
        if result == "success":
            # 回滚成功：设备恢复稳定版本；回滚失败保留现场版本。
            device["version"] = rollout["stableVersion"]
        elif version is not None:
            device["version"] = version
    entry["phase"] = (
        PHASE_ROLLBACK_SUCCEEDED if result == "success" else PHASE_ROLLBACK_FAILED
    )
    append_event(rollout, EVENT_ROLLBACK_REPORTED, at_instant,
                 batch_index=entry["batchIndex"], device_id=device_id, result=result,
                 phase_from=PHASE_ROLLING_BACK, phase_to=entry["phase"])

    finalize_rollback_if_done(rollout, at_instant=at_instant)

    view = batch_view(state, rollout, at_instant=at_instant)
    view["report"] = {"deviceId": device_id, "result": result,
                      "heartbeatAt": heartbeat_text, "idempotent": False}
    return view


# ---------------------------------------------------------------------------
# 回滚重试（rollback_failed 批次中失败/超时回滚再试一次）
# ---------------------------------------------------------------------------

def cmd_batch_rollback_retry(state, args):
    """对 rollback_failed 批次中最新回滚为 failure/timeout 的设备重新下发回滚任务。

    成功后设备回到 rolling_back（回滚记录重置为 pending），批次回到
    failed_stopped 并保持冻结，设备版本与心跳不改，rollbackDeadline 按重试时刻
    加 rollbackTimeoutSeconds 重算；被取代的失败尝试依序归档到批次级
    rollbackAttempts（只增不覆盖）。所有参数、批次状态与设备资格校验通过前
    不写任何状态。
    """
    batch_id = cli.require_id(args.batch_id, "batch-id")
    device_ids = require_retry_device_ids(getattr(args, "device_id", None))
    reason = cli.require_abort_reason(args.reason)
    _, at_instant = cli.require_time(args.at)
    rollout = get_rollout(state, batch_id)
    for device_id in device_ids:
        cli.get_device(state, device_id)
    if rollout["status"] != STATUS_ROLLBACK_FAILED:
        raise cli.InvalidState(
            "batch %s cannot retry rollback (status: %s)" % (batch_id, rollout["status"])
        )
    entries = rollout["devices"]
    for device_id in device_ids:
        entry = entries.get(device_id)
        if entry is None:
            raise cli.InvalidArgument(
                "device %s is not part of batch %s" % (device_id, batch_id)
            )
        record = entry.get("rollback")
        # 最新回滚须为 failure（设备回传 reason=reported）或 timeout（超时收束
        # reason=timeout）且未成功；无失败回滚或已成功均不可重试。
        if record is None or record.get("state") != "failure":
            raise cli.InvalidArgument(
                "device %s has no failed rollback to retry in batch %s"
                % (device_id, batch_id)
            )
    at_text = cli.format_instant(at_instant)
    timeout_seconds = rollout.get(
        "rollbackTimeoutSeconds", DEFAULT_ROLLBACK_TIMEOUT_SECONDS
    )
    rollout["status"] = STATUS_FAILED_STOPPED
    rollout["frozen"] = True
    rollout["rollbackDeadline"] = cli.format_instant(
        at_instant + timedelta(seconds=timeout_seconds)
    )
    mark_status_changed(rollout, at_instant)
    rollout["retryDeviceIds"] = list(device_ids)
    rollout["retryStartedAt"] = at_text
    attempts = rollout.setdefault("rollbackAttempts", [])
    for device_id in device_ids:
        entry = entries[device_id]
        record = entry["rollback"]
        # 归档被取代的失败尝试：依序保留 state/result/reason/heartbeatAt/version，
        # 只增不覆盖（result 与终态口径一致，失败尝试恒为 failure）。
        attempts.append({
            "state": record.get("state"),
            "result": record.get("state"),
            "reason": record.get("reason"),
            "heartbeatAt": record.get("heartbeatAt"),
            "version": record.get("version"),
        })
        entry["rollback"] = {
            "state": "pending", "heartbeatAt": None, "version": None, "reason": None,
        }
        entry["phase"] = PHASE_ROLLING_BACK
        append_event(rollout, EVENT_ROLLBACK_RETRY_STARTED, at_instant,
                     batch_index=entry["batchIndex"], device_id=device_id,
                     phase_from=PHASE_ROLLBACK_FAILED, phase_to=PHASE_ROLLING_BACK,
                     reason=reason)
    return batch_view(state, rollout, at_instant=at_instant)


# ---------------------------------------------------------------------------
# 状态视图
# ---------------------------------------------------------------------------

def effective_phase(rollout, entry, at_instant):
    """结合查询时刻计算外部可见阶段（只读，不补写超时结果）。"""
    phase = entry["phase"]
    if phase not in (PHASE_PENDING_UPGRADE, PHASE_UPGRADING):
        return phase
    # 终态后不会落到这里；停止后保留现场阶段（待升级/升级中），由 frozen 表达冻结。
    if rollout["status"] != STATUS_IN_PROGRESS:
        return phase
    if entry["batchIndex"] != rollout["currentBatch"]:
        return PHASE_QUEUED
    anchor = heartbeat_anchor(rollout, entry)
    timeout = timedelta(seconds=rollout["heartbeatTimeoutSeconds"])
    if anchor is not None:
        # 与 batch check 同一口径：暂停时长不计入心跳超时。
        paused_elapsed = paused_duration_between(rollout, anchor, at_instant)
        if at_instant - paused_elapsed > anchor + timeout:
            # 未取得终态且已无有效心跳：等待心跳（check 后才会落为 failed）。
            return PHASE_WAITING_HEARTBEAT
    return PHASE_UPGRADING if entry.get("lastHeartbeatAt") else PHASE_PENDING_UPGRADE


def _rate(failed, reported):
    if reported == 0:
        return None
    return round(float(Fraction(failed, reported)), 6)


def batch_view(state, rollout, at_instant, reported_device=None, result=None,
               heartbeat_at=None, version=None, idempotent=False):
    if at_instant is None:
        at_instant = datetime.now(timezone.utc)
    entries = rollout["devices"]
    devices = []
    phase_counts = {
        PHASE_QUEUED: 0, PHASE_PENDING_UPGRADE: 0, PHASE_UPGRADING: 0,
        PHASE_SUCCESS: 0, PHASE_FAILED: 0, PHASE_WAITING_HEARTBEAT: 0,
        PHASE_ROLLING_BACK: 0, PHASE_ROLLBACK_SUCCEEDED: 0,
        PHASE_ROLLBACK_FAILED: 0,
    }
    reported = failed = succeeded = 0
    batch_reported = batch_failed = 0
    rollback_total = rollback_pending = rollback_succeeded = rollback_failed = 0
    rollback_timed_out = 0
    current_index = rollout["currentBatch"]
    # 金丝雀身份只读展示：旧状态缺 canaryDeviceIds 时按 [] 显示，不补写历史。
    canary_ids = list(rollout.get("canaryDeviceIds") or [])
    canary_set = set(canary_ids)

    ordered = sorted(entries.items(), key=lambda kv: (kv[1]["batchIndex"], kv[0]))
    for device_id, entry in ordered:
        phase = effective_phase(rollout, entry, at_instant)
        phase_counts[phase] += 1
        terminal = entry["terminal"]
        if terminal is not None:
            reported += 1
            if terminal["result"] == "failure":
                failed += 1
            else:
                succeeded += 1
            if entry["batchIndex"] == current_index:
                batch_reported += 1
                if terminal["result"] == "failure":
                    batch_failed += 1
        rollback_record = entry.get("rollback")
        if rollback_record is not None:
            rollback_total += 1
            state_name = rollback_record["state"]
            if state_name == "pending":
                rollback_pending += 1
            elif state_name == "success":
                rollback_succeeded += 1
            else:
                rollback_failed += 1
            if rollback_record.get("reason") == "timeout":
                rollback_timed_out += 1
            # 旧记录缺 reason 时按状态补全（成功/待定为 null，失败为 reported），只读不补写。
            rollback_record = dict(rollback_record)
            rollback_record.setdefault(
                "reason", "reported" if state_name == "failure" else None
            )
        device = state["devices"].get(device_id, {})
        devices.append({
            "deviceId": device_id,
            "batchIndex": entry["batchIndex"],
            "phase": phase,
            # 仅标记金丝雀身份，不改变 phase/terminal/rollback 等既有语义。
            "canary": device_id in canary_set,
            "version": device.get("version"),
            "heartbeatAt": device.get("heartbeatAt"),
            "lastHeartbeatAt": entry.get("lastHeartbeatAt"),
            "terminal": terminal,
            "rollback": rollback_record,
        })

    # 重试历史：被取代的失败尝试只增不覆盖；rollbackTimedOutCount 累计各次尝试
    # 与当前记录中的 timeout（归档记录已被新 pending 记录取代，不会重复计数）。
    attempts = [
        dict(attempt) for attempt in rollout.get("rollbackAttempts") or []
        if isinstance(attempt, dict)
    ]
    rollback_timed_out += sum(
        1 for attempt in attempts if attempt.get("reason") == "timeout"
    )

    view = {
        "batchId": rollout["batchId"],
        "targetVersion": rollout["targetVersion"],
        "stableVersion": rollout["stableVersion"],
        "batchSize": rollout["batchSize"],
        "failureThreshold": float(Decimal(rollout["failureThreshold"])),
        "heartbeatTimeoutSeconds": rollout["heartbeatTimeoutSeconds"],
        # 旧状态缺少观察期字段时分别显示 0 与 null，status 只读不补写历史。
        "stabilizationSeconds": rollout.get("stabilizationSeconds", 0),
        "stabilizationDeadline": rollout.get("stabilizationDeadline"),
        # 旧状态缺 rollbackTimeoutSeconds 按 900 解释；缺 rollbackDeadline 显示 null，
        # 不据此猜测超时（check 同样只在截止时刻存在时判定）。
        "rollbackTimeoutSeconds": rollout.get(
            "rollbackTimeoutSeconds", DEFAULT_ROLLBACK_TIMEOUT_SECONDS
        ),
        "rollbackDeadline": rollout.get("rollbackDeadline"),
        "targetDeviceIds": list(rollout["targetDeviceIds"]),
        "canaryDeviceIds": canary_ids,
        # 人工放行门禁：是否开启、是否等待放行、approve 已打开的后续批次号与最近
        # 放行时刻；旧状态缺字段时依次显示 false、false、[]、null，只读不补写。
        "approvalRequired": bool(rollout.get("approvalRequired", False)),
        "awaitingApproval": bool(rollout.get("awaitingApproval", False)),
        "approvedBatchIndexes": list(rollout.get("approvedBatchIndexes") or []),
        "lastApprovedAt": rollout.get("lastApprovedAt"),
        "status": rollout["status"],
        "frozen": rollout["frozen"],
        # 旧状态缺少暂停字段时按从未暂停读取：false / null / null。
        "paused": rollout["status"] == STATUS_PAUSED,
        "pausedAt": rollout.get("pausedAt"),
        "resumedAt": rollout.get("resumedAt"),
        "stopReason": rollout.get("stopReason"),
        "abortReason": rollout.get("abortReason"),
        "abortedAt": rollout.get("abortedAt"),
        "currentBatch": rollout["currentBatch"],
        "currentBatchStartedAt": rollout.get("currentBatchStartedAt"),
        "batchCount": len(rollout["batches"]),
        "batches": rollout["batches"],
        # 已处理 = 已取得升级终态（成功/失败）的目标设备数；待处理 = 目标总数
        # 减去已处理（含当前批未终态与尚未开始的后续批次设备；启动前为全部目标）。
        # 回滚处置不改变这两个计数：已成功设备即使进入回滚仍计为已处理。
        "processedCount": reported,
        "pendingCount": len(rollout["targetDeviceIds"]) - reported,
        # 最近一次批次状态（status）变更时刻；同状态内的批次推进不更新。
        # 旧状态缺该字段时显示 null，只读不补写历史。
        "lastStatusChangedAt": rollout.get("lastStatusChangedAt"),
        "completedCount": succeeded,
        "failedCount": failed,
        "reportedCount": reported,
        "currentBatchReportedCount": batch_reported,
        "failureRate": _rate(batch_failed, batch_reported),
        "phaseCounts": phase_counts,
        "rollbackTotal": rollback_total,
        "rollbackPending": rollback_pending,
        "rollbackSucceeded": rollback_succeeded,
        "rollbackFailed": rollback_failed,
        "rollbackTimedOutCount": rollback_timed_out,
        # 回滚重试：最近一次重试的设备集合与时刻（缺省 [] / null），以及全部已归档
        # 的失败尝试（缺省 []）；旧状态缺这些字段时按缺省显示，只读不补写。
        "retryDeviceIds": list(rollout.get("retryDeviceIds") or []),
        "retryStartedAt": rollout.get("retryStartedAt"),
        "rollbackAttempts": attempts,
        "devices": devices,
    }
    if reported_device is not None:
        view["report"] = {
            "deviceId": reported_device,
            "result": result,
            "heartbeatAt": heartbeat_at,
            "version": version,
            "idempotent": idempotent,
        }
    return view


def cmd_batch_status(state, args):
    batch_id = cli.require_id(args.batch_id, "batch-id")
    rollout = get_rollout(state, batch_id)
    if args.at is None:
        at_instant = datetime.now(timezone.utc)
    else:
        _, at_instant = cli.require_time(args.at)
    return batch_view(state, rollout, at_instant=at_instant)


# ---------------------------------------------------------------------------
# 审计时间线只读查询
# ---------------------------------------------------------------------------

def require_after_sequence(value):
    """--after-sequence：缺省 0；非负整数，否则 InvalidArgument。"""
    if value is None:
        return 0
    try:
        sequence = int(str(value).strip())
    except (TypeError, ValueError):
        raise cli.InvalidArgument(
            "after-sequence must be a non-negative integer: %r" % (value,)
        )
    if sequence < 0:
        raise cli.InvalidArgument(
            "after-sequence must be a non-negative integer: %r" % (value,)
        )
    return sequence


def require_timeline_limit(value):
    """--limit：缺省 200；1 到 1000 的整数，否则 InvalidArgument。"""
    if value is None:
        return TIMELINE_DEFAULT_LIMIT
    try:
        limit = int(str(value).strip())
    except (TypeError, ValueError):
        raise cli.InvalidArgument(
            "limit must be an integer between 1 and %d: %r" % (TIMELINE_MAX_LIMIT, value)
        )
    if limit < 1 or limit > TIMELINE_MAX_LIMIT:
        raise cli.InvalidArgument(
            "limit must be an integer between 1 and %d: %r" % (TIMELINE_MAX_LIMIT, value)
        )
    return limit


def timeline_event_view(event):
    """按固定字段顺序输出一条事件；旧事件缺字段时补 None。"""
    return {
        "sequence": event.get("sequence"),
        "type": event.get("type"),
        "occurredAt": event.get("occurredAt"),
        "batchIndex": event.get("batchIndex"),
        "deviceId": event.get("deviceId"),
        "result": event.get("result"),
        "phaseFrom": event.get("phaseFrom"),
        "phaseTo": event.get("phaseTo"),
        "reason": event.get("reason"),
    }


def cmd_batch_timeline(state, args):
    """只读时间线：批次不存在 DeviceNotFound；参数非法 InvalidArgument；不落盘。"""
    batch_id = cli.require_id(args.batch_id, "batch-id")
    rollout = get_rollout(state, batch_id)
    after_sequence = require_after_sequence(args.after_sequence)
    limit = require_timeline_limit(args.limit)
    events = rollout.get("events")
    if not isinstance(events, list):
        # 旧状态不补历史：无事件、nextSequence=1。
        events = []
    selected = sorted(
        (event for event in events
         if isinstance(event, dict) and isinstance(event.get("sequence"), int)
         and event["sequence"] > after_sequence),
        key=lambda event: event["sequence"],
    )
    sequences = [event.get("sequence") for event in events
                 if isinstance(event, dict) and isinstance(event.get("sequence"), int)]
    next_sequence = max(sequences) + 1 if sequences else 1
    return {
        "batchId": rollout["batchId"],
        "nextSequence": next_sequence,
        "events": [timeline_event_view(event) for event in selected[:limit]],
    }


# ---------------------------------------------------------------------------
# 与公开心跳入口（device heartbeat）的桥接
# ---------------------------------------------------------------------------

def feed_public_heartbeat(state, device_id, version, heartbeat_text):
    """公开入口 ``device heartbeat`` 的心跳同时喂给包含该设备的进行中/暂停批次。

    暂停期间同样刷新公开设备与当前批心跳，但不改变设备阶段（阶段冻结由
    record_heartbeat 内的状态判断保证）、不生成终态。严格旁路：任何异常都不
    影响既有命令的语义与成功结果。
    """
    for rollout in rollouts(state).values():
        if rollout.get("status") not in (STATUS_IN_PROGRESS, STATUS_PAUSED):
            continue
        entry = rollout.get("devices", {}).get(device_id)
        if entry is None or entry["batchIndex"] != rollout.get("currentBatch"):
            continue
        if entry["terminal"] is not None or entry.get("rollback") is not None:
            continue
        try:
            at_instant = cli.parse_time(heartbeat_text)
        except cli.InvalidArgument:
            continue
        record_heartbeat(state, rollout, device_id, version, heartbeat_text, at_instant)


# ---------------------------------------------------------------------------
# 只读跨子系统风险总览（fleet rollout-status）
# ---------------------------------------------------------------------------

# 活动（需要风险跟踪）状态：与跨子系统占用口径一致——release 为
# in_progress/paused；batch rollout 为 in_progress/paused/failed_stopped。
ACTIVE_RELEASE_STATUSES = (STATUS_IN_PROGRESS, STATUS_PAUSED)
ACTIVE_BATCH_STATUSES = (
    STATUS_IN_PROGRESS, STATUS_PAUSED, STATUS_FAILED_STOPPED,
)

RELEASE_STATUS_SET = (
    STATUS_PENDING, STATUS_IN_PROGRESS, STATUS_PAUSED,
    STATUS_COMPLETED, STATUS_ROLLED_BACK,
)
BATCH_STATUS_SET = (
    STATUS_PENDING, STATUS_IN_PROGRESS, STATUS_PAUSED, STATUS_COMPLETED,
    STATUS_FAILED_STOPPED, STATUS_ROLLED_BACK, STATUS_ROLLBACK_FAILED,
)
ROLLBACK_STATE_SET = ("pending", "success", "failure")


def _corrupted(ref, detail="is corrupted"):
    raise cli.InvalidState("%s %s" % (ref, detail))


def _optional_time(value, ref, label):
    """可空时间戳：None/缺失 -> None；非法字符串语义不明 -> InvalidState。

    合法值统一换算回 UTC ``Z`` 文本输出，保证总览查询输出稳定。
    """
    if value is None:
        return None
    if not isinstance(value, str):
        _corrupted(ref)
    try:
        return cli.format_instant(cli.parse_time(value))
    except cli.InvalidArgument:
        _corrupted(ref, "has invalid %s" % label)


def _validate_device_table(devices):
    if not isinstance(devices, dict):
        _corrupted("state file")
    for device_id, record in devices.items():
        if not isinstance(device_id, str) or not isinstance(record, dict):
            _corrupted("state file")
        heartbeat_text = record.get("heartbeatAt")
        if heartbeat_text is not None:
            _optional_time(heartbeat_text, "device %s" % device_id, "heartbeatAt")


def _release_overdue_device_ids(state, release, at_instant):
    """release 超时口径下观察时刻已逾期且未补超时终态的当前批设备（升序）。

    与 ``release check`` 同一判据：仅当前批、reports 中无任何结果（含此前 check
    补写的 reason=timeout 终态）、设备 heartbeatAt 非空，且观察时刻严格晚于
    heartbeatAt + heartbeatTimeoutSeconds。release 的暂停只冻结 check/report
    写入口、不移动超时钟（无暂停时长扣除），故 in_progress/paused 均按同一墙钟
    口径列示；只读不补写。
    """
    if release.get("status") not in ACTIVE_RELEASE_STATUSES:
        return []
    current = release["currentBatch"]
    batches = release["batches"]
    if not isinstance(current, int) or current < 0 or current >= len(batches):
        return []
    timeout = timedelta(
        seconds=release.get("heartbeatTimeoutSeconds", cli.DEFAULT_HEARTBEAT_TIMEOUT_SECONDS)
    )
    reports = release["reports"]
    devices = state["devices"]
    overdue = []
    for device_id in batches[current]:
        if device_id in reports:
            continue
        record = devices.get(device_id)
        heartbeat_text = record.get("heartbeatAt") if isinstance(record, dict) else None
        if heartbeat_text is None:
            continue
        if at_instant > cli.parse_time(heartbeat_text) + timeout:
            overdue.append(device_id)
    overdue.sort()
    return overdue


def _batch_overdue_device_ids(rollout, at_instant):
    """batch 超时/暂停口径下的逾期设备：与 batch status 的 waiting_heartbeat 同口径。

    仅 in_progress 批次（暂停/停止/终态不补超时），判据扣除已闭合暂停时长：
    ``at - 暂停重叠 > 计时锚点 + heartbeatTimeoutSeconds``。
    """
    if rollout.get("status") != STATUS_IN_PROGRESS:
        return []
    overdue = [
        device_id for device_id, entry in rollout["devices"].items()
        if effective_phase(rollout, entry, at_instant) == PHASE_WAITING_HEARTBEAT
    ]
    overdue.sort()
    return overdue


def _batch_rollback_risk(rollout):
    """最新一条回滚任务（entry.rollback）为 pending/failure 的设备（升序）。

    被 rollback-retry 归档进 rollbackAttempts 的历史尝试不计入。
    """
    pending = []
    failed = []
    for device_id, entry in rollout["devices"].items():
        record = entry.get("rollback")
        if not isinstance(record, dict):
            continue
        state_name = record.get("state")
        if state_name == "pending":
            pending.append(device_id)
        elif state_name == "failure":
            failed.append(device_id)
    return sorted(pending), sorted(failed)


def _campaign_deadline(stabilization_text, rollback_text, at_instant):
    """取观察时刻尚未过去（截止时刻 >= 观察时刻）的 stabilization/rollback 截止最早者。"""
    candidates = []
    if stabilization_text is not None:
        candidates.append((cli.parse_time(stabilization_text), "stabilization"))
    if rollback_text is not None:
        candidates.append((cli.parse_time(rollback_text), "rollback"))
    pending = [
        (instant, kind) for instant, kind in candidates if instant >= at_instant
    ]
    if not pending:
        return None
    instant, kind = min(pending, key=lambda item: (item[0], item[1]))
    return {"type": kind, "at": cli.format_instant(instant)}


def _risk_rate(failed, reported):
    """rate = failed / reported，保留 6 位小数；reported 为 0 时 null。"""
    if reported == 0:
        return None
    return round(float(Fraction(failed, reported)), 6)


def _release_campaign(state, release_id, release, at_instant):
    ref = "release %s" % release_id
    if not isinstance(release, dict):
        _corrupted(ref)
    status = release.get("status")
    if status not in RELEASE_STATUS_SET:
        _corrupted(ref, "has unknown status %r" % (status,))
    target_version = release.get("version")
    rollback_version = release.get("previousVersion")
    if not isinstance(target_version, str) or not isinstance(rollback_version, str):
        _corrupted(ref)
    target_devices = release.get("targetDeviceIds")
    if target_devices is not None:
        if not isinstance(target_devices, list) \
                or not all(isinstance(device_id, str) for device_id in target_devices):
            _corrupted(ref)
        target_devices = sorted(target_devices)
    timeout_seconds = release.get(
        "heartbeatTimeoutSeconds", cli.DEFAULT_HEARTBEAT_TIMEOUT_SECONDS
    )
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) \
            or timeout_seconds < 1:
        _corrupted(ref)
    reports = release.get("reports")
    if not isinstance(reports, dict):
        _corrupted(ref)
    for device_id, report in reports.items():
        if not isinstance(device_id, str) or not isinstance(report, dict):
            _corrupted(ref)
        _optional_time(report.get("heartbeatAt"), ref, "report heartbeatAt")
    current = release.get("currentBatch")
    if current is not None and (
            not isinstance(current, int) or isinstance(current, bool) or current < 0):
        _corrupted(ref)
    batches = release.get("batches")
    # 结构校验：batches 必须为字符串列表的列表（损坏 -> InvalidState）。
    cli.occupancy_batch_indices(release, ref)
    if status in ACTIVE_RELEASE_STATUSES:
        # 活动发布必须有落在批次范围内的当前批，否则状态语义不明。
        if current is None or not isinstance(batches, list) or current >= len(batches):
            _corrupted(ref)

    reported = len(reports)
    failed = sum(
        1 for report in reports.values() if report.get("result") == "failure"
    )
    overdue = _release_overdue_device_ids(state, release, at_instant)
    stabilization = _optional_time(
        release.get("stabilizationDeadline"), ref, "stabilizationDeadline"
    )
    stop_reason = release.get("stopReason")
    if stop_reason is not None and not isinstance(stop_reason, str):
        _corrupted(ref)
    return {
        "kind": "release",
        "id": release_id,
        "status": status,
        "versions": {"target": target_version, "rollback": rollback_version},
        "targetDeviceIds": target_devices,
        "progress": {
            "current": current,
            "batchCount": len(batches),
            "reported": reported,
            "failed": failed,
            "rate": _risk_rate(failed, reported),
        },
        # release 子系统没有回滚任务，pending/failed 恒为空设备数组。
        "risk": {"overdue": overdue, "pending": [], "failed": []},
        "deadline": _campaign_deadline(stabilization, None, at_instant),
        "stopReason": stop_reason,
    }


def _batch_campaign(state, batch_id, rollout, at_instant):
    ref = "batch %s" % batch_id
    if not isinstance(rollout, dict):
        _corrupted(ref)
    status = rollout.get("status")
    if status not in BATCH_STATUS_SET:
        _corrupted(ref, "has unknown status %r" % (status,))
    target_version = rollout.get("targetVersion")
    rollback_version = rollout.get("stableVersion")
    if not isinstance(target_version, str) or not isinstance(rollback_version, str):
        _corrupted(ref)
    target_devices = rollout.get("targetDeviceIds")
    if target_devices is None:
        target_devices = []
    if not isinstance(target_devices, list) \
            or not all(isinstance(device_id, str) for device_id in target_devices):
        _corrupted(ref)
    target_devices = sorted(target_devices)
    timeout_seconds = rollout.get("heartbeatTimeoutSeconds")
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) \
            or timeout_seconds < 1:
        _corrupted(ref)
    current = rollout.get("currentBatch")
    if current is not None and (
            not isinstance(current, int) or isinstance(current, bool) or current < 0):
        _corrupted(ref)
    batches = rollout.get("batches")
    cli.occupancy_batch_indices(rollout, ref)
    _optional_time(rollout.get("currentBatchStartedAt"), ref, "currentBatchStartedAt")
    entries = rollout.get("devices")
    if not isinstance(entries, dict):
        _corrupted(ref)
    if status in ACTIVE_BATCH_STATUSES:
        # 活动批次的当前批必须落在批次范围内（failed_stopped 可能已派发回滚但
        # 批次结构仍完整），否则状态语义不明。
        if not isinstance(batches, list) or current is None or current >= len(batches):
            _corrupted(ref)
    reported = failed = 0
    for device_id, entry in entries.items():
        if not isinstance(device_id, str) or not isinstance(entry, dict):
            _corrupted(ref)
        phase = entry.get("phase")
        if not isinstance(phase, str):
            _corrupted(ref)
        batch_index = entry.get("batchIndex")
        if not isinstance(batch_index, int) or isinstance(batch_index, bool) \
                or batch_index < 0:
            _corrupted(ref)
        _optional_time(entry.get("lastHeartbeatAt"), ref, "lastHeartbeatAt")
        terminal = entry.get("terminal")
        if terminal is not None:
            if not isinstance(terminal, dict) \
                    or terminal.get("result") not in ("success", "failure"):
                _corrupted(ref)
            reported += 1
            if terminal.get("result") == "failure":
                failed += 1
        record = entry.get("rollback")
        if record is not None:
            if not isinstance(record, dict) \
                    or record.get("state") not in ROLLBACK_STATE_SET:
                _corrupted(ref)
    overdue = _batch_overdue_device_ids(rollout, at_instant)
    pending, rollback_failed = _batch_rollback_risk(rollout)
    stabilization = _optional_time(
        rollout.get("stabilizationDeadline"), ref, "stabilizationDeadline"
    )
    rollback_deadline = _optional_time(
        rollout.get("rollbackDeadline"), ref, "rollbackDeadline"
    )
    stop_reason = rollout.get("stopReason")
    if stop_reason is not None and not isinstance(stop_reason, str):
        _corrupted(ref)
    return {
        "kind": "batch",
        "id": batch_id,
        "status": status,
        "versions": {"target": target_version, "rollback": rollback_version},
        "targetDeviceIds": target_devices,
        "progress": {
            "current": current,
            "batchCount": len(batches),
            "reported": reported,
            "failed": failed,
            "rate": _risk_rate(failed, reported),
        },
        "risk": {"overdue": overdue, "pending": pending, "failed": rollback_failed},
        "deadline": _campaign_deadline(stabilization, rollback_deadline, at_instant),
        "stopReason": stop_reason,
    }


def cmd_fleet_rollout_status(state, args):
    """只读跨子系统风险总览：不建批次、不补超时、不写状态，重复调用结果稳定。"""
    if args.at is None:
        at_instant = datetime.now(timezone.utc)
    else:
        _, at_instant = cli.require_time(args.at)

    devices = state.get("devices")
    releases = state.get("releases")
    rollouts_map = state.get("batchRollouts", {})
    if not isinstance(devices, dict) or not isinstance(releases, dict) \
            or not isinstance(rollouts_map, dict):
        _corrupted("state file")
    _validate_device_table(devices)

    campaigns = []
    for release_id, release in releases.items():
        if not isinstance(release_id, str):
            _corrupted("state file")
        item = _release_campaign(state, release_id, release, at_instant)
        if item["status"] in ACTIVE_RELEASE_STATUSES:
            campaigns.append(item)
    for batch_id, rollout in rollouts_map.items():
        if not isinstance(batch_id, str):
            _corrupted("state file")
        item = _batch_campaign(state, batch_id, rollout, at_instant)
        if item["status"] in ACTIVE_BATCH_STATUSES:
            campaigns.append(item)
    campaigns.sort(key=lambda item: (item["kind"], item["id"]))

    status_counts = {}
    for item in campaigns:
        status_counts[item["status"]] = status_counts.get(item["status"], 0) + 1
    status_counts = {key: status_counts[key] for key in sorted(status_counts)}
    summary = {
        "campaignCount": len(campaigns),
        "statusCounts": status_counts,
        "risk": {
            "overdue": sum(len(item["risk"]["overdue"]) for item in campaigns),
            "pending": sum(len(item["risk"]["pending"]) for item in campaigns),
            "failed": sum(len(item["risk"]["failed"]) for item in campaigns),
        },
    }
    return {
        "at": cli.format_instant(at_instant),
        "summary": summary,
        "campaigns": campaigns,
    }


# ---------------------------------------------------------------------------
# 集中定时收批（fleet check）
# ---------------------------------------------------------------------------

# fleet check 处理的批次状态；paused/pending/completed/rolled_back/rollback_failed
# 只进入 skipped，不接受升级或回滚上报。
FLEET_CHECK_BATCH_STATUSES = (STATUS_IN_PROGRESS, STATUS_FAILED_STOPPED)


def cmd_fleet_check(state, args):
    """集中执行活动发布/批次的定时收批（写命令，一次持久化由调用方保证）。

    --at 必须显式提供，是唯一观察时刻；非法时间返回 InvalidArgument。先按
    releaseId 升序处理 in_progress 的 release，再按 batchId 升序处理
    in_progress/failed_stopped 的 batch rollout；其余状态只进入 skipped。
    入选对象沿用各自 check 的超时补写、稳定观察、失败阈值、自动停止、回滚
    任务与回滚超时规则，at 严格越过截止时刻才推进。对象状态结构非法、设备表
    损坏或时间口径冲突（时间戳无法解析）时返回 InvalidState，不改状态文件。
    旧状态缺 batchRollouts 或字段时按现有缺省读取，不回填。
    """
    _, at_instant = cli.require_time(args.at)

    devices = state.get("devices")
    releases = state.get("releases")
    rollouts_map = state.get("batchRollouts")
    if rollouts_map is None:
        # 旧状态缺 batchRollouts：按空表读取，不回填。
        rollouts_map = {}
    if not isinstance(devices, dict) or not isinstance(releases, dict) \
            or not isinstance(rollouts_map, dict):
        _corrupted("state file")

    # 全部对象处理前先校验：设备表与每个 release/batch 的结构、状态与时间口径
    # （与 fleet rollout-status 同一套只读校验），任一损坏即 InvalidState，
    # 此时尚未发生任何状态变更。
    _validate_device_table(devices)
    for release_id, release in releases.items():
        if not isinstance(release_id, str):
            _corrupted("state file")
        _release_campaign(state, release_id, release, at_instant)
    for batch_id, rollout in rollouts_map.items():
        if not isinstance(batch_id, str):
            _corrupted("state file")
        _batch_campaign(state, batch_id, rollout, at_instant)

    campaigns = []
    skipped = []
    for release_id in sorted(releases):
        release = releases[release_id]
        status = release.get("status")
        if status != STATUS_IN_PROGRESS:
            skipped.append({"kind": "release", "id": release_id, "status": status})
            continue
        expired = cli.run_release_check(state, release, at_instant)
        campaigns.append({
            "kind": "release",
            "id": release_id,
            "status": release["status"],
            "expiredDevices": expired,
            "rollbackExpiredDeviceIds": [],
            "stopReason": release.get("stopReason"),
        })
    for batch_id in sorted(rollouts_map):
        rollout = rollouts_map[batch_id]
        status = rollout.get("status")
        if status not in FLEET_CHECK_BATCH_STATUSES:
            skipped.append({"kind": "batch", "id": batch_id, "status": status})
            continue
        expired, rollback_expired = run_batch_check(state, rollout, at_instant)
        campaigns.append({
            "kind": "batch",
            "id": batch_id,
            "status": rollout["status"],
            "expiredDevices": expired,
            "rollbackExpiredDeviceIds": rollback_expired,
            "stopReason": rollout.get("stopReason"),
        })
    return {
        "at": cli.format_instant(at_instant),
        "campaigns": campaigns,
        "skipped": skipped,
    }


# ---------------------------------------------------------------------------
# 命令行注册（由 cli.build_parser 调用）
# ---------------------------------------------------------------------------

def register_parsers(subparsers, add_state_option):
    firmware = subparsers.add_parser("firmware", help="固件登记（批次灰度用）")
    firmware_sub = firmware.add_subparsers(dest="firmware_command")

    firmware_register = firmware_sub.add_parser("register", help="登记可用固件版本")
    firmware_register.add_argument("--version", required=True)
    firmware_register.set_defaults(handler=cmd_firmware_register, mutating=True)
    add_state_option(firmware_register)

    firmware_list = firmware_sub.add_parser("list", help="列出已登记固件版本")
    firmware_list.set_defaults(handler=cmd_firmware_list, mutating=False)
    add_state_option(firmware_list)

    batch = subparsers.add_parser("batch", help="批次灰度推进与自动故障回滚")
    batch_sub = batch.add_subparsers(dest="batch_command")

    batch_create = batch_sub.add_parser("create", help="创建批次发布")
    batch_create.add_argument("--batch-id", required=True)
    batch_create.add_argument("--target-version", required=True)
    batch_create.add_argument("--stable-version", required=True)
    batch_create.add_argument("--batch-size", required=True)
    batch_create.add_argument("--failure-threshold", required=True,
                              help="可继续推进的失败率阈值，严格大于 0 且小于 1 的小数（如 0.5）")
    batch_create.add_argument("--heartbeat-timeout-seconds", required=True,
                              help="心跳超时时长（秒），大于 0 的整数")
    batch_create.add_argument("--stabilization-seconds", default=None,
                              help="当前批终态集齐后的稳定观察秒数，大于等于 0 的整数，缺省 0；"
                                   "与 release create 的同名参数互不影响")
    batch_create.add_argument("--rollback-timeout-seconds", default=None,
                              help="回滚收束时限（秒），大于等于 1 的整数，缺省 900；"
                                   "停止或中止派发回滚后按停止时刻加该秒数生成 rollbackDeadline")
    batch_create.add_argument("--target-device-id", action="append",
                              default=argparse.SUPPRESS,
                              help="目标设备，可重复；集合必须非空")
    batch_create.add_argument("--canary-device-id", action="append",
                              default=argparse.SUPPRESS,
                              help="金丝雀设备，可重复；须属于 target-device-id 集合、"
                                   "不重复且数量不超过 batch-size，按 device-id 升序进入第一批")
    batch_create.add_argument("--approval-required", action="store_true",
                              default=False,
                              help="开启人工放行门禁：每批到达可推进边界且仍有下一批时"
                                   "进入等待，须 batch approve 放行后才打开下一批")
    batch_create.set_defaults(handler=cmd_batch_create, mutating=True)
    add_state_option(batch_create)

    batch_update = batch_sub.add_parser(
        "update", help="放量前调整 pending 批次的灰度策略与设备集合（不建批次）")
    batch_update.add_argument("--batch-id", required=True)
    batch_update.add_argument("--at", required=True, help="更新时刻（ISO 8601）")
    batch_update.add_argument("--target-device-id", action="append",
                              default=argparse.SUPPRESS,
                              help="目标设备，可重复；传入任一即以去重后的完整集合"
                                   "替换原目标，集合必须非空且设备已登记")
    batch_update.add_argument("--canary-device-id", action="append",
                              default=argparse.SUPPRESS,
                              help="金丝雀设备，可重复；传入任一即替换原金丝雀，"
                                   "须属于更新后的目标集合且数量不超过 batch-size")
    batch_update.add_argument("--clear-canary-device-ids", action="store_true",
                              default=False,
                              help="清空金丝雀设备；不能与 --canary-device-id 同用")
    batch_update.add_argument("--batch-size", default=None,
                              help="每批数量，正整数；缺省保留原值")
    batch_update.add_argument("--failure-threshold", default=None,
                              help="失败率阈值，严格大于 0 且小于 1 的小数；缺省保留原值")
    batch_update.add_argument("--heartbeat-timeout-seconds", default=None,
                              help="心跳超时时长（秒），大于 0 的整数；缺省保留原值")
    batch_update.add_argument("--stabilization-seconds", default=None,
                              help="稳定观察秒数，大于等于 0 的整数；缺省保留原值")
    batch_update.add_argument("--rollback-timeout-seconds", default=None,
                              help="回滚收束时限（秒），大于等于 1 的整数；缺省保留原值")
    batch_update.add_argument("--approval-required", action="store_true",
                              default=False,
                              help="开启人工放行门禁；不能与 --no-approval-required 同用")
    batch_update.add_argument("--no-approval-required", action="store_true",
                              default=False,
                              help="关闭人工放行门禁；不能与 --approval-required 同用")
    batch_update.set_defaults(handler=cmd_batch_update, mutating=True)
    add_state_option(batch_update)

    batch_plan = batch_sub.add_parser("plan", help="只读预检 pending 发布的启动资格与分批计划")
    batch_plan.add_argument("--batch-id", required=True)
    batch_plan.add_argument("--at", default=None,
                            help="预检时刻（ISO 8601），仅做合法性校验，缺省取当前 UTC")
    batch_plan.set_defaults(handler=cmd_batch_plan, mutating=False)
    add_state_option(batch_plan)

    batch_start = batch_sub.add_parser("start", help="开始放量，仅把第一批置为待升级")
    batch_start.add_argument("--batch-id", required=True)
    batch_start.add_argument("--at", default=None, help="放量开始时刻（ISO 8601），缺省取当前 UTC")
    batch_start.set_defaults(handler=cmd_batch_start, mutating=True)
    add_state_option(batch_start)

    batch_report = batch_sub.add_parser("report", help="设备回传心跳、当前固件版本与升级终态")
    batch_report.add_argument("--batch-id", required=True)
    batch_report.add_argument("--device-id", required=True)
    batch_report.add_argument("--version", required=True)
    batch_report.add_argument("--heartbeat-at", required=True)
    batch_report.add_argument("--result", choices=cli.VALID_RESULTS, default=None,
                              help="升级终态；省略表示仅心跳（设备升级中）")
    batch_report.set_defaults(handler=cmd_batch_report, mutating=True)
    add_state_option(batch_report)

    batch_check = batch_sub.add_parser("check", help="按给定时刻收批心跳超时设备并决定推进/停止")
    batch_check.add_argument("--batch-id", required=True)
    batch_check.add_argument("--at", required=True)
    batch_check.set_defaults(handler=cmd_batch_check, mutating=True)
    add_state_option(batch_check)

    batch_pause = batch_sub.add_parser(
        "pause",
        help="手动暂停进行中的批次：不再派发尚未开始的设备，当前批已开始设备仍可完成")
    batch_pause.add_argument("--batch-id", required=True)
    batch_pause.add_argument("--at", required=True, help="暂停时刻（ISO 8601）")
    batch_pause.set_defaults(handler=cmd_batch_pause, mutating=True)
    add_state_option(batch_pause)

    batch_resume = batch_sub.add_parser(
        "resume",
        help="恢复已暂停的批次：从尚未开始的设备继续，已成功设备不重复处理")
    batch_resume.add_argument("--batch-id", required=True)
    batch_resume.add_argument("--at", required=True, help="恢复时刻（ISO 8601）")
    batch_resume.set_defaults(handler=cmd_batch_resume, mutating=True)
    add_state_option(batch_resume)

    batch_approve = batch_sub.add_parser(
        "approve",
        help="人工放行：批准等待中的批次打开下一批（仅 awaitingApproval 时生效）")
    batch_approve.add_argument("--batch-id", required=True)
    batch_approve.add_argument("--at", required=True, help="放行时刻（ISO 8601）")
    batch_approve.set_defaults(handler=cmd_batch_approve, mutating=True)
    add_state_option(batch_approve)

    batch_abort = batch_sub.add_parser("abort", help="人工止损：冻结后续批次并回滚已成功设备")
    batch_abort.add_argument("--batch-id", required=True)
    batch_abort.add_argument("--reason", required=True,
                             help="人工止损原因，去除首尾空白后 1 到 200 个 Unicode 字符")
    batch_abort.add_argument("--at", required=True, help="止损时刻（ISO 8601）")
    batch_abort.set_defaults(handler=cmd_batch_abort, mutating=True)
    add_state_option(batch_abort)

    batch_rollback = batch_sub.add_parser(
        "rollback-report", help="回传回滚到稳定版本任务的结果")
    batch_rollback.add_argument("--batch-id", required=True)
    batch_rollback.add_argument("--device-id", required=True)
    batch_rollback.add_argument("--result", required=True, choices=cli.VALID_RESULTS)
    batch_rollback.add_argument("--heartbeat-at", required=True)
    batch_rollback.add_argument("--version", default=None,
                                help="回传时刻设备当前固件版本（可选）")
    batch_rollback.set_defaults(handler=cmd_batch_rollback_report, mutating=True)
    add_state_option(batch_rollback)

    batch_retry = batch_sub.add_parser(
        "rollback-retry", help="对 rollback_failed 批次中回滚失败/超时的设备再试一次回滚")
    batch_retry.add_argument("--batch-id", required=True)
    batch_retry.add_argument("--device-id", action="append", default=None,
                             help="要重试回滚的设备，可重复；至少一个、不得重复，按升序处理")
    batch_retry.add_argument("--reason", required=True,
                             help="重试原因，去除首尾空白后 1 到 200 个 Unicode 字符")
    batch_retry.add_argument("--at", required=True, help="重试时刻（ISO 8601）")
    batch_retry.set_defaults(handler=cmd_batch_rollback_retry, mutating=True)
    add_state_option(batch_retry)

    batch_status = batch_sub.add_parser("status", help="查询批次灰度状态（只读）")
    batch_status.add_argument("--batch-id", required=True)
    batch_status.add_argument("--at", default=None,
                              help="观察时刻（ISO 8601），缺省取当前 UTC；用于区分升级中/等待心跳")
    batch_status.set_defaults(handler=cmd_batch_status, mutating=False)
    add_state_option(batch_status)

    batch_timeline = batch_sub.add_parser("timeline", help="查询批次审计时间线（只读）")
    batch_timeline.add_argument("--batch-id", required=True)
    batch_timeline.add_argument("--after-sequence", default=None,
                                help="只返回 sequence 严格大于该值的事件，非负整数（默认 0）")
    batch_timeline.add_argument("--limit", default=None,
                                help="返回最早的事件条数，1 到 1000 的整数（默认 200）")
    batch_timeline.set_defaults(handler=cmd_batch_timeline, mutating=False)
    add_state_option(batch_timeline)
