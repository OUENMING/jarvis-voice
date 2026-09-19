# 核实：Turn Detection 选型冲突 + 引用链审计（2026-09-16 03:00 GMT+1）

> **触发**：Owen 指出三份报告冲突——文档/模型线把 Smart Turn v3 当真 SOTA，组件线拿 eot-bench 数据推翻。
> **方法**：直查一手（LiveKit 官方博客 + `livekit/eot-bench` 仓库 README + 第三方复现），并**审计我自己三份报告里的引用链**。
> **口径**：✅一手核实　🔴**推翻/修正**　🟡单源二手　🟢我亲测。**本轮的结论是：Owen 的核心裁决成立且比原判断更稳，但补救方案有一个前提是错的。**

---

## 0. 结论

1. ✅ **数字是真的，出处已锁定**：`livekit/eot-bench`（LiveKit 自建自评的开放 benchmark）。Owen 引用的两行数字与官方榜完全一致。
2. 🔴 **但"改用 LiveKit v1-mini 来替掉 Smart Turn"这个补救不成立**：那个 **3 倍优势属于 v1——v1 是云模型**（需 API key，走 LiveKit agent-gateway）；**v1-mini 对 Smart Turn 只有 1.2–1.3 倍优势**，且**"开源权重、CPU 本地跑"在官方仓库里查不到依据**。
3. 🟢 **但 Owen 的核心裁决比我给的原判断更稳**：三方独立评测（LiveKit / Krisp / Scicom）**都把 Smart Turn 排在中下**，且 Scicom 测出它 **p90 延迟 3.04 秒**——10% 的停顿要等 3 秒才接话。这是致命的体验缺陷。
4. 🔴 **我自己的报告里还有至少 5 处同类引用（单源二手/继承文档）**，已在 §5 逐条列出并分级。**Smart Turn 不是孤例。**
5. **对项目的最重要一条**：**"本地"的边界应该重定义**——我们反对的是**广域网税**，不是"不在同一台机器上"。局域网 RTT ~1ms，把重的转向检测放到 Windows 机上**不违反原则**。这一条打开了新空间。

---

## 1. 一手核实：数字与出处（✅）

来源：`livekit/eot-bench` README + LiveKit 博客《Solving end-of-turn detection: LiveKit Turn Detector v1.0》。**英文、300ms 延迟预算下的误切率**：

| 模型 | 误切 @300ms | 误切 @600ms | 延迟 @5% 误切 |
|---|---|---|---|
| **LiveKit Turn Detector v1** | **9.9%** | **4.5%** | 543 ms |
| Deepgram Flux | 12.9% | 9.9% | 1151 ms |
| ultraVAD | 27.7% | 11.9% | 899 ms |
| **LiveKit Turn Detector v1-mini** | **27.8%** | **12.1%** | 1070 ms |
| **SmartTurn v3.2** | **35.2%** | **14.8%** | 1051 ms |
| AssemblyAI | 49.4% | 14.6% | 1049 ms |
| Soniox | – | 5.5% | 647 ms |
| VAD baseline（纯静音计时） | 55.6% | 21.7% | 1600 ms |

**✅ 顺带证实的两件事**：
- **中文在数据集里**（14 语言：Arabic, **Chinese**, Dutch, English, French, German, Hindi, Indonesian, Italian, Japanese, Korean, Portuguese, Spanish, Turkish）；
- **指标定义与我们关心的一致**：官方原文 *"Latency here is conversational dead air, not compute"* ——是"等多久才敢接话"，不是推理耗时。这正是我们该看的指标。

**⚠️ 但必须挂上的偏置标注**（Owen 提的这点对，且比预想更严重）：
- README 明写 *"We built it to evaluate LiveKit Turn Detector v1"*，宣称 v1 *"posts the strongest overall results of any model we evaluated"*；
- **仓库里没有 Limitations / Bias 章节**，**没有任何一句承认"我们既是出题人又是冠军"**；
- v1 **是云模型**：流式适配器 *"scores each turn with the LiveKit Turn Detector v1 (`turn-detector-v1`) **cloud model** over the agent-gateway EOT websocket"*，需要 `LIVEKIT_API_KEY/SECRET`。

