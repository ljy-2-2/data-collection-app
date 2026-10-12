# MT6835 编码器采集链路适配

Status: implemented (2026-09-01)

## What

- Problem / goal: 更换为 ESP32-C6-WROOM-1 + MT6835 后，让设备设置、串口读取、左右设备映射、标定和 ROS 数据采集继续正常工作。
- User-visible behavior: 设备设置后仍生成 `/dev/galaxis_left_esp32c6` 与 `/dev/galaxis_right_esp32c6`；采集端以 115200 baud 读取 `DEVICE_ID,radians`，忽略 ROM 启动文本和 `#` 诊断行。
- Success criteria: 运行配置不再写死旧开发板的 USB by-id；打开串口不会通过 DTR/RTS 复位新主控；断线后自动重连；现有 `/joint_states` 与左右夹爪话题、零位标定和方向配置保持不变；协议和运行配置测试通过。
- Out of scope: 改变固件输出格式、改变 ROS 话题或消息类型、写入 MT6835 EEPROM、直接修改 `/etc` 或烧录实物。

## How

- Approach: 将串口协议解析提取为无 ROS 依赖的模块；ROS reader 使用预配置 DTR/RTS 为低的打开流程；统一配置改用设备设置生成的角色 symlink；补充协议与配置回归测试。
- Components / files touched: `galaxis_bringup/galaxis_bringup/encoder_protocol.py`、`encoder_node.py`、`config/galaxis_config.yaml`、设备设置说明及测试。
- Risks / unknowns: 当前环境没有连接主控，无法读取新板实际 USB 枚举；设备设置以 ESP32-C6 原生 USB `303a:1001` 为默认，同时允许通过 `--esp-vid/--esp-pid` 选择 CH340、CP210x 等独立 USB-UART。无唯一序列号时左右身份绑定到物理 USB 端口，之后不能互换插口。
- Safety boundary: 仅修改本地代码和文档，不执行 udev 写入、烧录、容器启动或数据采集。
- Verification plan: 已通过 55 项串口协议、统一配置与采集入口测试、目标 Python 文件编译检查、shell 语法检查和 diff whitespace 检查，并已刷新 `_runtime/single` 配置；实物端最后运行设备设置并检查两个角色 symlink 和 ROS 话题频率。
