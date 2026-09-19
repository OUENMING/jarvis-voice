# 语音栈整合（2026-09-19 收尾）

> **这是本次会话全部调研的合并入口。**冷启动仍先读 `HANDOVER-20260918.md`（项目现状），
> 然后读本文（本次新增的结论、代码改动、决策地图、下一步）。
> **口径**：🟢 一手实测/直读源码　🟡 单源二手　🔴 未验证。引用旧结论标注来源时间。

---

## 0. 一页速览

| 问题 | 答案 |
|---|---|
| 本次新增代码 | ✅ `echoguard.py` + 单测 + 接入（**已装、已验证**） |
| 本次新增文档 | 3 篇（见 §1） |
| **第一步做什么** | **`pip install pywebrtc-audio`，跑他们的 `e2e_verify.py` + `e2e_speech.py`** —— **测 P0 只需要 5 分钟，不用自己写脚本**（见 §4） |
| 为什么这么便宜 | 维护者已提供与你的 P0 设计**逐项对应**的示例脚本，**而且用的就是 sounddevice** |
| 最大的未知数 | **本机 ERL**（决定平台 AEC 值不值得换架构） |
| 可以确定的事 | 你已有 65% 的东西是对的；不要重写状态机（见 §5） |

---

## 1. 本次会话产出

### 1.1 新增/修改的代码（🟢 已验证）

| 文件 | 改动 | 状态 |
|---|---|---|
| **`jarvis_voice/echoguard.py`** | 🆕 回声文本护栏（LCS 相似度判据） | ✅ 24 断言全过 |
| **`tests/test_echoguard.py`** | 🆕 独立脚本，含正/负/边界/时间窗用例 | ✅ |
| `jarvis_voice/config.py` | +4 字段 +2 个 env 覆盖 | ✅ 导入通过 |
| `jarvis_voice/orchestrator.py` | +4 处接入（import/init/记录/判定） | ✅ 各 1 次，已自检 |

**回声护栏的实测分离度**：回声 0.91–1.00 / 真人说话含重叠词汇最高 0.60 → 阈值 0.75 两边留余量。
**可审计**：每次命中发 `echo_suppressed` 事件（带 score + 匹配文本）。
**零成本回滚**：`JARVIS_ECHO_GUARD=0` 关闭。

### 1.2 新增文档

| 文档 | 内容 |
|---|---|
| **`RESEARCH-UPGRADE-PLAN-20260919.md`** | 打断/端点/状态清理升级计划（4 个参考实现的一手源码 → 你的代码） |
| **`VERIFY-AEC-PLAN-20260919.md`** | AEC 计划独立核实（442 行：3 处更正 + 冲突矩阵 + 物理边界 + 验收判据重定义） |
| **`CONSOLIDATION-20260919.md`**（本文） | 合并入口 |

---

## 2. 结论总表（按置信度）

### 2.1 🟢 高置信（可据此决策）

