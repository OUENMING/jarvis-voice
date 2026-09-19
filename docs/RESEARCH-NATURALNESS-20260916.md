# 自然度 & "要不要插本地模型" 调研（2026-09-16）

> **触发**：Owen 两问——① 播报本质是文字转语音，语气情感会不会欠缺？"像人一样对话"现阶段能实现吗？② 有没有必要插一个本地模型来处理输入侧错误（自我纠正）与输出侧口语化？
> **方法**：三路并行调研（级联自然度天花板 / 输入侧纠错 / 输出侧改写），**加一项本机一手实验**（同题两套 system prompt 的 A/B）。
> **口径**：🟢一手核实（直读论文/官方文档/亲测）　🟡单源二手　🔴未验证/存疑。**本报告的结论是"不要插模型"，且证据方向是明确否定，不是权衡。**

---

## 0. 三句话结论

1. **声音会自然，对话只能到约七成。** 声学拟人度早已达标，不是瓶颈；缺的是**重叠说话、副语言交换、轮次协商**——要换架构才能补，而那会牺牲"忠实念稿"，正是我们否掉端到端模型的理由。**取舍没变，只是现在有证据了。**
2. **先天缺陷不在"TTS 念得没感情"，而在"它听不见你的感情"。** 真正丢信息的是 **ASR 那一步**：文字转写抹掉语气、犹豫、讽刺、笑声。
3. **输入侧与输出侧都不该插模型。** 输入侧有**明确否定的一手证据**；输出侧与项目第一硬需求（逐字忠实朗读）**直接冲突**，且我已实测证明 **prompt 层就够用**。

---

## 1. 必须拆成两个问题，否则结论必错

几乎所有争议都源于把这两件事混为一谈：

| | 2026 年能否达到 | 依据 |
|---|---|---|
| **"声音像人"**（声学拟人度） | ✅ **达标，不是瓶颈** | 当代神经 TTS 已过这条线 🟡 |
| **"对话像人"**（交互拟人度） | ⚠️ **约七成** | 缺重叠说话/副语言/轮次协商 |

---

## 2. 🔴 先天缺陷的真实位置

**不是输出端缺情感，是输入端丢了副语言。**

- 🟢 SenseVoice 确实输出 HAPPY/ANGRY 等标签（FunASR 官方博客），但**文字转写把语调、迟疑、讥讽、笑声全抹掉了**。
- 🟢 Inworld（2026-07）原话：*"The model perceives what a transcript destroys: tone, hesitation, sarcasm, laughter…"*
- ⚠️ 且 **SenseVoice 官方博客只教解析标签，没有任何"标签→TTS 情绪参数"的闭环示例**。

→ **代价是结构性的，换来的是"能忠实念稿 + 能可靠干活"。** 这是清醒取舍，不是缺陷。

---

## 3. 情感控制：句级够用，词级没有

| 层 | 级联能达到 | 依据 |
|---|---|---|
| **句级**情感/风格 | ✅ **成熟可用** | Azure styles / 阿里 instruct / Fish bracket（🟢 各家官方文档） |
| **词级**（句中情绪转折、重音） | ❌ **做不到** | 🟢 arXiv 2509.24629：「most existing research remains limited to utterance-level… fails to support word-level control」 |
| 词级控制的首个研究框架 | 🟡 2025-09 才出现（WeSCon） | 同上 |

**关于"情感旁路闭环"是空白区的说法**：基本成立，但需修正——存在对口期刊论文（An 等，Pattern Recognition 2026，「情感解析→参数映射→情感 TTS」三段闭环）🟡，**但无开源实现**。→ **这是能做研究工程的地方，不是能接现成 API 的地方。**

---

## 4. "像人一样对话"最致命的失败模式（按证据排序）

