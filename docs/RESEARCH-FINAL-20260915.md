# 终版调研报告：语音贾维斯的成熟方案 × 前沿研究（2026-09-15）

> **作者**：WorkBuddy（Mac 侧）
> **性质**：收官级综合调研——社区论坛 + 文献 + 前沿架构三路并行深挖后的头脑风暴综合，匹配 Owen 的需求与硬约束，给出推荐架构与分阶段细致计划。
> **口径**：✅=本项目实测/代码核实　🔗=联网调研（2026-09-15 检索，附来源）　⚠️=未验证/厂商数字/推断。
> **前置结论**（`docs/EVAL-SINGLE-BRAIN-20260915.md`）：双脑（MiniCPM-o 当耳嘴）五症状中三个是模型代际级死结；单脑级联可行。本报告在此之上做**终版细化**。
> **重要提醒**：本报告中 🔗 条目多为 agent 联网检索的**二手结论**。按项目规矩，**动手依赖任何具体组件/数字前，先亲自复核一手来源**（repo、arXiv、官方文档）。

---

## 0. TL;DR

1. **路线裁决不变且被加强**：级联单脑（VAD→ASR→Claude Code→流式 TTS）是正确主线。新证据：VoiceAgentBench 实测**工具调用上级联 > 语音大模型**（arXiv 2510.07978）；行业 V2V 延迟中位 1.4–1.7s，级联调好可进 0.7–1.1s；连 MiniCPM-o 4.5 自己的 TAIL 架构都是"LLM 产文本 + 小语音解码器发声"——**单脑模式被端到端厂商自己验证了**。
2. **三个改变细节判断的新发现**：① **Smart Turn v3** 语义端点（8MB ONNX、中文、CPU 12ms、BSD-2 全开源）应替代纯静音阈值；② **Kokoro v1.1-zh 在 M1 Pro 纯 CPU 上 RTF 0.88、首分片 <100ms**（100 款中文音色），但**必须用官方 KPipeline `lang_code='z'`**——第三方 ONNX 客户端有吞声调坑；③ **NVIDIA PersonaPlex 7B**（Moshi 架构全双工、开放权重、240ms 打断延迟）动摇了"16GB 内无真全双工替代"的结论——值得盯，但不做主线（同样不能朗读脚本、工具调用弱）。
3. **头脑风暴 TOP 改进**：投机打断框架（EagerEOT + abort）、小模型 filler 快路（有先例）、Qwen3-TTS/CosyVoice 3 克隆统一音色（embedding 缓存后零额外延迟）、claude-mem 记忆（已装，零成本）、SenseVoice 情感标签旁路（情感层不走主链）。
4. **计划**：P0 本周 Mac 单机薄闭环（1–2 天）→ P1 打断与对话纪律（2–3 天）→ P2 会话与感知 → P3 声音身份与情感层 → P4 视觉与本体。每阶段有验收标准和回退点。

---

## 1. 需求 × 约束匹配表

| Owen 的需求/约束 | 调研结论如何匹配 |
|---|---|
| 自然语音对话（像正常聊天） | 级联 + 语义端点 + 投机打断，2026 生产实践可达 0.7–1.1s 端到端（🔗 techsy.io/builderai.tools，厂商口径打折）；本地 0.8–2.2s（🔗 specpicks 聚合，⚠️未独立验证） |
| 能干活（派任务、联网、读写） | Claude Code + MCP，**级联在工具调用上明确强于语音大模型**（🔗 VoiceAgentBench arXiv 2510.07978） |
| 说得出、播报忠实 | 文本脑 + TTS 逐字朗读，结构性保证；E2E 模型"不会朗读"是公认限制（LiveKit 官方文档 + 本项目实测） |
| 体验底线：能打断 | barge-in = `tts.stop() + abort LLM`，目标 ~300ms 静默（🔗 forasoft.com 2026）；AEC 兜底防回声自激 |
| 体验底线：不自言自语 | 级联构造上不可能（无空转 decode） |
| 体验底线：被叫到会回应 | VAD 常开 + 端点确认即触发；无"KV 污染失聪" |
| 音色统一（闲聊+播报同一声音） | Kokoro 固定中文音色起步；P3 用 CosyVoice 3 / Qwen3-TTS 1.7B 克隆 Omen 音色（embedding 缓存一次，推理期零额外成本 🔗 blackglory.me） |
| 情绪识别（后期） | SenseVoice 自带 6 类情感标签 ✅已在手；走旁路注入 prompt，不进主链 |
| 视觉（后期） | MiniCPM-V 4.5 按需节点抽帧描述注入（方案池②设计）；级联组件可替换性对机器人本体最友好（⚠️推断） |
| 本地、不付云端 API 费 | 云端持续降价但仍不够：GPT-Live-1 $0.05/min 常开 ≈ $72/天（🔗 openai pricing 2026-09）→ **本地路线维持**。Claude Code 为订阅制固定成本，不产生按分钟语音费 |
| 硬件 5060 Ti 16GB + M1 Pro 16GB | 足够跑本报告全部推荐组件；若未来要本地脑/全双工备胎，二手 4090 ~$1000 是最高性价比解锁（🔗 gpunex.com 2026） |

