# 左手编码器方向修正

Status: implemented (2026-09-09)

## What

- Problem / goal: 左手开合角度的增长方向与右手相反，ROS 和采集数据中显示为负值。
- User-visible behavior: 从标定闭合位置打开左夹爪时，左手角度与右手一样沿正方向增长。
- Success criteria: 左侧方向系数为 `-1`，右侧仍为 `+1`；设备端口、115200 波特率、`DEVICE_ID,radians` 协议和 ROS 话题不变；运行时配置测试覆盖该约定。
- Out of scope: 修改固件角度值、右手方向、历史数据或机械零位。

## How

- Approach: 使用编码器节点已有的左右独立方向参数，只将统一源配置中的 `left_rotation_direction` 设为 `-1`，并在配置回归测试中固定左右符号。
- Components / files touched: `galaxis_bringup/config/galaxis_config.yaml`、`galaxis_bringup/test/test_build_runtime_configs.py`。
- Risks / unknowns: 如左侧旧标定点与当前机械闭合位置不一致，方向修正后仍需重新执行 Setup 捕获闭合零位。
- Safety boundary: 仅修改本地配置与测试，不改写已采集 HDF5/rosbag，不启动硬件服务。
- Verification plan: 生成运行时配置，检查左 `-1`/右 `+1`，运行编码器协议与配置回归测试。

## Verification result

- `make build-config` 已生成当前 profile 的运行时配置，实际值为左 `-1`/右 `+1`。
- 编码器协议与配置回归测试共 39 项通过；`git diff --check` 通过。
- 未主动重启采集容器，防止中断用户正在进行的 Episode；下次重启采集服务后生效。
