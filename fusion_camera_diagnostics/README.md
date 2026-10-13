# FUSION 三路相机诊断

**最新：原生 MJPEG 1024×768/约30 Hz → 笔记本实时解码缩放640×480，60秒完整测试通过。**
[最新测试报告](results/20260920-144427/REPORT.md) · [MJPEG实现说明](MJPEG.md)

下文部分保留早期 YUYV 测试的历史结果和设计。

本目录是独立的测试工具，不替换桌面 FusionCameraSDK 或控制器上的单帧服务。
2026-09-20 用户确认机械臂静止后，已完成 FUSION 本机编译和 60 秒三路采集。
实际接收约 16.06 Hz/路，未达到 30 Hz；测试结束，单帧服务已恢复。
完整结果见 [60 秒报告](results/20260920-125518/REPORT.md)。下文保留设计及验证范围。

## GDK 调查

智元 G2 GDK v2.1.0 厂家手册说明内部默认 DDS，可通过转发节点提供 ROS 2。
Camera 接口提供 GetLatestImage、GetNearestImage，Image 包含 timestamp_ns、宽高、编码和图像数据，
编码支持 JPEG、PNG、UNCOMPRESSED。这说明可以采用“机器人端采集/发布，电脑端订阅、缓存、按时间戳查找”的架构；
手册没有据此保证全部相机同时曝光。GDK 用户接口也不足以确定底层所有相机的总线、驱动、压缩位置及触发线路。

来源：<https://cdn.robotshop.com/media/A/Agb/RB-Agb-09/pdf/Genie-02-GDK-v2.1.0.pdf>
相关位置：第 3 页网络配置、第 5 页 DDS/ROS2、第 67–71 页 Camera/Image。
手册要求开发电脑配置 10.42.1.x/24，SDK 本身不会消除网络可达性要求。

本机安装文件也可交叉核对：

- `/home/yeap/.cache/agibot/app/gdk/build_dep/cpp/x86_64/include/gdk/sensors.h`
- `/home/yeap/.cache/agibot/app/gdk/build_dep/cpp/x86_64/include/gdk/bundle.h`
- `/home/yeap/.cache/agibot/app/gdk/config/r1_camera_conf.json`：配置中的相机为 30 fps，但不等于本次实测。
- `../teleop_control/vendor/gdk_data_collection/collector/gdk_io.py`：按 Bundle 声明的各相机时间戳取对应图像。

## 已完成的只读检查

FUSION 当前内核仍为 3.14.18-ipipe；独立单帧服务 PID 22124 正常运行。
eth1 报告 1000 Mb/s；USB 路径 1-1/1-2/1-4 对应设备号 2/3/4。
RTPSI、FX_RtTask 的 MSW/PF 当时均为 0。此信息不能确定机械臂是否静止，也不能证明采集不会影响实时延迟。

## 测试设计与约束

1. 本次按用户明确指定仅测试 60 秒，不自动延长或开启新一轮采集。
2. 仅用户态 libuvc/libusb；三路都实际协商 YUYV 640×480、30 fps，拒绝自动降级或高帧率抽帧。
3. 编译/运行使用 nice 19、CPU3；不设置实时优先级。仅在控制器 `/tmp/linux-3.14.18-ipipe-build` 的新子目录准备。
4. 测试期间只暂停我们新增的 fusion-camera 单帧服务，避免两者抢相机；退出后恢复该服务。
   不停止或修改任何厂家机器人服务。run_remote.sh 已在本次测试中执行该恢复流程。
5. 使用外层 timeout 限制运行时长和清理上限。程序内时间限制、输出停滞 1 秒、相机停滞 2 秒会结束测试。
   程序每 2 秒核对 RTPSI/FX_RtTask 的 MSW/PF，变化或消失时停止；另需检查机器人进程、dmesg、CPU/内存和网络。
6. 禁止 detach/attach 驱动、USB reset、set_configuration；仅为视频流配置 UVC probe/commit 和 alternate setting。
   USB 中断/传输仍会增加负载，绑 CPU 和低优先级不能提供零干扰保证。
7. 全部原始图像落盘到笔记本，不落盘到控制器。三路有效负载 55.296 MB/s（442.368 Mb/s），
   5 分钟约 16.59 GB、30 分钟约 99.53 GB，不含开销。当前笔记本可用空间约 211 GB。
8. 当前诊断输出可经独立 SSH 会话传输，因此测试包括 SSH 加密开销。若该链路成为瓶颈，不能直接认定独立相机协议也不可能。
   测试工具不是最终的免登录持续流 API。

禁止重启、修改内核/GRUB/boot、编译加载内核模块、修改 Xenomai/I-pipe。

## 时间戳和评估

`prepare.py` 复制 libuvc 到独立临时树，只在副本的帧缓冲完成处加入 CLOCK_MONOTONIC 采样。
不修改既有 SDK 的 libuvc。保留每帧原始设备 PTS/SCR，设备时钟未经校准不直接跨相机比较。

