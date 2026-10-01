"""命令行入口：python -m ota_canary ..."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

from . import core
from .errors import OtaError

DEFAULT_STATE_FILE = "ota-canary-state.json"


def _load_state(path):
    if not os.path.exists(path):
        return core.empty_state()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise OtaError("状态文件无法读取: %s (%s)" % (path, exc))
    if not isinstance(data, dict):
        raise OtaError("状态文件格式非法: %s" % path)
    data.setdefault("devices", {})
    data.setdefault("releases", {})
    return data


def _save_state(path, state):
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".ota-canary-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m ota_canary",
        description="设备固件灰度发布与故障回滚工具",
    )
    parser.add_argument(
        "--state", default=DEFAULT_STATE_FILE,
        help="状态文件路径（默认 %(default)s）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_state_flag(p):
        # 允许 --state 出现在子命令之后
        p.add_argument("--state", default=argparse.SUPPRESS,
                       help="状态文件路径")

    device = sub.add_parser("device", help="设备相关命令")
    device_sub = device.add_subparsers(dest="device_command", required=True)

    p = device_sub.add_parser("add", help="登记设备")
    p.add_argument("--device-id", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--heartbeat-at", required=True)
    add_state_flag(p)

    p = device_sub.add_parser("heartbeat", help="更新设备版本与最后心跳")
    p.add_argument("--device-id", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--heartbeat-at", required=True)
    add_state_flag(p)

    p = device_sub.add_parser("report", help="提交设备升级结果")
    p.add_argument("--release-id", required=True)
    p.add_argument("--device-id", required=True)
    p.add_argument("--result", required=True)
    p.add_argument("--heartbeat-at", required=True)
    add_state_flag(p)

    release = sub.add_parser("release", help="发布相关命令")
    release_sub = release.add_subparsers(dest="release_command", required=True)

    p = release_sub.add_parser("create", help="创建发布")
    p.add_argument("--release-id", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--previous-version", required=True)
    p.add_argument("--batch-size", required=True)
    p.add_argument("--max-failure-percent", required=True)
    add_state_flag(p)

    p = release_sub.add_parser("start", help="启动发布")
    p.add_argument("--release-id", required=True)
    add_state_flag(p)

    p = sub.add_parser("status", help="查看发布状态")
    p.add_argument("--release-id", required=True)
    add_state_flag(p)

    return parser


def _dispatch(args, state):
    if args.command == "device":
        if args.device_command == "add":
            return core.device_add(
                state, args.device_id, args.version, args.heartbeat_at), True
        if args.device_command == "heartbeat":
            return core.device_heartbeat(
                state, args.device_id, args.version, args.heartbeat_at), True
        if args.device_command == "report":
            return core.device_report(
                state, args.release_id, args.device_id,
                args.result, args.heartbeat_at), True
    elif args.command == "release":
        if args.release_command == "create":
            return core.release_create(
                state, args.release_id, args.version, args.previous_version,
                args.batch_size, args.max_failure_percent), True
        if args.release_command == "start":
            return core.release_start(state, args.release_id), True
    elif args.command == "status":
        return core.release_status(state, args.release_id), False
    raise OtaError("未知命令")  # pragma: no cover


def main(argv=None):
    args = _build_parser().parse_args(argv)
    try:
        state = _load_state(args.state)
        result, mutated = _dispatch(args, state)
        if mutated:
            _save_state(args.state, state)
    except OtaError as exc:
        json.dump({"error": exc.code, "message": exc.message},
                  sys.stderr, ensure_ascii=False)
        sys.stderr.write("\n")
        return 1
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
