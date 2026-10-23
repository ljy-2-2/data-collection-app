# App 内的 KUAVO 客户端

此目录是备份内 `pc-preparation-20261019T103718Z-817d8a77fbbb` 发布快照的 Python 源码与网络辅助脚本，源码未改写；摘要见 source-manifest.json。没有复制历史日志、开发缓存、机器人 TLS 私钥或旧工程的绝对路径启动脚本。

App 用独立进程加载本目录，避免与 G2 的同名 `teleop` 包相互影响。机器人正式服务继续拥有运动参数、夹爪标定、Y/B 回位逻辑；App 不上传或替换机器人代码。

本目录不包含认证凭据。当前机器的 connection.json/token 位于 App 的 teleop_control/runtime/kuavo，权限 0600，该目录不入 Git 或安装包。PICO SDK 和接收服务复用 App 的机器人运行环境。操作说明见 App 根目录 README.md 的 KUAVO 段落。
