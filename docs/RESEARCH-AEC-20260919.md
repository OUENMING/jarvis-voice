# AEC 增量调研与接入方案（2026-09-19）

> **定位**：对 `RESEARCH-BARGEIN-20260916.md` 的**增量更新**。旧文仍然成立的部分（"业界没人做到免提打断"、VPIO 坑与修法、不该走的四条路、LiveKit 参数集）**不重复**，直接引用。
> **触发**：Owen 2026-09-19 提出后续可能做 AEC，要求调研并更新计划。
> **口径**：🟢 一手核实　🟡 单源二手　🔴 未验证。引用旧结论标注来源时间。

---

## 0. 三句话结论

1. **旧调研（2026-09-16）的首选路径 A 本轮全部核实成立**：`pywebrtc-audio` 0.2.0（2026-09-03 发布）有 **cp314 macOS arm64 轮子**（🟢 PyPI JSON 直查）——项目 `.venv`（Python 3.14.7）`pip install` 即用，且该包 3→5→9 月三连发，**维护活跃**。API 是一行式 `ap.process(near, far)`，far 参考我们自己有完美版本（知道正在播什么）。
2. **旧调研缺的"怎么接进本项目"本轮补齐**（§2）：核心工程量在三处——far 参考信号的 44.1k→16k 下采样（scipy 已在 venv）、`audio_mode` 派生属性加第三档、**残余回声 × SenseVoice 幻觉的叠加防线**。改动面约 5 个文件，估算 1-2 天。
3. **一切仍卡在那半天实测上**（🔴 至今没人测过）：`pywebrtc-audio` 在 M1 Pro 内置麦+内置扬声器的 **ERLE 天花板**是唯一未知数——结构传声（机身振动）是非线性的，线性 AEC 建模不了（旧文 §2 路径 A 已指出）。§3 给出实测脚本设计与判定线，测出数字之前**免提维持半双工现状**。

---

## 1. 本轮增量核实（相对 2026-09-16）

| # | 事实 | 等级 | 来源 |
|---|---|---|---|
| 1.1 | `pywebrtc-audio` 0.2.0，2026-09-03 发布，含 `cp314-cp314-macosx_11_0_arm64.whl`；历史 0.0.1(2026-03-27) → 0.1.0(2026-05-19) → 0.2.0(2026-09-03)，活跃维护；Apache-2.0；requires_python ≥3.10 | 🟢 | PyPI JSON API 直查（2026-09-19） |
| 1.2 | API 形态：`AudioProcessor(sample_rate, num_channels, echo_cancellation, noise_suppression, high_pass_filter, auto_gain_control, ns_level, stream_delay_ms)`；`ap.process(near, far)` 收 int16/float32 numpy 任意长度，内部切 **10ms 帧**、GIL 外 C++ 执行；`ap.speech_probability`（0-1）可用作回声感知门控；**实例非线程安全**；AEC/NS/AGC/VAD 可拆成独立类单用 | 🟢 | PyPI README 直读（2026-09-19） |
| 1.3 | 性能：作者自测 M3 Pro 上 EchoCanceller 处理 100ms 音频 622µs（161× realtime）；48kHz 立体声全功能仍 82× | 🟡 | 厂商 README 自测（2026-09-19 读），**未独立复核** |
| 1.4 | 新发现候选 `aec-audio-processing` 1.0.1（BSD-3，2025-09-01）：API 同风格（`process_stream`/`process_reverse_stream`/`set_stream_delay`，10ms 帧），但**只有 Windows 轮子**，macOS 需源码编译（swig+meson）→ 仅当 pywebrtc-audio 出问题时启用 | 🟢 | PyPI 页面直读（2026-09-19） |
| 1.5 | OVC（arXiv 2606.23332，Interspeech 2026，TD-SpeakerBeam + Mamba-MinGRU masker，2ms 算法延迟）：论文确认在；**仍未发现权重释出证据** | 🟢（论文存在）/ 🔴（权重） | arXiv 检索（2026-09-19）；旧文 §2 路径 D |
| 1.6 | VPIO（Apple 系统级 AEC）：本轮未找到新的一手证据，**维持旧判**（2026-09-16 旧文 §2 路径 C：值得半天试、风险中等、有"前 3-6s 丢音"负面先例） | — | 引用旧文，标注来源时间 |
| 1.7 | scipy 1.18.1 已在 `.venv`（依赖带入）→ 44.1k→16k 重采样**零新增依赖** | 🟢 | 本地实测（2026-09-19） |

