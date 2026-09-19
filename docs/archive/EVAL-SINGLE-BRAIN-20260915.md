# 评估报告：双脑修复前景 vs 单脑方案（2026-09-15）

> **作者**：WorkBuddy（Mac 侧）
> **问题**（Owen 原话）：项目已修改多日仍无法解决——不播报、播报卡顿、播报丢字、自己说话、无法打断，播报 Claude Code 结果时同样卡顿、丢字、不播报，整体完全无法使用。问：① 彻底修复可能性？② 「语音直连 Claude Code + 播报、去掉中间模型」可行性？
> **方法**：通读 Mac 侧全部核心代码（`jarvis_omni.py` 1430 行 / `comni_ws.py` / `claude_bridge.py` / `jarvis_state.py`）+ 两路并行联网调研（MiniCPM-o 社区生态 / 单脑方案先例与组件）。
> **口径**：✅=本机实测/代码核实　🔗=联网调研（附 URL，2026-09-15 检索）　⚠️=未验证/推断。

---

## 0. 结论先行（TL;DR）

1. **双脑（MiniCPM-o 当耳嘴）彻底修复五个症状的可能性：低（⚠️ 估 <20%）。**
   卡顿/丢字有上游修复可迁（master 分支），但**不播报、自言自语、打断**三个是模型代际级/架构级问题——上游维护者原话是"下一代 Omni 才改进"，而下一代没有时间表；维护者重心 8 月下旬起已转向 VoxCPM2。
2. **单脑方案（去掉 MiniCPM-o，语音直连 Claude Code + TTS 播报）：可行（⚠️ 高置信 ~85%）。**
   五个症状在新架构里**四个结构性消失、一个变确定性工程问题**。所需组件**80% 本项目已有且实测过**（SenseVoice / ClaudeBridge / Kokoro / MCP 搜索），缺的只是一层编排薄壳 + VAD/Turn-detection + AEC。
3. **这其实就是方案池 9/14 已经定稿的「全进 CC」v1 主线**——9/15 凌晨切 Plan B 是对 omni 栈的续命尝试，社区调研证明那条路的天花板到了。**建议：回到 9/14 定稿，MiniCPM-o 转档案态，先做 Mac 单机闭环原型。**

---

## 1. 现状理解：架构与代码（✅ 代码核实）

### 1.1 当前实跑（双脑 + 双传输）

```
[Mac] jarvis_omni.py ───────── 双工主循环（1s 块，严格交替 prefill→decode）
  ├ MicCapture（sounddevice，有界队列丢最老）
  ├ _excise_echo / 回声地板（播放期喂数字静音）        ← 补偿层
  ├ SenseVoice ASR（任务路由：TASK_RE/TASK_HINT_RE）   ← 补偿层
  ├ DuplexPlayer（START_DELAY 1.5s 永久缓冲）          ← 补偿层
  ├ 反自言自语护栏（5拍→打断静音, 第2次→重置会话）       ← 补偿层
  ├ 闲置暂停（>6s 停送节拍防 KV 污染）                 ← 补偿层
  ├ 播报层（回灌+覆盖率判停+length_penalty 压制+重试）  ← 补偿层
  └ claude_bridge.py ←→ claude --input-format stream-json --bare（常驻，warm ~1-1.5s）
        │
        ├─ OMNI_TRANSPORT=http → [Win] 自研 wrapper :9060 → llama-omni-master :19060   (Plan A)
        └─ OMNI_TRANSPORT=ws   → [Win] gateway :8006 → worker :22400 → llama-demo :19060 (Plan B, 当前默认)
                                    模型同为 MiniCPM-o 4.5 GGUF Q4_K_M ctx 8192
```

### 1.2 关键代码事实（影响后面归因）

