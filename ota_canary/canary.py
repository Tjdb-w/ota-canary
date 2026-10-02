"""批次灰度推进与自动故障回滚（canary）。

与既有 release 流程相互独立的一套能力，复用公开的设备登记 / 心跳 / 结果上报入口，
不改变既有请求响应语义。状态与既有数据共用同一状态文件，落在独立的 ``canaryFirmwares``
与 ``canaryBatches`` 命名空间中。

批次状态机：
    QUEUED（创建）-> RUNNING（start，逐批放量）
    RUNNING -> COMPLETED（全部批次成功）
    RUNNING -> FAILED_STOPPED（本批失败率严格大于阈值；后续批次冻结）
    FAILED_STOPPED -> ROLLED_BACK / ROLLBACK_FAILED（回滚任务全部终态后）

设备阶段：
    queued / pending_upgrade / upgrading / waiting_heartbeat / success / failed
    / rolling_back / rollback_succeeded / rollback_failed
"""

from __future__ import annotations

import copy
from datetime import timedelta

from .cli import (
    DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
    DeviceNotFound,
    InvalidArgument,
    InvalidState,
    OtaError,
    format_instant,
    parse_time,
)


# ---------------------------------------------------------------------------
# 错误类型：业务错误仍以 JSON 形式输出到 stderr，退出码为 1
# ---------------------------------------------------------------------------


class BatchConflict(OtaError):
    code = "BatchConflict"


class FirmwareNotFound(OtaError):
    code = "FirmwareNotFound"


class InvalidBatchPolicy(OtaError):
    code = "InvalidBatchPolicy"


class EmptyDeviceSet(OtaError):
    code = "EmptyDeviceSet"


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

BATCH_STATUS_QUEUED = "QUEUED"
BATCH_STATUS_RUNNING = "RUNNING"
BATCH_STATUS_FAILED_STOPPED = "FAILED_STOPPED"
BATCH_STATUS_COMPLETED = "COMPLETED"
BATCH_STATUS_ROLLED_BACK = "ROLLED_BACK"
BATCH_STATUS_ROLLBACK_FAILED = "ROLLBACK_FAILED"

TERMINAL_BATCH_STATUSES = (
    BATCH_STATUS_COMPLETED,
    BATCH_STATUS_ROLLED_BACK,
    BATCH_STATUS_ROLLBACK_FAILED,
)

STAGE_QUEUED = "queued"
STAGE_PENDING = "pending_upgrade"
STAGE_UPGRADING = "upgrading"
STAGE_WAITING = "waiting_heartbeat"
STAGE_SUCCESS = "success"
STAGE_FAILED = "failed"
STAGE_ROLLING_BACK = "rolling_back"
STAGE_ROLLBACK_SUCCEEDED = "rollback_succeeded"
STAGE_ROLLBACK_FAILED = "rollback_failed"

FINAL_STAGES = (
    STAGE_SUCCESS,
    STAGE_FAILED,
    STAGE_ROLLING_BACK,
    STAGE_ROLLBACK_SUCCEEDED,
    STAGE_ROLLBACK_FAILED,
)
# 曾成功完成升级的阶段：success 本身，以及停止后转入的各回滚阶段。
UPGRADE_SUCCEEDED_STAGES = (
    STAGE_SUCCESS,
    STAGE_ROLLING_BACK,
    STAGE_ROLLBACK_SUCCEEDED,
    STAGE_ROLLBACK_FAILED,
)

PHASE_UPGRADE = "upgrade"
PHASE_ROLLBACK = "rollback"
VALID_PHASES = (PHASE_UPGRADE, PHASE_ROLLBACK)


# ---------------------------------------------------------------------------
# 状态辅助
# ---------------------------------------------------------------------------


def ensure_namespace(state):
    if not isinstance(state.get("canaryFirmwares"), dict):
        state["canaryFirmwares"] = {}
    if not isinstance(state.get("canaryBatches"), dict):
        state["canaryBatches"] = {}
    return state


def get_batch(state, batch_id):
    batch = state.get("canaryBatches", {}).get(batch_id)
    if batch is None:
        raise DeviceNotFound("canary batch not found: %s" % batch_id)
    return batch


