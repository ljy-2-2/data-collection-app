# Galaxis HDF5 数据架构设计与演进说明

状态：HDF5 v3 紧凑采集已实施（2026-08-27）  
适用仓库：`Galaxis-Data-Collection`  
目的：作为后续搭建“以 HDF5 为主”的采集、恢复、训练转换和数据治理架构的唯一入口文档。

> `--format hdf5` 当前默认生成紧凑 HDF5 v3 + H.264 CRF 14；无损 HDF5 v2
> 仅作为旧文件兼容格式保留。LeRobot 直读 HDF5 仍属于后续工作。

## 1. 目标与原则

Galaxis 当前以 rosbag2/MCAP 采集 ROS 2 数据。引入 HDF5 的长期目标包括：

1. 保持 Episode、topic、时间戳和训练数据的可追溯性。
2. 降低长期保存数据所需空间。
3. 支持采集暂停、连续下一回合、异常退出和自动恢复。
4. 为 LeRobot 提供稳定、版本化的数据入口。
5. 保持原有 `--format rosbag` 路径完全可用，不能因 HDF5 架构而回归。

架构优先级如下：

1. 不丢数据。
2. 图像与动作时间对齐正确。
3. Schema 可验证、可演进。
4. 文件归属和退出行为正确。
5. 在上述条件成立后最小化体积。

## 2. 当前采集命令

rosbag：

```bash
./tools/galaxis_collection.py sample \
  --task "Galaxis" \
  --device-setup \
  --format rosbag
```

当前紧凑 HDF5 v3：

```bash
./tools/galaxis_collection.py sample \
  --task "Galaxis" \
  --device-setup \
  --format hdf5
```

不传 `--format` 时默认仍为 `rosbag`。

## 3. 当前 rosbag Episode 组成

一个正常完成的 rosbag Episode：

```text
Galaxis_001_20260826-101049/
├── Galaxis_001_20260826-101049_0.mcap
├── metadata.yaml
└── meta.json
```

文件职责：

| 文件 | 职责 |
| --- | --- |
| `*_0.mcap` | 按全局顺序保存 ROS 消息时间戳和序列化 CDR；达到分包条件时还可能出现 `_1.mcap` 等文件 |
| `metadata.yaml` | rosbag2 生成的 topic、类型、消息数量、录制起止时间和存储文件索引 |
| `meta.json` | Galaxis Episode 业务元数据，包括任务、机器人、设备、程序、Episode 起止时间、标签和成功状态 |

完整 rosbag Episode 的定义：

```text
一个或多个 MCAP + metadata.yaml + meta.json
```

## 4. 旧版无损 HDF5 v2

### 4.1 最终目录

一个正常完成的 HDF5 v2 Episode 最终应为：

```text
Galaxis_001_20260826-095702/
├── Galaxis_001_20260826-095702.h5
└── meta.json
```

`metadata.yaml` 已原样嵌入 HDF5，因此最终目录不再单独保留它。

### 4.2 转换中的目录

```text
Galaxis_001_20260826-095702/
├── .rosbag_staging/
│   ├── .rosbag_staging_0.mcap
│   └── metadata.yaml
├── Galaxis_001_20260826-095702.h5.partial
└── meta.json
```

含义：

- `.rosbag_staging`：已经安全停止并完整 flush 的临时 MCAP，是恢复依据；文件管理器默认隐藏点开头目录。
- `*.h5.partial`：尚未完成或尚未通过校验的 HDF5，不能作为最终数据使用。
- `meta.json`：必须在停止服务返回前落盘，不能只存在后台线程内存中。

只有满足以下全部条件后才允许删除 staging：

1. 最终 `.h5` 已生成。
2. HDF5 标记为 `complete=true`。
3. topic/type/QoS 与 MCAP 一致。
4. 消息数量一致。
5. 全局 topic、时间戳、顺序和 CDR 内容 SHA-256 一致。
6. `meta.json` 已存在。

### 4.3 HDF5 v2 内部 Schema

