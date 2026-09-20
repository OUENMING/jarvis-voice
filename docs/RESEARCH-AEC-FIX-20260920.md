# 双讲近端损伤：修复方向调研（2026-09-20 晚）

> 触发：`AEC-AB-20260920.md` §10 已把症状坐实（3 轮配对，安静 7.4% → 双讲 23.6%，**3.19×**；
> 残余回声远低于门限 ⇒ 不是回声误触发，是**近端被压变形**）。
> 本文是「下一步该往哪修」的调研：业界惯例 + 前沿研究 + 社区实况 + 四条候选的可行性。
>
> **结论：我自己提的「缩短双讲窗口」作废（§1）；真正的根因官方已定名并已修，但我们的绑定拿不到（§2）。**

---

## 1. ❌ 先否掉我自己提的方向：「缩短双讲窗口」

我在上一轮说「损伤只在双讲那几百毫秒里，把宽限窗口缩短就行」。**错了，两个原因：**

1. **那 800ms 是「误判撤销窗口」，不是停播路径。** 生产里 `_begin_bargein()` 在 **VAD 起音那一刻就 `player.pause()`** 了 —— **停播本来就快**。
   缩短 800ms **不会让助手停得更快**，只会让「嗯/对」这类应答也被当成真打断。
2. **真正的双讲窗口 = VAD 起音延迟**（我们自己的实测：比真实开口晚 **~500ms**）。要挤也只能挤这里，跟 800ms 无关。

**业界也是反方向**：LiveKit 的 `false_interruption_timeout` 默认 **2.0s**，还有 `backchannel_boundary`（在助手说话的**起/末各 1 秒**里抑制打断判定）。它们**加**窗口，不加"停得更快"。

⇒ 唯一还站得住的那半条：**把 VAD 起音做快**（缩短"开口→暂停"）。与下面 2/3 不冲突，可以并行。

---

## 2. 🎯 真根因：官方已定名、已修，但**我们的绑定拿不到**

**WebRTC 官方 issue `442444736`**（Bug · **P1** · **Fixed**，创建 2025-09-02）原话：

> **"The residual echo suppression often overestimates the echo, sometimes causing oversuppression
> of nearend and poor transparency."**

**症状逐字命中。** 官方修法 = **Neural Residual Echo Estimator（ML-REE）**：TFLite 模型替掉手调的
残余抑制器 + 一套专用的 `EchoCanceller3Config`，CL 里明确提到
「dominant nearend 时用**无界回声掩码**」「改善 transparency」。

**配套机制解释**——`42220269`（"The AEC3 transparency is poor initially in the call when headsets are used"）：

> "In AEC3 the suppressor is **very conservative and applies more suppression than needed
> at the onset of the first render activity**. The reason is to avoid that it leaks echoes
> before the echo models are trained."

**这正是「每次开始播报/每次打断的开头被吞掉」的机制。**

### 我们的二进制里没有它（本地核实）

| 检查 | 结果 |
|---|---|
| `strings _webrtc_audio…so \| grep NeuralResidualEcho / neural_residual / ResidualEchoEstimator` | **0** |
| `… grep EnforceMoreTransparentNormal / SuppressorTuningOverride / SensitiveDominantNearend` | **0** |
| **阳性对照**：`WebRTC-Aec3` 8 命中、`RenderDelayController` 2 命中 | ✅ 方法有效 |
| `pywebrtc-audio` PyPI 最新版 | **0.2.0（2026-09-03）**，我们已是最新 |

⇒ **上游修好了，但这条 Python 绑定没带。** 这是目前**唯一"官方已修"**的路。

---

## 3. 四条候选的重新排序