def get_firmware(state, version):
    firmware = state.get("canaryFirmwares", {}).get(version)
    if firmware is None:
        raise FirmwareNotFound("firmware version not found: %s" % version)
    return firmware


def policy_error(message):
    return InvalidBatchPolicy(message)


def validate_policy(
    state,
    batch_id,
    target_version,
    stable_version,
    target_device_ids,
    batch_size,
    failure_threshold,
    heartbeat_timeout_seconds,
):
    """创建批次的全部前置校验。

    校验顺序固定：编号唯一（BatchConflict）-> 固件存在（FirmwareNotFound）
    -> 设备集合非空（EmptyDeviceSet）-> 策略取值（InvalidBatchPolicy）。
    目标固件与稳定版本都存在且不同、每批数量为正整数、失败率阈值严格在 (0, 1) 内、
    心跳超时时长大于 0，任一不满足都归入指定的唯一异常。
    """
    if batch_id in state.get("canaryBatches", {}):
        raise BatchConflict("canary batch already exists: %s" % batch_id)
    get_firmware(state, target_version)
    get_firmware(state, stable_version)
    if target_version == stable_version:
        raise FirmwareNotFound(
            "target version must differ from stable version: %s" % target_version
        )
    if not target_device_ids:
        raise EmptyDeviceSet("target device set must be non-empty")
    if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size < 1:
        raise policy_error("batch-size must be a positive integer: %r" % (batch_size,))
    if (
        not isinstance(failure_threshold, (int, float))
        or isinstance(failure_threshold, bool)
        or failure_threshold <= 0
        or failure_threshold >= 1
    ):
        raise policy_error(
            "failure-threshold must be strictly greater than 0 and less than 1: %r"
            % (failure_threshold,)
        )
    if (
        not isinstance(heartbeat_timeout_seconds, int)
        or isinstance(heartbeat_timeout_seconds, bool)
        or heartbeat_timeout_seconds <= 0
    ):
        raise policy_error(
            "heartbeat-timeout-seconds must be greater than 0: %r"
            % (heartbeat_timeout_seconds,)
        )


# ---------------------------------------------------------------------------
# 命令：固件与批次创建
# ---------------------------------------------------------------------------


def cmd_firmware_add(state, args):
    from .cli import require_id, require_version

    version = require_version(args.version)
    if version in state.get("canaryFirmwares", {}):
        raise BatchConflict("firmware version already registered: %s" % version)
    ensure_namespace(state)
    record = {"version": version}
    state["canaryFirmwares"][version] = record
    return dict(record)


def normalize_failure_threshold(value):
    """解析失败率阈值；非数值或越界返回 InvalidBatchPolicy。"""
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        raise policy_error("failure-threshold must be a number in (0, 1): %r" % (value,))
    if number != number or number in (float("inf"), float("-inf")):
        raise policy_error("failure-threshold must be a finite number in (0, 1): %r" % (value,))
    return number


