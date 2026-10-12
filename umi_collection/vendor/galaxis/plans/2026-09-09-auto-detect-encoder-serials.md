# 双执行器串口自动发现

Status: implemented

## What

- Problem / goal: portable 双臂 3 相机/2 执行器 Setup 以退出码 3 失败，尽管两块 MT6835 主控分别测试正常。
- User-visible behavior: 默认扫描 USB 串口并只接纳实际持续输出 `L###/R###,radians` 的 Galaxis 编码器；双臂模式识别到左右两块后正常进入 Setup。失败时列出所有候选串口及未通过原因。
- Success criteria: 不再依赖单一 `303a:1001` VID/PID；兼容原生 ESP32-C6 USB 与 USB-UART；不误把 ROM 日志或普通串口识别为编码器；保留 `--esp-vid/--esp-pid` 手动筛选；相关单元测试和原有采集测试通过。
- Out of scope: 修改固件协议、写入系统 udev 规则、启动实物采集或修复物理 USB 供电故障。

## How

- Approach: 在设备选择工具中并行探测全部带 VID/PID 的串口，打开前关闭 DTR/RTS，并要求观察到带 L/R 标识且角度有限的固件数据行。Shell 默认启用协议探测，仅在显式提供 VID/PID 时使用旧的硬件筛选方式。
- Components / files touched: `tools/galaxis_device_select_gui.py`、`tools/galaxis_udev_setup.sh`、相关测试与 README。
- Risks / unknowns: 当前执行环境未连接两块实物，无法复现其 VID/PID；实机仍可能受 USB 供电、线缆或端口占用影响，新增诊断将明确显示这些情况。
- Safety boundary: 只修改本地代码并进行模拟测试，不调用 sudo、不改 `/etc`、不重启容器。
- Verification plan: 用模拟串口覆盖左右识别、ROM/诊断过滤、超时和打开失败；运行现有配置与采集入口测试、Python 编译、shell 语法及 diff 检查。

## Verification result

- `bash -n tools/galaxis_udev_setup.sh` 通过。
- `python3 -m py_compile tools/galaxis_device_select_gui.py` 通过。
- 新增与相关回归测试共 58 项通过；`git diff --check` 通过。
- 当前环境未接入两块主控，实机双设备探测需在设备端执行 Setup 完成最终验收。
