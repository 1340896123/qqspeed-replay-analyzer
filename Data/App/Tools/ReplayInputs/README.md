# SAV 原生回放按键导出

直接读取游戏播放 SAV 时使用的输入快照，导出逐帧状态、按下/松开事件和持续区间。不启动游戏，不修改录像。需要 Windows PowerShell 5.1、.NET Framework、Python 3.10+，Python 无第三方依赖。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File Data/App/Tools/ReplayInputs/Export-ReplayInputs.ps1 -ReplayPath "<录像目录>"
```

`-ReplayPath` 接受一个 SAV、多个路径或包含 SAV 的目录。默认输出 `.temp/replay-inputs/`；再次执行请通过 `-OutDir ".temp/另一个目录"` 选择空目录，已有结果不会覆盖。原始录像按 SHA256 去重。

每份录像按 SHA256 建立子目录，包含：

- `frames.csv`：原生毫秒时间、记录序号、原文件绝对字节偏移、八个输入状态，1 为按下、0 为松开。
- `events.csv`：状态变化，`press` 为按下、`release` 为松开；第一帧已经按下的键记为 `initial`。
- `intervals.csv`：持续区间；首尾越过录像边界的区间用 `start_censored` / `end_censored` 标明。
- `summary.json`：帧数、按下/松开计数、原生记录表边界、最大采样间隔和哈希。

根目录 `replay_index.csv` 按原录像文件名列出对应的导出文件，`manifest.json` 保存字段语义。CSV 可直接用 Excel 或 Python 读取。索引保留原文件名，仅放在被 Git 忽略的本地输出目录中。

| 字段 | 游戏逻辑输入 | 每帧文件偏移 |
| --- | --- | --- |
| `left` | 左 | 60 |
| `right` | 右 | 61 |
| `up` | 主前进输入 | 62 |
| `down` | 后退/刹车 | 63 |
| `drift` | 漂移输入，通常对应 Shift | 64 |
| `nitro` | 氮气输入，通常对应 Ctrl | 65 |
| `reset` | 复位输入，通常对应 R | 66 |
| `up_secondary` | 备用前进输入，单独记录 | 67 |

这是游戏逻辑输入通道。改键后的原始物理键名没有保存在这些字段中；主前进与备用前进分别保留，可按任务需要自行取逻辑或。按键状态与漂移、氮气效果是否成功触发是不同的数据。

时间采用录像自身的时钟。一次变化发生在前后两条采样之间，事件 CSV 的 `time_ms` 是首次观察到该状态的采样时间；`previous_time_ms` 保留上一条采样时间。不会生成帧内未记录的短按，也不会在录像结尾补一个虚假的松键事件。

当前验证范围为 SAV 版本 50–55、230 字节物理记录、单一高频车辆流；真实样本验证版本为 55。多影子、低频联网影子、缺失原生记录计数或不支持的格式会明确失败，避免把未记录输入的零值当作松键标签。原生计数与检测终点一致时，会补回物理检测器因首帧长间隔而略过的开头记录。

静态依据见 [ADR 0017](../../docs/decisions/0017-replay-input-snapshots.md)。旧的轨迹训练导出工具仍使用自己的未确认输入掩码；本工具的确认结果和 CSV 是独立的新输出。