---

## 2. 三条技术路线的 2026 版图（🔗 文献+社区综合）

| 路线 | 代表 | 成熟度 | 对本项目 |
|---|---|---|---|
| **端到端全双工**（听+说一体） | MiniCPM-o 4.5（11GB INT4，唯一 16GB 可跑的真双工开源，活跃维护）；NVIDIA PersonaPlex 7B（Moshi 架构，240ms 打断，开放权重）；Moshi/Hibiki（停滞） | 体验上限高（200–500ms），但**不能朗读脚本、工具调用弱、推理能力灾难性遗忘**（URO-Bench arXiv 2502.17810） | ❌ 主线出局（已实测一个月）；PersonaPlex/下一代 MiniCPM-o 进**观察清单** |
| **纯级联**（ASR→LLM→TTS） | 生产主流：Deepgram+GPT-4o-mini+Cartesia；DIY：Whisper+Ollama+Piper/Kokoro | 最成熟，工具调用最强；输在延迟和副语言 | ✅ **本项目主线**（脑换 Claude Code） |
| **半级联/hybrid**（音频原生输入→文本推理→TTS；或小模型管轮次+大脑管任务） | OpenAI/Google 消费级产品的实际形态；"Hybrid Supervisor"（OpenAI 官方推荐变体）；Claudia Voice（本地 30B MoE 快路 + Claude 深路） | 2026 前沿方向，工程复杂度换体验 | 🟡 **演进方向**：P2 的 filler 快路 + P3 的情感旁路就是在向这个形态走 |

**关键判断**：级联输给 E2E 的是延迟和副语言，**不是智商**；而我们最痛的五个症状（不播报/卡顿/丢字/自言自语/无法打断）在级联里要么消失、要么是确定性工程问题。**用 0.5–1s 的延迟换"每句话可信、随时可打断、永不自言自语"，值。**

---

## 3. 能力维度调研要点（每个维度一句话裁决）

### 3.1 端点检测 / 轮流发言
- **裁决：Silero VAD（一级）+ Smart Turn v3（语义二级）**。Smart Turn v3 = Whisper-Tiny 骨干、8MB int8、23 语言含中文、CPU 12–60ms、BSD-2 全开源（权重+数据+训练脚本，可自微调）（🔗 daily.co 博客 2025-09-11）。TEN-VAD/TEN Turn Detection 的 98.9% 中文是厂商数字，⚠️ 无第三方验证，不做首选。
- 前沿：VAP（预测未来 2s 语音活动，多语言含普通话不掉点）、LiveKit turn detector（Qwen2.5-0.5B 蒸馏，ONNX <500MB RAM）——备选观察。

