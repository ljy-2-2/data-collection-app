# 使用指南（中文）— AgiBot G2 数据采集 + LeRobot v3.0 转换

本项目在**旁挂电脑（笔记本）**上运行，只通过 `agibot_gdk` SDK 读取机器人数据，
**不需要也不允许修改机器人本体**。流程分两步：

1. **采集**：把相机图像 + 关节状态等录成"原始 episode"目录；
2. **转换**：把原始 episode 转成标准 LeRobot v3.0 数据集，可直接用于策略训练。

---

## 0. 前置条件（一次性确认）

- 笔记本已通过网线接入机器人局域网，机器人开机。
  确认方法：`ping 10.42.1.101` 能通（或 `ssh g2` 能登录）。
- GDK 已安装在 `~/.cache/agibot/app/`（本机已装好）。
- 两个 conda 环境（本机已装好）：
  - `gdk`（Python 3.10，仅 numpy/opencv/pyyaml，很轻量）— 采集用。
    `agibot_gdk` 本身不装在任何 conda 环境里，它由 `source env.sh` 通过
    `PYTHONPATH` 从 `~/.cache/agibot/app/` 加载，环境里只需要 Python 3.10；
  - `lerobot` — 转换用，装有 lerobot v3.0 代码库。

> ⚠️ **重要**：采集和转换必须在**不同的进程/环境**里跑（命令里已经分开，
> 按下面的命令照抄即可）。另外不要把 `agibot_gdk` 和 `xrobotoolkit_sdk`
> 在同一个 Python 进程里 import，会直接崩溃。

---

## 1. 进入项目并加载环境

每开一个新终端都要先执行：

```bash
cd ~/src/gdk_data_collection
source env.sh
```

`env.sh` 会加载 GDK 运行环境，并定义两个变量：

- `$GDK_PY`   → 采集用的 Python（gdk 环境）
- `$LEROBOT_PY` → 转换用的 Python（lerobot 环境）

---

## 2. 连通性自检（建议每次采集前跑一次）

```bash
$GDK_PY scripts/probe.py
```

正常输出应包含：

- `bundle timestamp_ns: ...` 和 27 个 `state__*` 张量；
- 6 个相机，每个都显示 `exact-match OK`；
- `bundle rate: ~30.0 Hz`。

如果显示 `no bundle received`，说明没连上机器人（检查网线 / 机器人是否开机）。

---

## 3. 采集数据

### 3.1 固定时长采集（推荐新手）

```bash
$GDK_PY scripts/collect.py --task "拿起红色方块" --duration 30 --episodes 5
```

- `--task`：这条数据对应的任务描述（会写进数据集，训练时用，中英文均可）；
- `--duration 30`：每条 episode 录 30 秒；
- `--episodes 5`：连续录 5 条。

### 3.2 交互式采集（自己控制开始/结束）

```bash
$GDK_PY scripts/collect.py --task "叠毛巾"
```

- 按 **回车** 开始录制一条 episode；
- 再按 **回车** 结束这条；
- 输入 **q** 再回车退出程序；
- 任何时候 **Ctrl-C** 都会安全停止并保存当前 episode。

### 3.3 采集结果在哪里？

默认写到 `~/agibot_episodes/<episode_id>/`，每条 episode 一个目录：

```
~/agibot_episodes/20260716_093756_4c9749d0/
  camera/<相机名>/frames/00000000.jpg ...   # 每帧图像
  camera/<相机名>/<相机名>.txt              # 时间戳索引
  record/bundles.jsonl                      # 每帧的关节状态等全部张量
  record/clock_offset.jsonl                 # 机器人时钟 vs 电脑时钟
  meta_info.json                            # 任务名、计数器、配置快照
```

录完后看屏幕输出：每个相机的帧数应约等于 `30 × 秒数`，且
`(100.0% of bundles)`；如果比例明显偏低，说明丢帧了（见第 6 节排查）。

### 3.4 选择采集哪些传感器

编辑 `config/default.yaml`（或复制一份自己的，用 `--config 路径` 指定）。

```yaml
cameras:
  - name: head_color        # 头部彩色相机（必采：essential: true 表示缺帧就整帧作废）
    essential: true
  - name: hand_left_color   # 左手腕相机
    essential: true
  - name: hand_right_color  # 右手腕相机
    essential: true
  - name: head_depth        # 头部深度（非必采：缺帧只标记，不作废整帧）
    essential: false
  # 需要立体相机时取消注释（帧很大，网络压力高）：
  # - name: head_stereo_left
  # - name: head_stereo_right

imus: []       # 可填 [front, back, chassis]
lidars: []     # 可填 [{name: front, every_n_frames: 3}]
ultrasonic: false
```

本机器人可用的相机名（已实测）：
`head_color`、`hand_left_color`、`hand_right_color`、`head_depth`、
`head_stereo_left`、`head_stereo_right`。

---

## 4. 动作标签（action）从哪来？

训练视觉-运动策略需要 action。转换脚本按以下优先级自动选择：