def cmd_batch_create(state, args):
    from .cli import require_id, require_int, require_version

    ensure_namespace(state)
    batch_id = require_id(args.batch_id, "batch-id")
    target_version = require_version(args.target_version)
    stable_version = require_version(args.stable_version)

    raw_devices = args.device_id or []
    target_device_ids = []
    seen = set()
    for value in raw_devices:
        if not isinstance(value, str) or not value.strip():
            raise InvalidArgument("device-id must be a non-empty string: %r" % (value,))
        if value in seen:
            raise InvalidArgument("duplicate device-id: %s" % value)
        seen.add(value)
        target_device_ids.append(value)

    batch_size = require_int(args.batch_size, "batch-size")
    failure_threshold = normalize_failure_threshold(args.failure_threshold)
    heartbeat_timeout_seconds = require_int(args.heartbeat_timeout_seconds,
                                            "heartbeat-timeout-seconds")

    # 先跑策略校验（可能抛 BatchConflict/FirmwareNotFound/EmptyDeviceSet/InvalidBatchPolicy）。
    validate_policy(
        state,
        batch_id,
        target_version,
        stable_version,
        target_device_ids,
        batch_size,
        failure_threshold,
        heartbeat_timeout_seconds,
    )
    # 集合非空已确认；设备存在性沿用公开设备入口的语义（DeviceNotFound）。
    for device_id in target_device_ids:
        if device_id not in state["devices"]:
            raise DeviceNotFound("target device not found: %s" % device_id)

    groups = [
        target_device_ids[i:i + batch_size]
        for i in range(0, len(target_device_ids), batch_size)
    ]
    batch = {
        "batchId": batch_id,
        "targetVersion": target_version,
        "stableVersion": stable_version,
        "batchSize": batch_size,
        "failureThreshold": failure_threshold,
        "heartbeatTimeoutSeconds": heartbeat_timeout_seconds,
        "targetDeviceIds": list(target_device_ids),
        "status": BATCH_STATUS_QUEUED,
        "currentWave": 0,
        "waves": groups,
        "frozen": False,
        "stopReason": None,
        "stoppedAt": None,
        "devices": {
            device_id: {
                "deviceId": device_id,
                "stage": STAGE_QUEUED,
                "wave": wave_index,
                "targetVersion": None,
                "lastHeartbeatAt": None,
                "terminalResult": None,
                "terminalAt": None,
                "terminalHeartbeatAt": None,
                "failedReason": None,
                "rollback": {
                    "status": None,
                    "dispatchedAt": None,
                    "reportedAt": None,
                    "heartbeatAt": None,
                },
            }
            for wave_index, group in enumerate(groups)
            for device_id in group
        },
    }
    state["canaryBatches"][batch_id] = batch
    return batch_view(state, batch)


# ---------------------------------------------------------------------------
# 推进
# ---------------------------------------------------------------------------


def dispatch_next_wave(batch):
    """把下一批设备置为待升级：queued -> pending_upgrade。"""
    wave_index = batch["currentWave"]
    if wave_index >= len(batch["waves"]):
        return False
    for device_id in batch["waves"][wave_index]:
        entry = batch["devices"][device_id]
        if entry["stage"] == STAGE_QUEUED:
            entry["stage"] = STAGE_PENDING
    return True


def cmd_batch_start(state, args):
    from .cli import require_id

    batch_id = require_id(args.batch_id, "batch-id")
    batch = get_batch(state, batch_id)
    if batch["status"] != BATCH_STATUS_QUEUED:
        raise InvalidState(
            "canary batch %s is not QUEUED (status: %s)" % (batch_id, batch["status"])
        )
    batch["status"] = BATCH_STATUS_RUNNING
    dispatch_next_wave(batch)
    return batch_view(state, batch)


def evaluate_wave(state, batch, at_instant=None):
    """本批设备全部取得终态后计算失败率，决定停止回滚或继续放量。

    终态仅指 success / failed（waiting_heartbeat 的设备仍在等待）。
    失败率 = 本批失败数 / 本批已取得终态设备数；严格大于阈值即停止。
    """
    if batch["status"] != BATCH_STATUS_RUNNING:
        return
    wave_index = batch["currentWave"]
    if wave_index >= len(batch["waves"]):
        return
    members = batch["waves"][wave_index]
    entries = [batch["devices"][d] for d in members]
    if any(e["stage"] not in (STAGE_SUCCESS, STAGE_FAILED) for e in entries):
        return
    failed = sum(1 for e in entries if e["stage"] == STAGE_FAILED)
    rate = failed / len(entries) if entries else 0.0
    if rate > batch["failureThreshold"]:
        stop_and_dispatch_rollback(state, batch, rate, at_instant)
        return
    if wave_index + 1 == len(batch["waves"]):
        batch["status"] = BATCH_STATUS_COMPLETED
    else:
        batch["currentWave"] = wave_index + 1
        dispatch_next_wave(batch)


