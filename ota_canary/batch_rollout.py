"""批次灰度推进与自动故障回滚子系统。

本模块在既有产品边界上增量实现一套可独立运行的"批次发布（batch rollout）"能力：

- 沿用同一状态文件中的设备表（``devices``）作为公开设备/固件入口，新增
  ``firmwares`` 与 ``batchRollouts`` 两个状态键，不触碰 ``releases`` 的任何语义；
- 新增 ``firmware register|list`` 与 ``batch create|plan|start|report|check|
  rollback-report|abort|status|timeline`` 命令，不改变既有命令的请求/响应；
- ``batch plan`` 对 pending 发布做只读启动资格预检，``batch start`` 复用同一口径：
  仅当前版本等于 stableVersion 且未被其他 in_progress/failed_stopped 发布占用的设备
  合格，任一不合格即拒绝启动，全部合格时仅第一批置为 pending_upgrade；
- 批次逐批放量，设备通过公开入口回传心跳、当前固件版本与升级终态；
- 本批失败率严格大于阈值时自动 FAILED_STOPPED、冻结后续批次，并向已成功升级的
  设备下发回滚到稳定版本的任务，逐台记录 PENDING/成功/失败，最终 ROLLED_BACK
  或 ROLLBACK_FAILED；
- 人工止损 ``batch abort`` 仅在 IN_PROGRESS 时可用：立即冻结后续批次、拒绝新的
  升级报告，并向已成功设备下发同样的回滚任务，收束口径与自动停止一致。
- 仅追加的审计时间线记录批次创建、放量、上报、超时、推进、停止、止损、回滚与
  收束事件，严格递增且永不改写历史；``batch timeline`` 为只读分页查询入口。

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


# 批次发布状态
STATUS_PENDING = "pending"
STATUS_IN_PROGRESS = "in_progress"
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

# 占用设备的发布状态：仅 in_progress 与 failed_stopped 占用；
# completed、rolled_back、rollback_failed 均释放。
OCCUPYING_STATUSES = (STATUS_IN_PROGRESS, STATUS_FAILED_STOPPED)


# ---------------------------------------------------------------------------
# 审计时间线事件类型（仅追加、严格递增、永不改写历史）
# ---------------------------------------------------------------------------

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
EVENT_FINISHED = "finished"

# 停止事件 reason
STOP_REASON_FAILURE_THRESHOLD = "failure_threshold"
STOP_REASON_MANUAL_ABORT = "manual_abort"

# 完成事件 reason
FINISH_REASON_COMPLETED = "completed"
FINISH_REASON_ROLLED_BACK = "rolled_back"
FINISH_REASON_ROLLBACK_FAILED = "rollback_failed"


def events_of(rollout):
    bucket = rollout.get("timeline")
    if not isinstance(bucket, list):
        bucket = []
        rollout["timeline"] = bucket
    return bucket


def append_event(rollout, event_type, at_instant, batch_index=None, device_id=None,
                 result=None, phase_from=None, phase_to=None, reason=None):
    """在审计时间线末尾追加一个严格递增事件；历史事件永不修改。"""
    timeline = events_of(rollout)
    event = {
        "sequence": len(timeline) + 1,
        "type": event_type,
        "occurredAt": cli.format_instant(at_instant),
        "batchIndex": batch_index,
        "deviceId": device_id,
        "result": result,
        "phaseFrom": phase_from,
        "phaseTo": phase_to,
        "reason": reason,
    }
    timeline.append(event)
    return event


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

    # 4) 每批数量为正整数、失败率阈值严格 (0,1)、心跳超时大于 0
    batch_size = require_policy_batch_size(args.batch_size)
    _, threshold = require_failure_threshold(args.failure_threshold)
    heartbeat_timeout = require_policy_timeout(args.heartbeat_timeout_seconds)

    # 其余沿用既有入口约定：设备必须存在
    for device_id in device_ids:
        cli.get_device(state, device_id)

    rollout = {
        "batchId": batch_id,
        "targetVersion": target_version,
        "stableVersion": stable_version,
        "batchSize": batch_size,
        "failureThreshold": str(threshold),
        "heartbeatTimeoutSeconds": heartbeat_timeout,
        "targetDeviceIds": device_ids,
        "status": STATUS_PENDING,
        "frozen": False,
        "stopReason": None,
        "abortReason": None,
        "abortedAt": None,
        "batches": [],
        "currentBatch": None,
        "currentBatchStartedAt": None,
        "devices": {},
        "timeline": [],
    }
    rollouts(state)[batch_id] = rollout
    append_event(rollout, EVENT_CREATED, datetime.now(timezone.utc))
    return batch_view(state, rollout, at_instant=None)


# ---------------------------------------------------------------------------
# 启动前资格预检（batch plan 与 batch start 共用同一只读口径）
# ---------------------------------------------------------------------------

def occupied_device_ids(state, rollout):
    """其他仍占用设备的发布：仅 in_progress 与 failed_stopped。

    completed、rolled_back、rollback_failed 释放占用；pending 发布尚未建批，
    也不占用。占用集合取占用发布创建时确定的去重目标设备集。
    """
    occupied = set()
    for other in rollouts(state).values():
        if other is rollout:
            continue
        if other.get("status") not in OCCUPYING_STATUSES:
            continue
        occupied.update(other.get("targetDeviceIds", ()))
    return occupied


def evaluate_eligibility(state, rollout):
    """计算目标设备的启动资格（只读）。

    返回 (target_ids, eligible_ids, ineligible, batches)：
    - target_ids 为创建时去重集合，按 device-id 升序；
    - 当前版本等于 stableVersion 且未被其他 in_progress/failed_stopped 发布占用者合格；
    - 版本不符与占用兼一时 VERSION_MISMATCH 优先；
    - batches 只含合格设备并按 batchSize 切分，无合格设备时为 []。
    """
    target_ids = sorted(rollout["targetDeviceIds"])
    # 显式目标先确认仍然存在（DeviceNotFound），再判版本与占用资格。
    for device_id in target_ids:
        cli.get_device(state, device_id)
    stable_version = rollout["stableVersion"]
    busy = occupied_device_ids(state, rollout)
    eligible_ids = []
    ineligible = []
    for device_id in target_ids:
        device = state["devices"].get(device_id)
        version = device.get("version") if isinstance(device, dict) else None
        if version != stable_version:
            ineligible.append({"deviceId": device_id, "reason": REASON_VERSION_MISMATCH})
        elif device_id in busy:
            ineligible.append({"deviceId": device_id, "reason": REASON_DEVICE_BUSY})
        else:
            eligible_ids.append(device_id)
    size = rollout["batchSize"]
    batches = [eligible_ids[i:i + size] for i in range(0, len(eligible_ids), size)]
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
    # 与 plan 完全相同的资格、排序与分批口径：任一目标不合格即拒绝启动，
    # 不创建批次或设备阶段，不发生任何状态写入。
    ids, eligible_ids, ineligible, planned_batches = evaluate_eligibility(state, rollout)
    if ineligible:
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
    rollout["status"] = STATUS_IN_PROGRESS
    append_event(rollout, EVENT_STARTED, at_instant)
    promote_batch(rollout, 0, at_instant)
    return batch_view(state, rollout, at_instant=at_instant)


def promote_batch(rollout, index, at_instant):
    """把第 index 批设备置为待升级；每次只推进一批。"""
    rollout["currentBatch"] = index
    rollout["currentBatchStartedAt"] = cli.format_instant(at_instant)
    for device_id in rollout["batches"][index]:
        entry = rollout["devices"][device_id]
        entry["phase"] = PHASE_PENDING_UPGRADE
    append_event(rollout, EVENT_BATCH_OPENED, at_instant, batch_index=index)


# ---------------------------------------------------------------------------
# 设备回传：心跳时间、当前固件版本、升级终态
# ---------------------------------------------------------------------------

def heartbeat_anchor(rollout, entry):
    """等待终态期间的计时锚点：最近一次有效心跳；尚未收到则取本批开始时刻。"""
    text = entry.get("lastHeartbeatAt") or rollout.get("currentBatchStartedAt")
    return cli.parse_time(text) if text else None


def record_heartbeat(state, rollout, device_id, version, heartbeat_text, at_instant):
    """登记一次有效心跳：同步公开设备入口的版本/心跳，并推进待升级->升级中。"""
    device = state["devices"].get(device_id)
    if device is not None:
        device["heartbeatAt"] = heartbeat_text
        device["version"] = version
    entry = rollout["devices"].get(device_id)
    if entry is not None:
        entry["lastHeartbeatAt"] = heartbeat_text
        entry["lastVersion"] = version
        if entry["phase"] == PHASE_PENDING_UPGRADE:
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

    # 自动停止后冻结：后续批次及未取得终态的设备不再接收升级任务。
    if rollout["status"] != STATUS_IN_PROGRESS:
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
        # 仅心跳：保持升级中，等待终态。阶段发生推进则记录相位变化，否则相位不涉及。
        phase_after = entry["phase"]
        if phase_after != phase_before:
            append_event(rollout, EVENT_UPGRADE_REPORTED, at_instant,
                         batch_index=entry["batchIndex"], device_id=device_id,
                         phase_from=phase_before, phase_to=phase_after)
        else:
            append_event(rollout, EVENT_UPGRADE_REPORTED, at_instant,
                         batch_index=entry["batchIndex"], device_id=device_id)
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
                 batch_index=entry["batchIndex"], device_id=device_id,
                 result=result, phase_from=phase_before, phase_to=entry["phase"])
    finalize_batch_if_ready(state, rollout, at_instant)
    return batch_view(state, rollout, at_instant=at_instant, reported_device=device_id,
                      result=result, heartbeat_at=heartbeat_text, version=version)


def cmd_batch_check(state, args):
    batch_id = cli.require_id(args.batch_id, "batch-id")
    rollout = get_rollout(state, batch_id)
    _, at_instant = cli.require_time(args.at)
    if rollout["status"] == STATUS_FAILED_STOPPED:
        # 无回滚任务的停止态在显式收批入口再次到达时收束为 rolled_back；
        # 仍有 pending 回滚任务时保持 failed_stopped，等待 rollback-report。
        finalize_rollback_if_done(rollout, at_instant, converge_empty=True)
        view = batch_view(state, rollout, at_instant=at_instant)
        view["expiredDevices"] = []
        return view
    if rollout["status"] != STATUS_IN_PROGRESS:
        raise cli.InvalidState(
            "batch %s is not in progress (status: %s)" % (batch_id, rollout["status"])
        )
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
        if at_instant > anchor + timeout:
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
                         phase_from=phase_before, phase_to=PHASE_FAILED)
            expired.append(device_id)
    expired.sort()
    finalize_batch_if_ready(state, rollout, at_instant)
    view = batch_view(state, rollout, at_instant=at_instant)
    view["expiredDevices"] = expired
    return view


def finalize_batch_if_ready(state, rollout, at_instant):
    """本批全部取得终态后：判失败率 -> 自动停止回滚 / 推进下一批 / 完成。"""
    current = rollout["currentBatch"]
    group = rollout["batches"][current]
    entries = rollout["devices"]
    if any(entries[d]["terminal"] is None for d in group):
        return
    failed = sum(1 for d in group if entries[d]["terminal"]["result"] == "failure")
    rate = Fraction(failed, len(group))
    threshold = Fraction(Decimal(rollout["failureThreshold"]))
    if rate > threshold:
        trigger_failure_stop(state, rollout, at_instant)
        return
    if current + 1 == len(rollout["batches"]):
        rollout["status"] = STATUS_COMPLETED
        rollout["frozen"] = False
        rollout["currentBatchStartedAt"] = None
        append_event(rollout, EVENT_FINISHED, at_instant, reason=FINISH_REASON_COMPLETED)
    else:
        append_event(rollout, EVENT_BATCH_ADVANCED, at_instant, batch_index=current)
        promote_batch(rollout, current + 1, at_instant)


def freeze_and_dispatch_rollbacks(rollout, stop_reason, audit_reason, at_instant,
                                  aborted_reason=None):
    """冻结后续批次并向已成功设备下发回滚到 stableVersion 的任务。

    失败、超时、排队和未取得终态的设备不生成任务；设备阶段与终态报告保留现场。
    审计上先追加 stopped（自动失败阈值/人工止损同一状态迁移）；人工止损时紧接
    追加 aborted，再按 (batchIndex, deviceId) 升序逐台追加 rollback_started，
    历史不改写。
    """
    rollout["status"] = STATUS_FAILED_STOPPED
    rollout["frozen"] = True
    rollout["stopReason"] = stop_reason
    rollout["currentBatchStartedAt"] = None
    append_event(rollout, EVENT_STOPPED, at_instant, reason=audit_reason)
    if aborted_reason is not None:
        append_event(rollout, EVENT_ABORTED, at_instant, reason=aborted_reason)
    success_devices = sorted(
        ((entry["batchIndex"], device_id)
         for device_id, entry in rollout["devices"].items()
         if entry["phase"] == PHASE_SUCCESS)
    )
    for _, device_id in success_devices:
        entry = rollout["devices"][device_id]
        entry["phase"] = PHASE_ROLLING_BACK
        entry["rollback"] = {"state": "pending", "heartbeatAt": None, "version": None}
        append_event(rollout, EVENT_ROLLBACK_STARTED, at_instant,
                     batch_index=entry["batchIndex"], device_id=device_id,
                     phase_from=PHASE_SUCCESS, phase_to=PHASE_ROLLING_BACK)


def trigger_failure_stop(state, rollout, at_instant):
    """失败率超阈值：FAILED_STOPPED、冻结后续批次、向已成功设备下发回滚任务。

    立即置为 failed_stopped；若存在回滚任务，由各设备 rollback-report 收束为
    rolled_back/rollback_failed；若没有需要回滚的设备，保持 failed_stopped 直到
    下一次 batch check（显式收批入口）收束为 rolled_back，保证停止状态外部可见。
    """
    freeze_and_dispatch_rollbacks(
        rollout, "batch_failure_threshold", STOP_REASON_FAILURE_THRESHOLD, at_instant
    )


def cmd_batch_abort(state, args):
    """人工止损：仅进行中的批次可中止，固定 manual_abort。

    所有参数与状态校验（含批次存在性与当前状态）通过前不写入任何状态。
    """
    batch_id = cli.require_id(args.batch_id, "batch-id")
    reason = cli.require_abort_reason(args.reason)
    _, at_instant = cli.require_time(args.at)
    rollout = get_rollout(state, batch_id)
    if rollout["status"] != STATUS_IN_PROGRESS:
        raise cli.InvalidState(
            "batch %s cannot be aborted (status: %s)" % (batch_id, rollout["status"])
        )
    freeze_and_dispatch_rollbacks(
        rollout, "manual_abort", STOP_REASON_MANUAL_ABORT, at_instant,
        aborted_reason=reason,
    )
    rollout["abortReason"] = reason
    rollout["abortedAt"] = cli.format_instant(at_instant)
    # 尚有回滚任务时先保持 failed_stopped，由 rollback-report 收束；无任务时直接
    # 收束为 rolled_back（abort 响应本身已保证停止状态外部可见）。
    finalize_rollback_if_done(rollout, at_instant, converge_empty=True)
    return batch_view(state, rollout, at_instant=at_instant)


def finalize_rollback_if_done(rollout, at_instant=None, converge_empty=False):
    """回滚任务全部收束后决定最终状态。

    converge_empty 为 True（显式收批入口）时，没有任何回滚任务也收束为
    rolled_back；否则空集合保留 failed_stopped，保证停止状态外部可见。
    状态迁移到 rolled_back/rollback_failed 时追加 finished；重复收束不追加。
    """
    if rollout["status"] != STATUS_FAILED_STOPPED:
        return
    records = [e["rollback"] for e in rollout["devices"].values() if e["rollback"] is not None]
    if not records:
        if converge_empty:
            rollout["status"] = STATUS_ROLLED_BACK
            if at_instant is not None:
                append_event(rollout, EVENT_FINISHED, at_instant,
                             reason=FINISH_REASON_ROLLED_BACK)
        return
    if any(r["state"] == "pending" for r in records):
        return
    new_status = (
        STATUS_ROLLBACK_FAILED
        if any(r["state"] == "failure" for r in records)
        else STATUS_ROLLED_BACK
    )
    rollout["status"] = new_status
    if at_instant is not None:
        append_event(
            rollout, EVENT_FINISHED, at_instant,
            reason=(FINISH_REASON_ROLLBACK_FAILED
                    if new_status == STATUS_ROLLBACK_FAILED
                    else FINISH_REASON_ROLLED_BACK),
        )


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

    new_phase = (
        PHASE_ROLLBACK_SUCCEEDED if result == "success" else PHASE_ROLLBACK_FAILED
    )
    record["state"] = result
    record["heartbeatAt"] = heartbeat_text
    record["version"] = version
    device = state["devices"].get(device_id)
    if device is not None:
        device["heartbeatAt"] = heartbeat_text
        if result == "success":
            # 回滚成功：设备恢复稳定版本；回滚失败保留现场版本。
            device["version"] = rollout["stableVersion"]
        elif version is not None:
            device["version"] = version
    entry["phase"] = new_phase
    append_event(rollout, EVENT_ROLLBACK_REPORTED, at_instant,
                 batch_index=entry["batchIndex"], device_id=device_id, result=result,
                 phase_from=PHASE_ROLLING_BACK, phase_to=new_phase)

    finalize_rollback_if_done(rollout, at_instant)

    view = batch_view(state, rollout, at_instant=at_instant)
    view["report"] = {"deviceId": device_id, "result": result,
                      "heartbeatAt": heartbeat_text, "idempotent": False}
    return view


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
    if anchor is not None and at_instant > anchor + timeout:
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
    current_index = rollout["currentBatch"]

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
        if entry["rollback"] is not None:
            rollback_total += 1
            state_name = entry["rollback"]["state"]
            if state_name == "pending":
                rollback_pending += 1
            elif state_name == "success":
                rollback_succeeded += 1
            else:
                rollback_failed += 1
        device = state["devices"].get(device_id, {})
        devices.append({
            "deviceId": device_id,
            "batchIndex": entry["batchIndex"],
            "phase": phase,
            "version": device.get("version"),
            "heartbeatAt": device.get("heartbeatAt"),
            "lastHeartbeatAt": entry.get("lastHeartbeatAt"),
            "terminal": terminal,
            "rollback": entry.get("rollback"),
        })

    view = {
        "batchId": rollout["batchId"],
        "targetVersion": rollout["targetVersion"],
        "stableVersion": rollout["stableVersion"],
        "batchSize": rollout["batchSize"],
        "failureThreshold": float(Decimal(rollout["failureThreshold"])),
        "heartbeatTimeoutSeconds": rollout["heartbeatTimeoutSeconds"],
        "targetDeviceIds": list(rollout["targetDeviceIds"]),
        "status": rollout["status"],
        "frozen": rollout["frozen"],
        "stopReason": rollout.get("stopReason"),
        "abortReason": rollout.get("abortReason"),
        "abortedAt": rollout.get("abortedAt"),
        "currentBatch": rollout["currentBatch"],
        "currentBatchStartedAt": rollout.get("currentBatchStartedAt"),
        "batchCount": len(rollout["batches"]),
        "batches": rollout["batches"],
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


def require_after_sequence(value):
    try:
        after = int(str(value).strip())
    except (TypeError, ValueError):
        raise cli.InvalidArgument("after-sequence must be a non-negative integer: %r" % (value,))
    if after < 0:
        raise cli.InvalidArgument("after-sequence must be non-negative: %r" % (value,))
    return after


def require_timeline_limit(value):
    try:
        limit = int(str(value).strip())
    except (TypeError, ValueError):
        raise cli.InvalidArgument("limit must be an integer between 1 and 1000: %r" % (value,))
    if limit < 1 or limit > 1000:
        raise cli.InvalidArgument("limit must be between 1 and 1000: %r" % (value,))
    return limit


def cmd_batch_timeline(state, args):
    """只读审计时间线：严格递增、只追加的事件页，不补写、不改写历史。"""
    batch_id = cli.require_id(args.batch_id, "batch-id")
    after = require_after_sequence(args.after_sequence)
    limit = require_timeline_limit(args.limit)
    rollout = get_rollout(state, batch_id)  # 不存在沿用 DeviceNotFound
    timeline = rollout.get("timeline")
    if not isinstance(timeline, list):
        # 旧状态没有时间线：不补历史，返回空页。
        timeline = []
    page = [event for event in timeline if event["sequence"] > after][:limit]
    next_sequence = page[-1]["sequence"] + 1 if page else 1
    return {
        "batchId": rollout["batchId"],
        "nextSequence": next_sequence,
        "events": page,
    }


# ---------------------------------------------------------------------------
# 与公开心跳入口（device heartbeat）的桥接
# ---------------------------------------------------------------------------

def feed_public_heartbeat(state, device_id, version, heartbeat_text):
    """公开入口 ``device heartbeat`` 的心跳同时喂给包含该设备的进行中批次。

    严格旁路：任何异常都不影响既有命令的语义与成功结果。
    """
    for rollout in rollouts(state).values():
        if rollout.get("status") != STATUS_IN_PROGRESS:
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
    batch_create.add_argument("--target-device-id", action="append",
                              default=argparse.SUPPRESS,
                              help="目标设备，可重复；集合必须非空")
    batch_create.set_defaults(handler=cmd_batch_create, mutating=True)
    add_state_option(batch_create)

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

    batch_status = batch_sub.add_parser("status", help="查询批次灰度状态（只读）")
    batch_status.add_argument("--batch-id", required=True)
    batch_status.add_argument("--at", default=None,
                              help="观察时刻（ISO 8601），缺省取当前 UTC；用于区分升级中/等待心跳")
    batch_status.set_defaults(handler=cmd_batch_status, mutating=False)
    add_state_option(batch_status)

    batch_timeline = batch_sub.add_parser("timeline", help="查询批次审计时间线（只读）")
    batch_timeline.add_argument("--batch-id", required=True)
    batch_timeline.add_argument("--after-sequence", default=0,
                                help="只返回 sequence 严格大于该值的事件；默认 0，须为非负整数")
    batch_timeline.add_argument("--limit", default=200,
                                help="最多返回的事件条数（最早者优先），1 到 1000 的整数；默认 200")
    batch_timeline.set_defaults(handler=cmd_batch_timeline, mutating=False)
    add_state_option(batch_timeline)