### 3.2 ASR（中文）
- **裁决：SenseVoice-Small 继续用**（✅ 已在跑）。M5 实测 CER 10.11%/57.8×实时（🔗 53ai.com 2026-07）；非原生流式，工程上用"VAD 缓冲 + 每 ~200ms 重跑 partial"。
- 升级路径：长音频稳定性优先换 Fun-ASR-Nano（CER 8.59%/14×，长音频最稳 🔗 suyiiyii.com 2026）；准度优先换 Qwen3-ASR-0.6B（CER 7.65% 但仅 10.5×，P2 再评）。
- 附赠：SenseVoice 输出 6 类情感 + 8 类声学事件标签 → 情感层的免费输入。

### 3.3 TTS（中文，决定音色身份）
- **P0/P1：Kokoro v1.1-zh @ Mac CPU**。M1 Pro 实测 RTF 0.88、首分片 <100ms、内存 200MB、100 款中文音色、Apache-2.0（🔗 唐人专栏 2026 实测，单点）。**🔴 关键坑：必须用官方 KPipeline `lang_code='z'`——第三方 ONNX 客户端吞声调变"外星语"；长句前端分句。中文质量社区评价两极（MOS 3.9 vs 够用）→ 起步可接受，P3 升级。**
- **P3：CosyVoice 3 @ 5060 Ti**。0.5B Apache、双向流式首包 ~150ms（厂商值）、instruct 情感控制、零样本克隆（中文综合最强横评 CER 0.28 🔗 shengwang.cn 2026-05）。克隆 Omen 音色统一全部输出。
- 备选：Qwen3-TTS 1.7B（克隆 SOTA、3 秒样本、~97ms 首音）——⚠️ 注意**克隆模式与情感指令互斥**（🔗 neosophie.com 2026-03 实测）；Index-TTS2 情感控制最强但 B 站许可证，个人可用、商用需授权；GPT-SoVITS v4 MIT。
- ❌ 已判死：Qwen3-TTS 0.6B 在 Windows 上 RTF 4.6–8.3（✅ 本项目实测；注：这是 0.6B 在 WDDM 下的个例，不代表 1.7B）。

### 3.4 大脑
- **裁决：Claude Code 常驻 stream-json 不变**（✅ ClaudeBridge 已在跑）。社区已有多个同类项目（voice-cc 的 Stop hook + `<speak>` 标记模式可直接抄 🔗 github.com/LukeSmith25/voice-cc）。
- 🔴 **最大未知数：haiku/sonnet 在常驻 stream-json 会话的 warm TTFT 无公开数据 → P0 第一件事自己量**（工具链 schema + 系统提示有固定 token 开销 ⚠️推断）。
- 会话纪律：`--verbose` 必须；`/clear` 清空对话但保留 MCP/CLAUDE.md；崩溃用 `--resume session_id`（🔗 agentdm.ai / cola.dany 2026）。
- 备胎（若订阅/合规有变）：5060 Ti 16GB 甜点是 **Qwen3-14B Q4（~30+ t/s）或 gpt-oss-20b MoE（~49 t/s）**；**别碰 dense 32B**（强制 offload 仅 5–10 t/s 不可用 🔗 多源实测）。工具链质量逊 Claude（⚠️ vibes 无基准）。

### 3.5 打断 / AEC（成败点，不是模型）
- **裁决：三级方案**。① 首选 WebRTC AEC（`webrtc-audio-processing`，py-xiaozhi 在 macOS arm64 验证过 🔗）；② macOS VoiceProcessingIO 省事但坑多（输出静默变 9 通道、系统自动 duck、文档近零 🔗 dev.to/thehwang 2026）；③ **兜底必须有：TTS 播放期软件门控 VAD 灵敏度 + 打断冷却**（LiveKit 模式：`min_words≥2`、`min_duration 0.8–1.0s`、`false_interruption_timeout` 🔗 zian.ai 源码整理 2026-09-14）。
- WebRTC AEC3 硬限：回声延迟 >~500ms 失效（🔗 LiveKit 社区 2026-08）——参考信号与麦克风必须样本级对齐。
- 打断目标：用户开口 → agent 静默 ≤300ms；**必须同时取消 LLM 在途生成**（🔗 forasoft.com 2026）。