**web notes**：`WebSearch` 工具本轮两次超时，检索改走豆包搜索 + PyPI JSON API，不影响结论质量（关键事实全部落在 PyPI/arXiv 的一手页面）。

---

## 2. 接入方案（旧调研缺的部分）

### 2.1 数据通路

```
                          ┌─ far 参考：Player.write() mirror ─→ 环形缓冲(44.1k int16)
TTS ─→ TTSThread ─→ Player│                            └→ resample_poly(x, 160, 441) → 16k
                          └→ OutputStream ─→ 扬声器
麦克风 ─→ MicStream(16k f32, 100ms 块) ─→ [AecGate: ap.process(near, far)] ─→ VadGate ─→ (原链路)
                                              └ 播放期间附加 speech_probability 门控
```

- **near** = 麦克风流，已是 16kHz float32，**不用动**。
- **far** = `Player.write()` 收到的 PCM（44.1k int16）。mirror 点选在 `write()` 而不是 TTSThread，因为：① `write_filler` 也走 `write()`，填充音天然计入参考；② mirror 的是"真正进播放队列的音频"，与实际发声最接近。
- **对齐**：WebRTC APM 自带 delay estimator，`stream_delay_ms` 只需给个粗值。动态量：`player.buffered_seconds()`（现成计数器）+ 输出块延迟，起步值 ~150ms，实测微调（README 明确该参数运行时可改：`ap.stream_delay_ms = 50`）。

### 2.2 逐文件改动面

| 文件 | 改动 | 量 |
|---|---|---|
| `config.py` | `audio_mode` 加 `"speaker_aec"`；派生属性改为 `barge_in = mode in ("headphones","speaker_aec")`、`half_duplex = (mode == "speaker")`——**单一事实来源不变**；加 `aec_stream_delay_ms` | 小 |
| `player.py` | `write()` 加 mirror 到 far 环形缓冲（deque + lock，容量 ~2s）；提供 `take_far(n_samples_16k)` | 小 |
| 新 `jarvis_voice/aec.py` | `AecGate`：持有 `AudioProcessor`；`accept(chunk)` 内取 far、重采样、`process()`、返回干净信号；`echoning()` 帮助方法（播放期间用 `speech_probability` 复核） | 中 |
| `orchestrator.py` | 主循环 `utts = self.vad.accept(chunk)` 前插 AecGate；半双工分支条件**不改**（`cfg.half_duplex` 为 False 时自然跳过）；打断检测不动（barge_in 已为 True） | 小 |
| `dashboard.py` | 模式按钮加"免提+AEC"第三档 | 小 |
| **新增防线** | AEC 开启 + 正在播放期间：VAD 门限临时提高（`vad_min_snr` ×N）或要求 `speech_probability` 双确认——**必须做**，理由见 2.3 | 小 |

### 2.3 关键风险：残余回声 × SenseVoice 幻觉 = "莫名发声"通道

这是接入方案里**最重要的一个认知**：AEC 不可能 100% 消干净（结构传声、非线性、双讲），残余回声只要过 VAD 门限，就会撞上 SenseVoice 的已知行为——**对任何非语音都幻觉出文本**（静音→`그。`、白噪声→`Yeah.`，🟢 2026-09-16 实测，旧文与 HANDOVER §6）。也就是说：**AEC 做得"还行"反而可能比半双工更吵**——半双工是物理性全消，AEC 是"消了大半但留下会让 ASR 说话的碎片"。

防线（三层，全部已有基建）：
1. `vad.py` 段级 SNR 门限已有——播放期间把 `vad_min_snr` 从 3.0 提到 6.0（可配）；
2. `asr_min_utt_sec` 已滤 <0.4s 段；
3. 新增：播放期间要求 `ap.speech_probability` 双确认，或对"AEC 输出且播报中"的段做 ASR 后再加一道置信检查。

