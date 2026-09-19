# 延迟改善计划（2026-09-19）

> **触发**：Owen 要求「缩短延迟，让它更接近对话的感觉」。
> **口径**：🟢 本机一手实测 / 直读源码　🟡 单源二手（标日期）　🔴 未验证
> **规矩**：源 ≥2 且独立；厂商自报与第三方实测分开标；没验证的写「未验证」。

---

## 0. 一句话

**首音延迟已经被砍掉 1.08s（TTS 换 REST，已做）。剩下最大的一块是「工具回合 15.9s 死寂」——而实测发现 CC 自己已经在说承接语了，只是只有 8% 的时候说。所以第一步不是造机制，是提高它的频率。**

---

## 1. 现状实测（全部本机，可复现）

### 1.1 感知延迟分解

| 场景 | n | 首句 p50 | p90 | 构成 |
|---|---|---|---|---|
| **闲聊**（无工具） | 41 | **2.1s** | 4.3s | ASR ~0.1s + CC + TTS |
| **工具回合** | 16 | **15.9s** | **44.3s** | 工具前 3.2s + 工具后 9.5s |

### 1.2 工具链

| 指标 | 值 |
|---|---|
| 有工具的回合占比 | 26/101 |
| **链长** | p50 **3** / p90 **12** / max **28** |
| **每步墙钟** | p50 **4.50s** / p90 9.43s |
| 工具调用总数 | 115 次 / 12 种 |

### 1.3 TTS（今天已改）

| 路径 | TTFA p50 | p90 |
|---|---|---|
| WS（原） | 1559 ms | 1835 ms |
| **REST（现，已上线）** | **478 ms** | 733 ms |

- 差值**与文本长度无关**（2 字差 982ms、57 字差 893ms）→ 是**连接建立**，不是推理
- 两条路**同一 model**（已核）→ 音质不变
- 提交 `a75ab0d`；回退 `FISH_WS=1`

### 1.4 🔑 「CC 会不会先说话」——**本报告最重要的一手发现**

数 26 个工具回合里「首个 `tool` 事件之前有没有 `sentence`」：

**结果：2/26 = 8%。而这两条的内容是——**

| 用户说 | CC 先说的 | 后续链长 |
|---|---|---|
| 「那你去 bsp 上看有什么作业。」 | **「好，我上去看看。」** | 28 步 |
| 「你能看到我的 github 主页吗？」 | **「我看看有没有开着的页面。」** | 5 步 |

**→ 两条都是标准的「Tool Preamble」（承接语），不是结论。**

**结论：机制已经通了，只是不可靠（8%）。** 这比"完全不吐文本"是好得多的起点 ——
**要做的不是造机制，是提高频率 + 加兜底。**

### 1.5 根因（为什么只有 8%）

- `anthropics/claude-code` **issue #9128**「Excessive narration around tool calls violates
  minimize output instruction」→ **CLOSED / NOT_PLANNED**（2025-10-08）✅ 已亲自核实
- `openclaw#17915`：系统提示词硬编码「**Default: do not narrate routine, low-risk tool calls
  (just call the tool)**」🟡 单源
- **LiveKit 官方员工 Darryn Campbell**（社区，2026-03-04）：
  > 「LiveKit **does not enforce** any "pre-speech before tool call" behavior…
  > **This is model behavior, not a LiveKit toggle.**」🟡 单源

**→ CC 的默认人格是「别废话，直接干」。要它说承接语，得显式对抗这个默认。**

---

## 2. 方案（三档，按代价排序）

### P0 —— 提高 CC 自己说承接语的频率（改提示词，零机制）

**为什么先做 P0**：§1.4 证明机制已通。先试最便宜的一招。

**改法**（`orchestrator.py` 的 `SYSTEM_PROMPT`，规则 12/13 之后加一条）：

```
14. **多步任务开口先给一句「我去做 X」**，再开始调工具。≤12 字，说意图不说结果。
    ⚠️ 这条**覆盖**你默认的「不叙述例行工具调用」——那个默认对多步任务不适用。
    只在**开始一个新阶段**时说，不要每次调用都说。
    例：「好，我上去看看。」「我看看有没有开着的页面。」
```

**注意措辞必须显式覆盖默认** —— 否则两条冲突时谁赢不确定（🔴 未验证）。

**验收**：重跑 §1.4 的数法，目标从 **2/26** 提到 **>50%**。

---

### P1 —— 填充音改触发时机（P0 不达标才做）

**现在**：`orchestrator.py:611` `_arm_filler(turn)` **每个 turn 开始 900ms 后无条件播**。

**⚠️ 这个做法有反证**（ACM 3472307.3484181, Boukaram 2021）🟡 二手转引：

> 「if the agent used a filler **at every turn**, the users **rated it more negatively** with
> some even commenting that they would **prefer silence** to a filler at every turn.」
> → 该研究里的 filler「**were only used in half of the turns**」。

**改成**（照抄 Azure Voice Live `interim_response` 的 OR 逻辑）🟡 官方文档 2026-09-19 读：

