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

# 建立并启动发布（--heartbeat-timeout-seconds 可选，缺省 900）
python -m ota_canary release create --release-id R1 --version 2.0.0 \
    --previous-version 1.0.0 --batch-size 2 --max-failure-percent 50 \
    --heartbeat-timeout-seconds 900
python -m ota_canary release start --release-id R1

# 当前批设备上报结果
python -m ota_canary device report --release-id R1 --device-id d1 \
    --result success --heartbeat-at 2026-10-01T10:00:00Z

# 心跳超时收批：为当前批超时未上报的设备写入 timeout 失败结果
python -m ota_canary release check --release-id R1 --at 2026-10-01T10:00:00Z

# 查看发布状态
python -m ota_canary status --release-id R1
```

行为约定：

- 版本号格式为 `MAJOR.MINOR.PATCH`，时间为 ISO 8601（支持 `Z` 后缀）。
- 启动发布时，仅选择 `currentVersion == previousVersion` 且未被进行中发布占用的设备，
  按 `device-id` 升序分批；空匹配的发布直接 `completed`。
- 整批到齐后，若 `failed * 100 > 设备数 * max-failure-percent`，发布变为 `rolled_back`，
  涉及设备回到 `previousVersion` 且不再下发；否则进入下一批，全部完成时变为 `completed`。
- `release check` 仅处理 `in_progress` 发布：当前批中尚未显式上报、且 `--at` 严格晚于
  `heartbeatAt + heartbeat-timeout-seconds` 的设备，写入 `result=failure`、`reason=timeout`
  的结果（`heartbeatAt` 保留原值，显式结果不被覆盖），随后按同一失败率规则收批；
  返回视图新增顶层 `expiredDevices`（按 device-id 升序，无成员为 `[]`）。
  时间与 `at` 均换算为 UTC 瞬间比较，无偏移量按 UTC。已超时设备不可再显式上报。
- 创建发布时可指定 `--heartbeat-timeout-seconds`（>= 1 的整数，缺省 900）；
  视图中的 `heartbeatTimeoutSeconds` 对旧发布同样按 900 展示。
- 成功的命令输出 JSON 并持久化状态；`status` 为只读，不产生业务变化（不补写超时结果）。
- 失败命令以非零退出码结束，向 stderr 输出 JSON 错误，且不修改状态文件。
  错误码：`DeviceNotFound`（设备或发布不存在）、`DeviceExists`（重复登记或重复发布）、
  `InvalidArgument`（参数非法、重复或非当前批报告）、`InvalidState`（推进非进行中发布）。

## 约定

- 公开行为以 README 与源码为准。
- 后续需求在此基线上增量实现。
