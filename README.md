# OTA Canary

设备固件灰度发布与故障回滚系统：按批次推进升级、跟踪心跳与版本状态，在失败率超阈值时自动停止并回滚到上一个稳定版本。

## 范围

本仓库从零开始实现上述方向的可用工具，不依赖外部同类实现。

## 状态

初始基线：只有本说明，尚无实现。

## 约定

- 公开行为以 README 与源码为准。
- 后续需求在此基线上增量实现。

## 使用

仅依赖 Python 3 标准库，通过 `python -m ota_canary` 调用。默认读写当前目录下的
`ota-canary-state.json`，可用 `--state <路径>` 指定其他状态文件（全局选项，放在子命令前后均可）。

```bash
# 登记设备 / 更新心跳
python -m ota_canary device add --device-id D1 --version 1.0.0 --heartbeat-at 2026-10-01T08:00:00Z
python -m ota_canary device heartbeat --device-id D1 --version 1.0.0 --heartbeat-at 2026-10-01T09:00:00Z

# 创建并启动发布
python -m ota_canary release create --release-id R1 --version 2.0.0 --previous-version 1.0.0 \
    --batch-size 2 --max-failure-percent 50
python -m ota_canary release start --release-id R1

# 设备上报升级结果（success|failure）
python -m ota_canary device report --release-id R1 --device-id D1 --result success \
    --heartbeat-at 2026-10-01T09:30:00Z

# 查看发布状态
python -m ota_canary status --release-id R1
```

### 行为规则

- `release start` 只选择 `version` 等于 `previous-version` 且未被进行中发布占用的设备，
  按 device-id 升序分批；空匹配时发布直接变为 `completed`。
- 当前批全部设备到齐后评估：若 `failed * 100 > 批设备数 * max-failure-percent`，
  发布变为 `rolled_back`，该批设备回退到 `previous-version` 且不再下发；
  否则进入下一批，全部批次完成时变为 `completed`。
- 上报成功会把设备版本推进到发布版本；`device heartbeat` 直接更新设备版本与最后心跳。
- 版本号须为 `X.Y.Z` 三段数字，时间须为 ISO 8601（如 `2026-10-01T08:00:00Z`）。

### 错误

失败命令输出 `{"error": ..., "message": ...}` 到 stderr，以非零码退出，且不修改状态文件：

| 错误码 | 场景 |
| --- | --- |
| `DeviceNotFound` | 找不到设备或发布 |
| `DeviceExists` | 重复登记设备或重复创建发布 |
| `InvalidArgument` | 版本/时间格式非法、阈值不在 0–100、batch-size 小于 1、重复或非当前批报告 |
| `InvalidState` | 推进非进行中的发布（如重复启动、向已结束的发布提交报告） |