| 候选 | 先例 | 结论 |
|---|---|---|
| **① 拿到带 ML-REE 的 AEC3** | 官方 P1 已修（§2） | 🟢 **首选**。代价：得自己编 webrtc-audio-processing 或另找绑定 |
| **② 够到 AEC3 抑制器调参**（`EchoCanceller3Config`） | 独立工程记录（go-mediatoolkit `aec/README.md`）说用「门控抑制器 + 放松 `normal_tuning` 掩蔽阈值」治过同症状 | 🟢 与 ① 同一个工程（自建绑定）。⚠️ 已本地核实 **`pywebrtc_audio` 不暴露**（只有 5 个类：`EchoCanceller/NoiseSuppressor/VoiceDetector/GainController/AudioProcessor`）|
| **③ 把 VAD 起音做快** | 无直接先例，但逻辑清楚 | 🟡 可并行，窗口小（~500ms） |
| **④ wet/dry 混合**（Deepgram 推荐） | 官方建议 + 论文（Iwamoto Interspeech 2022 Observation Adding） | ❌ **已本地实测：更差**（见 §4） |
| **⑤ ASR 吃原始音频 + 文本护栏** | Deepgram / Google TEC / Hermes #75792 有同类先例 | ❌ **已本地实测：更差**（见 §4）|
| **⑥ target speaker extraction** | 有开源（clearvoice/MossFormer2、TIGER） | ⚠️ 需说话人注册，且是**生成式**（自带伪影 —— 恰恰是最伤 ASR 的东西） |

---

## 4. 已本地实测否掉的两条

`tools/aec_ab.py --sweep --gain 1.0`（回声增益调到**与语音同级**，20 个合成双讲场景 / 660 字）：

| 变体 | 转写错误率 |
|---|---|
| pyaec-lin (Speex 线性) | **18.0%** |
| webrtc（现行） | 51.4% |
| **wet70/dry30** | **71.5%** |
| **raw（不过 AEC）** | **77.4%** |
| wet50/dry50 | 87.0% |

**④ wet/dry 更差，且机制清楚**：`α·AEC + (1−α)·raw` 等价于「少消 α 份回声」——
在**回声 ≈ 语音**时是拿回声换近端，净亏。

⚠️ **它和文献不矛盾**：Iwamoto 的 Observation Adding（CHiME-3 上 ~20% 相对 WER 改善）
是**降噪**场景 —— 「伪影 vs **噪声**」。我们这里是「伪影 vs **对方在说话**」，
回声是**竞争语音**，掺回来代价大一个量级。**别人的结论换场景不能直接用。**

**⑤ 同理**：ASR 吃原始音频时回声与用户语音混在一起，两边都糊（77.4%）。
文本护栏（我们已有 `echoguard`）拦得住「整句都是回声」，拦不住「混在一起」。

⚠️ **但 ⑤ 有两条独立的一手先例值得记**：FireRedChat（arXiv 2509.06502）明说
「**pVAD 只用于时间戳与 barge-in 控制，不把去噪后的分段喂给下游**，而是用时间戳从
**原始未去噪音频**里切段给 ASR」——他们的场景里干扰是**噪声**不是**对方语音**，所以成立。

---

## 5. 行业惯例：耳机是默认假设

| 来源 | 原话/事实 | 置信 |
|---|---|---|
| Gemini 官方 cookbook（两个独立仓库） | 「**Use headphones**…否则模型会打断自己」 | 高 |
| OpenAI API Reference | 降噪分 `near_field`（耳机/近讲）vs `far_field`（笔记本/会议室）—— 官方承认远场是另一回事 | 高 |
| Pipecat | 「自己打断自己」的 issue **被直接关掉**，理由「out of scope，手机和浏览器自带 AEC」 | 高 |
| LiveKit 官方博客（2026-03） | 把 AEC 列为「直接用模型 API 会踩的坑」：「没有它，你要么自己搭信号处理，要么**忍受一个不断打断自己的 agent**」 | 高 |
| OpenAI 官方论坛 | 官方对同问题的「quick solution」就是**用耳机** | 高 |

**⇒ 业界主流不是"自己搭 AEC"，而是"用平台原生 AEC"或"戴耳机"。我们走的正是少数派那条。**

---

## 6. 同症状的先例（都是"没人解决"）

- **StackOverflow 68450087**（2021-07-20）标题就是「AEC3 suppress NearEnd audio when FarEnd has Noise」，**五年无人给出 API 级解法**。
- **discuss-webrtc**「Tuning AEC for double-talk robustness」：用例是「多人无耳机、共享同一音源、间歇插话」—— 与我们几乎一样；长期跑双讲后结论「**AEC in Chrome mangles audio in both directions**」，并在官方推动下定性为「**现有实现无法通过调参解决，需要一个全新的 AEC 实现**」。
- **`muesli` commit 7a31073**：**直接移除 WebRTC AEC**，理由原话「AEC 在关键路径上会**抹掉真实的"你"说的话**（延迟估计/收敛出错时）——**比残留回声更糟的失败模式**」。
- **WebRTC issue 42233568**「Excessive AEC suppression」：回声在麦克风里饱和 → 强 ducking。