### 2.4 与"耳机漏音"问题的关系（2026-09-18 审核发现）

AEC 只消**本项目自己播的**回声（far 参考 = Player 输出）。耳机里漏出的第三方音频（如网课）**不是回声**，AEC 消不掉——那需要说话人分离（OVC 方向，权重未释出）。两件事别混为一谈：**AEC 解决"免提时打断自己"，OVC 解决"环境里别人的声音"**。

---

## 3. P0 实测脚本设计（半天，不碰主链路）

`tests/test_aec_erle.py`，独立于应用（对齐项目"tests 是独立脚本"的惯例）：

1. **录音对照**：sounddevice 双流——OutputStream 外放一段 PCM（用 Fish TTS 现生成一句，或 `models/.../test_wavs/zh.wav`），InputStream 同时录。录制时**人不出声**。
2. **离线 AEC**：两路对齐后 `ap.process(near, far)`（far 用 `resample_poly` 降到 16k；`stream_delay_ms` 扫 100/150/200ms 三档）。
3. **指标**：
   - **ERLE** = 10·log10(近端播放段能量 / AEC 后同段能量)。判定线：**≥15dB 接入主链路**；10-15dB 可试但须强开 2.3 防线；<10dB 判死刑。
   - **幻觉测试**：AEC 后残余喂 SenseVoice，看输出文本——空/单标点 = 安全；稳定吐词 = 危险（即便 ERLE 达标也要加 2.3 防线）。
   - **双讲保真**：播放时真人读一句话，AEC 后喂 ASR，转写应基本完整（防"消回声把用户也消了"）。
4. **环境变量**：内置麦 + 内置扬声器（免提场景本体）；同时测第二组"内置麦 + 耳机输出"作上限参照。

> 旧文 §7 P0 原话是"把路径 A 测到极限，得出'免提是否可行'的**我们自己的数字**"——本节就是它的可执行版。

---

## 4. 决策树与排期

```
P0 半天实测（test_aec_erle.py）
├─ ERLE ≥15dB 且无幻觉 → Step2 接入主链路（1-2 天，§2.2 清单）
│    └─ Step3 真机验收：外放免提、说"停一下"、播报中插话 ×20 次
├─ 10-15dB → 加 2.3 三层防线后再评估真机听感
└─ <10dB → 软件路线到此为止
     ├─ 硬件路线 B：可退货 USB 会议麦（DSP 内 AEC，旧文 §2，🟢原理/🔴实测）
     └─ 或维持耳机 + 半双工（现状，零成本）
VPIO（路径 C）：仅当 pywebrtc 失败且想要系统级 AEC 时再投半天
OVC（路径 D）：保持观察——权重释出即重估（它是唯一能治"耳机漏音/他人语音"的）
```

排期建议：P0 可与 §8 真机验收同一天做（都是戴耳机/开外放的活）；Step2 排在填充音改造之后（避免同窗口改两处音频路径）。

---

## 5. 未验证清单（更新自旧文 §8）

- 🔴 pywebrtc-audio 在**本机 M1 Pro 内置麦+扬声器**的 ERLE——P0 要测的核心数字
- 🔴 AEC 残余喂 SenseVoice 的幻觉表现——P0 附带测
- 🔴 `stream_delay_ms` 在本项目缓冲结构下的最优值——P0 扫三档
- 🟡 pywebrtc-audio 性能数字（622µs/100ms）——厂商自测，接入后用实测 RTF 复核
- 🔴 VPIO"前 3-6 秒丢音"是否在本机复现——仅旧文一处负面先例，未重测
- 🔴 OVC 权重释出——2026-09-19 检索仍未发现

*本文为新增文件，写于 2026-09-19。对旧文（2026-09-16）只有一处口径修正：§9"别再试"里"免提 + AEC 打断"应理解为**"软件 AEC 未实测所以未做"**，而非"证明不可行"——本轮给出了可实测的方案，该路线**有条件重开**。*