---

## 2. 🔴 修正：补救方案里错的前提

Owen 的结论是"P0 就该把 v1-mini 和 SmartTurn 一起测"。方向对，但**两个前提需改**：

| 原说法 | 核实结果 |
|---|---|
| "600ms 时 v1 是 4.5%，SmartTurn 是 14.8%——差三倍" | ✅ 数字对，**但那是 v1（云）**。**v1-mini 在 600ms 是 12.1%，只比 SmartTurn 好 1.2 倍**；300ms 时 27.8% vs 35.2%，差 1.3 倍。**"三倍"不属于能本地跑的那个。** |
| "LiveKit v1-mini 是开源权重、CPU 本地跑" | 🔴 **查不到依据**。README **没有给 v1/v1-mini 的任何权重链接**，也**没给模型尺寸**；v1-mini 只写"走 public local-inference 接口"（`livekit-local-inference` 延迟导入），是否开放权重、多大、M1 Pro CPU 跑不跑得动——**全部未验证**。 |
| （隐含）"v1 可以本地" | 🔴 **v1 明确是云**（见上）。这**直接违反我们刚定的"云只上嘴"**——云转向检测 = 把网络税放进端点路径，正是我们否掉的东西。 |

**另一个架构歧义**：Krisp 的博客称 *"LiveKit's TT is a text-based model — it operates on transcripts rather than raw audio"*，而 LiveKit 自己的 v1 博客说 v1 *"adds audio encoders... no transcription step in between"*。两者矛盾——**推测 Krisp 评的是 v1 之前的旧（文本版）检测器**，但**未证实**。这影响"公平性"判断，需要动手前厘清。

---

## 3. 三方交叉验证：Smart Turn 的问题不是一家之言（✅ 这比单一榜更强）

| 评测方 | 结果 | 立场 |
|---|---|---|
| **LiveKit eot-bench** | SmartTurn v3.2 排第 5/7（35.2% @300ms） | 自评（自己第一） |
| **Krisp**（第三方） | Balanced Acc：TurnPred v3 88.05 > Flux 87.10 > **LiveKit 82.70** > **SmartTurn v3.2 77.41** → **SmartTurn 垫底** | 自评（自己第一），但把 LiveKit 也压在 Flux 之下 |
| **Scicom**（第三方，电话语音复现） | SmartTurn v3 **69.6%** 误切 @300ms（英）／78.1%（马来），**差于其模型，甚至差于 VAD 基线（77.3%）** | 自评（自己第一），但**公开了完整方法与对照** |

**三方都是"自评自己赢"，但三方都把 SmartTurn 排在中下——这是 O 型一致的负面结论，可信度高于任何单一榜。**

🔴 **还挖到一个更具体的失败模式**：Scicom 实测 **Smart Turn v3 的停顿判定延迟 p50 = 0.65s，p90 = 3.04s**（对比 VAD-only 0.71s / 其模型 0.74s）。
→ **含义：有 10% 的停顿，它要想 3 秒才敢开口。** 对"像正常聊天"这是致命伤——三秒死寂。**我们之前只看了平均/比率，没看 p90 尾巴。**

---

## 4. 采信裁决

| 结论 | 裁决 |
|---|---|
| "文档与模型线把 Smart Turn 当默认 SOTA 是错的" | ✅ **成立，且证据比我原来的更强**（三方一致 + p90 尾巴） |
| "这是二手引用扩散（文档怎么写、子 agent 就照着抄）" | ✅ **成立**。我的 lit 线原话是"当前中文 CPU 端点检测实际 SOTA"——**它读的是 Smart Turn 自己的仓库和 daily.co 博客，然后做了"SOTA"这个推断，没有去查任何 benchmark**。这是我的流程缺陷 |
| "改用 LiveKit v1-mini" | 🔴 **不成立**（§2：3× 属云版；开源/尺寸未证实） |
| "P0 把两个一起测" | 🟡 **方向对，但名单要改**：应是 **SmartTurn v3.2 / v1-mini（若确认可本地）/ VAP / ultraVAD / 以及 VAD-only 对照**，且**验收看 p90，不只看误切率** |

