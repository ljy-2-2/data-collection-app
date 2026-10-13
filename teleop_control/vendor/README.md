# 项目内遥操作 runner

`teleop_runtime/` 是从桌面 `Teleoperation/teleop-runtime` 内置的新遥操运行时。
网页普通遥操现由 `run_new_teleop_collection.py` 启动该运行时的
`XRFrame -> Retargeter -> Router -> SafetyGateway -> G2Adapter` 双臂控制链。
平台桥接层仅保留原有 A 开始录制、B 停止保存、额外相机和自动上传；
旧 `send.py` 不再是普通网页遥操的运动控制入口。

DAgger 通过 `dagger_send_runtime.py` 使用同一运行时的右臂视图；
HIL-SERL Actor 通过 `apps/platform_right_arm.py` 复用同一右臂控制链。
两者只替换专家遥操内核，各自的录制、接管、策略和训练协议保持不变。

本目录于 2026-08-24 从采集电脑的 `~/桌面/test-teleop` 复制必要 runner、配置和 G2 URDF，
使网页控制服务不依赖该桌面测试目录。`run_teleop_collection.py` 与 `send.py` 增加了 `--yes`
参数，用于网页已经完成安全确认后跳过第二次终端确认。

runner 仍依赖厂商 GDK 环境；数据采集项目 `gdk_data_collection` 已随本目录内置
（`teleop_control/vendor/gdk_data_collection`），不再依赖桌面路径。仍可通过
`TELEOP_COLLECTION_PROJECT` 覆盖为其他采集项目路径。