def stop_and_dispatch_rollback(state, batch, failure_rate, at_instant):
    """失败率超阈值：冻结后续批次，仅对此前已升级成功的设备下发回滚任务。"""
    batch["status"] = BATCH_STATUS_FAILED_STOPPED
    batch["frozen"] = True
    batch["stopReason"] = "failure_threshold_exceeded"
    batch["failureRateAtStop"] = failure_rate
    if at_instant is not None:
        batch["stoppedAt"] = format_instant(at_instant)
    # 尚未进入升级中的后续设备保持 queued，不再接收升级任务。
    for entry in batch["devices"].values():
        if entry["stage"] == STAGE_PENDING:
            # 阈值判定发生在整批集齐之后，正常不会残留 pending；防御性恢复为 queued。
            entry["stage"] = STAGE_QUEUED
        elif entry["stage"] in UPGRADE_SUCCEEDED_STAGES:
            entry["stage"] = STAGE_ROLLING_BACK
            entry["rollback"]["status"] = "PENDING"
            if at_instant is not None:
                entry["rollback"]["dispatchedAt"] = format_instant(at_instant)
    finalize_rollback_if_complete(batch)


def finalize_rollback_if_complete(batch):
    """所有回滚任务取得终态后，批次收敛为 ROLLED_BACK 或 ROLLBACK_FAILED。"""
    if batch["status"] != BATCH_STATUS_FAILED_STOPPED:
        return
    tasks = [
        entry["rollback"]
        for entry in batch["devices"].values()
        if entry["stage"] in (STAGE_ROLLING_BACK, STAGE_ROLLBACK_SUCCEEDED,
                              STAGE_ROLLBACK_FAILED)
    ]
    if not tasks:
        # 此前没有任何已升级成功的设备：无回滚任务可下发，直接视为回滚完成。
        batch["status"] = BATCH_STATUS_ROLLED_BACK
        return
    if any(task["status"] == "PENDING" for task in tasks):
        return
    if any(task["status"] == "FAILED" for task in tasks):
        batch["status"] = BATCH_STATUS_ROLLBACK_FAILED
    else:
        batch["status"] = BATCH_STATUS_ROLLED_BACK


# ---------------------------------------------------------------------------
# 设备终态上报（复用 device report 入口）
# ---------------------------------------------------------------------------


def report_terminal(state, batch_id, device_id, result, heartbeat_text, phase):
    """记录设备升级 / 回滚终态。幂等：同阶段重复上报沿用首次结果。"""
    batch = get_batch(state, batch_id)
    if device_id not in batch["devices"]:
        raise InvalidArgument(
            "device %s is not in canary batch %s" % (device_id, batch_id)
        )
    entry = batch["devices"][device_id]

    if phase == PHASE_ROLLBACK:
        return report_rollback(state, batch, entry, device_id, result, heartbeat_text)

    # 升级终态：同阶段重复上报幂等，沿用首次结果（批次停止后亦同）。
    if entry["stage"] in (STAGE_SUCCESS, STAGE_FAILED):
        return entry
    if batch["status"] != BATCH_STATUS_RUNNING:
        raise InvalidState(
            "canary batch %s is not running (status: %s)" % (batch_id, batch["status"])
        )
    current_members = batch["waves"][batch["currentWave"]]
    if device_id not in current_members:
        # 自动停止后后续设备或非当前批设备不得再上报升级终态。
        raise InvalidArgument(
            "device %s is not in the current wave of canary batch %s"
            % (device_id, batch_id)
        )
    if entry["stage"] == STAGE_QUEUED:
        raise InvalidArgument(
            "device %s has not been dispatched in canary batch %s" % (device_id, batch_id)
        )
    # pending_upgrade / upgrading / waiting_heartbeat 接受首个终态。
    entry["terminalResult"] = result
    entry["terminalAt"] = heartbeat_text
    entry["terminalHeartbeatAt"] = heartbeat_text
    if entry["lastHeartbeatAt"] is None:
        entry["lastHeartbeatAt"] = heartbeat_text
    if result == "failure":
        entry["stage"] = STAGE_FAILED
        entry["failedReason"] = "terminal_failure"
    else:
        entry["targetVersion"] = batch["targetVersion"]
        settle_upgrade_success(state, batch, entry)
    evaluate_wave(state, batch)
    return entry