### 3.6 记忆 / 人格
- **裁决：CLAUDE.md（人设）+ claude-mem（✅ 已装，hook 自动记录）起步**。单用户助理场景，社区经验是这个组合往往已够，mem0/Letta 属过度工程（⚠️ vibes）；mem0 有"记忆污染"问题（旧事实误导）。现有 `jarvis_state.py` 的 state.json 机制可并入。

### 3.7 延迟基准（对照表，🔗）
| 形态 | 端到端 |
|---|---|
| 人类期望 | <1.5s |
| 行业 V2V 中位 | 1.4–1.7s |
| 云端调优级联 | 0.7–1.1s |
| 本地（3060 级 DIY） | 0.8–2.2s（⚠️聚合数字） |
| 本项目单脑预算（P0 实测前为估算） | **p50 ~1.7–2.6s，加填充音感知 <500ms** |
| 成本中心排序 | CC 首 token > 语义端点 > ASR > TTS |

---

## 4. 头脑风暴改进池（成熟方案 × 前沿研究，12 项）

| # | 改进 | 来源/先例 | 价值 | 成本 | 阶段 |
|---|---|---|---|---|---|
| 1 | **Smart Turn v3 语义端点** | daily.co，BSD-2 全开源 | 区分"思考停顿 vs 说完"，端点改进的体感收益 > 压缩组件延迟（行业共识 🔗） | 低（8MB CPU 12ms） | **P1** |
| 2 | **打断纪律参数化**（min_words≥2 / min_duration 0.8s / 冷却 / backchannel 边界） | LiveKit 1.8.x 生产参数 🔗 | 治"嗯一声就误打断"和"喊不停"两头 | 低 | **P1** |
| 3 | **投机打断框架**：EagerEOT 提前启动 CC turn，TurnResumed 则 abort | Deepgram Flux 参考实现 🔗 devsatva.com 2026 | 级联体验逼近全双工的关键技巧 | 中 | **P2** |
| 4 | **填充音/承接快路**：本地预制 ack（"嗯，我想想"）或 Qwen3-0.6B 小模型即时承接 | Claudia Voice 双 LLM 路由 🔗 benzanghi.com；企业 backchannel <100ms 小通道是 2026 标准 | 感知延迟 <500ms，治"CC 首 token 1–1.5s"的空窗 | 低（预制音）/ 中（小模型） | **P1（预制音）→ P2（小模型，可选）** |
| 5 | **统一声音身份**：CosyVoice 3 克隆 Omen 音色，embedding 缓存一次 | 克隆推理期与预设音色成本几乎相同 🔗 blackglory.me 2026 | 闲聊/播报/情感全链路同一声音；Omen Alpha 人设完整化 | 中（部署 5060 Ti） | **P3** |
| 6 | **情感旁路**：SenseVoice 情感标签 → prompt 风格调制 → CosyVoice instruct 情感 TTS | 组件全成熟；完整闭环无知名开源参考（空白=机会） | Phase ③ 情感层落地 | 中 | **P2（标签注入）→ P3（闭环）** |
| 7 | **EMA 情感惯性**（μ≈0.8） | 文献调研确认**无专门论文，研究空白**——自行实现即可，别等学界 | 情绪不跳变，像人 | 低 | **P3** |
| 8 | **claude-mem 记忆层** | ✅ 本机已装（87k star 级）；CLAUDE.md + 少量文件对单用户已够 | 跨会话记住 Owen 的事 | 零 | **P1** |
| 9 | **Kokoro 官方 KPipeline `lang_code='z'`** | 第三方 ONNX 吞声调坑 🔗 唐人实测 | 中文声调正确的硬前提 | 零 | **P0** |
| 10 | **会话滚动**：`/clear` 节奏 + 实测 token 增长曲线 | stream-json 社区模式 🔗 agentdm.ai 2026 | 防长会话首 token 退化 | 低 | **P2** |
| 11 | **音频感知旁路（远期）**：Ultravox 式音频直喂理解模型做副语言/情绪，级联负责说 | Ultravox v0.7 先例 🔗；混合式无成熟开源，**可探索空白** | 情感/副语言上限 | 高 | **P4（探索）** |
| 12 | **全双工备胎**：PersonaPlex 7B / 下一代 MiniCPM-o / Qwen3.5-Omni-Light 权重 | 🔗 2026 路线图信号 | 离网模式/未来换脑选项 | 观察即可 | **观察清单** |

