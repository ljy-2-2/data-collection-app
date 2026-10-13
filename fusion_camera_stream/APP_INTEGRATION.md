# App 接入准备（v2.1，尚未整体验收）

## 当前可用接口

`FusionStreamClient.stream(seconds=60, hz=30, joint_hz=100)` 输出 `start/image/joints/health/end` 事件，正常取流不需要 SSH。`client.close()` 停止自己的采集。服务在控制器 `/mnt/fusion-camera` 持久化，已有启动项保持不变；没有为本次更新重启控制器验证。

`image` 是1024×768原生MJPEG，电脑保存器解码缩至640×480。`joints` 是左7+右7，原始位置 degree、速度 degree/s、力矩 Nm；episode的joint tensors转为rad、rad/s。`record/joints.jsonl`保留全部100Hz反馈；`bundles.jsonl`约30Hz，不能用bundle条数当作关节采样数。

`record_episode(client, output_root, seconds=60, hz=30, joint_hz=100, gripper_source=None, gripper_hz=20)` 使用已验证episode布局。夹爪来源显式注入，不在TLS相机服务内部隐式启动厂家SDK。

## 夹爪来源约定

`gripper_source.sample(temperatures=False)` 返回 `fusion_camera/grippers.py` 中 `ReadOnlyGrippers.sample()` 的结构：`motors.left/right.fields` 包含 `position_rad`、`output_encoder_rad`；每个字段保留 `value/raw_value/valid/pc_query_start_ns/pc_reply_ns`。还保留 `within_calibrated_travel`、`closure_fraction`、`calibration`。未知数据使用null和原因，不填0冒充测量。

采集器在工作线程按20Hz调用该来源；适配器需要线程安全、有界等待，提供真实反馈时间和新鲜度，不能每次给旧缓存伪造新时间。现有接口用查询/回复中点作软件时间估计；如果App直接订阅已有反馈，接入前需要扩展时间来源字段，不能将控制目标/缓存读取时间冒充采样时间。夹爪20Hz样本可被相邻30Hz bundle引用，原始样本编号和时间差保留。

夹爪保存为：

- `record/grippers.jsonl`：左右完整原始反馈、有效性、时间、标定范围。
- `bundle.grippers.left/right`：最近样本及其索引；`gripper_delta_ms`：相对bundle时间差。
- `state__left_effector__position` / `state__right_effector__position`：单元素角度rad。
- `state__left_effector__output_encoder_position` / 右侧同名字段：输出编码器角rad。

两个夹爪不改变14维arm向量，开口比例不等于反馈角度。当前温度/标定问题没有解决，不能将 `acquisition_passed` 当作物理标定通过。

## 单连接原则和验收阻碍

独立 `ReadOnlyGrippers` 仅用于静止、没有其它厂家SDK使用者的诊断。厂家UDP端点有连接/回包归属，第二个会话可能抢占回包。App需要由现有控制连接所有者统一收发并分发反馈；不能一边运行控制SDK、一边另起该类。检查本机UDP4730和文件锁不能证明没有其它电脑或控制器内部使用者。

60秒包含夹爪的联合测试在约33秒被保护逻辑终止：收到未请求的寄存器0x00只读响应。尚未确定是其它来源、旧缓存还是传输问题；不能忽略未知帧或清空厂家缓存绕过。独立100Hz双臂+三路图像60秒测试已通过。

双方夹爪角度超出旧开/闭标定范围，开合比例为null；左侧温度0值未经验证。仅发送寄存器读，不进行了标定、使能、移动或零点变更。下一步需要先确认夹爪唯一通信所有者和反馈新鲜度，再核对实际开合标定，最后重做完整60秒联合验收。当前接口可用于App的数据结构准备，夹爪稳定性/运动数据采集还没有完成验收。

图像/关节服务仍保留静止保护（速度超过1度/秒停止采集），独立夹爪读取还要求双臂idle。运动数据采集尚未授权验证；不要为了接App直接删除保护。60秒通过不代表10分钟可靠性，也不是实时控制不受影响的绝对保证。
