"""canary 批次灰度推进与自动故障回滚的端到端测试。

直接驱动 cli.main(argv)，使用临时状态文件，验证 stdout JSON 与退出码。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class CliHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = os.path.join(self.tmp.name, "state.json")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv, expect=0):
        proc = subprocess.run(
            [sys.executable, "-m", "ota_canary", "--state", self.state_path, *argv],
            cwd=MODULE_DIR,
            capture_output=True,
            text=True,
        )
        if proc.returncode != expect:
            self.fail(
                "cli %s expected exit %d got %d\nstdout:%s\nstderr:%s"
                % (argv, expect, proc.returncode, proc.stdout, proc.stderr)
            )
        if expect == 0:
            return json.loads(proc.stdout)
        return json.loads(proc.stderr)

    def add_device(self, device_id, version="1.0.0", at="2026-10-01T08:00:00Z"):
        return self.run_cli(
            "device", "add", "--device-id", device_id,
            "--version", version, "--heartbeat-at", at,
        )

    def add_firmware(self, version):
        return self.run_cli("canary", "firmware-add", "--version", version)

    def heartbeat(self, device_id, version, at):
        return self.run_cli(
            "device", "heartbeat", "--device-id", device_id,
            "--version", version, "--heartbeat-at", at,
        )

    def report_upgrade(self, device_id, result, at, batch_id="B1"):
        return self.run_cli(
            "device", "report", "--batch-id", batch_id,
            "--device-id", device_id, "--result", result, "--heartbeat-at", at,
        )

    def report_rollback(self, device_id, result, at, batch_id="B1"):
        return self.run_cli(
            "device", "report", "--batch-id", batch_id, "--phase", "rollback",
            "--device-id", device_id, "--result", result, "--heartbeat-at", at,
        )

    def status(self, batch_id="B1"):
        return self.run_cli("canary", "status", "--batch-id", batch_id)

    def check(self, at, batch_id="B1"):
        return self.run_cli("canary", "check", "--batch-id", batch_id, "--at", at)

    def create_batch(self, *device_ids, batch_id="B1", size="2", threshold="0.5",
                     timeout="900", target="2.0.0", stable="1.0.0", expect=0):
        argv = [
            "canary", "batch-create", "--batch-id", batch_id,
            "--target-version", target, "--stable-version", stable,
            "--batch-size", size, "--failure-threshold", threshold,
            "--heartbeat-timeout-seconds", timeout,
        ]
        for device_id in device_ids:
            argv.extend(["--device-id", device_id])
        return self.run_cli(*argv, expect=expect)

    def start(self, batch_id="B1"):
        return self.run_cli("canary", "start", "--batch-id", batch_id)


class BatchCreateValidationTests(CliHarness):
    def test_unique_errors(self):
        for d in ("d1",):
            self.add_device(d)
        self.add_firmware("2.0.0")
        self.add_firmware("1.0.0")

        # 固件缺失 -> FirmwareNotFound
        err = self.create_batch("d1", target="9.9.9", expect=1)
        self.assertEqual(err["error"], "FirmwareNotFound")

        # 目标与稳定版本相同 -> FirmwareNotFound
        err = self.create_batch("d1", target="2.0.0", stable="2.0.0", expect=1)
        self.assertEqual(err["error"], "FirmwareNotFound")

        # 空设备集合 -> EmptyDeviceSet
        err = self.create_batch(batch_id="Bempty", expect=1)
        self.assertEqual(err["error"], "EmptyDeviceSet")

        # 非法策略 -> InvalidBatchPolicy
        err = self.create_batch("d1", batch_id="Bp1", size="0", expect=1)
        self.assertEqual(err["error"], "InvalidBatchPolicy")
        err = self.create_batch("d1", batch_id="Bp2", threshold="0", expect=1)
        self.assertEqual(err["error"], "InvalidBatchPolicy")
        err = self.create_batch("d1", batch_id="Bp3", threshold="1", expect=1)
        self.assertEqual(err["error"], "InvalidBatchPolicy")
        err = self.create_batch("d1", batch_id="Bp4", threshold="abc", expect=1)
        self.assertEqual(err["error"], "InvalidBatchPolicy")
        err = self.create_batch("d1", batch_id="Bp5", timeout="0", expect=1)
        self.assertEqual(err["error"], "InvalidBatchPolicy")

        # 成功创建
        view = self.create_batch("d1", batch_id="Bok", size="1")
        self.assertEqual(view["status"], "QUEUED")
        # 编号重复 -> BatchConflict
        err = self.create_batch("d1", batch_id="Bok", size="1", expect=1)
        self.assertEqual(err["error"], "BatchConflict")


class FullRolloutTests(CliHarness):
    def test_wave_by_wave_completion(self):
        for d in ("d1", "d2", "d3", "d4"):
            self.add_device(d)
        self.add_firmware("1.0.0")
        self.add_firmware("2.0.0")
        self.create_batch("d1", "d2", "d3", "d4", size="2", threshold="0.5")
        view = self.start()
        self.assertEqual(view["status"], "RUNNING")
        self.assertEqual(view["currentWave"], 0)
        self.assertEqual(view["futureWavesFrozen"], False)
        stages = {d["deviceId"]: d["stage"] for d in view["devices"]}
        self.assertEqual(stages["d1"], "pending_upgrade")
        self.assertEqual(stages["d2"], "pending_upgrade")
        self.assertEqual(stages["d3"], "queued")
        self.assertEqual(stages["d4"], "queued")

        # d1：先升级中心跳，后目标版本心跳与成功终态
        self.heartbeat("d1", "1.0.0", "2026-10-01T09:00:00Z")
        v = self.status()
        self.assertEqual(next(d for d in v["devices"] if d["deviceId"] == "d1")["stage"],
                         "upgrading")
        self.heartbeat("d1", "2.0.0", "2026-10-01T09:05:00Z")
        v = self.status()
        self.assertEqual(next(d for d in v["devices"] if d["deviceId"] == "d1")["stage"],
                         "waiting_heartbeat")
        v = self.report_upgrade("d1", "success", "2026-10-01T09:05:30Z")
        self.assertEqual(next(d for d in v["devices"] if d["deviceId"] == "d1")["stage"],
                         "success")

        # d2：终态先到（等待心跳），目标版本心跳后补齐成功，自动推进下一批
        v = self.report_upgrade("d2", "success", "2026-10-01T09:06:00Z")
        self.assertEqual(next(d for d in v["devices"] if d["deviceId"] == "d2")["stage"],
                         "waiting_heartbeat")
        self.assertEqual(v["status"], "RUNNING")
        self.assertEqual(v["currentWave"], 0)
        self.heartbeat("d2", "2.0.0", "2026-10-01T09:06:30Z")
        v = self.status()
        self.assertEqual(v["currentWave"], 1)
        self.assertEqual(
            next(d for d in v["devices"] if d["deviceId"] == "d3")["stage"],
            "pending_upgrade",
        )

        # 第二波全部成功
        self.report_upgrade("d3", "success", "2026-10-01T09:10:00Z")
        self.heartbeat("d3", "2.0.0", "2026-10-01T09:10:30Z")
        self.report_upgrade("d4", "success", "2026-10-01T09:11:00Z")
        self.heartbeat("d4", "2.0.0", "2026-10-01T09:11:30Z")
        v = self.status()
        self.assertEqual(v["status"], "COMPLETED")
        self.assertEqual(v["completedCount"], 4)
        self.assertEqual(v["failedCount"], 0)
        self.assertEqual(v["currentFailureRate"], 0.0)

    def test_failure_rate_equal_threshold_continues(self):
        for d in ("d1", "d2", "d3", "d4"):
            self.add_device(d)
        self.add_firmware("1.0.0")
        self.add_firmware("2.0.0")
        self.create_batch("d1", "d2", "d3", "d4", size="2", threshold="0.5")
        self.start()
        # 50% 失败，比例不严格大于 0.5 -> 继续
        self.report_upgrade("d1", "success", "2026-10-01T09:05:00Z")
        self.heartbeat("d1", "2.0.0", "2026-10-01T09:05:30Z")
        v = self.report_upgrade("d2", "failure", "2026-10-01T09:05:00Z")
        self.assertEqual(v["status"], "RUNNING")
        self.assertEqual(v["currentWave"], 1)
        self.assertAlmostEqual(v["currentFailureRate"], 0.0)

    def test_idempotent_reports_and_late_heartbeat(self):
        for d in ("d1", "d2"):
            self.add_device(d)
        self.add_firmware("1.0.0")
        self.add_firmware("2.0.0")
        self.create_batch("d1", "d2", size="2", threshold="0.49")
        self.start()
        # d1 成功
        self.report_upgrade("d1", "success", "2026-10-01T09:05:00Z")
        self.heartbeat("d1", "2.0.0", "2026-10-01T09:05:30Z")
        # 重复成功幂等，不增加失败率，也不报错
        v = self.report_upgrade("d1", "success", "2026-10-01T09:06:00Z")
        self.assertEqual(v["failedCount"], 0)
        # d2 失败
        v = self.report_upgrade("d2", "failure", "2026-10-01T09:06:00Z")
        self.assertEqual(v["status"], "FAILED_STOPPED")
        # 晚到的成功终态 / 目标版本心跳都不得改回成功
        self.report_upgrade("d2", "success", "2026-10-01T09:07:00Z")
        self.heartbeat("d2", "2.0.0", "2026-10-01T09:07:30Z")
        v = self.status()
        d2 = next(d for d in v["devices"] if d["deviceId"] == "d2")
        self.assertEqual(d2["stage"], "failed")
        self.assertEqual(d2["terminalResult"], "failure")


class StopAndRollbackTests(CliHarness):
    def _prepare_two_waves(self, threshold="0.49"):
        for d in ("d1", "d2", "d3", "d4"):
            self.add_device(d)
        self.add_firmware("1.0.0")
        self.add_firmware("2.0.0")
        self.create_batch("d1", "d2", "d3", "d4", size="2", threshold=threshold)
        self.start()
        # 第一波：d1 成功（并已心跳目标版本），d2 失败 -> 失败率 0.5 > 0.49
        self.report_upgrade("d1", "success", "2026-10-01T09:05:00Z")
        self.heartbeat("d1", "2.0.0", "2026-10-01T09:05:30Z")
        v = self.report_upgrade("d2", "failure", "2026-10-01T09:06:00Z")
        return v

    def test_stop_freezes_future_waves_and_dispatches_rollback(self):
        v = self._prepare_two_waves()
        self.assertEqual(v["status"], "FAILED_STOPPED")
        self.assertEqual(v["frozen"], True)
        self.assertEqual(v["futureWavesFrozen"], True)
        self.assertEqual(v["currentWave"], 0)
        stages = {d["deviceId"]: d["stage"] for d in v["devices"]}
        self.assertEqual(stages["d1"], "rolling_back")
        self.assertEqual(stages["d2"], "failed")
        self.assertEqual(stages["d3"], "queued")
        self.assertEqual(stages["d4"], "queued")
        d1 = next(d for d in v["devices"] if d["deviceId"] == "d1")
        self.assertEqual(d1["rollback"]["status"], "PENDING")

        # 冻结后后续设备不得再接收升级任务/上报
        err = self.run_cli(
            "device", "report", "--batch-id", "B1", "--device-id", "d3",
            "--result", "success", "--heartbeat-at", "2026-10-01T09:10:00Z",
            expect=1,
        )
        self.assertEqual(err["error"], "InvalidState")

        # 回滚成功
        v = self.report_rollback("d1", "success", "2026-10-01T09:12:00Z")
        self.assertEqual(v["status"], "ROLLED_BACK")
        d1 = next(d for d in v["devices"] if d["deviceId"] == "d1")
        self.assertEqual(d1["stage"], "rollback_succeeded")
        # 回滚成功后设备版本恢复为稳定版本
        self.assertEqual(d1["version"], "1.0.0")
        d3 = next(d for d in v["devices"] if d["deviceId"] == "d3")
        self.assertEqual(d3["stage"], "queued")

    def test_rollback_failure_terminal(self):
        self._prepare_two_waves()
        v = self.report_rollback("d1", "failure", "2026-10-01T09:12:00Z")
        self.assertEqual(v["status"], "ROLLBACK_FAILED")
        d1 = next(d for d in v["devices"] if d["deviceId"] == "d1")
        self.assertEqual(d1["stage"], "rollback_failed")
        # 重复回滚结果幂等
        v2 = self.report_rollback("d1", "success", "2026-10-01T09:13:00Z")
        self.assertEqual(v2["status"], "ROLLBACK_FAILED")
        d1 = next(d for d in v2["devices"] if d["deviceId"] == "d1")
        self.assertEqual(d1["stage"], "rollback_failed")

    def test_rollback_only_for_previously_succeeded(self):
        for d in ("d1", "d2", "d3"):
            self.add_device(d)
        self.add_firmware("1.0.0")
        self.add_firmware("2.0.0")
        # size 2: 第一波 d1,d2 全失败 -> 失败率 1.0
        self.create_batch("d1", "d2", "d3", size="2", threshold="0.5")
        self.start()
        v = self.report_upgrade("d1", "failure", "2026-10-01T09:05:00Z")
        v = self.report_upgrade("d2", "failure", "2026-10-01T09:05:30Z")
        # 没有任何成功设备 -> 无回滚任务，立即收敛 ROLLED_BACK
        self.assertEqual(v["status"], "ROLLED_BACK")
        self.assertEqual(v["frozen"], True)
        self.assertEqual(
            next(d for d in v["devices"] if d["deviceId"] == "d3")["stage"], "queued"
        )


class HeartbeatTimeoutTests(CliHarness):
    def test_timeout_marks_failure_and_can_stop(self):
        for d in ("d1", "d2"):
            self.add_device(d)
        self.add_firmware("1.0.0")
        self.add_firmware("2.0.0")
        self.create_batch("d1", "d2", size="2", threshold="0.49", timeout="900")
        self.start()
        # d1 成功
        self.report_upgrade("d1", "success", "2026-10-01T09:05:00Z")
        self.heartbeat("d1", "2.0.0", "2026-10-01T09:05:30Z")
        # d2 仅在 09:00 有一次升级中心跳，09:20 check 已超时
        self.heartbeat("d2", "1.0.0", "2026-10-01T09:00:00Z")
        v = self.check("2026-10-01T09:15:00Z")
        self.assertEqual(v["expiredDevices"], [])
        d2 = next(d for d in v["devices"] if d["deviceId"] == "d2")
        self.assertEqual(d2["stage"], "upgrading")
        v = self.check("2026-10-01T09:15:01Z")
        self.assertEqual(v["expiredDevices"], ["d2"])
        self.assertEqual(v["status"], "FAILED_STOPPED")
        d2 = next(d for d in v["devices"] if d["deviceId"] == "d2")
        self.assertEqual(d2["stage"], "failed")
        self.assertEqual(d2["failedReason"], "heartbeat_timeout")
        # 晚到心跳不得改回成功
        self.heartbeat("d2", "2.0.0", "2026-10-01T09:30:00Z")
        v = self.status()
        self.assertEqual(
            next(d for d in v["devices"] if d["deviceId"] == "d2")["stage"], "failed"
        )


class ExistingBehaviorRegressionTests(CliHarness):
    def test_legacy_release_flow_unchanged(self):
        for d in ("d1", "d2"):
            self.add_device(d)
        self.run_cli(
            "release", "create", "--release-id", "R1", "--version", "2.0.0",
            "--previous-version", "1.0.0", "--batch-size", "2",
            "--max-failure-percent", "50",
        )
        self.run_cli("release", "start", "--release-id", "R1")
        v = self.run_cli(
            "device", "report", "--release-id", "R1", "--device-id", "d1",
            "--result", "success", "--heartbeat-at", "2026-10-01T10:00:00Z",
        )
        # 旧视图不含 canary 字段
        self.assertNotIn("futureWavesFrozen", v)
        self.run_cli(
            "device", "report", "--release-id", "R1", "--device-id", "d2",
            "--result", "success", "--heartbeat-at", "2026-10-01T10:00:00Z",
        )
        v = self.run_cli("status", "--release-id", "R1")
        self.assertEqual(v["status"], "completed")

    def test_report_requires_exactly_one_identifier(self):
        self.add_device("d1")
        err = self.run_cli(
            "device", "report", "--device-id", "d1",
            "--result", "success", "--heartbeat-at", "2026-10-01T10:00:00Z",
            expect=1,
        )
        self.assertEqual(err["error"], "InvalidArgument")
        err = self.run_cli(
            "device", "report", "--release-id", "R1", "--batch-id", "B1",
            "--device-id", "d1", "--result", "success",
            "--heartbeat-at", "2026-10-01T10:00:00Z",
            expect=1,
        )
        self.assertEqual(err["error"], "InvalidArgument")

    def test_unknown_batch(self):
        err = self.run_cli("canary", "status", "--batch-id", "nope", expect=1)
        self.assertEqual(err["error"], "DeviceNotFound")
        err = self.run_cli("canary", "start", "--batch-id", "nope", expect=1)
        self.assertEqual(err["error"], "DeviceNotFound")


if __name__ == "__main__":
    unittest.main()