1. **`record/actions.jsonl`**（最优）：遥操作程序把每条发出的控制指令写到
   episode 目录下的这个文件，每行：
   `{"ts_ns": <机器人时钟纳秒>, "action": [数值数组]}`；
2. **官方遥操作**：如果采集时官方 teleop 在跑，bundle 里会有
   `teleop__action__*`，自动使用；
3. **兜底**：都没有时用 `action[t] = state[t+1]`（下一帧状态当作动作），
   并在 `agibot_conversion.json` 里明确标记 `action_backfilled`。

静置机器人测试时用的就是第 3 种；正式采集请配合遥操作使用第 1 或第 2 种。

---

## 5. 转换成 LeRobot v3.0 数据集

采集完成后（注意换用 `$LEROBOT_PY`）：

```bash
$LEROBOT_PY scripts/convert_to_lerobot.py \
    --episodes ~/agibot_episodes \
    --repo-id chloeya/agibot_g2_pick \
    --root ~/lerobot_datasets/agibot_g2_pick
```

- `--episodes`：原始 episode 的根目录（会转换该目录下**所有** episode，
  所以不同任务建议用不同的 `output_root`，或转换前把不要的 episode 目录删掉/移走）；
- `--repo-id`：数据集名字（`用户名/数据集名` 格式）；
- `--root`：输出目录；已存在时加 `--overwrite` 覆盖。

常用可选参数：

| 参数 | 说明 |
|------|------|
| `--cameras head_color,hand_left_color` | 只转换指定相机（默认全部彩色相机；深度相机自动跳过） |
| `--task "..."` | 覆盖录制时的任务名 |
| `--action-source state_backfill` | 强制指定动作来源（auto/actions_file/teleop/state_backfill） |
| `--vcodec libx264` | AV1 编码报错时换 H.264 |
| `--robot-timestamps` | 用真实机器人时间戳（默认用 帧号/30，对 lerobot 更稳） |

转换成功会打印：

```
LeRobot v3.0 dataset written to /home/.../agibot_g2_pick
  episodes: 5, frames: 4500
```

### 5.1 验证数据集能被加载

```bash
$LEROBOT_PY -c "
from lerobot.datasets.lerobot_dataset import LeRobotDataset
ds = LeRobotDataset(repo_id='chloeya/agibot_g2_pick', root='/home/chloeya/lerobot_datasets/agibot_g2_pick')
print(ds)
print(ds[0]['observation.state'].shape, ds[0]['action'].shape)
"
```

能打印出 shape（默认 24 维：14 手臂关节 + 2 夹爪 + 3 头 + 5 腰）即为成功。

---

## 6. 常见问题排查

| 现象 | 原因 / 解决 |
|------|-------------|
| `no bundle received` | 没连上机器人：检查网线、`ping 10.42.1.101`、机器人是否开机 |
| `import agibot_gdk` 失败 | 没有先 `source env.sh`，或用错了 Python（必须用 `$GDK_PY`） |
| 屏幕上偶尔刷 `ImageNearest failed` | 启动头几秒的正常现象（相机缓冲还没热），只要相机帧数接近 100% 就没问题 |
| 某相机帧数明显低于 100% | 网络带宽不够：减少相机数量、去掉立体相机，或把该相机 `retries` 调大 |
| 转换时报 `xxx exists — pass --overwrite` | 输出目录已存在，加 `--overwrite` 或换目录 |
| 转换时 AV1/SVT 报错 | 加 `--vcodec libx264` |
| 数据集里 action 和 state 几乎一样 | 采集时没有动作来源，走了兜底 backfill（见第 4 节），属预期行为 |

---

## 7. 一条龙示例（从零到数据集）

```bash
cd ~/src/gdk_data_collection && source env.sh

# 1) 自检
$GDK_PY scripts/probe.py

# 2) 清掉旧的测试数据（可选，避免混入数据集）
mv ~/agibot_episodes ~/agibot_episodes_old 2>/dev/null; mkdir -p ~/agibot_episodes

# 3) 录 10 条、每条 20 秒
$GDK_PY scripts/collect.py --task "拿起红色方块放进盒子" --duration 20 --episodes 10

# 4) 转成 LeRobot v3.0
$LEROBOT_PY scripts/convert_to_lerobot.py \
    --episodes ~/agibot_episodes \
    --repo-id chloeya/agibot_g2_pick_cube \
    --root ~/lerobot_datasets/agibot_g2_pick_cube

# 5) 验证
$LEROBOT_PY -c "
from lerobot.datasets.lerobot_dataset import LeRobotDataset
ds = LeRobotDataset(repo_id='chloeya/agibot_g2_pick_cube', root='/home/chloeya/lerobot_datasets/agibot_g2_pick_cube')
print(ds)
"
```

更多技术细节（数据格式、完整性规则、与官方管线的对照）见英文版 `README.md`
和设计文档 `~/src/design/IMPL_PLAN_gdk_data_collection.md`。