| 触发器 | 时机 | 值 |
|---|---|---|
| **`tool`** | 首个 tool 事件，且 **CC 这次没说 preamble** | delay **0** |
| **`latency`** | 兜底：还没出首句 | **2000 ms**（对齐 Azure 默认） |

**架构类比**（跨会话巧合）：本项目 `interrupt_confirm_ms=300` 的注释自己写着
「廉价版 **false_interruption_timeout**」——**同一套「照抄供应商参数」的做法**。

---

### P2 —— 文案从「无内容」改「有内容」（处理长静音）

**现状**：`fillers.py:30` 的 4 条 = `["嗯……", "那个……", "让我想想。", "我看一下。"]`，
**平均 0.96s**（本机实测 0.93/0.98/0.98/0.98）。

**覆盖率的账**：

| 静音段 | p50 | 0.96s 填充音能覆盖 |
|---|---|---|
| 工具**前** | 3.2s | 30% |
| 工具**后** | 9.5s | **10%** |

**→ 一条 1 秒的填充音盖不住 9.5 秒的空白。**

**两处改动**：

1. **文案分两档**

| 档 | 例子 | 用在哪 | 依据 |
|---|---|---|---|
| **意向式**（不承诺结果） | 「我看看这个」「我去翻一下」 | 兜底填充音 | 规避"说了没做"的尴尬 |
| **结果式** | 「拿到了，课程表在这儿」 | CC 的 preamble（P0） | agentpatterns.ai |

⚠️ **`fillers.py:29` 的注释写「**不要**说'我查一下'——万一没查就尴尬」。这个顾虑真实，
但用措辞规避即可**：说**意向**（「我看一下」）不说**已完成**（「我查过了」）。
**现有那 4 条里的「我看一下」其实已经合格，被误归类了。**

2. **长静音加第二条「进度叙述」**

🔴 Roark 的回归清单（2026-08-24）点了这条（「需要第二条进度叙述？」）但**没给答案**。
**未验证，P2 要做实验。**

---

## 3. 实施顺序与验收

| 阶段 | 动作 | 验收（可测） |
|---|---|---|
| **P0** | 加提示词规则 14，**只观测不改填充音** | §1.4 的数法：tool 前 sentence 从 **2/26** → **>50%** |
| **P1** | 填充音触发改 tool 事件 + 2000ms 兜底 | `played_s`；工具回合首音从 15.9s → <2s |
| **P2** | 文案改意向式 + 第二条进度叙述 | Roark 清单（见下） |

**⚠️ P0 只改提示词不改行为** —— 先拿到「CC 听不听话」这个数，再决定 P1 要不要做。
**这个顺序本身就是 [[measure-before-claiming]] 的应用。**

### 验收清单（Roark 的回归表，🟡 2026-08-24，直接可用）

| 场景 | 要问 |
|---|---|
| 快路径 | 工具 200ms 返回 —— 填充音**还该响吗**？（「A filler on a fast tool is **a self-inflicted second of latency**」） |
| 慢路径 | 工具 6–8s —— 填充音**够长吗**？ |
| 失败路径 | 工具报错 —— 有面向用户的兜底话术吗？ |
| 中途打断 | 用户 400ms 插话 —— TTS 干净取消吗？ |
| 连续调用 | 一轮两次工具 —— **同一句话 20 秒内会不会重复播两次**？ |

**⚠️ 必须带对照**（沿用 `VERIFY-TURN-DETECTION-20260916.md` 的纪律）：改前改后各跑一场同场景。

---

## 4. Jev：社区常用配合方式（2026-09-19）

### 4.1 接入方式（Owen 提供的 OpenRouter 入口）🟢 页面直读

```
POST https://openrouter.ai/api/alpha/decisions
Authorization: Bearer $OPENROUTER_API_KEY
Model: typesafe/jev-1.13    （或 ~typesafe/jev-latest）
```

**两个坑**：

1. **不是 OpenAI 兼容端点** —— 官方原话「chat completions SDKs **will not work** with it」
2. **OpenRouter 在这里没有路由价值**：官方页面「hosted by **one provider**… no routing
   decisions to make」→ 单供应商 = 多一跳代理。

**⚠️ 但网络实测反转了这个判断** 🟢 本机：

| 端点 | TCP+TLS p50（都柏林） |
|---|---|
| `api.typesafe.ai`（直连） | **377 ms** |
| `openrouter.ai` | **26 ms**（Cloudflare 边缘） |

**→ TypeSafe 自己没边缘部署。走 OpenRouter 可能是对的，但必须实测冷/热连接。**

**延迟数字（三个独立来源）**：

| 来源 | 值 |
|---|---|
| `jev-ultrafast`（HN 首页） | 178 ms |
| **OpenRouter P50** | **210 ms**（P90 350 / P99 530） |
| DataCamp 转 TypeSafe | 0.4 s |

**🔴 全部未验证「从都柏林测是多少」。** 要 key 才能测。

### 4.2 社区常用的 6 种配合方式

