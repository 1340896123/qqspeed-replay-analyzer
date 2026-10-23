# 自动驾驶训练数据导出

从 SAV 导出逐帧事实、原生动作证据和可直接加载的数值时序样本。全程离线，不启动游戏。

当前可直接训练**未来轨迹预测**，也可按有效性掩码训练未来动作事件、效果状态的辅助任务。键盘行为克隆和视觉驾驶需要另外取得经过验证的按键标签、同步图像。

## 运行

分析器本身仍不依赖 Python；此可选导出工具需要 Python 3.10+ 和 NumPy。

在项目根目录执行：

```powershell
python -m pip install -r Data/App/Tools/TrainingData/requirements.txt
powershell -NoProfile -ExecutionPolicy Bypass -File Data/App/Tools/TrainingData/Export-AutodriveDataset.ps1 -ReplayPath "<录像目录>" -OutDir ".temp/training-data"
```

`-ReplayPath` 可指定一个 SAV 文件或包含 SAV 的目录；目录只读取本层 `.sav`。原始文件保持不变，同内容文件按 SHA256 去重。输出目录必须位于项目内且为空，避免覆盖已有数据。

默认每秒 20 个采样时刻，历史 10 帧，预测未来 20 帧，每 5 帧创建一个样本，允许的原始数据间隔最多 150 ms。可用 `-Hz`、`-History`、`-Future`、`-Stride`、`-MaxGapMs` 调整。历史覆盖约 0.45 秒，预测覆盖未来 1 秒。

训练窗口默认只使用本地高频影子。`-IncludeNetwork` 可加入网络影子的轨迹窗口；它们不会继承本地玩家的动作标签。所有已解析影子的逐帧数据仍会导出。

过程日志和可重用解析缓存位于 `.temp/training-data-cache/`。原始数据、缓存及数据集不随源码发布；推荐将导出放在 `.temp/` 或已忽略的 `Data/TrainingData/` 下。

## 产物

| 文件 | 内容 |
| --- | --- |
| `manifest.json` | 字段顺序、单位、时间参数、训练就绪状态、按录像划分的数据集、能力与质量报告 |
| `normalization.json` | 仅用训练集拟合的特征均值、尺度和有效值计数 |
| `samples.jsonl` | 每个训练样本的分片、行号、录像 SHA256、影子与锚点时间 |
| `physical/*.npz` | 物理记录的时间、位置、四元数、原生速度等数值，以及独立标记的未解释候选字段 |
| `frames/*.npz` | 逻辑影子的全量逐帧状态、有效性掩码、时间断点、原生事件和未命名输入候选 |
| `sequences/*.npz` | 历史特征与掩码、未来相对轨迹、辅助标签与掩码、对应原始行索引 |

`physical` 的 `values` 列顺序在 manifest 对应的 `fields` 中；`mask` 表示有限数值，**不表示候选字段已经具有按键语义**。`unmapped_fields` 中的 `state_rel_*`、`input_bool_candidate_*` 不进入已确认的控制标签。

`frames` 的 `values`、`mask` 列顺序由 `state_fields` 给出。缺失状态保留缺失或掩码；不可用的效果即使原始 CSV 写了 `False`，其掩码仍为假。`controls` 当前全为 `-1`、`control_mask` 全为假，不提供伪造的按键监督。

`sequences` 的 `history` 为 `[样本数, 历史帧数, 特征数]`；`future_position_delta` 为 `[样本数, 未来帧数, 3]`。未来坐标以最后一次历史观测位置为原点，轴向保留世界坐标，单位为游戏坐标单位，不假定等于米。每个未来位置有效，无需填补缺失轨迹。

`history_time_s` 给出实际观测时间相对锚点的偏移。窗口锚点的间隔为 `Stride/Hz`，未来点位于 `1/Hz, 2/Hz, ... Future/Hz`。历史用最近过去观测，不用未来插值；未来位置目标允许在同一连续有效片段内线性插值。效果状态使用过去值保持，事件标签表示对应未来时间区间内是否发生。

缺失值在加载器输出中填为 0，且必须配合 `input_mask` 使用。数据量不足的录像可能没有可用窗口，但其逐帧事实仍保留；总训练窗口为零时命令明确失败，不生成就绪 manifest。

## 直接加载

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path("Data/App/Modules/TrainingData").resolve()))
from replay_dataset import AutodriveDataset

dataset = AutodriveDataset(".temp/training-data", split="train")
sample = dataset[0]
print(sample["input"].shape, sample["target_trajectory"].shape)
```

安装 PyTorch 后可直接交给 `torch.utils.data.DataLoader`：

```python
import torch
from torch.utils.data import DataLoader

batch = next(iter(DataLoader(dataset, batch_size=32, shuffle=True)))
x = torch.cat([batch["input"], batch["input_mask"].float(),
               batch["history_time_s"].unsqueeze(-1)], dim=-1)
# x 可交给 GRU/Transformer；主目标是 batch["target_trajectory"]。
# 效果与事件任务分别使用 target_effect_mask / target_event_mask 计算损失。
```

建议将掩码和时间偏移输入模型。GRU 的输入维度为 `2 * len(feature_fields) + 1`；未来轨迹回归头输出 `Future * 3` 个数值。

无需 PyTorch 也能运行一个小型轨迹预测基线，验证数据真正能进入训练与独立评估：

```powershell
python Data/App/Tools/TrainingData/train_baseline.py .temp/training-data --out .temp/trajectory-baseline
```

该命令仅用训练集拟合岭回归，输出权重及各划分的位置预测误差。它是离线轨迹预测基线，不包含游戏控制器。

## 数据可靠性约定

- 数据按整份录像 SHA256 划分为 train/val/test，重复文件只算一次，不随机拆散相邻帧。仅有 1 份录像时只有训练集，2 份时有 train/val；至少 3 份才有三个划分。共享赛道或同次比赛的不同录像仍可能高度相似，评估泛化时应额外按赛道或比赛分组。
- 窗口不跨越无效位置、时间倒退、过大时间间隔、姿态跳变或物理流边界。同时间戳保留最后一条有效记录用于采样，全量原始行仍保留。
- 历史速度使用当前与过去位置的差分；只有已确认的现代原生速度可替代标量速度。分析器中使用中心差分得到的速度、侧滑角不作为历史输入，避免读取未来数据。
- 原生漂移状态和喷气效果是驾驶状态，不等于 Shift/Ctrl 的按住区间。原生事件码也不能直接还原完整的方向键操作。
- 原生事件保留其时间与码；辅助事件目标仅在本地动作表与其语义校验通过时有效。WCW、组合、小喷确认标记是事件标记，不是新的键盘动作。
- 普通小喷和氮气的游戏结算口径仍未闭合；可用的原生效果区间作为效果证据保留，不承诺与结算次数相同。
- 不制造碰撞、奖励、比赛终止、库存或图片。manifest 的 `keyboard_behavior_cloning` 和 `vision` 当前明确为 false。

## 验证

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File Data/App/Tests/Smoke-TrainingData.ps1
```

测试覆盖因果时间采样、断点、重复时间、缺失掩码、动作归属、整录像划分、训练集归一化、NPZ 加载及合成 SAV 到命令行导出的完整链路。私有真实样本只在本地验收，不写入测试夹具。
