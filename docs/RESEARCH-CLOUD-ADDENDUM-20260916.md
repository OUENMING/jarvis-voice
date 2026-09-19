# 附录 A：云端开放后的方案重估（2026-09-16 00:05 GMT+1）

> **触发**：Owen 拍板「接受低成本的云端方案，隐私不是首要」——推翻原硬约束「本地跑、不付云端 API 费」。
> **前置**：`docs/RESEARCH-FINAL-20260915.md`（级联单脑主线 + P0–P4 计划）。本附录只写**增量**：什么变、什么不变、多少钱。
> **口径**：✅实测　🔗联网调研（2026-09-15/16 检索，官方计费页优先）　⚠️未验证。价格均为刊例价，充值前用最小金额实测。

---

## 0. TL;DR

1. **架构主线不变**：VAD/端点 → ASR → Claude Code（订阅制，无边际成本）→ 流式 TTS。云端开放改变的是**组件选型**，不是骨架。
2. **三档方案**：
   - **Tier 1（推荐 v1）：只上云嘴**——本地 SenseVoice + CC + 云 TTS（MiniMax 海外版或阿里新加坡）。**¥10–37/月**（45 分钟/天档），换克隆 Omen 音色 + 情感参数 + 中文音质全面提升，Windows 整机退役。
   - **Tier 2（体验档，可选实验）：Gemini Live 当语音壳**——原生亚秒打断、自然韵律、服务端 VAD。**¥75–220/月**，是体验上限最高的路；Pipecat 有成熟开源模式；但"逐字朗读注入文本"不保证，须实测。
   - **Tier 3（观察）：GLM-Realtime-Flash**——0.18 元/分但**计费口径至今未定**（官方 usage 字段返回 0，"实际计算规划开发中"）且无国际端点。不押注。
3. **两个诚实的权衡**：① 云 TTS 首音可能比本地 Kokoro **慢 250–350ms**（网络 RTT），换来的是质量/克隆/情感——P0 双轨 A/B 实测定夺；② ASR **建议留本地**（SenseVoice 免费、上云只加延迟加成本、准确率提升有限）。
4. **对计划的改动**：P0 增加"云 TTS 后端抽象 + MiniMax ¥10 实测"；P3 的本地 CosyVoice 3 克隆方案**作废**（云克隆 9.9 元一次性替代）；新增可选的"Tier 2 一周实验"。

---

## 1. 成本模型（月费，30 天，按活跃语音分钟计）

> 用量假设：轻 15 分钟/天、中 45 分钟/天、重 120 分钟/天（"活跃说话"，不含挂机监听——VAD/端点永远本地，免费）。
> TTS 字数 = 时长 × 0.4（播占空比）× 4 字/秒；ASR 小时 = 时长。

| 方案 | 轻 | 中 | 重 | 备注 |
|---|---|---|---|---|
| **Tier 1a：全本地**（Kokoro+SenseVoice） | ¥0 | ¥0 | ¥0 | 基线，音色/情感弱 |
| **Tier 1b：阿里百炼新加坡**（qwen3-tts-flash 0.8 元/万 + qwen3-asr 1.19 元/时） | ~¥12 | ~¥37 | ~¥99 | 最便宜；克隆 0.01 元/音色；国际节点官方支持 |
| **Tier 1c：MiniMax 海外版**（speech-2.8-turbo 2 元/万，只换 TTS 不换 ASR） | ~¥10 | ~¥26 | ~¥69 | 克隆 9.9 元一次性；emotion 参数；Visa 直付无实名；TTFA ~200ms |
| **Tier 2：Gemini Live 付费**（入 $0.005/分 + 出 $0.018/分 ≈ $0.023/双向分） | ~¥75 | ~¥220 | ~¥590 | 原生打断/韵律/服务端 VAD |
| Tier 2 免费档（AI Studio "Free of charge"） | ¥0 | 限额内 | 超限 | Preview 限额未验证；EEA 条款灰色 |
| GLM-Realtime-Flash（0.18 元/分，口径未定） | ¥81? | ¥243? | ¥648? | ⚠️ 按连接时长算则出局 |
| OpenAI Realtime-2.1 / mini | $22–67 / $9–22 | … | … | 比组件云贵 3–10 倍，**排除** |
| 豆包实时（火山） | — | — | — | 需大陆实名 + 无国际实时端点，**排除** |

**锚点结论**：¥10–50/月预算内，**Tier 1 完全可行**（中档用量 ¥10–37）；Tier 2 要放宽到 ¥75–220。120 分钟/天档所有云方案都超 ¥50，届时回退本地。