| # | 失败模式 | 证据 |
|---|---|---|
| **1** | **延迟 > 4 秒** | 🟢 arXiv 2507.22352 (CUI'25) 实测：延迟超 4s 显著劣化体验；**且"自然填充词"能改善*感知*响应时间** |
| 2 | 念书面语 | 提示词问题，可修（见 §6 一手实验） |
| 3 | 单调语调 | 🟡 单源：听众对 AI 语音的人似度评分本就偏低 |
| 4 | 无法重叠说话 | 结构性，架构决定 |

→ **第 1 条直接决定项目优先级：填充音（filler）从"可选"升为 P1 必须项。**

---

## 5. Q2 输入侧（纠错/自我纠正）：❌ 不该插模型

### 决定性证据（两处，均 🟢 一手）

**① arXiv 2608.03970《Should We Type or Talk to LLM Agents?》(2026-08)** —— 做的正是这件事：把干净文本口语化，再试各种"修复"预处理看能否救回。
- 去填充词（filler-stripping）**救不回来**：配对差 −0.22±1.08（t=−1.00，**不显著**）
- 原文：**"no repair we test (filler stripping, structure-preserving cleanup, or LLM editing) recovers the damage"**
- 处方：**"the fix must happen at composition time or in the model, not in between"**，并建议 **"do not reformat or restructure what the user said"**
- ⚠️ 该文是**文本层**代理实验，非真人语音

**② NVIDIA voice-agent-examples 官方 BEST_PRACTICES §3.6** —— 整节讲转写质量，建议是自定义词表增强、ITN、好音频、避免重采样、**"只依据 final transcript 做关键决策"**。**通篇没有"清洗口语"这一条**；唯一提到 filler 处方向相反：**让 agent 主动生成 filler 掩盖延迟**。

### 按问题类型拆

| 问题 | 该怎么做 | 代价 | 依据 |
|---|---|---|---|
| 填充词（嗯/呃/那个） | 规则过滤，甚至**不做** | ~0 | 去 filler 不提升 🟢 |
| backchannel（嗯/对） | 规则白名单，**绝不能删真回答** | ~0 | ⚠️ "对/好的"也可能是正经回答 |
| 自我纠正（"周五——不对，周四"） | **交给 CC**（文本 LLM 擅长服从最后一条指令） | 0 | 🟢 外部修复无收益 |
| 专名识别错（UCD/人名） | **给 ASR 加偏置**，不是下游补救 | 见下 | — |

### 两个具体发现

- 🔴 **SenseVoice 用不了热词**：sherpa-onnx 官方原话 **"Only transducer models support hotwords… All other models don't support hotwords."** → 最便宜的输入侧改进在当前 ASR 上**做不了**。
  - 替代：`rule_fsts`/`rule_fars`（🟢 SenseVoice **支持**，本机 docstring 确认）做确定性专名替换。
- 💡 **真正的杠杆是换更强的 ASR，不是加裁判** —— 本机 sherpa-onnx **已有 `from_qwen3_asr`**；项目文档记 Qwen3-ASR CER 7.65% vs SenseVoice 10.11%（🟡 单一聚合站，未交叉）。**建议纳入 A/B。**

### 未测到的（不编）
- **本地模型的延迟代价没测到**：本机未装任何本地推理运行时（ollama/llama.cpp/mlx 全无，venv 内无 torch/transformers）。但即便按乐观 +200ms 算，CC 首句已 1333–3069ms，为**证据显示无收益**的一层再加延迟是坏交易。

---

## 6. Q2 输出侧（口语化）：❌ 不加改写层，但**该做**——做在 prompt 层与 TTS 层

### 6.1 本机一手实验：prompt 层就够用（✅ 亲测）

`test_spoken_style.py`：同一批问题（列举型/解释型/任务汇报型），两套 system prompt，haiku，n=3。

| 场景 | A 宽松版首句 | B 口语化强约束版首句 |
|---|---|---|
| 列举型 | 60 字（像念备忘录） | **9 字**："行，我给你编三个。" |
| 解释型 | 50 字 | 40 字 + "打个比方" |
| 任务汇报型 | 「已改完 /Users/owen/proj/config.py，超时从 **30** 秒改为 **60** 秒。」 | **4 字**："改好了。" |

**四条结论：**
1. ✅ **prompt 约束效果巨大**（首句 60→9、47→4 字）——同时改善自然度与感知延迟。
2. ✅ **保真度没有牺牲**：B 里文件路径**原样保留**，数字变成"三十秒/六十秒"（口语读法，**更准不是更不准**）。
3. ⚠️ **B 仍吐 `\n\n` 空行**——prompt 管不住，必须由**规则**兜住（朗读时会变成莫名长停顿）。
4. ⚠️ **代价**：B 首句**到达时间更慢**（3733ms vs 3072ms）——约束越多思考越久；但首句只有 9 字，所以"开口"其实更快。**首句延迟 vs 首句长度 是个真实权衡。**

### 6.2 补充证据

- 🟢 **Google SSML 文档**：数字/日期/缩写/首字母的**念法是 TTS 厂商职责**（`say-as interpret-as="cardinal"`、`<sub alias=…>`）→ **不需要本地模型做文本规范化**。
- 🔴 **没找到**任何"改写成口语 → 提升主观自然度"的直接评测证据。
- 🟡 改写确实会动事实（引用"71% 命名实体超出原文"，**单一二手源，存疑**）；**RewritingBench**（中文改写诊断基准）自述核心难题正是"preserve meaning while improving expression"，且评价高度主观。
- 🟡 业内把风格约束放**生成侧**而非事后改写（ZenML 案例库原话 "generate output as if conversation rather than writing"）。

### 6.3 方案对照

| 方案 | 自然度收益 | 保真风险 | 延迟 | 判读 |
|---|---|---|---|---|
| (a) 生成侧 prompt 约束 | 高（治根因） | 低 | **0** | ✅ |
| (b) TTS 层 style/instruct | 中–高 | **0**（不改文本） | 0 | ✅ |
| (c) 规则化清洗 | 中 | 低（确定性） | ~0 | ✅ |
| (d) 事后 LLM 改写层 | 🔴未证实 | **高** | +1 跳 | ❌ |

**为什么 (d) 与第一硬需求直接冲突**：否决端到端模型的理由就是"它无法忠实朗读"。在 CC 与 TTS 之间插生成模型，等于**把这个被否决的性质从声学层搬回文本层**。

---

## 7. 落地清单（已并入计划）

| 项 | 状态 |
|---|---|
| `jarvis_voice/sanitize.py` 确定性清洗（去 markdown/空白/URL/emoji，**不丢内容**） | ✅ 已做，9/9 测试通过（`test_sanitize.py`） |
| `persona.txt` 重写为口语化强约束版（现版仍是双脑时代） | ⬜ P0 |
| 填充音（filler） | ⬜ **P1 必须**（依据 §4-1） |
| 切句器接 `sanitize_for_speech` | ⬜ P0 |
| 评估 Qwen3-ASR（`from_qwen3_asr`）替代 SenseVoice | ⬜ 纳入 A/B |
| TTS 层句级情感控制（阿里 instruct / Azure styles / Fish bracket） | ⬜ P1 |
| 情感闭环（SenseVoice 标签 → 参数映射 → 情感 TTS） | ⬜ P2 研究工程，无现成实现 |

---

## 8. 未验证清单

- 词级情感是否有可用开源权重 —— 无一手证据
- 各 TTS 的中文韵律师听测试 —— 无一手证据
- 改写模型对"数字/路径/专名"的**精确漂移率** —— 无可信测量，本报告不引用存疑数字为结论
- Audio MultiChallenge 的 Voice Editing 轴（arXiv 2512.14865）**只有端到端音频模型的成绩**，**无"级联 vs E2E"分轴对比**（🔴 不得当结论用）
- 本地小模型在本机的实际延迟（未装运行时，未测）

---

## 9. 来源

**本机一手**：`test_spoken_style.py`（prompt A/B，n=3）、`test_sanitize.py`（9 例）、本机 sherpa-onnx docstring

**论文（🟢 直读原文/摘要）**：arXiv 2608.03970（type vs talk）、arXiv 2507.22352（CUI'25 延迟与填充词）、arXiv 2509.24629（WeSCon 词级情感）、arXiv 2512.14865（Audio MultiChallenge，🟡）、arXiv 2606.01016（PolySpeech-100，E2E 反超）

**官方文档（🟢 直读）**：NVIDIA voice-agent-examples BEST_PRACTICES §3.6、FunASR SenseVoice 博客、Google Cloud TTS SSML、sherpa-onnx hotwords 文档

**期刊（🟡 未见开源）**：An 等，Pattern Recognition 2026（情感解析→参数映射→情感 TTS）

*本文为新增文件。§6.1 是本机亲测，其余均标注来源级别；未验证项已显式列出，不作为结论使用。*