| 结论 | 依据 |
|---|---|
| **`session.py` 已经是正确的世代令牌实现**（`finish_turn` 同锁比对+置位 ≡ HF 的 compare-and-clear） | 读代码 + HF `cancel_scope.py` 源码 |
| **音频路径有 3 个「卡住即永久哑」的候选**：`_paused` / `_hd_gated`+`is_playing()` / `reopen()` 未重启流（`player.py:56-72`） | 读代码；症状与 HF `websocket_router.py:257-265` docstring 记录的同名 bug 同构 |
| **LiveKit 有 5.0s 看门狗；HF 没有，且 issue #363 承认这是缺口** | 一手源码 `speech_handle.py:16` + HF 全树 grep |
| **看门狗是 P0 必需件而非加固** | 两家在同一缺口上相反 |
| **sherpa-onnx 1.13.7 无任何 VAD padding 参数** | 本机 `.venv` 实测 API |
| **加预滚缓冲会撞破你自己的 SNR 门限**（`vad.py:66` 对整段算 RMS） | 读代码直接推出 |
| **「业界没人做到免提打断」不成立** —— ChatGPT/Gemini 靠平台 AEC 做到了 | `ksg98/fastaf-ide#7`（2026-08-05） |
| **ERLE 与主观相关性弱**（PCC 0.31），且**只适用单讲、需静音室** | ICASSP 2023 AEC Challenge，arXiv 2309.12553 |
| **打断场景 = double-talk = AEC 最弱环节**（headroom 0.57 最大） | 同上 |
| **ACOM = ERL + ERLE** —— 判效果要看两者之和，不是只看 ERLE | EE Times / Wikipedia / Dialogic 三源一致 |
| **pywebrtc-audio 由 `strands-labs`（AWS Strands Agents）维护**，vendor WebRTC `aec3` | 仓库一手 |
| **转写注入是 OWASP #1 风险，且音频被点名为文本防御盲区** | 多源（IBM/Cloudflare/Microsoft/Trend Micro/tianpan.co） |

### 2.2 🟡 中置信（需实测确认）

| 结论 | 缺什么 |
|---|---|
| `_paused` 是那个「谜」的具体凶手 | **未复现**——三个候选机制都符合，需 Phase 0 观测判 |
| false-interruption pause/resume 的收益 | LiveKit 有该机制（2.0s 默认），对你场景收益未测 |
| 文本 ONNX 端点检测的中文能力 | `multlingual` 分支存在，**中文实测数据全球无**（你 09-16 原话仍成立） |
| VPIO 的丢音/增益变化 | 一处负面先例（Fora Soft），未在本机复现 |

### 2.3 🔴 未验证（禁入决策）

- **本机 ERL** ← 判决「能不能达到效果」的前置数字
- **pywebrtc-audio 在本机内置麦+内置扬声器的 ERLE 天花板**
- **AEC 模式下打断检测首次连续运行**（从未测过的代码路径）
- **macOS VPIO 的实测 AEC 质量**（专项检索**零结果**，全球无量化报告）
- **无 bundle 的 CLI 能否启动 VoiceProcessingIO**（JUCE 报 `null bundleID`，但纯 C++ 版 reportedly 无此问题）

---

## 3. 决策地图（依赖关系）

```
                        ┌─────────────────────────────┐
                        │ ① 测本机 AEC 基线（5 分钟）  │  ← 第一步
                        │   跑维护者的 e2e 示例       │
                        └──────────────┬──────────────┘
                                       │ 产出：far / near_raw / near_clean
                                       ▼
              ┌────────────────────────┴────────────────────────┐
              │                                                 │
     near_raw 够干净                                    near_raw 脏
     （现有门限就挡住）                                  （需要 AEC）
              │                                                 │
              ▼                                                 ▼
   ┌──────────────────────┐                    ┌───────────────────────────┐
   │ AEC 边际价值小        │                    │ 接 AecGate（1–2 天）       │
   │ → 省下这条路          │                    │ 再看 near_clean 够不够     │
   │ → 用文本护栏兜        │                    └─────────────┬─────────────┘
   └──────────────────────┘                                  │
                                                             ▼
                                          ┌──────────────────────────────────┐
                                          │ ERLE 够用 → 结束                  │
                                          │ ERLE 卡低且残余非线性 → 换参考也救 │
                                          │   不了（Microchip）→ VPIO / 硬件  │
                                          └──────────────────────────────────┘

并行轨道（互不依赖，可随时做）：
  • Phase 0 观测（/api/state + gate_watchdog）→ 解「音频路径变哑」
  • P0-C 注入隔离（<transcript> 包裹 + system prompt 第 12/13 条）
  • 回声文本护栏 → ✅ 已完成
```

**关键：第一步产出的 `near_raw.wav` 同时服务于两条路** —— 它既是"无 AEC 的对照"（你 09-16 方法论要求的「笨办法对照」），也是判断"要不要做 AEC"的唯一依据。