---

## 7. 语义层回声过滤：我们已有，保留，但别当修复手段

- **Google Textual Echo Cancellation**（ASRU 2021, arXiv 2008.06006）：只用 TTS **源文本**做 side input。
  它自己的结论：**仍不如用真实播放音频的 AEC** —— 而我们**恰好有真实播放音频**，条件比它好。
  但**无开源权重**，只作架构参考。
- **Hermes #75780 → PR #75792**：与我们逐字相同的问题，修法是 **playback-phase transcript guard**
  （把打断捕获的转写与当前 TTS 文本比对，高相似就丢弃），**且专门处理无空格语言（中文）**。
- Deepgram 官方：比对 STT 输出与刚播的 TTS 文本，匹配就丢。

⇒ `echoguard` 是这一类。它**必须留**（防"回应自己"），但它**治不了近端被压变形** —— 两者是不同的病。

---

## 8. 下一步（修正后）

1. **查/编带 ML-REE 的 AEC3** —— 官方已修的正是我们的症状。
   路线：`webrtc-audio-processing` 新版有没有 ML-REE？没有就自己编 WebRTC 的 `modules/audio_processing`，
   写 ctypes 绑定（或替换 `pywebrtc_audio`）。**这是唯一有官方背书的路。**
2. **同一个工程顺带拿到 `EchoCanceller3Config`** —— 抑制器透明度调参（`normal_tuning.mask_lf.enr_transparent`
   默认 0.3 等）。独立工程记录说这条路治过同症状。
3. **并行**：把 VAD 起音做快（唯一还能挤的双讲窗口，~500ms）。
4. **保留** `echoguard`；**不采用** wet/dry、raw-to-ASR、target speaker extraction。

⚠️ **诚实交代**：§2/§3 里来自调研代理的若干细节（`enr_transparent` 的具体数值、
go-mediatoolkit 的修法、某些 issue 号）**我没能全部亲自复核**。
已亲自复核的是：**442444736 存在且措辞如引**、**42220269 的起音保守机制**、
**`pywebrtc_audio` 的 API 表面**、**我们的二进制没有 ML-REE 字符串**、
以及 §4 那张表的**全部数字（自己跑的）**。


---

## 9. 工程落地结果（2026-09-20 晚）—— **三条路都试了，都没走通**

真去做了：编译 `webrtc-audio-processing` v2.1 + 写 ctypes wrapper + 打补丁。
详细构建配方与三个使用陷阱见 **`tools/webrtc-aec/README.md`**。

### 9.1 ✅ 意外收获：找到并修了上游一个真 bug

`AudioProcessing::Config::echo_canceller.export_linear_aec_output` **从没被写进它真正控制的
`EchoCanceller3Config.filter.export_linear_aec_output`**（`audio_processing_impl.cc:1892`
那个 `EchoCanceller3Config config;` 是纯默认构造）。后果：线性输出缓冲被分配、
AEC3 却不往里写 → **`ProcessStream` 段错误**（`EXC_BAD_ACCESS @ BlockFramer`，空指针）。
一行补丁接上。**这是 v2.1 的真 bug**（那个公开 flag 一开就崩）。

### 9.2 ❌ 「绕开抑制器、只取线性输出」——不成立

`GetLinearAecOutput()` 在本 build 里返回的**几乎是输入本身**：

| | 输入 RMS | 输出 RMS |
|---|---|---|
| 线性抽头 | 0.141 | **0.140**（未处理） |
| 全链（抑制器之后） | 0.141 | 0.026 |

⇒ **抑制器在做必需的活**，抽掉它回声全剩。这条路作废。

### 9.3 ❌ 光升级到 v2.1 —— 合成场景下没用

| 变体 | ASR 字符错误率（4 段配对） |
|---|---|
| raw | 359% |
| **2020 快照全链（生产现行）** | **90.7%** |
| v2.1 全链 | **88.9%**（持平） |

### 9.4 ❌ 高频掩蔽阈值 —— **真机实测：更差 3.5 倍，A 到此关闭**

