"""核心状态机：设备登记、发布创建/启动、报告推进、状态查询。

状态文件为 JSON，结构如下::

    {
      "devices":  {"<device-id>":  {"version": "1.0.0", "heartbeatAt": "..."}},
      "releases": {"<release-id>": {
          "releaseId": "...", "version": "...", "previousVersion": "...",
          "batchSize": 2, "maxFailurePercent": 50,
          "status": "created|in_progress|completed|rolled_back",
          "devices": ["<device-id>", ...],   # 启动时按 device-id 升序选定
          "currentBatch": 0,                  # 0 基批次下标
          "reports": {"<device-id>": "success|failure"}
      }}
    }
"""

from __future__ import annotations

import re
from datetime import datetime

from .errors import (
    DeviceExists,
    DeviceNotFound,
    InvalidArgument,
    InvalidState,
)

# 版本号：三段数字，如 1.2.3
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
# 时间：ISO 8601，如 2026-10-01T12:00:00 或带 .fff / Z / +08:00
_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?$"
)

RESULTS = ("success", "failure")

RELEASE_CREATED = "created"
RELEASE_IN_PROGRESS = "in_progress"
RELEASE_COMPLETED = "completed"
RELEASE_ROLLED_BACK = "rolled_back"


# ---------------------------------------------------------------- 校验

def _require_id(value, field):
    if not isinstance(value, str) or not value.strip():
        raise InvalidArgument("%s 不能为空" % field)
    return value


def _require_version(value, field="version"):
    if not isinstance(value, str) or not _VERSION_RE.match(value):
        raise InvalidArgument("版本格式非法: %r（应为 X.Y.Z）" % (value,))
    return value


def _require_timestamp(value, field="heartbeat-at"):
    if not isinstance(value, str) or not _TIMESTAMP_RE.match(value):
        raise InvalidArgument("时间格式非法: %r（应为 ISO 8601）" % (value,))
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        datetime.fromisoformat(text)
    except ValueError:
        raise InvalidArgument("时间格式非法: %r（应为 ISO 8601）" % (value,))
    return value


def _require_batch_size(value):
    if isinstance(value, bool):
        raise InvalidArgument("batch-size 必须为不小于 1 的整数")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and re.match(r"^\d+$", value.strip()):
        number = int(value.strip())
    else:
        raise InvalidArgument("batch-size 必须为不小于 1 的整数")
    if number < 1:
        raise InvalidArgument("batch-size 必须为不小于 1 的整数")
    return number