---

## 4. ✅ 第一步已完成 —— 结果见 `PROBE-AEC-RESULTS-20260919.md`

**AEC 在本机有效，且余量很大：稳态 ERLE 34–36 dB / ACOM ≈ 40 dB / +16dB 音量仍过门限。**

判决落在你决策树的「**接入**」档，而且**不是勉强够，是 2.3 倍余量**。
→ **下一步：接 `AecGate`**（逐文件清单见 `RESEARCH-AEC-20260919.md` §2.2）。

**⚠️ 两条必须带走的更正**（详见结果文档）：
1. **ERLE 必须取稳态**（丢弃前 3 秒）—— 第一版量整段，重复跑出 14.6 dB 的假"不可靠"结论
2. **幻觉判据不能是「SenseVoice 有没有非空输出」** —— `'.'`/`'The.'` 是项目早已记录的噪声伪影；
   正确判据是**残余是否越 VAD 门限**（`vad.py:_passes_gate` 在 ASR 之前就拦掉了）

**唯一还没测的关键项**：**双讲保真**（接入后 AEC 会不会把用户语音也消掉）—— 需跑 `--double-talk` 并出声。

*(以下为执行前的原计划，保留作为方法记录)*

---

### 4.0 原计划（已执行）

**为什么选它** —— 你 P0 设计的三项，维护者已写好，**而且用的就是 sounddevice**：

| 你的 `RESEARCH-AEC-20260919.md` §3 设计 | 他们的示例 | 对应 |
|---|---|---|
| ① 录音对照：外放 + 同时录，人不出声 | **`examples/e2e_verify.py`** | ✅ 逐项对应 |
| ② 离线 AEC + 指标 | 同脚本，输出三个 wav | ✅ |
| ③ 双讲保真：播放时真人读一句 | **`examples/e2e_speech.py`** | ✅ 逐项对应 |

`e2e_verify.py` 的 docstring（一手）：
> Plays a tone through your speakers for a few seconds while recording from your mic. Saves three wav files so you can listen and compare:
> - `far.wav`: what was played through the speaker (reference)
> - `near_raw.wav`: raw mic capture (**contains echo + noise**)
> - `near_clean.wav`: mic capture after echo cancellation + noise suppression

`e2e_speech.py`（一手）：
> Plays a tone through speakers while you talk into the mic. Saves raw and cleaned recordings so you can verify:
> - **Your speech is preserved**
> - **The speaker tone is removed**
> - Background noise is suppressed

**两个都标 `Requires: pip install sounddevice`** —— 你已经有。

### 4.1 具体动作

```bash
cd ~/jarvis-voice
.venv/bin/python -m pip install pywebrtc-audio
# 拿他们的示例（三个 wav 输出会落在当前目录）
mkdir -p /tmp/aec-probe && cd /tmp/aec-probe
curl -sLO https://raw.githubusercontent.com/strands-labs/pywebrtc-audio/main/examples/e2e_verify.py
.venv/bin/python e2e_verify.py     # ← 会出声 5 秒，人别说话
```

⚠️ **会出声。需确认环境允许**（你的项目红线 §10.8：跑测试前先确认音量/戴耳机）。

### 4.2 立刻能读出什么

| 文件 | 怎么读 | 得到 |
|---|---|---|
| `near_raw.wav` | 听 + 算能量 | **无 AEC 时残余有多大**（≈ ERL 的直观版） |
| `near_clean.wav` | 听 + 比能量 | **ERLE = 10·log10(raw/clean)** |
| 两者的比值 | — | **本机 AEC3 的天花板** |

**再加一步（关键）**：把 `near_raw.wav` 和 `near_clean.wav` **分别喂 SenseVoice**，看输出文本：
- 都转不出东西 → **残余过不了 ASR，AEC 的边际价值小**
- raw 转出东西、clean 转不出 → **AEC 有效，值得接**
- 两个都转出东西 → **AEC 不够，考虑 VPIO/硬件**