- **任务路由是"猜"的**：双工下模型**从不输出 `<task>` 标签**（独立审计取证确认），只能靠"模型说了『我为你查一下』"（`TASK_HINT_RE`）去**配对最近一句用户原话**——配对窗口 12s，张冠李戴风险内置。
- **播报（回灌）两条路都不硬**：
  - Plan A：文本 prefill 进双工 KV，模型可判"主人没出声"→ LISTEN → **0.00s 音频**。客户端靠覆盖率 LCS 判停 + 未出声重置会话重试一次兜底。
  - Plan B：官方双工协议**没有文本通道** → 客户端用 **macOS `say`（Tingting 音色）合成"请把这条消息原样念给主人听"的指令音频**，当麦克风音频喂进去，模型"回应"式复述。✅ 已核实 `_synth_prompt()` 就是这么干的。
- **播报期间的 turn 保持靠 `length_penalty`**：每拍显式传（C++ 里是持久状态），念够 0.85 覆盖率或"不再增长"立刻松手——松晚一拍它就接着编。
- **ClaudeBridge 本身很健康**：常驻 stream-json 双向、partial→切句、进程死亡自动重启重发、`--bare`+显式挂 Tavily MCP。✅ 它是单脑方案里**可以直接复用**的核心资产。
- **~40% 的客户端代码是在给中间模型"擦屁股"**：回声擦除/回声地板、反自言自语护栏、闲置暂停、AGC、覆盖率判停、重试、TASK_HINT 配对、断口统计……**单脑后这些整块删除**。

---

## 2. 五大症状归因与上游修复前景（✅代码 + 🔗社区）

| # | 症状 | 代码层机制（✅） | 上游状态（🔗） | 可修性裁决 |
|---|---|---|---|---|
| 1 | **不播报** | 端到端双工模型只会"回应"不会"朗读"（LiveKit 官方文档 + 本项目一个月实测，§7.4）；Plan A 文本注入常被无视（0.00s），Plan B 根本没有文本通道 | tc-mb 上游**无任何对应修复/讨论**；demo #60 只有一个未获回复的提问 | 🔴 **结构性死胡同** |
| 2 | **播报卡顿** | 供给≈1.0x 零余量（块间隔中位 994ms vs 消耗 1.0s）→ DuplexPlayer 欠载断口；`length_penalty` 历史问题（1.0 时每 1-2 拍断回合，已改 1.1） | **上游已修**：PR #47（连续 speak chunk 吞字/断字，2026-06-02 合 master）、PR #78（2026-07-01 合 r2）。⚠️ 但 feat/web-demo（v1.0.22 二进制基座，2026-04-29）**早于这两个 PR，大概率没吃到**；#88 尾块晚 276ms 维护者答 "by design" | 🟡 **部分可修**（迁 master 构建 / 等回流） |
| 3 | **播报丢字** | 同 #2 的上游吞字/断字；TTS 采样**写死 C++**（temp 0.8/top_p 0.85/top_k 25，CLI 改不动）；覆盖率 0.85 提前松压制是故意容忍 | 同 #2 | 🟡 部分可修（迁 master + 改 C++ 重编译采样） |
| 4 | **自己说话** | ① 引擎空转 ~40 tok/s 灌 KV → ~2.3min 撞 ctx 滑窗 → 失聪/乱语；② 回声自激（自己的声音喂回去 → 应答自己 → 循环）。客户端护栏（打断+递增静音+重置）是打地鼠 | 社区同款报告全 open：demo #49、#74 Q2、#88 Q3；维护者原话：**"current model generation 的限制，下一代 Omni 会改进"** | 🔴 **死（等下一代，无时间表）** |
| 5 | **无法打断** | C++ 全分支**缺 listen 守门**（官方 Python `modeling_minicpmo.py:3237-3239` 有，C++ 没有）——项目已自行补丁；客户端 RMS 双阈值插话，但 macOS 无真 AEC（PortAudio 拿不到 VoiceProcessingIO）→ 回声误打断/漏打断两头堵 | C++ 无任何分支实现；最接近的 fork `unal-ai/llama.cpp-omni@4d56a52` 旧基座已停维护；demo #5 维护者：实验性功能，**靠下一代模型训练数据修** | 🔴 **半死**（自己补丁 + 客户端天花板低） |

**播报 Claude Code 结果时的同样症状** = 同一条回灌通道的病，不是 CC 的锅（外加 CC 爱先吐英文前言导致模型不出声，客户端已用 `_cjk` 过滤补偿）。

