# HDF5 数据检查脚本

脚本：`scripts/check_hdf5_quality.py`。这是独立的离线检查工具，不启动 App、
不连接设备、不上传数据、不修改输入文件，也不会自动删除或修复异常样本。

## 使用

在 App 文件夹运行，路径含空格或中文时建议加引号：

```bash
python3 scripts/check_hdf5_quality.py "/完整路径/采集文件.h5"
```

默认在当前工作目录的 `hdf5-reports/文件名_时间/` 新建报告目录。终端会打印
HTML 报告和曲线图集的完整位置；检测完成后会自动生成 PNG，无需再单独运行绘图命令。
双击 `report.html` 可离线查看，其中有“查看自动生成的全部曲线图片”入口；
也可直接打开 `curves/index.html`，不依赖网络或 CDN。
重复运行生成不同目录，指定已存在的目录会拒绝覆盖。

数值检测依赖 `numpy`、`h5py`，自动图片依赖 `numpy`、`matplotlib`。脚本会先尝试当前
Python 绘图，如果缺少绘图依赖，再尝试本机 `/usr/bin/python3`，不会自动安装或修改环境。
当前电脑可直接使用同一条 `python3` 检测命令。其他电脑缺少依赖时可先执行：

```bash
python3 -m pip install numpy h5py matplotlib
```

可选参数示例：

```bash
python3 scripts/check_hdf5_quality.py "/完整路径/采集文件.h5" \
  --max-order 3 --z-threshold 3 --gap-factor 5 \
  --output-dir "./hdf5-reports/本次检查"
```

- `--max-order`：0～3，默认 3。0 阶只检查原值。
- `--z-threshold`：默认 3，标记严格满足 `|Z| > 3` 的有效样本。
- `--gap-factor`：默认 5，时间间隔大于正间隔中位数的 5 倍视为长间隔，不跨越求差分。
- `--quaternion-norm-tolerance`：默认 0.05，标记四元数模长偏离 1 超过此值的样本。
- `--plot-metric derivative`：自动 PNG 改画时间归一化变化率；默认绘制普通差分。交互式 HTML 和 NPZ 始终保留两套结果。
- `--no-plots`：只生成交互式 HTML 和计算结果，跳过独立 PNG 图片；默认不跳过。
- `--fail-on-findings`：用于批处理，发现需复核或结构问题时退出码为 2；默认报告生成成功即返回 0。输入/执行失败返回 1。

如检测完成但图片生成失败，会保留已有计算报告、明确打印失败原因并返回 1，
不会声称已生成图片。修复依赖后可用下述单独绘图命令补画。

请在一个回合完成保存后运行，不要针对仍在写入的文件判断数据质量。

## 输出文件和自动曲线图

| 文件 | 用途 |
|---|---|
| `report.html` | 首选查看入口：逐路选择信号和阶次，查看数值/Z-score 曲线、统计表、时间轴及四元数检查 |
| `summary.json` | 每路、每阶的均值、总体标准差、极值、最大绝对 Z、超阈值数量/比例、结构问题和参数 |
| `anomalies.csv` | 全部超 Z 阈值点，含信号、指标、阶次、来源样本索引范围、相对时间和 Z；可用表格软件打开 |
| `series.npz` | 完整分辨率的原值、时间戳、0～3 阶普通差分/时间变化率及各自 Z-score，使用 NumPy 读取 |
| `curves/index.html` | 自动生成的曲线图集入口 |
| `curves/*.png` | 各阶总览、Z-score 总览和每路详细图；23 路、最高三阶时共 31 张 |

HTML 的图形做极值抽样以控制大小，完整数组与 CSV 不抽样。CSV 不重复输出
0 阶的两套同值结果。时间戳重复、倒序、非有限值等结构问题见 JSON/HTML，
并不混充 Z-score 异常点。

NPZ 的 `signal_0000` 等编号到实际信号名的映射在 `summary.json` 的
`signals[].npz_prefix` / `key` 中。读取示例：