---

## 2. 调研数据要点

### 2.1 云 TTS（中文，决定音色身份）

| 厂商 | 价格 | 流式首包 | 克隆 | 情感 | 爱尔兰接入 |
|---|---|---|---|---|---|
| **MiniMax speech-2.8-turbo（海外版）** | 2 元/万字符 | ~200ms（⚠️三方实测） | 9.9 元/音色一次性 | emotion 参数（happy/sad/whisper…） | ✅ platform.minimax.io，Visa 直付，有美欧低延迟端点 api-uw |
| **阿里百炼 qwen3-tts-flash**（新加坡区） | 0.8 元/万（realtime 版 1 元） | 官方未公布 ⚠️ | 0.01 元/音色 | instruct 版支持 | ✅ DashScope intl，官方国际部署 |
| 火山 Seed-TTS 2.0 | 3 元/万（资源包 2.4） | 官称 ~600ms（三方 300–400ms） | 复刻音色 138 元/个 + 0.0008 元/字符 | 自然语言指令控情绪（最强） | ❌ 大陆实名 + 大陆节点，**出局** |
| 腾讯云 / 讯飞 | 1.2–3 元/万 / 2 元/万 | 未公布 | 9.9 元/次 / 5 元/次 | 参数级 / 拟人度高 | 腾讯国际站可注册；讯飞需实名 |
| ElevenLabs | ~3.5 元/万 | Flash ~75ms | ✅ | Audio Tags | ✅ 都柏林 50–150ms，但**中文非顶尖** |

### 2.2 云 ASR（结论：**留本地**）

- 阿里 qwen3-asr-flash-realtime 1.19 元/时（新加坡区有货）；腾讯 ~1 元/时；火山 1 元/时（实名出局）；Deepgram Nova-3 中文"过得去非顶尖" 2.2 元/时。
- **裁决**：SenseVoice 本地免费、CER 10%、utterance 级已够用；上云只加 RTT（170–300ms）加成本，准确率收益有限。**除非**实测发现口音/专有名词识别差，再补阿里新加坡区作 fallback。

### 2.3 V2V 实时 API（Tier 2/3）

| API | 价格 | 免费档 | 文本注入朗读 | 爱尔兰接入 |
|---|---|---|---|---|
| **Gemini Live**（3.1-flash-live-preview / 2.5-native-audio） | 入 $0.005/分 + 出 $0.018/分 | AI Studio 标 "Free of charge"（限额未验证 ⚠️） | ✅ 官方 `send_realtime_input(text)` + AUDIO 出——**但逐字性不保证，需 instructions 强约束 + 实测** | ✅ |
| OpenAI Realtime-2.1 / mini | $0.05–0.15 / $0.02–0.05 每分钟 | ❌ 无免费分钟 | ✅ `conversation.item.create`（async function calling 可边说边等工具） | ✅ 但贵 3–10 倍 |
| **GLM-Realtime-Flash** | 0.18 元/分 | 未列 | ✅ `ConversationItemCreate`（官方注明可传 function call 结果文本） | ❌ 无国际端点（z.ai 未上架）；**计费口径官方未写明，usage 字段"暂时都返回 0"** ⚠️ |
| Qwen3.5-Omni-Flash-Realtime（新加坡） | ≈$0.034/分（自算 ⚠️）；1M token 免费额度 | 90 天内 | ⚠️ 能力表**不支持 Function Calling**，verbatim 未验证 | ✅ DashScope intl |
| 豆包实时 | ≈¥0.57/分 | — | — | ❌ 实名 + 无国际端点 |

**可组合性判定**：「语音壳包外部文本脑」（音频入/文本入/语音出）在 Gemini Live / OpenAI / GLM 三家**官方支持**此会话形态，Pipecat 有成熟开源实现；**唯一风险点是逐字朗读**——注入文本会被当对话内容再加工。**这是 Tier 2 必须先实测的第一件事。**

---

## 3. 修订后的推荐架构

### v1 主线（Tier 1，预算 ¥10–37/月）

```
[Mac M1 Pro 单机]
  麦克风 → AEC → Silero VAD → Smart Turn v3 → SenseVoice（本地，免费）
        → backchannel 过滤 → Claude Code 常驻 stream-json（订阅制，不变）
        → 聚句 → TTS 后端抽象层 ┬ Kokoro v1.1-zh（本地，免费，首音快）
                               └ MiniMax turbo / 阿里 qwen3-tts（云，克隆+情感+音质）
  打断：VAD → tts.stop() + abort CC turn + 继续听（云 TTS 同样适用：断流即停）
  填充音：本地预制（免费）
```