**硬件可选项**：二手 4090 ~$1000（24GB）可解锁 27B 本地脑或 7–11B 全双工备胎——若哪天真要"全本地、零订阅"，这是最划算的一步（🔗 gpunex.com 2026）。当前不必要。

---

## 5. 推荐架构 v1（定稿）

```
┌──────────────────── Mac M1 Pro（前哨 + 脑 + 嘴，单机闭环） ────────────────────┐
│  麦克风                                                                        │
│    → WebRTC AEC（P1；兜底=播放期 VAD 门控）                                     │
│    → Silero VAD（一级，<1ms/帧）                                                │
│    → Smart Turn v3 语义端点（P1 接入；P0 先用 800ms 静音阈值）                    │
│    → SenseVoice-Small ASR（sherpa-onnx，VAD 缓冲+200ms partial；情感标签旁路）    │
│    → backchannel/应答词过滤（"嗯/对/哈哈" <6 字不进脑）                           │
│                                                                                │
│  脑：Claude Code 常驻 stream-json（claude_bridge.py ✅ 已有）                    │
│    · persona = CLAUDE.md（口语化、≤2 句、无 markdown、Omen Alpha）                │
│    · Tavily MCP 联网（mcp-search.json ✅）+ claude-mem 记忆（✅ 已装）            │
│    · /clear 会话滚动（P2）+ --resume 抗崩溃                                     │
│                                                                                │
│  嘴：partial tokens → 聚句器（SENT_END ✅ 已有）→ Kokoro v1.1-zh                 │
│    （官方 KPipeline lang_code='z'，固定中文音色，分句流式，首分块 <100ms）          │
│    → 流式播放（DuplexPlayer 骨架复用）                                            │
│                                                                                │
│  打断：VAD 命中（播放中走 AEC 后信号）→ tts.stop() + abort/kill CC turn + 继续听  │
│  填充音：端点确认即播本地预制 ack（感知 <500ms）                                  │
└────────────────────────────────────────────────────────────────────────────────┘
         （P3 起）5060 Ti 转按需节点：CosyVoice 3 克隆音色 / MiniCPM-V 视觉 / 情感 TTS
```

**为什么是它**：工具调用最强（VoiceAgentBench）、播报逐字可信、构造上无自言自语、打断确定性可达、80% 组件本项目已实测（SenseVoice/ClaudeBridge/Kokoro/MCP/claude-mem）、每台设备都有明确升级路径且互不阻塞。

---

## 6. 细致计划（P0 → P4）

> 每条标工作量（Owen + AI 协作口径）与**验收标准**（不写"应该没问题"）。任何阶段不达标 → 回退点明确。

### P0 · Mac 单机薄闭环（1–2 天）—— 先能聊、先能播

| 任务 | 细节 | 验收 |
|---|---|---|
| 0.1 **先测量** | 用现成 `claude_bridge.py` 冒烟：haiku vs sonnet 的 warm TTFT、整轮耗时、token 成本，连测 20 轮记录原始数据 | 数据落盘 `docs/bench/`；❗若 warm TTFT >2.5s → 触发降级讨论（模型选择/filler 提前） |
| 0.2 VAD | sherpa-onnx Silero（项目依赖里已有 sherpa-onnx 体系） | 说话/静默分段正确，CPU 占用可忽略 |
| 0.3 ASR 复用 | SenseVoice 现有管线 + VAD 缓冲；端点先用 800ms 静音阈值 | 整句识别正确率与现有一致 |
| 0.4 TTS | kokoro **官方 KPipeline `lang_code='z'`**，选定 1–2 款中文音色试听；分句流式 | 声调正确（对照"外星语"坑）；首分块 <150ms 实测 |
| 0.5 编排薄壳 | 新文件（如 `jarvis_voice_only.py`）：mic→VAD→ASR→ClaudeBridge→聚句→TTS→播放；**不动 `jarvis_omni.py`**；改前 `.bak` 惯例 | — |
| 0.6 CLAUDE.md 人设 | 口语化/≤2 句/无 markdown/Omen Alpha；参考 voice-cc 的 `<speak>` 标记模式 | 闲聊 10 轮语气合格 |