把 `normal_tuning` 的高频阈值从 (.07,.1) 改成 (.3,.4)（上游 `...NormalSuppressorHfTuning`
字段试用干的事），编好，然后在**真机双讲录音**上离线 A/B（录一次、换后端重算，见下）。

| 后端 | 安静 CER | 双讲 CER | 倍数 |
|---|---|---|---|
| **prod（现行 `pywebrtc-audio`）** | 6.0% | **18.5%** | **3.08×** |
| **v2.1 + 高频补丁（自编）** | 6.0% | **64.8%** | **10.77×** ❌ |

**安静段两者完全一致（6.0%）** —— 没回声时 AEC 不起作用，这也顺带说明测法没问题。

**结论**：放松高频掩蔽 = 少消一份残余回声。而我们的场景是**回声 ≈ 语音**，
多漏回来的回声**比近端被压的损失更贵** —— 与 §4 的 wet/dry 是同一个道理。
**⇒ 不做。39MB 源码树 + meson 工具链的维护成本也不必付了。**

⚠️ **顺带一条重要的方法论结论**：同一件事，**合成测试判不出来**（§9.3 里两种实现在合成上
是 90.7% vs 88.9%，几乎无区别），**真机双讲协议一测就分开了**（18.5% vs 64.8%）。
⇒ **AEC 类改动一律以真机双讲协议为准；合成只配用来排错方向。**

### 9.4.1 真机双讲录音已存档（可反复复算，不用重念）

`tools/measure_echo_delay.py --double-talk --save-dt X.npz` 录一次，
`--eval-dt X.npz --aec {prod,v21}` 离线换后端重算（**不碰任何音频设备**）。
这次那份存在 **`~/.jarvis/dt-recordings/20260920-dt.npz`**（8.4MB，**不入库** ——
仓库是公开的，里面有用户真声）。

### 9.5 🟡 原来的「高频阈值」条目（保留作过程记录）

已按源码把 `normal_tuning` 的**高频**阈值从 `(.07,.1)` 改成 `(.3,.4)`（上游那个
`EnforceMoreTransparentNormalSuppressorHfTuning` 字段试用干的事）并编译好。
**合成场景下无区别（90.7% vs 90.7%）** —— 因为合成里没有房间混响，掩蔽阈值不是瓶颈。
**要判它只能用真机双讲协议**（基线 **7.4% → 23.6%，3.19×**）。

### 9.5 ⚠️ 我在这一轮里犯的两个错（都已撤回）

1. **`Initialize()` 与 `ApplyConfig()` 顺序反了** → `Initialize` 把配置重置掉 →
   AEC 根本没启用 → 输出 == 输入。**我据此得出过「线性输出没用」的结论** ——
   那个结论当时是**无效的**（直到我把顺序修对、又用正弦自检才发现）。
2. 修好顺序后，我一度**又**得出「线性输出 ≈ raw」并当作结论 —— 这次是**真的**
   （§9.2 的正弦自检证实），但**第一次得出时是错的**。
⇒ **教训**：封装别人的 DSP 库时，**先做一次"输入变了、输出必须变"的自检**，
再拿它跑任何对比。否则测的是"什么都没发生"。



---

## 10. 🔴 附：VAD 起音那次改动的回归（2026-09-20 深夜）

**做了什么**：`vad_min_speech` 0.25 → 0.05（离线把打断延迟 400ms → 200ms，
`tests/test_bargein_latency.py` 有 red-green 的预算门禁）。

**结果**：**真机立刻回归 —— 「每次开播都被自己的声音打断」。已回退到 0.25。**

**⚠️ 最要紧的一条：离线测试既没预测出它、也没能复现它。**
- 我原来的安全论证是「瞬态噪声免疫」测试全过 —— 但那组用**白噪声**，
  而真机漏回的是**助手的语音**（Silero 会判它是 speech）。
- 改用「一小段真人语音」当回声去复现 —— **同样失败**（120ms 语音在 0.25 下也触发）。
  ⇒ **`min_speech_duration` 根本不是挡回声的那一层，最小复现至今没找到。**

**⇒ 硬规矩（写进记忆）**：**涉及实时交互的参数（VAD 阈值/延迟/打断判据），
离线测试通过 ≠ 真机不会坏。必须先在真机上验一次。**
这一类参数的失效模式**依赖真实回声的频谱与语谱结构**，白噪声合成测不出来。

