# docs 索引

> 本项目从「双脑(端到端语音模型)」**重写**为「级联(ASR→Claude Code→TTS)」。
> 描述旧架构的文档已移到 [`archive/`](archive/) —— **不删**，因为它们是"为什么走到今天"的底账。

## 当前有效

| 文档 | 内容 |
|---|---|
| **`HANDOVER-20260918.md`** | ⭐ **冷启动入口** —— 现状全文交接（架构/文件地图/未解决问题/下一步/红线）。接手先读它 |
| **`CONSOLIDATION-20260919.md`** | ⭐ **本次会话（09-19）全部调研的合并入口** —— 结论总表（按置信度）+ 决策地图 + **第一步（15 分钟测 AEC 可行性）** + 已否掉的路线 + 合并的未验证清单。**读完 HANDOVER 读它** |
| `RESEARCH-FINAL-20260915.md` | 主线调研（级联 vs 端到端、各组件选型） |
| `RESEARCH-CLOUD-ADDENDUM-20260916.md` | 云端开放后的成本重估 |
| `RESEARCH-TTS-COST-20260916.md` | 云 TTS 性价比复核（含都柏林裸 TCP 实测） |
| `RESEARCH-NATURALNESS-20260916.md` | 自然度天花板 + **要不要插本地模型**（结论：两侧都不插） |
| `RESEARCH-BARGEIN-20260916.md` | 免提打断 / AEC 调研（结论：用耳机） |
| `RESEARCH-AEC-20260919.md` | **AEC 增量调研 + 接入方案**（pywebrtc-audio 0.2.0 核实可用、逐文件改动面、P0 半天 ERLE 实测设计、决策树） |
| **`PROBE-AEC-RESULTS-20260919.md`** | ⭐ **AEC 实测结果** —— 本机稳态 **ERLE 34–36 dB / ACOM ≈40 dB**，**判决落在「接入」档且余量 2.3 倍**；+16dB 音量仍过门限；`stream_delay_ms` 不需调。**含一次自我纠错**（第一版把收敛期算进去得出错误结论） |
| **`VERIFY-AEC-PLAN-20260919.md`** | ⭐ **对 AEC 计划的独立核实** —— 三处更正（「业界没人做到」不成立且**改变路径 C 优先级**；ERLE 与主观相关性仅 PCC 0.31；**必须分安静/真实桌面两组测**）+ 与升级计划的**冲突矩阵**（`vad_min_snr` 复合冲突）+ **一条计划漏掉的风险**（打断检测首次在播放期连续运行） |
| `VERIFY-AND-BRAINSTORM-20260916.md` | 对上面几篇的独立核实（抓出两处错误） |
| `VERIFY-TURN-DETECTION-20260916.md` | 端点检测选型冲突核实 + 引用链审计 |
| `VERIFY-FISH-EMOTION-20260916.md` | **Fish 情感标签实测无效**（F0 统计 + 重复采样） |
| `LANDABILITY-AUDIT-20260916.md` | 落地性 / 实现效果 / 前沿性 |
| **`RESEARCH-UPGRADE-PLAN-20260919.md`** | ⭐ **打断/端点/状态清理升级计划** —— 4 个参考实现的一手源码阅读（Handy / LiveKit Agents / HF speech-to-speech / silero）+ 与本文档库既有结论的对齐。含「音频路径卡住即哑」的世代判断+看门狗修法、话首切字的预滚方案（**含 SNR 顺序陷阱**）、`<transcript>` 注入隔离。**§7 的验收纪律沿用 `VERIFY-TURN-DETECTION-20260916.md`** |
| `WORKORDER-01-master-verify.md` | 核实任务书 |

## archive/（描述已废弃的双脑架构）

`JARVIS-HANDOVER-20260915.md`、`JARVIS-HANDOVER.md`、`EVAL-SINGLE-BRAIN-20260915.md`、
`AUDIT-REPORT-20260913.md`、`AUDIT-HANDOVER-PROMPT.md`、`HANDOVER-VERIFY-20260915.md`、
`README-M1Pro-Phase2-历史.md`、`WORKORDER-02-control-experiment.md`

⚠️ **读这些要留意时间**：里面的结论多数已被推翻（例如"端点用 Smart Turn 是 SOTA"、
"MiniCPM-o 是唯一可行解"）。当前实现以 `../README.md` 与代码为准。

## 阅读口径（项目规矩）

- **一手 > 二手**：能自己跑的实验 > 能读的文档 > 能听的说法。
- 引用旧结论时**标注来源时间**；数字/状态这类易变信息**先查当前状态**再引用。
- 文档里的 `🟢/🟡/🔴` 分别表示：一手核实 / 单源二手 / 未验证。