```text
episode.h5
├── attributes
│   ├── format = "galaxis-hdf5"
│   ├── format_version = 2
│   ├── source_storage_id = "mcap"
│   ├── serialization_format = "cdr"
│   ├── complete = true
│   ├── message_count
│   └── content_sha256
├── topics/
│   ├── 0000/
│   │   └── attributes
│   │       ├── id
│   │       ├── name
│   │       ├── type
│   │       ├── serialization_format
│   │       └── offered_qos_profiles
│   ├── 0001/
│   └── ...
├── messages/
│   ├── data
│   ├── topic_ids
│   ├── timestamps_ns
│   ├── offsets
│   └── lengths
└── rosbag_metadata_yaml
```

消息 `i` 的读取规则：

```text
topic    = topics[topic_ids[i]]
time_ns  = timestamps_ns[i]
offset   = offsets[i]
length   = lengths[i]
cdr      = data[offset : offset + length]
```

### 4.4 当前等价性契约

HDF5 v2 与 staging MCAP 应满足：

- topic 集合相同；
- ROS 类型相同；
- serialization format 相同；
- offered QoS 信息相同；
- 消息数量相同；
- rosbag 纳秒时间戳相同；
- 全局消息顺序相同；
- 每条序列化 CDR 字节相同；
- 原始 `metadata.yaml` 文本相同。

因此 v2 的本质是“把完全相同的 rosbag 消息流换成 HDF5 容器”，不是训练数据重编码格式。

## 5. 已发现并修复的生命周期问题

### 5.1 暂停后无法进入下一回合

原因：早期实现把 HDF5 转换放在停止服务的同步路径内，转换超过任务管理器超时。

当前策略：

```text
停止 MCAP并 flush
→ recording=false
→ 持久化 meta.json
→ 发布 recording_saved
→ 返回控制器
→ 单后台队列转换 HDF5
```

下一回合无需等待 HDF5 压缩完成。

### 5.2 网页 Episode 一直为 0

原因：网页原来只监听最终 `recording_completed`，HDF5 后台转换没有完成时不会计数。

当前事件语义：

| 事件 | 含义 | 使用者 |
| --- | --- | --- |
| `recording_saved` | 回合已经停止并安全保存，可立即计数 | Monitor |
| `recording_completed` | 最终文件已经转换和校验完成，可以上传 | Monitor 去重、StorageNode |

Monitor 按回合路径去重，两个事件不会导致同一 Episode 重复 `+1`。

### 5.3 `Container galaxis_core Stopping` 长时间不结束

原因：线程池 shutdown 等待后台转换、Compose 停止宽限期过长。

当前策略：

- shutdown 设置取消事件；
- 取消尚未开始的转换；
- 当前转换先 `terminate`，超时后 `kill`；
- 完整 staging 保留；
- 容器退出不无限等待。

### 5.4 多个回合目录看起来为空

原因：转换任务只在进程内存队列里，退出后队列丢失；实际 MCAP 位于隐藏的 `.rosbag_staging`。

当前策略：

- `meta.json` 在进入后台队列前持久化；
- RecordManager 启动时扫描 `.rosbag_staging/metadata.yaml`；
- 自动重新排队未完成转换；
- 如果最终 HDF5 已存在，则先重新验证，不直接覆盖；
- 缺少历史 `meta.json` 时保留 staging，不伪造元数据。

### 5.5 文件所有者为 root/nobody

当前要求：

- CLI 将宿主用户 UID/GID 传入容器；
- staging 停止后归一化所有权；
- 最终 HDF5、`meta.json` 和失败的 partial 都再次归一化；
- 正常最终文件应属于操作员，例如 `ljy:ljy`。

## 6. 为什么当前 HDF5 没有明显变小

对一个真实约 20 秒 HDF5 样本的只读分析结果：

```text
原始 CDR 总量：249,635,332 bytes
最终 HDF5：    247,609,807 bytes
HDF5/原始比：  99.19%
实际节省：      约 0.81%
```

topic 字节占比：

| topic | 占原始 CDR |
| --- | ---: |
| `/center_camera/image_raw/compressed` | 78.48% |
| `/right_camera/image_raw/compressed` | 20.39% |
| 所有其他 ROS 数据 | 约 1.13% |