def settle_upgrade_success(state, batch, entry):
    """成功终态与目标版本心跳均到位才记 success；否则等待心跳。"""
    device = state["devices"].get(entry["deviceId"])
    heartbeat_version = device.get("version") if device is not None else None
    if heartbeat_version == batch["targetVersion"]:
        entry["stage"] = STAGE_SUCCESS
    else:
        entry["stage"] = STAGE_WAITING


def report_rollback(state, batch, entry, device_id, result, heartbeat_text):
    if batch["status"] not in (BATCH_STATUS_FAILED_STOPPED, BATCH_STATUS_ROLLBACK_FAILED,
                               BATCH_STATUS_ROLLED_BACK):
        raise InvalidState(
            "canary batch %s has no rollback in progress (status: %s)"
            % (batch["batchId"], batch["status"])
        )
    task = entry["rollback"]
    if task["status"] is None:
        raise InvalidArgument(
            "no rollback task dispatched for device %s in canary batch %s"
            % (device_id, batch["batchId"])
        )
    if task["status"] != "PENDING":
        # 幂等：重复回滚结果沿用首次。
        return entry
    task["status"] = "SUCCEEDED" if result == "success" else "FAILED"
    task["reportedAt"] = heartbeat_text
    task["heartbeatAt"] = heartbeat_text
    if result == "success":
        entry["stage"] = STAGE_ROLLBACK_SUCCEEDED
        device = state["devices"].get(device_id)
        if device is not None:
            device["version"] = batch["stableVersion"]
    else:
        entry["stage"] = STAGE_ROLLBACK_FAILED
    finalize_rollback_if_complete(batch)
    return entry


# ---------------------------------------------------------------------------
# 心跳钩子（复用 device heartbeat 入口）
# ---------------------------------------------------------------------------


def on_device_heartbeat(state, device_id, version, heartbeat_text):
    """设备心跳到达后驱动其在批次中的阶段迁移。

    - upgrading 收到心跳：仍在升级中；版本已到目标版本则等待终态确认。
    - waiting_heartbeat 收到目标版本心跳：补齐成功。
    - 已经失败的设备：晚到心跳不得改回成功。
    """
    for batch in state.get("canaryBatches", {}).values():
        entry = batch["devices"].get(device_id)
        if entry is None:
            continue
        entry["lastHeartbeatAt"] = heartbeat_text
        stage = entry["stage"]
        if stage == STAGE_PENDING:
            entry["stage"] = STAGE_UPGRADING
        if stage in (STAGE_PENDING, STAGE_UPGRADING):
            if version == batch["targetVersion"] and entry["terminalResult"] == "success":
                entry["stage"] = STAGE_SUCCESS
                evaluate_wave(state, batch)
            elif version == batch["targetVersion"]:
                entry["stage"] = STAGE_WAITING
            else:
                entry["stage"] = STAGE_UPGRADING
        elif stage == STAGE_WAITING:
            if entry["terminalResult"] == "success" and version == batch["targetVersion"]:
                entry["stage"] = STAGE_SUCCESS
                evaluate_wave(state, batch)
        # failed / success / rolling_back / rollback_*：心跳不改终态。


# ---------------------------------------------------------------------------
# 心跳超时收批
# ---------------------------------------------------------------------------


def cmd_batch_check(state, args):
    from .cli import require_id, require_time

    batch_id = require_id(args.batch_id, "batch-id")
    _, at_instant = require_time(args.at)
    batch = get_batch(state, batch_id)
    expired = []
    if batch["status"] == BATCH_STATUS_RUNNING:
        timeout = timedelta(
            seconds=batch.get("heartbeatTimeoutSeconds", DEFAULT_HEARTBEAT_TIMEOUT_SECONDS)
        )
        members = batch["waves"][batch["currentWave"]]
        for device_id in members:
            entry = batch["devices"][device_id]
            if entry["stage"] in (STAGE_SUCCESS, STAGE_FAILED):
                continue
            heartbeat_text = entry["lastHeartbeatAt"]
            if heartbeat_text is None:
                # 从设备公开入口取一次（start 后尚未有任何批次内心跳的情况）。
                device = state["devices"].get(device_id)
                heartbeat_text = device.get("heartbeatAt") if device is not None else None
                if heartbeat_text is not None:
                    entry["lastHeartbeatAt"] = heartbeat_text
            if heartbeat_text is None:
                continue
            if at_instant > parse_time(heartbeat_text) + timeout:
                entry["stage"] = STAGE_FAILED
                entry["failedReason"] = "heartbeat_timeout"
                entry["terminalResult"] = "failure"
                entry["terminalAt"] = heartbeat_text
                entry["terminalHeartbeatAt"] = heartbeat_text
                expired.append(device_id)
        expired.sort()
        evaluate_wave(state, batch, at_instant=at_instant)
    elif batch["status"] == BATCH_STATUS_FAILED_STOPPED:
        # 回滚阶段不做升级超时；仅尝试收敛（结果由公开上报入口写入）。
        finalize_rollback_if_complete(batch)
    view = batch_view(state, batch)
    view["expiredDevices"] = expired
    view["at"] = format_instant(at_instant)
    return view