### 生态活性（🔗）

- `tc-mb/llama.cpp-omni`：6–8 月活跃（master 推进 comni-2.0 / DuplexPipeline），**8 月下旬起重心转 VoxCPM2**（#91/#106），MiniCPM-o 双工无新提交；**最新 release 仍是 v1.0.22（2026-04-29）**；本项目提的 **#114 至今 open、无回应**。
- OpenBMB 侧：MiniCPM-V 仓仍在维护，**无 MiniCPM-o 5.0 迹象**。
- 替代品：16GB 内可替换 MiniCPM-o 4.5 的**真全双工开源方案目前不存在**（Qwen3.5-Omni-Light 的权重/GGUF/全双工能力均无法核实）。

> **调研来源**（🔗 均为 2026-09-15 检索；动手前建议对要依赖的具体条目再核一次）：
> github.com/tc-mb/llama.cpp-omni（releases / pull/47 / pull/78 / pull/94 / issues/74·88·90·102·114）、
> github.com/OpenBMB/MiniCPM-o-Demo（issues/5·30·49·60）、github.com/unal-ai/llama.cpp-omni、
> datalearner.com Qwen3.5-Omni 条目。

---

## 3. 单脑方案评估：语音直连 Claude Code + TTS 播报（🔗+✅）

### 3.1 架构（去掉 MiniCPM-o，Windows 可完全退出链路）

```
[Mac M1 Pro 单机]
  麦克风 → Silero VAD（sherpa-onnx，已在项目依赖里）
        → Smart Turn v3 语义端点（8M ONNX, CPU ~12ms, 中文已核实支持）
        → SenseVoice ASR（已在跑；顺带输出 <|HAPPY|> 等情感标签 → 情感层补偿）
        → ClaudeBridge（✅ 已存在：常驻 stream-json，persona 走 CLAUDE.md，Tavily MCP 联网）
        → partial tokens → 聚句器（✅ claude_bridge.py 已有 SENT_END 切句）
        → Kokoro 82M TTS（✅ 已实测：首块 128ms, RTF 0.02-0.12；Mac 有 MLX 版 TTFB 40-126ms）
        → 扬声器
  打断 = VAD 命中 → 停 TTS 播放 + 中断 CC 在途 turn + 继续听（AEC 兜底防自激）
```

### 3.2 先例（🔗）

| 项目 | 说明 |
|---|---|
| `0xj7r/claude-voice-mode` | **与本方案最贴近**：whisper.cpp + Kokoro + Claude Code Stop hook，全本地 Mac，MIT，活跃。差距：按键说话、TTS 回复完才播（流式在 roadmap） |
| `marc-shade/voicemode` | Edge TTS + 本地 Whisper 作 MCP 工具挂进 CC（非透明前端） |
| Claude Code 官方 voice 模式（feature-flagged） | Deepgram 云端 STT 流式——说明"语音前端 + CLI agent"方向官方也在做 |
| `py-xiaozhi` / LocalCat | 完整 ASR→LLM→TTS + **WebRTC AEC on macOS arm64** 的成熟范本；LocalCat 实测 Apple Silicon 上 Kokoro TTFB 40-80ms |
| Pipecat / LiveKit Agents / TEN-framework | 生产级框架，证明 VAD+语义端点+句子缓冲流式 TTS+barge-in 是标准结构 |

### 3.3 为什么五个症状在单脑里**结构性消失**

| 症状 | 单脑下的命运 |
|---|---|
| 不播报 | ✅ **消失**。TTS 逐字读文本，CC 说啥念啥——"端到端模型不朗读"这个结构性死胡同**整层不存在** |
| 播报卡顿 | ✅ **消失**。Kokoro RTF 0.02-0.12 本地句子级流式，没有 1s 训练节拍约束、没有零余量供给问题 |
| 播报丢字 | ✅ **消失**。文本管线没有"吞字"这回事 |
| 自己说话 | ✅ **构造上不可能**。没有空转 decode、没有 KV 污染——CC 只响应被提交的轮次 |
| 无法打断 | 🟡 **变成确定性工程问题**：停播+中断+继续听，逻辑上必然可达；唯一难点是播放期间麦克风要开着 → 需要真 AEC（见风险 3） |