两路相机合计约占 98.87%，而消息本身已经是 JPEG。JPEG 已消除大部分空间冗余，HDF5 gzip 无法再次显著压缩。

结论：

> HDF5 是容器，不会天然比 MCAP 小。只要仍要求保留原始 JPEG/CDR 字节，当前体积就已经接近可达到的下限。

提高 gzip 等级、改用普通 zstd 或在 HDF5 外再打包，通常都只能得到很小收益。

### 6.1 HDF5 v3 实测结果

2026-08-27 使用已有 20 秒 rosbag 做端到端验证：

```text
源 MCAP：             255,762,140 bytes
HDF5 v3：                842,259 bytes
两路 H.264 CRF 14：   28,329,470 bytes
紧凑 artifact 合计：  29,171,729 bytes
相对 MCAP 减少：      约 88.6%
```

验证覆盖 30,326 条全局消息、633 帧 center camera 和 623 帧 right camera；
非图像 CDR SHA-256、全局 topic/时间戳顺序、视频 640×480/30 FPS/帧数和视频
SHA-256 均通过首次转换与重启恢复复验。

## 7. 当前默认：紧凑 HDF5 v3 + H.264

### 7.1 最终目录

当前一个最终 Episode 使用以下结构。视频保持在顶层，以兼容当前只上传顶层文件的上传器：

```text
Galaxis_001_20260826-120000/
├── Galaxis_001_20260826-120000.h5
├── center_camera.mp4
├── right_camera.mp4
├── meta.json
└── manifest.json
```

文件职责：

| 文件 | 必需 | 职责 |
| --- | --- | --- |
| `<episode>.h5` | 是 | 保存非图像 ROS CDR、全局时间线、视频帧索引、相机时间戳和同步信息 |
| `meta.json` | 是 | 与 rosbag Episode 相同的 Galaxis 业务元数据 |
| `manifest.json` | 是 | 声明 Schema 版本、状态、各 artifact 路径/大小/SHA-256、编码参数和校验结果 |
| `*_camera.mp4` | 有相机且有帧时必需 | 每路相机一个 H.264 MP4 |

最终目录不保留 `.partial`；`.rosbag_staging` 仅在转换未完成、验证失败或恢复保护时存在。

### 7.2 当前视频参数

当前相机配置为 640×480、30 FPS，转换器固定使用：

```text
codec:       H.264 / libx264
resolution:  640x480
fps:         30
capture CRF: 14
preset:      medium
pixel format:yuv420p
container:   MP4
```

选择 CRF 14 的原因：当前 LeRobot 管线还会根据 ROS 时间戳选帧并以 H.264 CRF 20 编码；第一次编码使用较高质量可以减轻二次编码损失。

H.264 不会自动改变分辨率。只要没有配置 resize，训练仍读取 640×480；但 H.264 是有损压缩，解码后的像素会有轻微变化。

### 7.3 HDF5 v3 Schema

```text
<episode>.h5
├── attributes
│   ├── format = "galaxis-hdf5"
│   ├── format_version = 3
│   ├── storage_profile = "compact-video"
│   ├── complete = true
│   ├── message_count
│   ├── non_image_message_count
│   ├── source_content_sha256
│   ├── non_image_content_sha256
│   └── source_image_payload_sha256
├── topics/
│   └── <topic_id>/
│       └── attributes: name, type, serialization_format,
│                       offered_qos_profiles, storage_kind,
│                       camera_key/video_path（仅相机）
├── timeline/
│   ├── topic_ids
│   ├── timestamps_ns
│   ├── storage_kinds
│   ├── cdr_offsets
│   ├── cdr_lengths
│   └── video_frame_indices
├── cdr/
│   └── data
├── videos/
│   └── <camera_key>/
│       ├── timestamps_ns
│       ├── frame_indices
│       ├── source_topic
│       ├── relative_path
│       ├── frame_count
│       ├── width
│       ├── height
│       ├── fps
│       ├── codec
│       ├── crf
│       ├── preset
│       ├── pixel_format
│       ├── bytes
│       └── sha256
└── rosbag_metadata_yaml
```