**P0 验收三项**：① 派活（"帮我查都柏林明天天气"）→ 播报**逐字**、声调正常 ② 首音延迟实测记录（目标 p50 <3s，记录分布）③ 静置 5 分钟**零自言自语**（构造保证，但要实测证明）。
**不做**：打断、AEC（P0 用"说话期间不播、播期间不听"的半双工规避）。
**回退点**：整条链任一组件不达标 → 单组件替换（ASR 换 Fun-ASR-Nano / TTS 换 MeloTTS-ZH），不动架构。

### P1 · 打断与对话纪律（2–3 天）—— 像正常聊天

| 任务 | 细节 | 验收 |
|---|---|---|
| 1.1 **interrupt 验证** | stream-json 能否中断在途 turn（发 control/abort）；不行则 kill 子进程 + `--resume`（bridge 已有重启逻辑，代价 ~1s） | 中断后 agent 静默 ≤500ms（目标 300ms）；**此条是整个 P1 的钥匙，先做** |
| 1.2 AEC | 先试 `webrtc-audio-processing`；不行再 VoiceProcessingIO（注意 9 通道/ducking 坑）；**兜底：播放期 VAD 门控** | 播报中麦克风收到的用户语音可辨识（录诊断样本） |
| 1.3 barge-in | VAD 命中 → `tts.stop() + abort + 继续听`；纪律参数：`min_words≥2`、`min_duration 0.8s`、冷却窗 | 三场景实测：播报中/思考中/静默中被叫，全部 ≤1s 有反应 |
| 1.4 Smart Turn v3 | 替换 800ms 静音阈值；中文实测 | "思考停顿 vs 说完"区分合格；误截率 <10% |
| 1.5 backchannel 过滤 | 方案池五件套①：短承接词不进脑、不打断 | "嗯/对/哈哈"不触发任务、不打断播报 |
| 1.6 填充音 | 端点确认即播预制 ack 音频（3–5 条轮换） | 感知延迟（停口→第一声）<500ms |
| 1.7 claude-mem | 接入常驻会话，验证跨会话记忆 | 隔天问"我上次说的那个项目"能接上 |

**P1 验收三项（对齐 Owen 的体验底线）**：① 说话能打断 ② 静置 30s 再说话仍有反应 ③ 连续 30 分钟无自言自语、无误打断。
**回退点**：AEC 不达标 → 长期用"播放期门控"兜底（体验降半档但可用）；interrupt 不支持 → 永久 kill+resume（代价已知）。

### P2 · 会话与感知（1 周，可与 P1 部分并行）

- 2.1 会话滚动：实测 token 增长曲线 → 定 `/clear` 节奏（如每 50 轮或 token 阈值）；验证人设无损恢复。验收：30 分钟长会话首 token 退化 <30%。
- 2.2 情感旁路（上）：SenseVoice 情感标签 → prompt 注入（"主人现在听起来有点累"）。验收：故意换语气，应答风格可感知变化。
- 2.3 投机启动 EagerEOT（改进池 #3）。验收：简单问答端到端 -300ms 以上。
- 2.4（可选，若 P0 实测 p50 >2.5s 则提前）Qwen3-0.6B filler 快路。验收：感知延迟 <500ms 且不错答。
- 2.5 state.json 与 claude-mem 合并；`snapshot_for_prompt()` 迁移。

### P3 · 声音身份与情感层（中期，1–2 周）

- 3.1 CosyVoice 3 部署 5060 Ti：克隆 Omen 音色（用现有 `assets/omen_ref_16k.wav` 重制样本），embedding 缓存；双机拓扑定型（Mac 音频 I/O ↔ Win TTS，注意 LAN 抖动进音频链 → 加大 jitter buffer）。
- 3.2 情感闭环：标签 → EMA 平滑（μ≈0.8）→ instruct 情感 TTS（改进池 #6/#7）。
- 3.3 全线换克隆音色后回归验收 P0/P1 全部三项。
- **决策点**：若 CosyVoice 3 双机链路不稳 → 退回 Mac Kokoro 固定音色（体验降档但链路单机化）。