### 3.4 延迟预算（用户停口 → 首音频，⚠️ 估算）

| 环节 | 耗时 |
|---|---|
| 语义端点确认（Smart Turn + 静音前缀） | 250–400ms |
| SenseVoice 整句识别 | ~150–300ms（非流式，⚠️ 未在 M1 Pro 实测） |
| **Claude Code 首 token（warm）** | **~1000–1500ms ← 最大成本** |
| 聚句到首个标点 | +100–300ms（CLAUDE.md 可强制"首句短"） |
| Kokoro 首块 | ~130ms |
| **合计 p50** | **~1.7–2.6s** |

对比现状 MiniCPM-o 前台首音 1.10s——**变慢，但换来的是"说的每句话都可信"**。感知优化：端点确认后立即播本地预制应答音（"嗯，我想想"），感知延迟 <500ms。

### 3.5 丢失的能力与补偿

- **情绪识别**：SenseVoice 自带情感/事件标签输出（被低估的补偿）+ ASR 文本情绪分类器（后期）
- **视觉（Phase ④）**：独立 VLM（MiniCPM-V 4.5 按需节点，方案池②已有此设计）——文本管线下反而更清晰
- **边听边说的原生感**：文本管线打断天然慢一拍，无补偿，靠 AEC 质量兜底
- **前台 1.10s 首音**：→ ~2s（§3.4）

### 3.6 风险清单（按严重度）

1. 🔴 **同机全双工 AEC**（最大工程坑）：扬声器声音进麦克风 → 误打断/漏打断。`webrtc-audio-processing`（py-xiaozhi 已在 macOS arm64 验证，100ms 音频仅耗 ~0.7ms）；**耳机是最廉价规避**。
2. 🟡 **stream-json 长会话上下文膨胀** → 首 token 变慢。对策：会话滚动策略（定期 `/compact` 或重启，persona 在 CLAUDE.md 无损恢复）。⚠️ 各版本行为未逐一验证。
3. 🟡 **stream-json 的 interrupt（中断在途 turn）支持度未实测** —— P0 第一件事先验证这个；不支持下策 = kill 子进程重开（bridge 已有自动重启逻辑，代价 ~1s）。
4. 🟡 **backchannel 误打断**（用户"嗯"一声不应打断）→ 应答词过滤器（方案池五件套①）+ 延迟确认。
5. 🟢 SenseVoice 非流式 → 端点前无 partial，端点延迟有下界（可接受）。
6. 🟢 TTS 建议落 Mac（MLX Kokoro 已够快）→ Windows 完全退出链路，**顺带消掉整个双栈/19060 抢占/防火墙暴露/重启死循环那摊事**（见 `HANDOVER-VERIFY-20260915.md`）。

---

## 4. 建议路线

**回 9/14 方案池定稿（「全进 CC」v1 主线 + 五件套），MiniCPM-o 转档案态。**

- **P0（1–2 天，Mac 单机最薄闭环）**：Silero VAD → SenseVoice → ClaudeBridge → 聚句 → Kokoro(Mac) → 播放。**先不做打断**。验收：延迟实测 + 播报逐字忠实。同步验证风险 3（interrupt 支持度）。
- **P1**：打断三层（AEC + 停 TTS + 中断/重启 turn）+ 应答词过滤器 + 填充音。
- **P2**：会话滚动策略 + 情感标签利用 + 真机验收三项（派活能听见播报 / 说话能打断 / 静置 30s 再说话仍有反应）。
- **P3**：Windows 转按需节点（MiniCPM-V 视觉/情绪、CosyVoice 精报）；上云 GLM-Realtime-Flash 保留为备选③（10 元实测后再判）。
- **MiniCPM-o 栈**：不停机归档（文档/补丁/构建保留），若未来 MiniCPM-o 5.0 解决代际问题可复活评估；上游 #114 留着等回应。

**一句话**：双脑的病，两个能修（迁 master）、三个是模型代际天花板；单脑的病，全是工程问题，且 80% 组件已经在本项目里实测过了。