`storage_kind` 固定枚举：

```text
0 = exact_cdr
1 = external_h264_frame
```

非图像消息继续保存完整 CDR；图像消息在全局 timeline 中保留 topic、原始 rosbag 时间戳和视频帧索引，不再保存大体积 JPEG CDR。

### 7.4 v3 的等价性定义必须改变

v3 不能再声称“全部消息与 rosbag 逐字节相同”。正确契约应拆分为：

非图像数据：

- topic/type/QoS 相同；
- 纳秒时间戳和全局顺序相同；
- CDR 字节和 SHA-256 相同。

图像数据：

- topic 和相机身份相同；
- 每条图像消息都有唯一帧索引；
- 时间戳和帧序映射完整；
- 分辨率保持 640×480；
- 帧率目标为 30 FPS；
- 像素允许 H.264 有损变化；
- MP4 codec、分辨率、帧数、时长和 SHA-256 必须验证。

## 8. HDF5 v3 的事务与恢复模型

当前状态机：

```text
RECORDING_MCAP
    ↓ B/Y
STAGING_FLUSHED
    ↓ 持久化 meta.json，并把回合加入单后台队列
CONVERTING
    ↓ <episode>.h5.partial + *_camera.mp4.partial
VALIDATING
    ↓ 所有校验通过，先发布 MP4 和 HDF5
COMMITTING
    ↓ 原子发布 manifest.json(state=complete)
COMPLETE
    ↓ 删除 staging
```

异常规则：

- 任意阶段失败都不能删除 staging。
- 最终 `manifest.json` 是 Episode 的提交记录。
- `manifest.json` 发布且后台转换成功之前，RecordManager 不发布 `recording_completed`，上传器不会收到该回合。
- HDF5 和视频应先写 `.partial`，验证后原子改名。
- 最后原子发布 `manifest.json`，使其成为整组文件的 commit marker。
- 重启时扫描 `.rosbag_staging/metadata.yaml`，重新生成或复验已有 artifact，幂等恢复。
- 已存在的最终 artifact 不能盲目覆盖，应先验证或重新生成到新 partial。

`manifest.json` 示例：

```json
{
  "schema": "galaxis-episode",
  "schema_version": 3,
  "storage_profile": "compact-video",
  "state": "complete",
  "episode_id": "Galaxis_001_20260826-120000",
  "artifacts": [
    {
      "path": "Galaxis_001_20260826-120000.h5",
      "kind": "hdf5",
      "bytes": 1234567,
      "sha256": "..."
    },
    {
      "path": "center_camera.mp4",
      "kind": "video",
      "codec": "h264",
      "width": 640,
      "height": 480,
      "fps": 30,
      "frame_count": 600,
      "crf": 14,
      "preset": "medium",
      "pixel_format": "yuv420p",
      "bytes": 12345678,
      "sha256": "..."
    }
  ]
}
```

## 9. 与 LeRobot 的关系

### 9.1 当前事实

当前 `tools/rebake_lerobot_v30.py` 只发现和读取 rosbag，不读取 HDF5。

当前 Rebake/LeRobot 视频路径会：

1. 按 ROS 时间戳执行 30 FPS 零阶保持同步；
2. 根据 Episode/primitive 边界选帧；
3. 必要时重复或丢弃图像帧以对齐动作；
4. 重新编码最终训练视频；
5. 当前 Galaxis v3.0 配置输出 H.264、CRF 20、GOP 30，未配置 resize。

因此不能简单把整段采集 MP4 复制到 LeRobot 输出。直接复制可能保持画质，却让视频帧和关节动作错位。

### 9.2 推荐的第一阶段接入

训练正确性优先：

```text
HDF5 timeline + H.264 capture
→ 按 timestamps_ns 解码/选择帧
→ 与动作统一到 30 FPS
→ LeRobot H.264 CRF 20
```

特征：