`receive.py` 保存三份 YUYV 文件及 frames.csv，包含控制器完成时间、序号、电脑接收时间、原始设备 PTS/SCR、文件偏移。
接收与控制器单调时钟不共用原点，不直接相减估计单向网络延迟。
分析每路帧率、间隔 p50/p95/p99/max、序号缺口；缺口无法涵盖所有相机内部丢帧，须结合实测帧率。
`pairs.jsonl` 以原始控制器时间戳顺序一对一配对，跨度最多 20 ms，丢弃无法配对的帧并报告数量。
绝不复制帧补齐、重写时间戳或将配对当作硬件同步。接收完成时间仍包括 USB 传输/调度抖动；
真正同时曝光需要相机的硬件同步支持或另做视觉时序标定。

本机准备及离线验证：

```sh
python3 -m unittest discover -s fusion_camera_diagnostics -p 'test_*.py' -v
python3 fusion_camera_diagnostics/prepare.py /tmp/fusion-camera-sdk /tmp/fusion-three-prepared
```

prepare 的目标目录必须不存在。本次 `/tmp/fusion-three-prepared` 已准备；本机 x86_64 编译检查成功，
FUSION 的 i686 编译、三路模式协商和 60 秒帧率已实测；更长时间稳定性尚未验证。
不能用单帧取图成功、离线单元测试或接收器退出码代替三路持续采集通过。

## YUYV 分离版本（历史实现与实测）

原 60 秒报告属于旧版单线程取帧/发送循环，不能作为新版性能结果。
新版流程：libusb 接收线程 → 主线程轮询三路并复制入队 → 独立 pthread 发送线程。
网络写入不持有队列锁；互斥锁仅覆盖固定大小的帧复制和队列计数。
这减少发送阻塞造成的取帧延迟，但不是无锁或硬实时采集实现。

- 每路最多 8 帧，独立队列；发送线程轮询三路，避免一路长期占据发送机会。
- 队列满时丢该路最旧的待发送帧，保留新数据；正在发送的帧有独立副本，不会被覆盖。
- 三路队列与一个发送副本总计约 15.36 MB（约 14.65 MiB），不包括原 libuvc 缓冲；整体仍受 256 MiB 地址空间上限约束。
- FUV1 数据格式、每帧原始控制器单调时间戳、设备 PTS/SCR 和序号均保持兼容。
- 原进度日志 `frames` 现在是采集线程取到的帧数；新 `queue_event` 日志区分 captured、sent、overflow_dropped、shutdown_dropped、failed_frames。
- `pending`、`in_flight`、`high_water` 显示队列和发送状态；`max_queue_age_ns` 是开始发送前的最大等待时间，不是端到端延迟。
- `sequence_gaps` 现在对应采集线程未取到的 libuvc 帧；电脑 frames.csv 中的序号缺口还可能包含队列丢帧，二者不能混同。
- 采集到期即结束取帧；剩余队列最多用 1 秒启动发送，单帧写入另有 1 秒上限，避免无限等待清空。
- 退出时每路满足 `captured = sent + overflow_dropped + shutdown_dropped + failed_frames`，队列及在途数量归零。
- 新版 run_local.py 提取最终队列计数为 pipeline-stats.json，并检查上述计数是否对得上。

源码在本目录；本机准备副本在 `/tmp/fusion-three-queued-prepared`，控制器新版本目录为
`/tmp/linux-3.14.18-ipipe-build/fusion-three-queued-20260920`。
FUSION 的新 32 位用户态二进制已编译成功，二进制 SHA256 为
`7489d84e5eed4fc2ee2f53722ae788e7a996287f5a646d2ab88724b801eaf814`。
编译后单帧服务仍为 PID 22651，实时任务 MSW/PF 仍为 0。
已有 60 秒数据及旧构建目录保留。单帧 SDK/服务未替换；新版随后按用户要求完成了单次 60 秒采集并恢复单帧服务。
控制器 `/tmp` 中二进制是诊断副本，重启后不保证保留；本机源码和报告持久保存在工作目录。

本次离线验证：

- 生产发送线程配合模拟帧：FIFO/三路轮询、完整负载与原始时间戳一致性。
- 接收端完全停滞：生产者继续入队；1 秒发送超时；溢出、失败、剩余帧计数闭合。
- 满队列准确丢弃最旧帧；正常关闭的截止时间生效。
- AddressSanitizer 与 UndefinedBehaviorSanitizer 检查通过；当前沙箱不支持 LeakSanitizer，因此未运行泄漏扫描。
- 原有 4 项 Python 时间戳配对测试通过。

新增测试复现（仅本机，不在控制器上运行模拟测试）：

```sh
cp fusion_camera_diagnostics/test_pipeline.c /tmp/fusion-three-queued-prepared/
cd /tmp/fusion-three-queued-prepared
gcc -std=gnu99 -O1 -g -fsanitize=address,undefined -pthread \
  -Ivendor -Ivendor/libuvc-0.0.6/include test_pipeline.c frame_queue.c \
  vendor/libuvc-0.0.6/src/{ctrl,ctrl-gen,device,diag,frame,init,stream,misc}.c \
  /usr/lib/x86_64-linux-gnu/libusb-1.0.so.0 -lrt -o test-pipeline
ASAN_OPTIONS=detect_leaks=0 ./test-pipeline
```

新版已完成 60 秒实测：[完整结果](results/20260920-134736/REPORT.md)。采集约 29.8–30 Hz，电脑接收约 15.84 Hz；SSH 会话平均占用 99.23% 的一个 CPU 核心，队列满造成丢帧。持续吞吐不足时，有界队列仍会丢帧，不能靠缓冲弥补永久的带宽不足。