```python
import numpy as np

with np.load("报告目录/series.npz", allow_pickle=False) as result:
    original = result["signal_0000/values_original"]
    timestamp_ns = result["signal_0000/timestamps_ns"]
    third_difference = result["signal_0000/order_3/raw_difference"]
    third_difference_z = result["signal_0000/order_3/raw_z"]
    third_rate = result["signal_0000/order_3/time_derivative"]
    third_rate_z = result["signal_0000/order_3/derivative_z"]
    third_time_s = result["signal_0000/order_3/time_s"]
```

## 导出独立曲线图片

正常运行检测命令已经会自动输出图片。只有需要给旧报告补画或另选指标时，才需要
单独用 `scripts/plot_hdf5_quality.py` 把报告目录中的计算结果画成 PNG：

```bash
/usr/bin/python3 scripts/plot_hdf5_quality.py "hdf5-reports/实际报告目录"
```

当前电脑的系统 Python 已有 `numpy` 和 `matplotlib`；其他环境运行前需要安装
这两个依赖。脚本生成各阶总览、Z-score 总览和每路独立详细图，附带可离线打开
的 `index.html` 图片目录。默认绘制普通差分，添加 `--metric derivative` 可改画
时间归一化变化率。图片按完整分辨率数据绘制，原 HDF5 不修改。

## 计算约定

两套结果同时保存，不把普通差分和导数混在一起：

1. 普通差分：`Δx[i] = x[i+1] - x[i]`，重复计算到最高三阶。
2. 时间变化率：`D1[i] = (x[i+1] - x[i]) / (t[i+1] - t[i])`；结果定位到两个
   时间的中点，再对这一序列继续求差得到 D2、D3。单位分别是原单位/秒、
   原单位/秒²、原单位/秒³。不规则采样下是离散估计，不是精确解析导数。
3. 每路、每阶、每类指标分别计算 `Z = (值 - 均值) / 总体标准差`，使用有效
   有限样本，`ddof=0`；常量序列定义 Z 为 0，少于两个有效样本提示不足。

Z-score 的基准是当前文件这一条序列，不是外部正常数据集，不跨通道混算，
也不是滑动窗口 Z-score。0 阶会保留额外的原值统计。

第 k 阶结果索引 i 对应原始数据 `i..i+k` 的样本范围。时间是相对该信号
第一条记录的秒数，各通道各自从 0 开始；跨通道对齐请用 NPZ 中的绝对纳秒
时间戳，不能直接拿这些相对时间互相对齐。

不排序、不平滑、不插值，不跨重复/倒序时间戳、NaN/Inf 或长间隔求差分。
无效位置保留 NaN，便于定位原始样本，而不是把断档两侧拼在一起。

## 检查范围和结论

- 遍历 `/signals/*`：兼容新版 23 路及带数值曲线表的旧文件，不硬编码必须 23 路。
- 视频仅检查每帧时间戳、帧索引及帧数属性，不解码画面，不能证明画面正常或源头无丢帧。
- 校准偏移量作为元信息检查，不当成连续曲线做差分。
- 原始 CDR、消息索引等结构会列在数据集清单中，但不当传感器曲线求差分。
- 没有 `/signals` 表的旧版原始 CDR 文件会明确报不支持，不会假装检查通过。
- 四元数额外检查模长和相邻负点积。`q` 与 `-q` 表示同一姿态，符号翻转可能导致
  分量差分出现尖峰。脚本只提示，不自动改值；分量导数也不是物理角速度。

报告分为“结构错误”“需要复核”“未发现规则触发”。默认只要存在 Z 超阈值点
就标记需要复核，所以长序列、多通道、多阶检查时可能每路都出现提示。这不是
判废结论，应看发生比例、位置、原曲线和动作内容。三阶差分特别容易放大噪声和
时间抖动；真实快速动作也会产生尖峰。常量可能意味着静止，也可能意味着卡死。
整段固定偏移、匀速漂移等问题，仅凭本文件内 Z-score 可能检查不出来。

时间间隔检查不等于传输延迟测量；目前曲线时间轴取自录制侧，不能从它单独
推出完整的硬件到电脑延迟。