- **TTS 双轨**是 P0 的正式任务：抽象成 10 行的接口，本地/云两个后端，A/B 实测**首音延迟 + 音质 + 长句稳定性**后定默认档。
- **Windows 彻底退役**（原 P3 的 CosyVoice 3 本地克隆方案作废）——双栈/19060/防火墙/重启循环那一摊事全部终结。
- 云 TTS 的网络抖动进音频链 → jitter buffer 加到 ~400ms（云档专属参数）。

### Tier 2 体验档（可选实验，预算放宽时）

Gemini Live 当语音壳 + CC 当脑（Pipecat 模式）：原生亚秒打断、服务端 VAD、自然韵律。
**实验前置三问**（不通过就不上）：① 注入 CC 结果能否逐字朗读（instructions 强约束下实测 20 条）② 免费档限额够不够一周实验 ③ EEA 条款下绑卡走付费是否顺畅。
**定位**：不是替代 Tier 1，是"体验上限探路"。若逐字朗读不稳，Tier 2 出局，主线仍是 Tier 1。

---

## 4. 对 P0–P4 计划的修订（delta）

| 阶段 | 原计划 | 修订 |
|---|---|---|
| **P0**（1–2 天） | Kokoro 单轨 | **+ TTS 后端抽象层（本地/云双轨）**；+ MiniMax 充 ¥10 实测（首音/音质/克隆流程）；其余不变（0.1 先测 CC warm TTFT 仍是第一件事） |
| **P1**（2–3 天） | 打断/AEC/Smart Turn | 不变。云 TTS 下打断 = 断流 + 弃缓冲，逻辑相同 |
| **P2**（1 周） | 会话滚动/情感标签/投机启动 | 情感标签注入保留；**+（可选）Tier 2 Gemini Live 一周实验**（前置三问通过才做） |
| **P3** | CosyVoice 3 本地克隆 @ 5060 Ti | **作废**。改为：云克隆 Omen 音色（MiniMax 9.9 元一次性 / 阿里 0.01 元）+ emotion 参数接情感回路（EMA 平滑照旧）。**Windows 退役** |
| **P4** | 视觉/本体 | 视觉节点改为"云或本地皆可"（MiniCPM-V 本地 or VLM API，届时再选）；全双工备胎观察清单不变 |

**新增持续项**：盯 GLM-Realtime 计费口径官宣（usage 字段实装之日重估）；盯 MiniMax/阿里价格与免费额度变动。

---

## 5. 需 Owen 拍板的三个决策点

1. **TTS 默认档**：本地 Kokoro（免费、首音快、音色一般）vs 云 MiniMax（¥10–30/月、克隆+情感+音质、首音慢 ~300ms）→ **建议 P0 双轨 A/B 后用耳朵定**。
2. **是否跑 Tier 2 实验**（Gemini Live 语音壳，一周，先免费档探限额，通过三问再绑卡 ~¥75/月）→ 体验上限最高但逐字朗读是未知数。
3. **ASR 留本地**的裁决是否接受（省 ¥12–99/月，代价是口音/专名识别上限）→ 若接受，P0 不加云 ASR。

---

## 6. 来源（🔗 2026-09-15/16 检索）

**官方计费页**：platform.minimaxi.com（MiniMax 国内）、platform.minimax.io（海外）、help.aliyun.com/zh/model-studio（百炼，含 qwen3-tts/asr 与国际部署价）、volcengine.com/docs/82379/2516288（方舟）、docs/6561/1585106（豆包语音）、cloud.tencent.com/document/product/647/111976、xfyun.cn、openai.com/api/pricing、docs.bigmodel.cn（GLM，含 usage 字段"返回 0"注记）、docs.z.ai/guides/overview/pricing、alibabacloud.com help（qwen3-omni-flash-realtime intl）
**三方/聚合（打折看待）**：new.qq.com MiniMax TTFA 实测（2026-04）、apirank.vip、rundown.ai / laozhang.ai Gemini 转述、forasoft.com OpenAI 每分钟换算、developer.volcengine.com Seed-TTS 首包实测（2026-05）
**未验证清单**：各厂首包延迟的都柏林侧实测、Gemini 免费档 RPD 限额、Gemini/OpenAI/GLM 注入文本的逐字朗读保真度、GLM 计费口径、MiniMax 海外版从都柏林的实际 RTT——**全部列入 P0/实验的实测清单**。

*本附录为新增文件，未改动 RESEARCH-FINAL-20260915.md 原文（其 §7 决策备忘中"云端出局"一条由本附录修订）。*
