"""端到端验证回滚收束时限（rollback-timeout-seconds）增量需求。"""
import json
import subprocess
import sys
import tempfile
import os

PY = sys.executable
FAILURES = []


def run(args, state_path, expect_fail=False):
    proc = subprocess.run(
        [PY, "-m", "ota_canary", "--state", state_path] + args,
        capture_output=True, text=True, cwd=os.path.dirname(os.path.abspath(__file__)),
    )
    if expect_fail:
        if proc.returncode == 0:
            raise AssertionError("expected failure but got success: %s\n%s" % (args, proc.stdout))
        return json.loads(proc.stderr)
    if proc.returncode != 0:
        raise AssertionError("command failed: %s\n%s" % (args, proc.stderr))
    return json.loads(proc.stdout)


def check(name, cond, detail=""):
    if cond:
        print("PASS:", name)
    else:
        FAILURES.append(name)
        print("FAIL:", name, detail)


def setup_device(state, did, version="1.0.0", at="2026-10-01T07:00:00Z"):
    run(["device", "add", "--device-id", did, "--version", version, "--heartbeat-at", at], state)


def create_batch(state, bid="B1", timeout=None, devices=("d1", "d2"), threshold="0.5",
                 stab=None, batch_size="2"):
    args = ["batch", "create", "--batch-id", bid, "--target-version", "2.0.0",
            "--stable-version", "1.0.0", "--batch-size", batch_size,
            "--failure-threshold", threshold, "--heartbeat-timeout-seconds", "900"]
    if timeout is not None:
        args += ["--rollback-timeout-seconds", str(timeout)]
    if stab is not None:
        args += ["--stabilization-seconds", str(stab)]
    for d in devices:
        args += ["--target-device-id", d]
    return run(args, state)