- 训练分辨率仍为 640×480；
- 图像与动作对齐逻辑保持现有语义；
- 会发生第二次有损编码；
- 采集视频使用 CRF 14，以降低第一次压缩造成的影响。

### 9.3 未来直接复用视频的必要条件

只有同时满足以下条件才能 remux/copy 而不重新编码：

1. 采集 MP4 恰好只包含该 Episode 的训练帧。
2. 每一训练步对应连续且唯一的视频帧。
3. 不需要零阶保持、丢帧、重复帧或帧选择。
4. 源视频已经是目标 H.264、640×480、30 FPS 和兼容 pixel format。
5. Episode 边界与视频 GOP/关键帧边界兼容。
6. LeRobot 元数据中的帧数、时长和时间基与 MP4 一致。

如果任何条件不成立，必须重新同步并编码，不能为了避免画质损失牺牲动作对齐。

## 10. 当前 CLI 行为

用户采集命令直接启用紧凑视频 HDF5：

```bash
./tools/galaxis_collection.py sample \
  --task "Galaxis" --device-setup \
  --format hdf5
```

RecordManager 会调用 `compact-video` profile，参数为 H.264、CRF 14、medium、30 FPS。
转换器内部仍保留 `lossless-cdr` profile，用于读取/验证旧 v2 文件，但采集 CLI
不再把 v2 作为 `--format hdf5` 的默认输出。

转换器内部参数：

```text
--video-crf 14
--video-preset medium
--video-fps 30
```

分辨率应来自相机实际输入和 profile，不建议由存储参数隐式缩放。

## 11. Schema 版本和兼容策略

| 版本 | 含义 | 读取要求 |
| --- | --- | --- |
| v2 | 全部 ROS 消息原始 CDR，图像也保留 JPEG CDR | 当前无损 HDF5 reader |
| v3 | 非图像精确 CDR，图像外置 H.264，HDF5 保存帧映射 | 新 HDF5 + video reader |

读取器必须先检查：

```text
format == "galaxis-hdf5"
format_version
storage_profile
complete == true
```

禁止根据“是否存在 videos 目录”猜测 Schema。

迁移原则：

- 不自动原地改写 v2。
- v2→v3 必须写入新目录，验证完成后由操作员决定是否保留源数据。
- 未知版本必须拒绝读取并给出明确错误。
- 训练数据应记录源 Episode Schema 版本和 artifact SHA-256。

## 12. 验收标准

### 12.1 采集与控制

- A/X 开始回合。
- B/Y 在服务超时内完成暂停并允许下一回合。
- 网页 Episode 每个安全保存回合只增加一次。
- `Ctrl+C` 在有限时间内关闭容器。
- 中断转换后，下次启动自动恢复。

### 12.2 v2 无损数据

- 全 topic/type/QoS 一致。
- 时间戳、全局顺序、消息数量一致。
- 全部 CDR SHA-256 一致。
- HDF5 内嵌 `metadata.yaml` 与源相同。

### 12.3 v3 非图像数据

- 非图像 CDR SHA-256 与 staging MCAP 一致。
- 每个非图像消息的 topic、时间戳和顺序一致。
- 全局 timeline 不因图像外置而丢失顺序关系。

### 12.4 v3 视频

每路视频必须通过 `ffprobe` 或等价检查：

```text
codec_name = h264
width = 640
height = 480
avg_frame_rate = 30/1
frame_count = HDF5 对应 camera frame count
```

同时检查：

- 每个图像时间戳只有一个帧索引。
- 帧索引严格递增且无越界。
- `ffprobe` 帧数与 HDF5 camera frame count 一致。
- 中断或校验失败时 staging 保留。

### 12.5 LeRobot

- 训练视频仍为 640×480。
- 每个 Episode 的视频帧数与数据行数一致。
- 第一帧、末帧和随机抽样帧能对应正确动作时间。
- 不允许静默丢相机、静默 resize 或静默回退到错误帧率。
- 直接复用视频时必须证明无需选帧；否则必须走同步重编码路径。

### 12.6 体积与性能