**一个必须正视的旁支**：Owen 说"组件线查到了 livekit/eot-bench"，但我复核自己的记录，**那个社区调研 agent 的原文是"中文语音上三者的独立第三方误触发率对比：未找到——未验证"**。**这三行数字不在我拿到的任何报告里。**
→ 也就是说：**我们连"这数字从哪来"都没有干净的记录**。这本身就是同一个病的又一次发作——**引用链断在了我们自己的工作流内部**。我这次是直接去一手仓库核实的，这才是正确姿势。

---

## 5. 引用链审计：我三份报告里的claim分级

> 规则：🟢=我亲手核实/亲测　🟡=单源二手（引了源但只有一处，未交叉）　🔴=继承自项目文档，未独立核实

| # | Claim | 级别 | 备注 |
|---|---|---|---|
| 1 | eot-bench 那 8 行数字 | 🟢 | 本轮一手核实（LiveKit 博客 + 仓库 README） |
| 2 | Smart Turn v3 是 SOTA | 🔴 | **错**——继承 daily.co 博客 + 它自己仓库的自我描述 |
| 3 | VoiceAgentBench"级联 > SpeechLM，最高 60.6%" | 🟢 | 亲读 arXiv 2510.07978 摘要 |
| 4 | MiniMax 国际 TTS = $60/百万字符 | 🟢 | 亲读官方计费页 |
| 5 | 阿里国际有 cosyvoice-v3.5-flash / qwen3-tts-vc-realtime 等 | 🟢 | 亲读官方文档；但**单价未核**（仍🔴） |
| 6 | pywebrtc-audio 有 cp310–cp314 arm64 轮子 | 🟢 | 亲查 PyPI |
| 7 | CC warm TTFT 1333–3069ms（首句） | 🟢 | **我自己实测** |
| 8 | 阿里新加坡 RTT 179ms | 🟢 | **我自己实测** |
| 9 | SenseVoice CER 10.11% / Qwen3-ASR 7.65% | 🟡 | 单一聚合站（53ai.com），**未交叉** |
| 10 | Kokoro v1.1-zh：RTF 0.88、首分片 <100ms、100 音色 | 🟡 | **单一个人博客**，且是 P0 的默认 TTS，**风险集中** |
| 11 | ARGUS：合成音降 94.7%、内置扬声器 70–85% | 🟡 | agent 转述仓库，**我没读源码** |
| 12 | py-xiaozhi"笔记本内置麦+扬声器物理耦合" | 🟡 | agent 引官方文档，未亲验 |
| 13 | LiveKit `aec_warmup_duration` 默认 3.0s | 🟡 | 二手源码整理文 |
| 14 | MiniCPM-o 4.5 是 16GB 内唯一可行解 | 🔴 | 继承文档；本轮已发现 PersonaPlex 7B 削弱它 |
| 15 | Smart Turn"与 Silero 配合、只在静音期跑" | 🟢 | 亲读仓库 README（事实部分成立，问题只在"SOTA"这个评价） |

**最该补的三条**：#9、#10、#11——它们都在关键路径上（ASR 能力上限、默认 TTS 性能、AEC 效果预期），却都只有单一二手来源。**#10 尤其危险：整个 P0 的默认嘴都押在一篇个人博客上。**

---

## 6. 头脑风暴（7 条）