### P4 · 视觉与本体（远期）

- 4.1 MiniCPM-V 4.5 视觉节点：抽帧描述注入文本流（方案池②）。
- 4.2 全双工备胎评估：PersonaPlex 7B / 下一代 MiniCPM-o / Qwen3.5-Omni-Light 权重（若发布）→ 仅作"离网模式"候选。
- 4.3 机器人本体：级联组件按需替换/降级（Jetson Orin 级边缘算力 ⚠️推断可扛 VAD+ASR+TTS 前哨）；音频感知旁路（改进池 #11）探索。

### 持续项（不占阶段）

- 盯：上游 #114 回应；MiniCPM-o 5.0 / PersonaPlex 更新；Qwen3.5-Omni-Light 权重；云端价格（$0.05/min 仍太贵，降一个数量级再重估）。
- 每次动手前：对要依赖的组件/数字**亲自复核一手来源**（本报告 🔗 条目均为二手检索）。
- 每次改代码：`.bak-<主题>` + 一次一变量 + 真机验证记原始输出（项目规矩）。

---

## 7. 决策备忘（为什么不选 X）

| 不选 | 理由 |
|---|---|
| 继续 MiniCPM-o 双脑 | 三症状（不播报/自言自语/打断）是模型代际级，上游"下一代才修"且无时间表；维护者重心已转 VoxCPM2 |
| PersonaPlex 7B / Moshi 类全双工做脑 | 同样不能朗读脚本；工具调用/推理被级联碾压（VoiceAgentBench/URO-Bench）；7B 智商天花板。→ 备胎观察 |
| Step-Audio 2 mini（语音原生 tool-calling） | 半双工；工具生态远逊 Claude Code+MCP；值得盯不值得换 |
| 云端（GPT-Live-1 $0.05/min） | 常开 ≈ $72/天，违背立项初衷；订阅制 Claude Code 不产生边际语音费 |
| mem0/Letta 记忆 | 单用户过度工程；claude-mem 已装；mem0 有记忆污染问题 |
| Kokoro 长期方案 | 不能克隆、情感弱、中文评价两极 → 起步用，P3 换 CosyVoice 3 |
| dense 32B 本地脑（若弃 CC） | 16GB 放不下（offload 5–10 t/s 不可用）；甜点是 14B/MoE |

---

## 8. 主要来源（🔗 均为 2026-09-15 检索）

**基准/文献**：VoiceAgentBench (arXiv 2510.07978)、URO-Bench (2502.17810)、FullDuplexBench (2503.04721)、MTR-DuplexBench (2511.10262)、VoiceBench (2410.17196)、MiniCPM-o 4.5 技术报告 (arXiv 2604.27393)、mem0 (2504.19413)、emotion2vec (2312.15185)
**组件**：daily.co Smart Turn v3 博客 (2025-09-11)、ten-vad GitHub、FunAudioLLM/CosyVoice GitHub、neosophie.com TTS 实测 (2026-03)、唐人专栏 Kokoro v1.1-zh 实测 (2026)、53ai.com ASR 横评 (2026-07-27)、suyiiyii.com 长音频实测
**实践**：github.com/LukeSmith25/voice-cc、agentdm.ai stream-json 指南 (2026)、zian.ai LiveKit 源码整理 (2026-09-14)、forasoft.com barge-in (2026)、dev.to/thehwang VoiceProcessingIO (2026)、benzanghi.com Claudia Voice
**前瞻**：openai.com pricing (2026-09)、project-delphi voice-ai-architectures-2026、hub.baai.ac.cn (2026-04/05)、gpunex.com GPU 价格 (2026)
**厂商数字（需打折）**：TEN 98.9%、CosyVoice 3 首包 150ms、Qwen3-TTS 97ms、"<15% S2S 采用率"

*本报告为新增文件。动手前的复核清单见 §6 各阶段"先做"项。*