| # | 配合 | 代表 | 做法 |
|---|---|---|---|
| 1 | **Jev + LLM（只在要打字时叫）** | `Jev Ultrafast`、`fastbrowse` | Jev 选动作+元素；LLM 只在 `TYPE_TEXT` 时生成 |
| 2 | **Jev + 编码 agent 当代理** | `yoshi`(**Claude Code**)、`pi-jev` | yoshi：判哪些历史该留；pi-jev：语义工具路由 |
| 3 | **Jev + LangChain** | `langchain-typesafe` | 模型路由 + 工具护栏 |
| 4 | **Jev + RAG 检索器** | Kush Bhuwalka（416 赞） | 逐块判「该不该留」 |
| 5 | **Jev + 浏览器（WebMCP/Stagehand）** | Stagehand、WebMCP bench | 站点暴露结构化工具 → Jev 选 |
| 6 | **Jev 只出概率，阈值在代码里** | `super-jev` | 「turns a Jev answer into a **bounded action**」 |

**贯穿全部的一条原则**：**Jev 给概率，你的代码定阈值。** 官方样板：

```
Threshold: Yes-probability ≥ 80%  →  Auto-execute the tool call
```

### 4.3 对 jarvis 的位置（修正上一版结论）

**上一版我说「不做」——部分错了。** 更正：

| 场景 | Jev 值不值 |
|---|---|
| **现在**（20 个工具，规则匹配够） | ❌ 不值 |
| **工具集做大到需要 gating 时** | ✅ **值** —— 「从 100 个工具里挑」是 Jev 的招牌用例 |

**依据（arXiv 2604.21816, 2026-04）**：MCP 每轮急切注入全部 schema 的开销是
**10k–60k tokens**，且**接近 70% 上下文利用率时开始推理退化**。
Anthropic 自己的 benchmark：**Opus 4 在大工具集下工具选择准确率掉到 49%**。

**jarvis 现在**：3 个 MCP server / ~20 工具 ≈ **6,900 tok** + 系统提示词 3,010 tok
→ **离危险线还远**。

**翻转条件**：工具集扩到 50+ 时再评估。那时解法是**懒加载 schema + gating**，
而 **Jev 正好是那个 0.178s 的门控**。

### 4.4 ⚠️ 用 Jev 前必须知道的失败模式

- **「type-valid but wrong」**（`explainx.ai`, 2026-09-19）：通过全部 schema 校验、
  带着看起来合理的置信度，**但结论是错的**。举例：该判 `technical` 的判成 `billing`。
- **官方自曝**（`docs.typesafe.ai/model-jaggedness/jev-1.13`）：字面理解 / **数不准**
  （官方原话「**count in code**」）/ **state 里的对抗内容能移动答案** / state 塞无关材料精度就掉
- **动作空间决定命中率**（WebMCP benchmark，第三方）：Jev 单独操作浏览器 **25/49**；
  站点暴露结构化工具后 **49/49**

---

## 5. 🔴 未验证清单

| 项 | 为什么必须测 |
|---|---|
| **CC 加了规则 14 会不会照做** | 实测 2/26 是**没加提示词**时的数。加了之后**只能实测** |
| **规则 14 与 CC 默认「不叙述」冲突时谁赢** | P0 能否成立的前提 |
| **Jev 从都柏林的真实延迟** | 三个来源（178/210/400ms）测点未知。需要 key |
| **直连 TypeSafe vs OpenRouter** | 只测了 TCP+TLS，没测完整决策调用 |
| **0.96s 填充音 vs 9.5s 静音的实际听感** | 覆盖率 10%，但「有声音」和「全静默」的主观差别没测 |
| **第二条进度叙述的时机** | Roark 提了问题没给答案 |
| **preamble 会不会让闲聊变啰嗦** | agentpatterns.ai 强调「phase boundary 不是 per call」，无实测数据 |

---

## 6. 来源

**本机一手 🟢**：`~/.jarvis/events.jsonl`（26 个工具回合的 sentence/tool 时序、首句延迟）、
`~/.jarvis/fillers/*.wav`（片段时长）、`jarvis_voice/{fillers,orchestrator,config}.py`、
`claude_bridge.py`、Fish TTS 双路径 TTFA（n=20 交替测）、`api.typesafe.ai` vs `openrouter.ai` TCP+TLS

**已亲自核实的一手**：`anthropics/claude-code#9128`（CLOSED / NOT_PLANNED）

**二手 🟡（均标日期）**：Azure Voice Live `interim_response` 文档（2026-09-19 读）、
LiveKit 社区官方员工回帖（2026-03-04）、agentpatterns.ai Tool Preamble（2026-05-27）、
ACM 3472307.3484181 Boukaram 2021、Roark 填充音回归清单（2026-08-24）、
OpenRouter Jev 页面（2026-09-18）、`awesome-jev`、arXiv 2604.21816（2026-04）

---

*本文写于 2026-09-19。§1.4 的核心发现（CC 已经在说承接语，8%）推翻了一份子调研的
「0/20、完全不吐文本」——**子代理给的数字必须亲自复算**。*