| # | 点子 | 为什么 |
|---|---|---|
| **B1** | **重定义"本地"的边界：局域网 ≠ 广域网** | 我们反对的是**WAN 税**（阿里 SG 实测 179ms），不是"不在同一台机器"。**LAN RTT ~1ms**。→ 转向检测/AEC 这类重活可以放 **Windows 5060 Ti**，既不交 WAN 税，又能跑本地跑不动的模型。**这打开了 v1-mini/VAP 的可行性**，也让那台机器从"冷却的兜底"变成"感知加速器" |
| **B2** | **自己跑 eot-bench**（开源、含中文、有 CLI、artifacts 可复现） | 目前**没有任何人公布过中文的转向检测数字**。我们自己跑一遍 = 拿到一手中文数据。这是"benchmark 我们能跑 > 只能读"的典型 |
| **B3** | **自建 100 条 Owen 语音的小评测** | 别人的榜再多也只是参考。**唯一重要的评测是：你会不会因为它的误切而重复一遍话。** 录 100 条真实停顿样本（含句中停顿 vs 真结束），人工标注，比任何公开榜都有意义 |
| **B4** | **把 VAD-only 当正式对照，不是垫脚石** | Scicom 数据显示 VAD-only 的 p90（0.71s）**远好于** SmartTurn（3.04s），误切率在 600ms 预算下也仅差 1.5 倍。**单一已知用户**场景下，调好的静音阈值 + 上下文规则可能就够了，而且零依赖。别默认"学习模型一定赢" |
| **B5** | **验收指标加 p90 / 最差 10%** | 本轮最大教训之一：我们一直看比率和均值，漏了尾巴。**3 秒死寂出现在 10% 的停顿上，体感上比 5% 的误切更糟**。以后转向检测的验收必须写"p90 停顿判定延迟 ≤ X ms" |
| **B6** | **建 claims register（声明登记表）** | 把文档里所有 load-bearing claim 登记成表（claim / 级别 / 来源 / 复核日期），放进 `docs/`。**新 agent 开工先读登记表**，就不会把未核实的东西当事实再扩散一次。本报告 §5 就是它的第一版 |
| **B7** | **制度化 4 条评测规矩** | ①任何"SOTA"必须写清 **bench 名 + 评测方 + 评测方是否即受益方**；②优先能用 ≥2 个独立评测印证的；③**能自己跑的 bench > 只能读的 bench**；④**永远带上"笨办法"对照**（VAD-only、静音阈值）。外加一条元规则：**榜是别人家的数据；自己的耳朵才是终审。** |

---

## 7. 对计划的具体改动

- **P0 新增**：录 100 条自用语音样本（含句中停顿）+ 跑一遍 eot-bench（英文+中文），产出**我们自己的**误切/p90 表。**在写编排薄壳之前先做**——因为端点方案决定主循环结构。
- **P1 插入项**（原 P1 的 Smart Turn 接入改为）：
  1. 候选集 **{VAD-only 调参、SmartTurn v3.2、v1-mini（先确认权重是否开放/尺寸）、VAP、ultraVAD}** 同台 A/B；
  2. 验收口径：**p90 停顿判定延迟** + 误切率 + 中文表现；
  3. 才决定接哪个。
- **P0/P1 的可选加速**：若某候选在 M1 Pro CPU 上不可行，**放 Windows 机经 LAN 提供服务**（B1），RTT ~1ms 可接受。
- **文档维护**：`docs/` 增 claims register（B6），并把 `JARVIS-HANDOVER-20260915.md` §3.3 里"端点"相关表述去 SOTA 化。

---

## 8. 来源

**一手（本轮亲核）**：
- `github.com/livekit/eot-bench`（README：14 语言含 Chinese、模型适配器清单、v1 为云模型需 API key、无权重链接/无尺寸、延迟定义）
- `livekit.com/blog/solving-end-of-turn-detection`（原始榜单与 eot-bench 发布声明）
- `huggingface.co/datasets/livekit/eot-bench-data`（数据集）

**第三方（🟡 交叉验证，均为自评）**：
- `krisp.ai`《A New Approach to Turn-Taking》— Turn Prediction v3 vs SmartTurn v3.2 vs LiveKit vs Flux；含"LiveKit TT 是文本模型"的说法（与 LiveKit 自述矛盾）
- `huggingface.co/Scicom-intl/semantic-vad-eot-whisper-tiny` 与 `-base` — 电话语音复现，**Smart Turn p50/p90 = 0.65/3.04s**、误切 69.6%@300ms（英）

*本文为新增文件。§5 的 claims register 建议正式落到 `docs/CLAIMS-REGISTER.md`。*
