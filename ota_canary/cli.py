"""设备固件灰度发布与故障回滚的命令行实现。

仅使用 Python 3 标准库。状态持久化在 JSON 文件中（默认 ota-canary-state.json），
成功的写命令才会落盘；失败命令以非零退出码结束且不修改状态文件。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone

DEFAULT_STATE_FILE = "ota-canary-state.json"
DEFAULT_HEARTBEAT_TIMEOUT_SECONDS = 900

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
VALID_RESULTS = ("success", "failure")


# ---------------------------------------------------------------------------
# 错误类型：业务错误以 JSON 形式输出到 stderr，退出码为 1
# ---------------------------------------------------------------------------

class OtaError(Exception):
    code = "InvalidArgument"

    def __init__(self, message):
        super().__init__(message)
        self.message = message


class DeviceNotFound(OtaError):
    code = "DeviceNotFound"


class DeviceExists(OtaError):
    code = "DeviceExists"


class InvalidArgument(OtaError):
    code = "InvalidArgument"


class InvalidState(OtaError):
    code = "InvalidState"


# ---------------------------------------------------------------------------
# 参数校验
# ---------------------------------------------------------------------------

def require_id(value, name):
    if not isinstance(value, str) or not value.strip():
        raise InvalidArgument("%s must be a non-empty string" % name)
    return value


def require_version(value):
    if not isinstance(value, str) or not VERSION_RE.match(value):
        raise InvalidArgument("invalid version format: %r (expected MAJOR.MINOR.PATCH)" % (value,))
    return value


def require_time(value):
    if not isinstance(value, str) or not value.strip():
        raise InvalidArgument("invalid timestamp: %r" % (value,))
    text = value.strip()
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        raise InvalidArgument("invalid timestamp: %r (expected ISO 8601)" % (value,))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return value, parsed.astimezone(timezone.utc)


def parse_time(value):
    """将已校验的 ISO 8601 字符串换算为 UTC 瞬间；无偏移按 UTC。"""
    _, instant = require_time(value)
    return instant


def format_instant(instant):
    """将 UTC 瞬间格式化为 ISO 8601 字符串（Z 后缀）。"""
    return instant.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def require_int(value, name):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise InvalidArgument("%s must be an integer: %r" % (name, value))


def require_batch_size(value):
    size = require_int(value, "batch-size")
    if size < 1:
        raise InvalidArgument("batch-size must be >= 1: %r" % (value,))
    return size


def require_max_failure_percent(value):
    percent = require_int(value, "max-failure-percent")
    if percent < 0 or percent > 100:
        raise InvalidArgument("max-failure-percent must be between 0 and 100: %r" % (value,))
    return percent


def require_max_release_failure_percent(value):
    if value is None:
        return None
    percent = require_int(value, "max-release-failure-percent")
    if percent < 0 or percent > 100:
        raise InvalidArgument(
            "max-release-failure-percent must be between 0 and 100: %r" % (value,)
        )
    return percent


def require_heartbeat_timeout_seconds(value):
    seconds = require_int(value, "heartbeat-timeout-seconds")
    if seconds < 1:
        raise InvalidArgument("heartbeat-timeout-seconds must be >= 1: %r" % (value,))
    return seconds


def require_stabilization_seconds(value):
    seconds = require_int(value, "stabilization-seconds")
    if seconds < 0:
        raise InvalidArgument("stabilization-seconds must be >= 0: %r" % (value,))
    return seconds


def require_target_device_ids(values):
    """校验可重复的 --target-device-id：未提供返回 None；空值或重复返回 InvalidArgument。"""
    if values is None:
        return None
    seen = set()
    target_ids = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise InvalidArgument("target-device-id must be a non-empty string: %r" % (value,))
        if value in seen:
            raise InvalidArgument("duplicate target-device-id: %s" % value)
        seen.add(value)
        target_ids.append(value)
    return sorted(target_ids)


def require_result(value):
    if value not in VALID_RESULTS:
        raise InvalidArgument("result must be one of %s: %r" % ("/".join(VALID_RESULTS), value))
    return value


MAX_ABORT_REASON_CHARS = 200


def require_abort_reason(value):
    if not isinstance(value, str):
        raise InvalidArgument("reason must be a string: %r" % (value,))
    reason = value.strip()
    if not reason:
        raise InvalidArgument("reason must be non-empty after trimming")
    if len(reason) > MAX_ABORT_REASON_CHARS:
        raise InvalidArgument(
            "reason must be at most %d characters: %d given"
            % (MAX_ABORT_REASON_CHARS, len(reason))
        )
    return reason


# ---------------------------------------------------------------------------
# 状态读写
# ---------------------------------------------------------------------------

def empty_state():
    return {"devices": {}, "releases": {}}


def load_state(path):
    if not os.path.exists(path):
        return empty_state()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise InvalidState("cannot read state file %s: %s" % (path, exc))
    if not isinstance(data, dict):
        raise InvalidState("state file %s is corrupted" % path)
    data.setdefault("devices", {})
    data.setdefault("releases", {})
    return data


def save_state(path, state):
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp_path = tempfile.mkstemp(prefix=".ota-canary-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, ensure_ascii=False, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_path, path)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# 发布视图
# ---------------------------------------------------------------------------

def release_view(state, release):
    batches = release["batches"]
    current = release["currentBatch"]
    reports = release["reports"]
    pending = []
    if release["status"] == "in_progress" and current < len(batches):
        pending = [d for d in batches[current] if d not in reports]
    devices = []
    for batch in batches:
        for device_id in batch:
            device = state["devices"].get(device_id, {})
            devices.append({
                "deviceId": device_id,
                "version": device.get("version"),
                "heartbeatAt": device.get("heartbeatAt"),
                "report": reports.get(device_id),
            })
    reported_count = len(reports)
    failed_count = sum(1 for report in reports.values() if report.get("result") == "failure")
    return {
        "releaseId": release["releaseId"],
        "version": release["version"],
        "previousVersion": release["previousVersion"],
        "batchSize": release["batchSize"],
        "maxFailurePercent": release["maxFailurePercent"],
        "maxReleaseFailurePercent": release.get("maxReleaseFailurePercent"),
        "heartbeatTimeoutSeconds": release.get(
            "heartbeatTimeoutSeconds", DEFAULT_HEARTBEAT_TIMEOUT_SECONDS
        ),
        "stabilizationSeconds": release.get("stabilizationSeconds", 0),
        "stabilizationDeadline": release.get("stabilizationDeadline"),
        "targetDeviceIds": release.get("targetDeviceIds"),
        "status": release["status"],
        "stopReason": release.get("stopReason"),
        "abortReason": release.get("abortReason"),
        "abortedAt": release.get("abortedAt"),
        "currentBatch": current,
        "batchCount": len(batches),
        "batches": batches,
        "reportedCount": reported_count,
        "failedCount": failed_count,
        "pendingDevices": pending,
        "devices": devices,
    }


def get_release(state, release_id):
    release = state["releases"].get(release_id)
    if release is None:
        raise DeviceNotFound("release not found: %s" % release_id)
    return release


def get_device(state, device_id):
    device = state["devices"].get(device_id)
    if device is None:
        raise DeviceNotFound("device not found: %s" % device_id)
    return device


# ---------------------------------------------------------------------------
# 命令处理
# ---------------------------------------------------------------------------

def cmd_device_add(state, args):
    device_id = require_id(args.device_id, "device-id")
    version = require_version(args.version)
    heartbeat_at, _ = require_time(args.heartbeat_at)
    if device_id in state["devices"]:
        raise DeviceExists("device already registered: %s" % device_id)
    record = {"deviceId": device_id, "version": version, "heartbeatAt": heartbeat_at}
    state["devices"][device_id] = record
    return dict(record)


def cmd_device_heartbeat(state, args):
    device_id = require_id(args.device_id, "device-id")
    version = require_version(args.version)
    heartbeat_at, _ = require_time(args.heartbeat_at)
    device = get_device(state, device_id)
    device["version"] = version
    device["heartbeatAt"] = heartbeat_at
    return dict(device)


def cmd_release_create(state, args):
    release_id = require_id(args.release_id, "release-id")
    version = require_version(args.version)
    previous_version = require_version(args.previous_version)
    batch_size = require_batch_size(args.batch_size)
    max_failure_percent = require_max_failure_percent(args.max_failure_percent)
    max_release_failure_percent = require_max_release_failure_percent(
        args.max_release_failure_percent
    )
    heartbeat_timeout_seconds = require_heartbeat_timeout_seconds(
        args.heartbeat_timeout_seconds
    )
    stabilization_seconds = require_stabilization_seconds(args.stabilization_seconds)
    target_device_ids = require_target_device_ids(args.target_device_id)
    if release_id in state["releases"]:
        raise DeviceExists("release already exists: %s" % release_id)
    release = {
        "releaseId": release_id,
        "version": version,
        "previousVersion": previous_version,
        "batchSize": batch_size,
        "maxFailurePercent": max_failure_percent,
        "maxReleaseFailurePercent": max_release_failure_percent,
        "heartbeatTimeoutSeconds": heartbeat_timeout_seconds,
        "stabilizationSeconds": stabilization_seconds,
        "stabilizationDeadline": None,
        "targetDeviceIds": target_device_ids,
        "status": "pending",
        "stopReason": None,
        "abortReason": None,
        "abortedAt": None,
        "batches": [],
        "currentBatch": 0,
        "reports": {},
    }
    state["releases"][release_id] = release
    return release_view(state, release)


def compute_release_candidates(state, release):
    """按 release start 的口径只读计算候选设备。

    返回 (targetDeviceIds, eligible)：targetDeviceIds 为 None 表示未指定定向；
    显式目标先校验全部存在（DeviceNotFound），再校验版本与占用（InvalidArgument）。
    eligible 已按 device-id 升序（显式目标集合在 create 时已排序）。
    """
    target_device_ids = release.get("targetDeviceIds")
    occupied = set()
    for other in state["releases"].values():
        if other["status"] in ("in_progress", "paused"):
            for batch in other["batches"]:
                occupied.update(batch)
    if target_device_ids is not None:
        # 显式目标：先确认全部存在（DeviceNotFound），再确认版本与占用（InvalidArgument）。
        for device_id in target_device_ids:
            if device_id not in state["devices"]:
                raise DeviceNotFound("target device not found: %s" % device_id)
        for device_id in target_device_ids:
            device = state["devices"][device_id]
            if device.get("version") != release["previousVersion"]:
                raise InvalidArgument(
                    "target device %s version %r does not match previousVersion %r"
                    % (device_id, device.get("version"), release["previousVersion"])
                )
            if device_id in occupied:
                raise InvalidArgument(
                    "target device %s is occupied by another in_progress or paused release"
                    % device_id
                )
        return target_device_ids, list(target_device_ids)
    eligible = sorted(
        device_id
        for device_id, device in state["devices"].items()
        if device.get("version") == release["previousVersion"] and device_id not in occupied
    )
    return target_device_ids, eligible


def cmd_release_start(state, args):
    release_id = require_id(args.release_id, "release-id")
    release = get_release(state, release_id)
    if release["status"] != "pending":
        raise InvalidState("release %s is not pending (status: %s)" % (release_id, release["status"]))
    # 全部校验通过前不修改发布状态、批次、报告、设备版本、心跳或占用关系。
    _, eligible = compute_release_candidates(state, release)
    batch_size = release["batchSize"]
    release["batches"] = [eligible[i:i + batch_size] for i in range(0, len(eligible), batch_size)]
    release["currentBatch"] = 0
    release["reports"] = {}
    release["status"] = "in_progress" if release["batches"] else "completed"
    return release_view(state, release)


def cmd_release_plan(state, args):
    release_id = require_id(args.release_id, "release-id")
    release = get_release(state, release_id)
    if release["status"] != "pending":
        raise InvalidState("release %s is not pending (status: %s)" % (release_id, release["status"]))
    # 只读计算：不创建批次、reports，不改设备版本、心跳、占用关系或状态文件。
    target_device_ids, eligible = compute_release_candidates(state, release)
    batch_size = release["batchSize"]
    batches = [eligible[i:i + batch_size] for i in range(0, len(eligible), batch_size)]
    return {
        "releaseId": release["releaseId"],
        "targetDeviceIds": target_device_ids,
        "eligibleDeviceIds": eligible,
        "batchSize": batch_size,
        "candidateCount": len(eligible),
        "batches": batches,
    }


def cmd_device_report(state, args):
    release_id = require_id(args.release_id, "release-id")
    device_id = require_id(args.device_id, "device-id")
    result = require_result(args.result)
    heartbeat_at, _ = require_time(args.heartbeat_at)
    release = get_release(state, release_id)
    device = get_device(state, device_id)
    if release["status"] != "in_progress":
        raise InvalidState("release %s is not in progress (status: %s)" % (release_id, release["status"]))
    current_batch = release["batches"][release["currentBatch"]]
    if device_id not in current_batch:
        raise InvalidArgument("device %s is not in the current batch of release %s" % (device_id, release_id))
    existing = release["reports"].get(device_id)
    if existing is not None:
        if existing.get("reason") == "timeout":
            raise InvalidArgument("device %s has timed out in release %s" % (device_id, release_id))
        raise InvalidArgument("device %s already reported for release %s" % (device_id, release_id))
    release["reports"][device_id] = {"result": result, "heartbeatAt": heartbeat_at}
    device["heartbeatAt"] = heartbeat_at
    if result == "success":
        device["version"] = release["version"]
    advance_if_batch_complete(state, release)
    view = release_view(state, release)
    view["report"] = {"deviceId": device_id, "result": result, "heartbeatAt": heartbeat_at}
    return view


def cmd_release_check(state, args):
    release_id = require_id(args.release_id, "release-id")
    release = get_release(state, release_id)
    _, at_instant = require_time(args.at)
    if release["status"] != "in_progress":
        raise InvalidState("release %s is not in progress (status: %s)" % (release_id, release["status"]))
    timeout = timedelta(
        seconds=release.get("heartbeatTimeoutSeconds", DEFAULT_HEARTBEAT_TIMEOUT_SECONDS)
    )
    batch = release["batches"][release["currentBatch"]]
    reports = release["reports"]
    expired = []
    for device_id in batch:
        if device_id in reports:
            continue
        device = state["devices"].get(device_id)
        heartbeat_at = device.get("heartbeatAt") if device is not None else None
        if heartbeat_at is None:
            continue
        if at_instant > parse_time(heartbeat_at) + timeout:
            # 显式结果永不覆盖；heartbeatAt 保留设备原值
            reports[device_id] = {
                "result": "failure",
                "reason": "timeout",
                "heartbeatAt": heartbeat_at,
            }
            expired.append(device_id)
    expired.sort()
    advance_if_batch_complete(state, release, at_instant=at_instant)
    view = release_view(state, release)
    view["expiredDevices"] = expired
    return view


def cmd_release_pause(state, args):
    release_id = require_id(args.release_id, "release-id")
    release = get_release(state, release_id)
    if release["status"] != "in_progress":
        raise InvalidState("release %s is not in progress (status: %s)" % (release_id, release["status"]))
    release["status"] = "paused"
    return release_view(state, release)


def cmd_release_resume(state, args):
    release_id = require_id(args.release_id, "release-id")
    release = get_release(state, release_id)
    if release["status"] != "paused":
        raise InvalidState("release %s is not paused (status: %s)" % (release_id, release["status"]))
    release["status"] = "in_progress"
    return release_view(state, release)


def cmd_release_abort(state, args):
    release_id = require_id(args.release_id, "release-id")
    reason = require_abort_reason(args.reason)
    _, at_instant = require_time(args.at)
    release = get_release(state, release_id)
    if release["status"] not in ("in_progress", "paused"):
        raise InvalidState(
            "release %s cannot be aborted (status: %s)" % (release_id, release["status"])
        )
    roll_back(state, release, "manual_abort")
    release["abortReason"] = reason
    release["abortedAt"] = format_instant(at_instant)
    return release_view(state, release)


def roll_back(state, release, stop_reason):
    """按既有口径回滚：恢复已纳入批次设备版本，记录停止原因并解除发布占用。"""
    release["status"] = "rolled_back"
    release["stopReason"] = stop_reason
    release["stabilizationDeadline"] = None
    for group in release["batches"]:
        for device_id in group:
            device = state["devices"].get(device_id)
            if device is not None:
                device["version"] = release["previousVersion"]


def advance_if_batch_complete(state, release, at_instant=None):
    """整批集齐后决定回滚、观察或推进。

    先判当前批失败率，超阈值立即 rolled_back（stopReason=batch_failure_threshold）；
    配置了发布级累计失败预算时，再判全程失败率，超阈值立即 rolled_back
    （stopReason=release_failure_threshold）。否则 stabilizationSeconds > 0 时进入观察：
    保持 currentBatch，记录 stabilizationDeadline（当前批报告中最晚 heartbeatAt
    加观察秒数），仅在 at_instant 严格晚于截止时刻时推进下一批或 completed。
    at_instant 为 None（device report 路径）时只进入观察，不推进。
    """
    batches = release["batches"]
    current = release["currentBatch"]
    batch = batches[current]
    reports = release["reports"]
    if any(device_id not in reports for device_id in batch):
        return
    failed = sum(1 for device_id in batch if reports[device_id]["result"] == "failure")
    if failed * 100 > len(batch) * release["maxFailurePercent"]:
        roll_back(state, release, "batch_failure_threshold")
        return
    max_release_percent = release.get("maxReleaseFailurePercent")
    if max_release_percent is not None:
        reported_count = len(reports)
        failed_count = sum(
            1 for report in reports.values() if report.get("result") == "failure"
        )
        if failed_count * 100 > reported_count * max_release_percent:
            roll_back(state, release, "release_failure_threshold")
            return
    stabilization = release.get("stabilizationSeconds", 0)
    if stabilization > 0:
        deadline_text = release.get("stabilizationDeadline")
        if deadline_text is None:
            latest = max(parse_time(reports[device_id]["heartbeatAt"]) for device_id in batch)
            deadline_text = format_instant(latest + timedelta(seconds=stabilization))
            release["stabilizationDeadline"] = deadline_text
        if at_instant is None or at_instant <= parse_time(deadline_text):
            return
    release["stabilizationDeadline"] = None
    if current + 1 == len(batches):
        release["status"] = "completed"
    else:
        release["currentBatch"] = current + 1


def cmd_status(state, args):
    release_id = require_id(args.release_id, "release-id")
    release = get_release(state, release_id)
    return release_view(state, release)


def fleet_device_row(device_id, device, at_instant, stale_boundary):
    """构造单设备的 fleet status 行；心跳时间已由调用方校验为 UTC 瞬间或 None。"""
    heartbeat_text = device.get("heartbeatAt")
    heartbeat_instant = parse_time(heartbeat_text) if heartbeat_text is not None else None
    if heartbeat_instant is None:
        age_seconds = None
        heartbeat_state = "unknown"
    else:
        # timedelta 整除给出向下取整秒；心跳等于或晚于观察时刻时归零。
        age_seconds = max(0, (at_instant - heartbeat_instant) // timedelta(seconds=1))
        heartbeat_state = "stale" if heartbeat_instant < stale_boundary else "fresh"
    return {
        "deviceId": device_id,
        "version": device.get("version"),
        "heartbeatAt": heartbeat_text,
        "ageSeconds": age_seconds,
        "heartbeatState": heartbeat_state,
    }


def cmd_fleet_status(state, args):
    if args.at is None:
        at_instant = datetime.now(timezone.utc)
    else:
        _, at_instant = require_time(args.at)
    timeout_seconds = require_heartbeat_timeout_seconds(args.heartbeat_timeout_seconds)
    release_id_arg = args.release_id
    if release_id_arg is not None:
        release_id_arg = require_id(release_id_arg, "release-id")

    devices = state.get("devices")
    releases = state.get("releases")
    if not isinstance(devices, dict) or not isinstance(releases, dict):
        raise InvalidState("state file is corrupted")

    stale_boundary = at_instant - timedelta(seconds=timeout_seconds)

    def device_record(device_id):
        record = devices.get(device_id, {})
        if not isinstance(record, dict):
            raise InvalidState("device record is corrupted: %s" % device_id)
        heartbeat_text = record.get("heartbeatAt")
        if heartbeat_text is not None:
            try:
                parse_time(heartbeat_text)
            except InvalidArgument:
                raise InvalidState("device %s has invalid heartbeatAt" % device_id)
        return record

    def batch_ids_of(release, ref):
        batches = release.get("batches")
        if not isinstance(batches, list):
            raise InvalidState("release %s is corrupted" % ref)
        ids = set()
        for batch in batches:
            if not isinstance(batch, list):
                raise InvalidState("release %s is corrupted" % ref)
            for device_id in batch:
                if not isinstance(device_id, str):
                    raise InvalidState("release %s is corrupted" % ref)
                ids.add(device_id)
        return ids

    rows = []
    if release_id_arg is not None:
        release = get_release(state, release_id_arg)
        if not isinstance(release, dict) or not isinstance(release.get("reports"), dict):
            raise InvalidState("release %s is corrupted" % release_id_arg)
        reports = release["reports"]
        for device_id in sorted(batch_ids_of(release, release_id_arg)):
            record = device_record(device_id)
            row = fleet_device_row(device_id, record, at_instant, stale_boundary)
            row["releaseId"] = release_id_arg
            row["report"] = reports.get(device_id)
            rows.append(row)
    else:
        # 扫描 in_progress/paused 发布的占用关系；正常状态下设备不会被多个活动发布占用，
        # 出现冲突说明状态损坏。
        occupancy = {}
        for other_id, other in releases.items():
            if not isinstance(other, dict):
                raise InvalidState("release %s is corrupted" % other_id)
            if other.get("status") not in ("in_progress", "paused"):
                continue
            for device_id in batch_ids_of(other, other_id):
                owner = occupancy.get(device_id)
                if owner is not None and owner != other_id:
                    raise InvalidState(
                        "device %s is occupied by multiple active releases: %s, %s"
                        % (device_id, owner, other_id)
                    )
                occupancy[device_id] = other_id
        for device_id in sorted(devices):
            if not isinstance(device_id, str):
                raise InvalidState("state file is corrupted")
            record = device_record(device_id)
            row = fleet_device_row(device_id, record, at_instant, stale_boundary)
            owner = occupancy.get(device_id)
            row["releaseId"] = owner
            row["report"] = None
            rows.append(row)

    summary = {
        "totalDevices": len(rows),
        "freshCount": sum(1 for row in rows if row["heartbeatState"] == "fresh"),
        "staleCount": sum(1 for row in rows if row["heartbeatState"] == "stale"),
        "unknownCount": sum(1 for row in rows if row["heartbeatState"] == "unknown"),
        "occupiedCount": sum(1 for row in rows if row["releaseId"] is not None),
    }
    return {
        "at": format_instant(at_instant),
        "heartbeatTimeoutSeconds": timeout_seconds,
        "summary": summary,
        "devices": rows,
    }


# ---------------------------------------------------------------------------
# 命令行解析
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="ota_canary",
        description="设备固件灰度发布与故障回滚工具",
    )
    parser.add_argument("--state", default=DEFAULT_STATE_FILE,
                        help="状态文件路径（默认 %(default)s）")
    parser.add_argument("--target-device-id", dest="target_device_id_pre",
                        action="append", default=None, metavar="DEVICE_ID",
                        help="定向发布目标设备，可重复；仅对 release create 生效")
    subparsers = parser.add_subparsers(dest="command")

    def add_state_option(sub):
        # 允许 --state 出现在子命令之后；SUPPRESS 保证未提供时不覆盖顶层值
        sub.add_argument("--state", default=argparse.SUPPRESS,
                         help="状态文件路径（默认 %s）" % DEFAULT_STATE_FILE)

    device = subparsers.add_parser("device", help="设备登记、心跳与结果上报")
    device_sub = device.add_subparsers(dest="device_command")

    device_add = device_sub.add_parser("add", help="登记设备")
    device_add.add_argument("--device-id", required=True)
    device_add.add_argument("--version", required=True)
    device_add.add_argument("--heartbeat-at", required=True)
    device_add.set_defaults(handler=cmd_device_add, mutating=True)
    add_state_option(device_add)

    device_heartbeat = device_sub.add_parser("heartbeat", help="更新设备版本与最后心跳")
    device_heartbeat.add_argument("--device-id", required=True)
    device_heartbeat.add_argument("--version", required=True)
    device_heartbeat.add_argument("--heartbeat-at", required=True)
    device_heartbeat.set_defaults(handler=cmd_device_heartbeat, mutating=True)
    add_state_option(device_heartbeat)

    device_report = device_sub.add_parser("report", help="上报当前批设备的升级结果")
    device_report.add_argument("--release-id", required=True)
    device_report.add_argument("--device-id", required=True)
    device_report.add_argument("--result", required=True)
    device_report.add_argument("--heartbeat-at", required=True)
    device_report.set_defaults(handler=cmd_device_report, mutating=True)
    add_state_option(device_report)

    release = subparsers.add_parser("release", help="发布创建与启动")
    release_sub = release.add_subparsers(dest="release_command")

    release_create = release_sub.add_parser("create", help="建立发布")
    release_create.add_argument("--release-id", required=True)
    release_create.add_argument("--version", required=True)
    release_create.add_argument("--previous-version", required=True)
    release_create.add_argument("--batch-size", required=True)
    release_create.add_argument("--max-failure-percent", required=True)
    release_create.add_argument("--max-release-failure-percent", default=None,
                                help="发布级累计失败百分比上限，0 到 100 的整数；"
                                     "缺省只判逐批失败率")
    release_create.add_argument("--heartbeat-timeout-seconds",
                                default=DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
                                help="心跳超时秒数，>=1 的整数（默认 %(default)s）")
    release_create.add_argument("--stabilization-seconds", default=0,
                                help="批次稳定观察秒数，>=0 的整数（默认 %(default)s）")
    release_create.add_argument("--target-device-id", action="append",
                                default=argparse.SUPPRESS,
                                help="定向发布目标设备，可重复；启动时只在这些设备中选择，"
                                     "均须版本等于 previous-version 且未被进行中/暂停发布占用")
    release_create.set_defaults(handler=cmd_release_create, mutating=True)
    add_state_option(release_create)

    release_start = release_sub.add_parser("start", help="启动发布")
    release_start.add_argument("--release-id", required=True)
    release_start.set_defaults(handler=cmd_release_start, mutating=True)
    add_state_option(release_start)

    release_plan = release_sub.add_parser("plan", help="只读预览 pending 发布的分批计划")
    release_plan.add_argument("--release-id", required=True)
    release_plan.set_defaults(handler=cmd_release_plan, mutating=False)
    add_state_option(release_plan)

    release_check = release_sub.add_parser("check", help="按给定时刻收批心跳超时设备")
    release_check.add_argument("--release-id", required=True)
    release_check.add_argument("--at", required=True, help="判定时刻（ISO 8601）")
    release_check.set_defaults(handler=cmd_release_check, mutating=True)
    add_state_option(release_check)

    release_pause = release_sub.add_parser("pause", help="暂停进行中的发布")
    release_pause.add_argument("--release-id", required=True)
    release_pause.set_defaults(handler=cmd_release_pause, mutating=True)
    add_state_option(release_pause)

    release_resume = release_sub.add_parser("resume", help="恢复已暂停的发布")
    release_resume.add_argument("--release-id", required=True)
    release_resume.set_defaults(handler=cmd_release_resume, mutating=True)
    add_state_option(release_resume)

    release_abort = release_sub.add_parser("abort", help="人工终止异常发布并回滚")
    release_abort.add_argument("--release-id", required=True)
    release_abort.add_argument("--reason", required=True,
                               help="人工终止原因，去除首尾空白后 1 到 200 个字符")
    release_abort.add_argument("--at", required=True, help="终止时刻（ISO 8601）")
    release_abort.set_defaults(handler=cmd_release_abort, mutating=True)
    add_state_option(release_abort)

    status = subparsers.add_parser("status", help="查看发布状态")
    status.add_argument("--release-id", required=True)
    status.set_defaults(handler=cmd_status, mutating=False)
    add_state_option(status)

    fleet = subparsers.add_parser("fleet", help="设备 fleet 视图")
    fleet_sub = fleet.add_subparsers(dest="fleet_command")

    fleet_status = fleet_sub.add_parser("status", help="只读查看设备心跳与版本状态")
    fleet_status.add_argument("--release-id", default=None,
                              help="只列该发布批次设备；缺省列出全部设备")
    fleet_status.add_argument("--at", default=None,
                              help="观察时刻（ISO 8601）；缺省取调用时 UTC")
    fleet_status.add_argument("--heartbeat-timeout-seconds",
                              default=DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
                              help="心跳超时秒数，>=1 的整数（默认 %(default)s）")
    fleet_status.set_defaults(handler=cmd_fleet_status, mutating=False)
    add_state_option(fleet_status)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    # 合并子命令前后出现的 --target-device-id（分别解析到不同 dest，避免 append 被覆盖）
    pre = getattr(args, "target_device_id_pre", None)
    post = getattr(args, "target_device_id", None)
    args.target_device_id = (pre or []) + (post or []) if (pre is not None or post is not None) else None
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 2
    try:
        state = load_state(args.state)
        result = handler(state, args)
        if getattr(args, "mutating", False):
            save_state(args.state, state)
    except OtaError as exc:
        print(json.dumps({"error": exc.code, "message": exc.message}, ensure_ascii=False),
              file=sys.stderr)
        return 1
    except OSError as exc:
        print(json.dumps({"error": "InvalidState", "message": str(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0