**这三条分叉直接决定后续全部路线**，而成本是 15 分钟。

### 4.3 为什么不用自己写

你的计划估计 1–2 天做集成 + 半天做 P0。**维护者的脚本把这半天的 P0 变成 5 分钟** —— 因为：
- 它用 **sounddevice**（你的库），不是 PyAudio
- 16kHz / 10ms 帧 —— 与你的 `SR=16000` 和 `vad_window` 同量级
- 它已经处理了 **far/near 的配对与时序**（见 §7.2 的实现约束）

**自己写的唯一增益**是"用自己的 `Player`/`MicStream` 而不是裸 sounddevice"。**但那可以在第二步做** —— 第一步先用他们的脚本拿到「这个算法在这台机器上行不行」，再决定要不要把它接进你的类。

---

## 5. 不要重开（已证伪或已判定）

| 项 | 为什么 |
|---|---|
| **重写状态机** | `session.py` 已是正确的世代令牌实现，与 HF compare-and-clear 同构。重写是纯风险 |
| **抄 Handy 的引擎抽象层** | 给多模型热切换用的；你只需要 SenseVoice 一个 |
| **抄 Handy 的线程模型** | Rust+Tauri 事件循环 vs 你的纯 Python 线程+队列。**你的「全栈阻塞式不混 asyncio」是刻意的正确选择** |
| **抄 Handy 的 LLM 后处理层** | 每句过云 LLM = +2 秒；你的硬需求是首音快（实测闲聊 ~1s） |
| **自建自适应打断模型** | `filler.py:4-5` 引用的调研结论：去 filler 收益为零。规则版够 |
| **`hold_or_toggle`（Handy）** | 不适用（这是 Handy 的设置，不是你的） |
| **DeepFilterNet3 当 AEC** | 它是降噪不是 AEC，没有 far-end 参考 |
| **等 ICASSP AEC Challenge 新榜** | 大概率停在 2023（你旧文判断，我未找到反证） |
| **OVC / LAEC+RES / E2E-AEC 直接可用** | 权重均未释出 |

---

## 6. 未验证清单（合并全部，按优先级）

| 优先级 | 项 | 成本 |
|---|---|---|
| **P0** | **本机 ERL + pywebrtc 的 ERLE 天花板** | **15 分钟**（§4） |
| **P0** | **残余喂 SenseVoice 是否幻觉**（三条分叉） | 15 分钟（§4.2） |
| P0 | 音频路径卡死的具体凶手（`_paused` / `_hd_gated` / `reopen()`） | 半天（Phase 0 观测） |
| P1 | **AEC 模式下打断检测连续运行**（从未测过的路径） | 接入后才能测 |
| P1 | 预滚缓冲 × `vad_min_snr=6.0` 的叠加效应 | 接入后 |
| P1 | VPIO 无 bundle 能否启动 + 前 3–6 秒丢音 | 各半天 |
| P2 | 文本 ONNX 端点的中文能力 | 需自建评测（你 B3 的 100 条样本） |
| P2 | `Player.pause()/resume()` 与 AEC 延时估计的交互 | 设计阶段 |
| P2 | pywebrtc-audio 的许可证（PyPI metadata 无 license 字段） | 看仓库 LICENSE |

---

## 7. 补充调研细节（本轮新增的一手事实）

### 7.1 pywebrtc-audio 的真实 API（🟢 README 直读，非转述）