def main():
    d = tempfile.mkdtemp()

    # ---- 1. 非法 rollback-timeout-seconds 返回 InvalidBatchPolicy 且不写状态 ----
    for bad in ("0", "-1", "abc", "1.5"):
        state = os.path.join(d, "s_bad_%s.json" % bad.replace("-", "m"))
        setup_device(state, "d1")
        run(["firmware", "register", "--version", "2.0.0"], state)
        err = run(["batch", "create", "--batch-id", "B1", "--target-version", "2.0.0",
                   "--stable-version", "1.0.0", "--batch-size", "2",
                   "--failure-threshold", "0.5", "--heartbeat-timeout-seconds", "900",
                   "--rollback-timeout-seconds", bad, "--target-device-id", "d1"],
                  state, expect_fail=True)
        check("invalid rollback-timeout %r -> InvalidBatchPolicy" % bad,
              err["error"] == "InvalidBatchPolicy", err)
        data = json.load(open(state, encoding="utf-8"))
        check("invalid value %r does not write state" % bad, "batchRollouts" not in data)

    # ---- 2. 缺省 900；合法值落盘 ----
    state = os.path.join(d, "s_default.json")
    setup_device(state, "d1")
    run(["firmware", "register", "--version", "2.0.0"], state)
    view = create_batch(state, bid="B1", timeout=None, devices=("d1",))
    check("default rollbackTimeoutSeconds=900", view["rollbackTimeoutSeconds"] == 900)
    check("default rollbackDeadline null", view["rollbackDeadline"] is None)
    check("default rollbackTimedOutCount 0", view["rollbackTimedOutCount"] == 0)

    # ---- 3. 主流程：两台设备，第一批都成功 -> completed 路径不涉及回滚 deadline ----
    # 构造失败率超阈值停止：batch-size=2，d1 success d2 failure(threshold 0.5: 1/2 不大于 0.5)
    # 用 threshold 0.4 使 1/2=0.5 > 0.4 触发停止。
    state = os.path.join(d, "s_main.json")
    setup_device(state, "d1")
    setup_device(state, "d2")
    run(["firmware", "register", "--version", "2.0.0"], state)
    create_batch(state, bid="B1", timeout="600", devices=("d1", "d2"), threshold="0.4")
    run(["batch", "start", "--batch-id", "B1", "--at", "2026-10-01T08:00:00Z"], state)
    run(["batch", "report", "--batch-id", "B1", "--device-id", "d1",
         "--version", "2.0.0", "--heartbeat-at", "2026-10-01T08:05:00Z",
         "--result", "success"], state)
    v = run(["batch", "report", "--batch-id", "B1", "--device-id", "d2",
             "--version", "1.0.0", "--heartbeat-at", "2026-10-01T08:06:00Z",
             "--result", "failure"], state)
    check("auto stop -> failed_stopped", v["status"] == "failed_stopped", v["status"])
    check("rollbackDeadline = stop+600",
          v["rollbackDeadline"] == "2026-10-01T08:16:00Z", v.get("rollbackDeadline"))
    # 找到 d1 的回滚记录
    d1 = next(x for x in v["devices"] if x["deviceId"] == "d1")
    check("rollback pending with reason null",
          d1["rollback"] == {"state": "pending", "reason": None,
                             "heartbeatAt": None, "version": None}, d1["rollback"])

    # at == deadline: check 不超时，rollbackExpiredDeviceIds 空
    v = run(["batch", "check", "--batch-id", "B1", "--at", "2026-10-01T08:16:00Z"], state)
    check("at == deadline -> not expired", v["rollbackExpiredDeviceIds"] == [])
    check("still failed_stopped at deadline", v["status"] == "failed_stopped")
    d1 = next(x for x in v["devices"] if x["deviceId"] == "d1")
    check("still pending at deadline", d1["rollback"]["state"] == "pending")

    # at 早于 deadline：不变
    v = run(["batch", "check", "--batch-id", "B1", "--at", "2026-10-01T08:10:00Z"], state)
    check("at < deadline -> empty", v["rollbackExpiredDeviceIds"] == [])
    check("status unchanged before deadline", v["status"] == "failed_stopped")

    # at == deadline 仍可正常上报成功
    v = run(["batch", "rollback-report", "--batch-id", "B1", "--device-id", "d1",
             "--result", "success", "--version", "1.0.0",
             "--heartbeat-at", "2026-10-01T08:16:00Z"], state)
    check("report at deadline succeeds", v["report"]["idempotent"] is False)
    check("after sole rollback success -> rolled_back", v["status"] == "rolled_back", v["status"])
    dev = json.load(open(state, encoding="utf-8"))["devices"]["d1"]
    check("device version restored", dev["version"] == "1.0.0")

    # ---- 4. 超时路径：两台成功后失败率停止，两台回滚任务，一台正常失败，一台超时 ----
    state = os.path.join(d, "s_timeout.json")
    for did in ("d1", "d2", "d3"):
        setup_device(state, did)
    run(["firmware", "register", "--version", "2.0.0"], state)
    # batch-size 3: 2 success 1 failure -> 1/3 > 0.3 停止，两台需回滚
    create_batch(state, bid="B1", timeout="600", devices=("d1", "d2", "d3"),
                 threshold="0.3", batch_size="3")
    run(["batch", "start", "--batch-id", "B1", "--at", "2026-10-01T08:00:00Z"], state)
    for did, hb in (("d1", "08:05"), ("d2", "08:06"), ("d3", "08:07")):
        res = "failure" if did == "d3" else "success"
        run(["batch", "report", "--batch-id", "B1", "--device-id", did,
             "--version", "2.0.0" if res == "success" else "1.0.0",
             "--heartbeat-at", "2026-10-01T%s:00Z" % hb, "--result", res], state)
    v = run(["batch", "status", "--batch-id", "B1"], state)
    check("stopped with 2 rollbacks", v["status"] == "failed_stopped"
          and v["rollbackPending"] == 2)
    check("deadline 08:17", v["rollbackDeadline"] == "2026-10-01T08:17:00Z", v["rollbackDeadline"])

    # d1 在 deadline 前正常回报失败
    v = run(["batch", "rollback-report", "--batch-id", "B1", "--device-id", "d1",
             "--result", "failure", "--version", "2.0.0",
             "--heartbeat-at", "2026-10-01T08:10:00Z"], state)
    d1 = next(x for x in v["devices"] if x["deviceId"] == "d1")
    check("reported failure reason=reported",
          d1["rollback"]["state"] == "failure"
          and d1["rollback"]["reason"] == "reported", d1["rollback"])
    check("phase rollback_failed", d1["phase"] == "rollback_failed")

    # check 严格晚于 deadline：d2 超时
    v = run(["batch", "check", "--batch-id", "B1", "--at", "2026-10-01T08:17:01Z"], state)
    check("expired list [d2]", v["rollbackExpiredDeviceIds"] == ["d2"],
          v.get("rollbackExpiredDeviceIds"))
    check("converged rollback_failed", v["status"] == "rollback_failed", v["status"])
    check("rollbackTimedOutCount=1", v["rollbackTimedOutCount"] == 1)
    check("rollbackFailed=2", v["rollbackFailed"] == 2)
    d2 = next(x for x in v["devices"] if x["deviceId"] == "d2")
    check("d2 timeout record",
          d2["rollback"]["state"] == "failure"
          and d2["rollback"]["reason"] == "timeout"
          and d2["rollback"]["heartbeatAt"] is None
          and d2["rollback"]["version"] is None, d2["rollback"])
    check("d2 phase rollback_failed", d2["phase"] == "rollback_failed")
    # 公开设备表 version/heartbeatAt 不变（d2 停在 2.0.0, 心跳 08:06）
    dev = json.load(open(state, encoding="utf-8"))["devices"]["d2"]
    check("device version unchanged on timeout", dev["version"] == "2.0.0", dev)
    check("device heartbeat unchanged on timeout",
          dev["heartbeatAt"] == "2026-10-01T08:06:00Z", dev)

    # 时间线事件
    tl = run(["batch", "timeline", "--batch-id", "B1"], state)
    to = [e for e in tl["events"] if e["type"] == "rollback_timed_out"]
    check("one rollback_timed_out event", len(to) == 1, to)
    e = to[0]
    check("event fields",
          e["deviceId"] == "d2" and e["result"] == "failure"
          and e["reason"] == "timeout" and e["phaseTo"] == "rollback_failed"
          and e["occurredAt"] == "2026-10-01T08:17:01Z", e)

    # 重复 check：deadline 前的 check 不变更/不追加；deadline 后的超时只记一次。
    # 终态后再 check 沿用既有 InvalidState 语义，事件也不重复追加。
    # （此处状态已收束 rollback_failed，用只读查询确认现场稳定。）
    v = run(["batch", "status", "--batch-id", "B1", "--at", "2026-10-01T09:00:00Z"], state)
    check("repeat query status stable", v["status"] == "rollback_failed")
    check("repeat query expired counts stable",
          v["rollbackFailed"] == 2 and v["rollbackTimedOutCount"] == 1)
    err = run(["batch", "check", "--batch-id", "B1", "--at", "2026-10-01T09:00:00Z"],
              state, expect_fail=True)
    check("check after terminal -> InvalidState (existing semantics)",
          err["error"] == "InvalidState", err)
    tl2 = run(["batch", "timeline", "--batch-id", "B1"], state)
    check("no duplicate events after repeat check", len(tl2["events"]) == len(tl["events"]))

    # 超时后 d2 重复 failure 幂等不计数
    v = run(["batch", "rollback-report", "--batch-id", "B1", "--device-id", "d2",
             "--result", "failure", "--version", "2.0.0",
             "--heartbeat-at", "2026-10-01T09:05:00Z"], state)
    check("repeat failure idempotent", v["report"]["idempotent"] is True)
    check("counts stable after idempotent",
          v["rollbackFailed"] == 2 and v["rollbackTimedOutCount"] == 1)
    d2 = next(x for x in v["devices"] if x["deviceId"] == "d2")
    check("timeout record preserved", d2["rollback"]["reason"] == "timeout")

    # 超时后 success 返回 InvalidArgument 且不改状态
    err = run(["batch", "rollback-report", "--batch-id", "B1", "--device-id", "d2",
               "--result", "success", "--version", "1.0.0",
               "--heartbeat-at", "2026-10-01T09:06:00Z"], state, expect_fail=True)
    check("late success -> InvalidArgument", err["error"] == "InvalidArgument", err)
    raw = json.load(open(state, encoding="utf-8"))
    d2r = raw["batchRollouts"]["B1"]["devices"]["d2"]["rollback"]
    check("state unchanged after rejected success",
          d2r["state"] == "failure" and d2r["reason"] == "timeout", d2r)

    # ---- 5. 全部超时 -> rollback_failed；全部正常成功 -> rolled_back ----
    state = os.path.join(d, "s_all_timeout.json")
    for did in ("d1", "d2"):
        setup_device(state, did)
    run(["firmware", "register", "--version", "2.0.0"], state)
    create_batch(state, bid="B1", timeout="120", devices=("d1", "d2"), threshold="0.4")
    run(["batch", "start", "--batch-id", "B1", "--at", "2026-10-01T08:00:00Z"], state)
    # d1 success; d2 failure => 1/2 > 0.4 stop
    run(["batch", "report", "--batch-id", "B1", "--device-id", "d1",
         "--version", "2.0.0", "--heartbeat-at", "2026-10-01T08:05:00Z",
         "--result", "success"], state)
    run(["batch", "report", "--batch-id", "B1", "--device-id", "d2",
         "--version", "1.0.0", "--heartbeat-at", "2026-10-01T08:06:00Z",
         "--result", "failure"], state)
    v = run(["batch", "check", "--batch-id", "B1", "--at", "2026-10-01T08:10:00Z"], state)
    check("single rollback timeout -> rollback_failed",
          v["status"] == "rollback_failed"
          and v["rollbackExpiredDeviceIds"] == ["d1"], (v["status"], v.get("rollbackExpiredDeviceIds")))

    # ---- 6. abort 路径：paused abort 后 deadline 用 abort 时刻 ----
    state = os.path.join(d, "s_abort.json")
    for did in ("d1", "d2"):
        setup_device(state, did)
    run(["firmware", "register", "--version", "2.0.0"], state)
    create_batch(state, bid="B1", timeout="300", devices=("d1", "d2"))
    run(["batch", "start", "--batch-id", "B1", "--at", "2026-10-01T08:00:00Z"], state)
    run(["batch", "report", "--batch-id", "B1", "--device-id", "d1",
         "--version", "2.0.0", "--heartbeat-at", "2026-10-01T08:05:00Z",
         "--result", "success"], state)
    run(["batch", "pause", "--batch-id", "B1", "--at", "2026-10-01T08:10:00Z"], state)
    v = run(["batch", "abort", "--batch-id", "B1", "--reason", "manual stop",
             "--at", "2026-10-01T08:20:00Z"], state)
    check("abort deadline = abort+300",
          v["rollbackDeadline"] == "2026-10-01T08:25:00Z", v.get("rollbackDeadline"))
    check("abort paused -> failed_stopped pending rollback",
          v["status"] == "failed_stopped" and v["rollbackPending"] == 1)
    tl = run(["batch", "timeline", "--batch-id", "B1"], state)
    ab = [e for e in tl["events"] if e["type"] == "aborted"]
    check("aborted event phaseFrom paused", ab and ab[0]["phaseFrom"] == "paused")

    # ---- 7. 无回滚任务停止：rollbackDeadline null，直接/ check 收束 rolled_back ----
    state = os.path.join(d, "s_noroll.json")
    for did in ("d1", "d2"):
        setup_device(state, did)
    run(["firmware", "register", "--version", "2.0.0"], state)
    create_batch(state, bid="B1", timeout="600", devices=("d1", "d2"), threshold="0.9")
    run(["batch", "start", "--batch-id", "B1", "--at", "2026-10-01T08:00:00Z"], state)
    # 两台都 failure（无成功设备 -> 无回滚任务）；2/2 > 0.9 停止
    for did in ("d1", "d2"):
        run(["batch", "report", "--batch-id", "B1", "--device-id", did,
             "--version", "1.0.0", "--heartbeat-at", "2026-10-01T08:05:00Z",
             "--result", "failure"], state)
    v = run(["batch", "status", "--batch-id", "B1"], state)
    # 无任务在 report 路径 trigger_failure_stop 后保持 failed_stopped（无显式收束）
    check("no-rollback stop deadline null", v["rollbackDeadline"] is None)
    v = run(["batch", "check", "--batch-id", "B1", "--at", "2026-10-01T09:00:00Z"], state)
    check("no-rollback converges rolled_back", v["status"] == "rolled_back")
    check("no-rollback expired empty", v["rollbackExpiredDeviceIds"] == [])

    # ---- 8. abort 无成功设备：直接 rolled_back，deadline null ----
    state = os.path.join(d, "s_abort_empty.json")
    for did in ("d1",):
        setup_device(state, did)
    run(["firmware", "register", "--version", "2.0.0"], state)
    create_batch(state, bid="B1", timeout="600", devices=("d1",))
    run(["batch", "start", "--batch-id", "B1", "--at", "2026-10-01T08:00:00Z"], state)
    v = run(["batch", "abort", "--batch-id", "B1", "--reason", "x",
             "--at", "2026-10-01T08:30:00Z"], state)
    check("abort no tasks -> rolled_back immediately", v["status"] == "rolled_back")
    check("abort no tasks deadline null", v["rollbackDeadline"] is None)

    # ---- 9. 旧状态兼容：缺 rollbackTimeoutSeconds 按 900；failed_stopped 缺 deadline 显示 null ----
    state = os.path.join(d, "s_legacy.json")
    setup_device(state, "d1")
    run(["firmware", "register", "--version", "2.0.0"], state)
    create_batch(state, bid="B1", timeout="600", devices=("d1",))
    run(["batch", "start", "--batch-id", "B1", "--at", "2026-10-01T08:00:00Z"], state)
    run(["batch", "report", "--batch-id", "B1", "--device-id", "d1",
         "--version", "2.0.0", "--heartbeat-at", "2026-10-01T08:05:00Z",
         "--result", "success"], state)
    # 手工把状态篡改成旧格式：failed_stopped + pending 回滚但无 rollbackDeadline / 策略字段
    raw = json.load(open(state, encoding="utf-8"))
    r = raw["batchRollouts"]["B1"]
    r["status"] = "failed_stopped"
    r["frozen"] = True
    r["devices"]["d1"]["phase"] = "rolling_back"
    r["devices"]["d1"]["rollback"] = {"state": "pending",
                                      "heartbeatAt": None, "version": None}
    r.pop("rollbackDeadline", None)
    r.pop("rollbackTimeoutSeconds", None)
    json.dump(raw, open(state, "w", encoding="utf-8"))
    v = run(["batch", "status", "--batch-id", "B1"], state)
    check("legacy policy defaults 900", v["rollbackTimeoutSeconds"] == 900)
    check("legacy deadline shows null", v["rollbackDeadline"] is None)
    # 即使 at 很晩，也不猜超时：pending 不变，列表空
    v = run(["batch", "check", "--batch-id", "B1", "--at", "2026-10-02T00:00:00Z"], state)
    check("legacy no deadline -> no forced timeout",
          v["rollbackExpiredDeviceIds"] == [] and v["status"] == "failed_stopped")
    d1 = next(x for x in v["devices"] if x["deviceId"] == "d1")
    check("legacy pending preserved", d1["rollback"]["state"] == "pending")
    # 正常回报仍可收束
    v = run(["batch", "rollback-report", "--batch-id", "B1", "--device-id", "d1",
             "--result", "success", "--version", "1.0.0",
             "--heartbeat-at", "2026-10-02T00:05:00Z"], state)
    check("legacy report converges rolled_back", v["status"] == "rolled_back")

    # ---- 10. 部分超时 + 部分成功收束 -> rollback_failed；全部正常成功 -> rolled_back ----
    state = os.path.join(d, "s_mix.json")
    for did in ("d1", "d2", "d3", "d4"):
        setup_device(state, did)
    run(["firmware", "register", "--version", "2.0.0"], state)
    # 4 台一批：3 success 1 failure => 1/4=0.25, threshold 0.2 -> 停止
    create_batch(state, bid="B1", timeout="600", devices=("d1", "d2", "d3", "d4"),
                 threshold="0.2", batch_size="4")
    run(["batch", "start", "--batch-id", "B1", "--at", "2026-10-01T08:00:00Z"], state)
    for did, hb in (("d1", "08:05"), ("d2", "08:06"), ("d3", "08:07")):
        run(["batch", "report", "--batch-id", "B1", "--device-id", did,
             "--version", "2.0.0", "--heartbeat-at", "2026-10-01T%s:00Z" % hb,
             "--result", "success"], state)
    run(["batch", "report", "--batch-id", "B1", "--device-id", "d4",
         "--version", "1.0.0", "--heartbeat-at", "2026-10-01T08:08:00Z",
         "--result", "failure"], state)
    # d1/d2 正常回滚成功；d3 超时
    for did, hb in (("d1", "08:12"), ("d2", "08:13")):
        run(["batch", "rollback-report", "--batch-id", "B1", "--device-id", did,
             "--result", "success", "--version", "1.0.0",
             "--heartbeat-at", "2026-10-01T%s:00Z" % hb], state)
    v = run(["batch", "check", "--batch-id", "B1", "--at", "2026-10-01T08:20:00Z"], state)
    check("mixed -> rollback_failed", v["status"] == "rollback_failed")
    check("expired sorted [d3]", v["rollbackExpiredDeviceIds"] == ["d3"])
    check("timed out count 1, succeeded 2, failed 1",
          v["rollbackTimedOutCount"] == 1 and v["rollbackSucceeded"] == 2
          and v["rollbackFailed"] == 1, v)

    # rollback_timed_out 事件按 device id 升序（多台）
    state = os.path.join(d, "s_multi.json")
    for did in ("d1", "d2", "d3"):
        setup_device(state, did)
    run(["firmware", "register", "--version", "2.0.0"], state)
    create_batch(state, bid="B1", timeout="600", devices=("d1", "d2", "d3"),
                 threshold="0.3", batch_size="3")
    run(["batch", "start", "--batch-id", "B1", "--at", "2026-10-01T08:00:00Z"], state)
    run(["batch", "report", "--batch-id", "B1", "--device-id", "d3",
         "--version", "2.0.0", "--heartbeat-at", "2026-10-01T08:05:00Z",
         "--result", "success"], state)
    run(["batch", "report", "--batch-id", "B1", "--device-id", "d1",
         "--version", "2.0.0", "--heartbeat-at", "2026-10-01T08:06:00Z",
         "--result", "success"], state)
    run(["batch", "report", "--batch-id", "B1", "--device-id", "d2",
         "--version", "1.0.0", "--heartbeat-at", "2026-10-01T08:07:00Z",
         "--result", "failure"], state)
    v = run(["batch", "check", "--batch-id", "B1", "--at", "2026-10-01T08:30:00Z"], state)
    check("multi expired sorted", v["rollbackExpiredDeviceIds"] == ["d1", "d3"],
          v.get("rollbackExpiredDeviceIds"))
    tl = run(["batch", "timeline", "--batch-id", "B1"], state)
    tos = [e["deviceId"] for e in tl["events"] if e["type"] == "rollback_timed_out"]
    check("timeout events ascending", tos == ["d1", "d3"], tos)

    print()
    if FAILURES:
        print("%d FAILURES:" % len(FAILURES))
        for f in FAILURES:
            print(" -", f)
        sys.exit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