def _require_max_failure_percent(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise InvalidArgument("max-failure-percent 必须在 0 到 100 之间")
    if number != number or number < 0 or number > 100:  # NaN 或越界
        raise InvalidArgument("max-failure-percent 必须在 0 到 100 之间")
    if float(number).is_integer():
        return int(number)
    return number


def _require_result(value):
    if value not in RESULTS:
        raise InvalidArgument("result 必须为 success 或 failure")
    return value


# ---------------------------------------------------------------- 状态读写

def empty_state():
    return {"devices": {}, "releases": {}}


def _get_device(state, device_id):
    try:
        return state["devices"][device_id]
    except KeyError:
        raise DeviceNotFound("设备不存在: %s" % device_id)


def _get_release(state, release_id):
    try:
        return state["releases"][release_id]
    except KeyError:
        raise DeviceNotFound("发布不存在: %s" % release_id)


def _batches(release):
    """把发布选定的设备按 batchSize 切批。"""
    devices = release["devices"]
    size = release["batchSize"]
    return [devices[i:i + size] for i in range(0, len(devices), size)]


def _occupied_device_ids(state):
    """被进行中发布占用的设备集合。"""
    occupied = set()
    for rel in state["releases"].values():
        if rel["status"] == RELEASE_IN_PROGRESS:
            occupied.update(rel["devices"])
    return occupied


# ---------------------------------------------------------------- 命令

def device_add(state, device_id, version, heartbeat_at):
    _require_id(device_id, "device-id")
    _require_version(version)
    _require_timestamp(heartbeat_at)
    if device_id in state["devices"]:
        raise DeviceExists("设备已登记: %s" % device_id)
    state["devices"][device_id] = {
        "version": version,
        "heartbeatAt": heartbeat_at,
    }
    return {
        "deviceId": device_id,
        "version": version,
        "heartbeatAt": heartbeat_at,
    }


def device_heartbeat(state, device_id, version, heartbeat_at):
    _require_id(device_id, "device-id")
    _require_version(version)
    _require_timestamp(heartbeat_at)
    device = _get_device(state, device_id)
    device["version"] = version
    device["heartbeatAt"] = heartbeat_at
    return {
        "deviceId": device_id,
        "version": version,
        "heartbeatAt": heartbeat_at,
    }


def release_create(state, release_id, version, previous_version,
                   batch_size, max_failure_percent):
    _require_id(release_id, "release-id")
    _require_version(version)
    _require_version(previous_version, "previous-version")
    batch_size = _require_batch_size(batch_size)
    max_failure_percent = _require_max_failure_percent(max_failure_percent)
    if release_id in state["releases"]:
        raise DeviceExists("发布已存在: %s" % release_id)
    release = {
        "releaseId": release_id,
        "version": version,
        "previousVersion": previous_version,
        "batchSize": batch_size,
        "maxFailurePercent": max_failure_percent,
        "status": RELEASE_CREATED,
        "devices": [],
        "currentBatch": 0,
        "reports": {},
    }
    state["releases"][release_id] = release
    return _release_summary(release)


def release_start(state, release_id):
    _require_id(release_id, "release-id")
    release = _get_release(state, release_id)
    if release["status"] != RELEASE_CREATED:
        raise InvalidState(
            "发布 %s 当前状态为 %s，不能启动" % (release_id, release["status"])
        )
    occupied = _occupied_device_ids(state)
    selected = sorted(
        device_id
        for device_id, device in state["devices"].items()
        if device["version"] == release["previousVersion"]
        and device_id not in occupied
    )
    release["devices"] = selected
    release["currentBatch"] = 0
    release["reports"] = {}
    release["status"] = RELEASE_IN_PROGRESS if selected else RELEASE_COMPLETED
    return _release_summary(release)


def device_report(state, release_id, device_id, result, heartbeat_at):
    _require_id(release_id, "release-id")
    _require_id(device_id, "device-id")
    _require_result(result)
    _require_timestamp(heartbeat_at)
    release = _get_release(state, release_id)
    device = _get_device(state, device_id)
    if release["status"] != RELEASE_IN_PROGRESS:
        raise InvalidState(
            "发布 %s 当前状态为 %s，不能提交报告"
            % (release_id, release["status"])
        )
    batch = _batches(release)[release["currentBatch"]]
    if device_id not in release["devices"] or device_id not in batch:
        raise InvalidArgument("设备 %s 不在发布 %s 的当前批次中" % (device_id, release_id))
    if device_id in release["reports"]:
        raise InvalidArgument("设备 %s 已提交过报告" % device_id)

    release["reports"][device_id] = result
    device["heartbeatAt"] = heartbeat_at
    if result == "success":
        device["version"] = release["version"]

    if all(d in release["reports"] for d in batch):
        _advance_batch(state, release, batch)

    summary = _release_summary(release)
    summary["report"] = {
        "releaseId": release_id,
        "deviceId": device_id,
        "result": result,
        "heartbeatAt": heartbeat_at,
    }
    return summary


def _advance_batch(state, release, batch):
    """整批报告到齐后评估：超阈值回滚，否则进入下一批或完成。"""
    failed = sum(1 for d in batch if release["reports"][d] == "failure")
    if failed * 100 > len(batch) * release["maxFailurePercent"]:
        release["status"] = RELEASE_ROLLED_BACK
        for device_id in batch:
            state["devices"][device_id]["version"] = release["previousVersion"]
        return
    if release["currentBatch"] + 1 >= len(_batches(release)):
        release["status"] = RELEASE_COMPLETED
    else:
        release["currentBatch"] += 1


def release_status(state, release_id):
    _require_id(release_id, "release-id")
    release = _get_release(state, release_id)
    return _release_summary(release, state=state, full=True)


# ---------------------------------------------------------------- 输出

def _release_summary(release, state=None, full=False):
    batches = _batches(release)
    summary = {
        "releaseId": release["releaseId"],
        "status": release["status"],
        "version": release["version"],
        "previousVersion": release["previousVersion"],
        "batchSize": release["batchSize"],
        "maxFailurePercent": release["maxFailurePercent"],
        "totalBatches": len(batches),
        "currentBatch": release["currentBatch"] + 1 if batches else 0,
        "devices": list(release["devices"]),
    }
    if release["status"] == RELEASE_IN_PROGRESS and batches:
        batch = batches[release["currentBatch"]]
        summary["pendingDevices"] = [
            d for d in batch if d not in release["reports"]
        ]
    else:
        summary["pendingDevices"] = []
    if full and state is not None:
        summary["reports"] = dict(release["reports"])
        summary["deviceDetails"] = [
            {
                "deviceId": device_id,
                "version": state["devices"][device_id]["version"],
                "heartbeatAt": state["devices"][device_id]["heartbeatAt"],
                "report": release["reports"].get(device_id),
            }
            for device_id in release["devices"]
        ]
    return summary