def cmd_batch_status(state, args):
    from .cli import require_id

    batch_id = require_id(args.batch_id, "batch-id")
    batch = get_batch(state, batch_id)
    return batch_view(state, batch)


# ---------------------------------------------------------------------------
# 视图
# ---------------------------------------------------------------------------


def device_view(state, entry):
    device = state["devices"].get(entry["deviceId"], {})
    return {
        "deviceId": entry["deviceId"],
        "wave": entry["wave"],
        "stage": entry["stage"],
        "version": device.get("version"),
        "targetVersion": entry.get("targetVersion"),
        "lastHeartbeatAt": entry.get("lastHeartbeatAt"),
        "terminalResult": entry.get("terminalResult"),
        "terminalAt": entry.get("terminalAt"),
        "failedReason": entry.get("failedReason"),
        "rollback": copy.deepcopy(entry.get("rollback")),
    }


def batch_view(state, batch):
    waves = batch["waves"]
    current = batch["currentWave"]
    entries = list(batch["devices"].values())
    # 已完成数：曾成功完成升级的设备（含此后进入回滚各阶段者）；失败数：升级失败设备。
    completed_count = sum(1 for e in entries if e["stage"] in UPGRADE_SUCCEEDED_STAGES)
    failed_count = sum(1 for e in entries if e["stage"] == STAGE_FAILED)

    # 当前失败率：本批已取得终态（success/failed）设备中失败设备所占比例。
    if batch["status"] == BATCH_STATUS_RUNNING and current < len(waves):
        members = waves[current]
        terminal = [
            batch["devices"][d]
            for d in members
            if batch["devices"][d]["stage"] in (STAGE_SUCCESS, STAGE_FAILED)
        ]
        current_failure_rate = (
            sum(1 for e in terminal if e["stage"] == STAGE_FAILED) / len(terminal)
            if terminal else 0.0
        )
    elif batch["status"] in (BATCH_STATUS_FAILED_STOPPED, BATCH_STATUS_ROLLED_BACK,
                             BATCH_STATUS_ROLLBACK_FAILED):
        current_failure_rate = batch.get("failureRateAtStop")
    else:
        current_failure_rate = 0.0

    frozen = bool(batch.get("frozen"))
    return {
        "batchId": batch["batchId"],
        "targetVersion": batch["targetVersion"],
        "stableVersion": batch["stableVersion"],
        "batchSize": batch["batchSize"],
        "failureThreshold": batch["failureThreshold"],
        "heartbeatTimeoutSeconds": batch["heartbeatTimeoutSeconds"],
        "targetDeviceIds": list(batch["targetDeviceIds"]),
        "status": batch["status"],
        "currentWave": current if batch["status"] != BATCH_STATUS_QUEUED else 0,
        "waveCount": len(waves),
        "waves": waves,
        "frozen": frozen,
        "futureWavesFrozen": frozen,
        "stopReason": batch.get("stopReason"),
        "stoppedAt": batch.get("stoppedAt"),
        "completedCount": completed_count,
        "failedCount": failed_count,
        "currentFailureRate": current_failure_rate,
        "devices": [device_view(state, batch["devices"][d]) for d in batch["targetDeviceIds"]],
    }