- 使用相同场景和近似运动的 20 秒样本比较。
- 分别记录 MCAP、HDF5 v2、HDF5 v3 总字节数。
- 记录转换耗时、CPU、峰值内存和临时双份空间。
- 不用不同画面复杂度的两个回合直接宣称压缩率。

## 13. 推荐实施阶段

### 阶段 0：冻结采集基线（已完成）

- 保留 rosbag 和旧 HDF5 v2 兼容能力。
- 固定真实 20 秒测试样本、topic 列表、消息数和 SHA-256。
- LeRobot 训练输出的完整基线对比归入阶段 3。

### 阶段 1：定义 v3 Schema 和 reader（已完成）

- 先完成 HDF5 timeline、非图像 CDR和视频索引 Schema。
- reader 必须能验证模拟文件和拒绝不完整文件。
- 此阶段不接实机默认采集。

### 阶段 2：离线 MCAP→v3 转换器（已完成）

- 从复制的测试 MCAP 生成 HDF5 + H.264。
- 验证非图像 SHA-256、视频参数、恢复和原子提交。
- 量化实际空间收益与画质。

### 阶段 3：LeRobot HDF5 输入（待实施）

- 读取 v3 timeline 和视频帧映射。
- 复用现有时间同步语义。
- 先接受高质量采集视频到 CRF 20 的同步重编码。
- 建立图像/动作对齐回归测试。

### 阶段 4：接入在线采集（已完成代码接入）

- `--format hdf5` 默认使用 compact-video；rosbag 模式不变。
- 测试连续多回合、积压、退出、重启恢复和所有权。

### 阶段 5：评估直接视频复用（待实施）

- 只有对齐证明成立才增加 direct-remux 快速路径。
- direct-remux 必须是显式、可验证的优化，不能成为静默默认行为。

### 阶段 6：长期兼容与退役决策（待长期验证）

- v3 已成为 `--format hdf5` 的默认 Schema；`--format rosbag` 默认行为不变。
- 未完成训练回归前不能移除 rosbag 或 v2 读取/验证能力。

## 14. 当前禁止事项

- 不要因为看到“空目录”而删除 `.rosbag_staging`。
- 不要把 `.h5.partial` 当成最终数据。
- 不要为旧回合伪造 `meta.json`。
- 不要把 v3 宣称为与 rosbag 图像 CDR 逐字节等价。
- 不要仅为了减少体积直接复制视频而跳过动作同步。
- 不要在未验证前自动删除现有 rosbag/HDF5。
- 不要把 HDF5 文件扩展名当成 Schema；必须检查内部版本。

## 15. 当前相关代码位置

| 组件 | 路径 |
| --- | --- |
| 采集 CLI | `tools/galaxis_collection.py` |
| RecordManager | `galaxis-core/galaxis-core/galaxis_core/record_manager.py` |
| HDF5 v2/v3 转换器 | `galaxis-core/galaxis-core/galaxis_core/hdf5_recorder.py` |
| RecordManager 测试 | `galaxis-core/galaxis-core/test/test_record_manager_upload_notify.py` |
| HDF5 Schema 测试 | `galaxis-core/galaxis-core/test/test_hdf5_recorder.py` |
| Monitor 状态桥接 | `monitor/bridge/galaxis_studio_bridge/ros_state.py` |
| 当前 LeRobot 入口 | `tools/rebake_lerobot_v30.py` |
| Galaxis LeRobot 配置 | `rebake/config/pipeline/galaxis_v30_local.yaml` |
| 相机/录制配置 | `galaxis_bringup/config/galaxis_config.yaml` |

## 16. 后续仍需验证的产品事项

已确定图像允许 H.264 有损变化，并采用实测后的 CRF 14。后续还需要：

1. 用真实训练任务比较 v3 与 rosbag 基线成功率。
2. 实现 LeRobot 对 HDF5 v3 时间线和视频索引的直接读取。
3. 决定 LeRobot 第一阶段是否接受同步后二次编码。
4. 验证远端对象存储对多文件 Episode 的完整上传和恢复语义。
5. 根据长期训练结果决定是否长期保留 v2 `lossless-cdr` 归档入口。