| 事实 | 含义 |
|---|---|
| **`speech_probability` 总是可用**（三级回退：NS 谱估计 → AGC 的 RNN VAD → 轻量谱分析） | **比你的计划假设的更强** —— 关掉 NS/AGC 也拿得到，§2.3 第三层防线稳 |
| **处理顺序：HP filter → AEC → NS → AGC** | **AEC 在 NS 之前** —— NS 不会干扰 AEC 收敛 |
| **`stream_delay_ms` 默认 0 = 让 AEC3 自估**，给提示只是「helps it converge faster」 | **你的「扫 100/150/200 三档」可简化为：先跑 0，再试 100** |
| 采样率须 10ms 内有整数样本，否则「may reduce echo-cancellation quality」 | 16k = 160 samples ✅ 完美；**44.1k 必须重采样**（你计划已有 `resample_poly(x,160,441)` ✅） |
| 任意长度输入，内部切 10ms 帧，**末帧零填充，输出截回原长度** | **不用自己切帧** —— 消除一个实现风险 |
| **五类各自独立可用**（`EchoCanceller` 可单用） | **只开 AEC 不开 NS 是可行的** —— 若发现 NS 伤 ASR，可关 |
| 有 `AudioProcessor.reset()` | 模式切换/重置时要调 |
| 性能（厂商自测 🟡）：EchoCanceller 622µs/10ms = 161× realtime | 单流可忽略 |

### 7.2 ⚠️ 实现约束：far/near 的时序（这是唯一的真风险）

**WebRTC 官方用法注释**（`api/audio/audio_processing.h`，一手）明确了顺序：
```
// Render frame arrives bound for the audio HAL ...
//   apm->ProcessReverseStream(render_frame);
// ... Capture frame arrives from the audio HAL ...
//   apm->set_stream_delay_ms(delay_ms);
//   apm->ProcessStream(capture_frame);
```
→ **必须先喂 render（远端），再处理 capture（近端）。**

**好消息**：`DelayAgnostic` 是 WebRTC 的官方内建配置（一手）：
> Enables delay-agnostic echo cancellation. This feature relies on **internally estimated delays** between the process and reverse streams, thus **not relying on reported system delays**.

**坏消息**：far/near 严重失步时 AEC 会自我保护。`echo_cancellation.c`（一手）的注释：
> **For really bad systems, don't disable the echo canceller for more than 0.5 sec.**

→ 这解释了 Chromium issue 503521340「render reference all zeros → echo not cancelled」的机制：**参考信号错 = AEC 停用，且静默。**

**你的优势**：你的 far 参考是 `Player.write()` mirror —— 你知道**确切**在播什么，比任何通用 AEC 拿到的都好。**但 `AecGate` 必须做 far/near 的配对与缓冲**（两者在不同线程、不同速率）。这是 1–2 天估算里真正的工作量所在。**先跑 §4 的示例能验证这个配对在原理上成立。**

### 7.3 平台 AEC 免费（🟢）

- `kAudioUnitSubType_VoiceProcessingIO` 属 **Audio Toolbox**（系统框架，macOS 10.12+）
- **无 API key、无授权费、无需付费账号**
- ⚠️ **VoIP Entitlement 是另一回事**（后台 PushKit + CallKit，上架场景），与语音处理无关
- ⚠️ 唯一待验：JUCE 报 `AUVoiceIO can't set vp bypass state for null bundleID`，但同帖称纯 C++ 版无需 bundle

---

## 8. 并行推进的三条轨道

| 轨道 | 动作 | 成本 | 依赖 |
|---|---|---|---|
| **A. AEC 可行性** | §4 的 15 分钟测量 | 15 分钟 | 需允许出声 |
| **B. 卡死修复** | Phase 0 观测（`/api/state` + `gate_watchdog`）→ 复现 → 修 | 半天 | 无 |
| **C. 注入隔离** | `<transcript>` 包裹 + system prompt 第 12/13 条 | 1 小时 | 无 |
| ~~D. 回声护栏~~ | ✅ **已完成** | — | — |

**三条互不阻塞，B 和 C 随时能开始。**

---

*本文写于 2026-09-19，是本次会话调研的合并入口。§4 是明确的下一步；§7 是本轮新增的一手事实。*
