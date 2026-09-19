# JARVIS 项目完整交接文档

> ⚠️ **本文已部分过时**（写于 2026-09-13）。最新全量交接见 [`JARVIS-HANDOVER-20260915.md`](JARVIS-HANDOVER-20260915.md)。
> 本文仍有效的部分：第 5 节踩坑清单、第 8 节 Windows 运维红线。其余（架构、路线、TTS 选型）以新版为准。

> 面向：Windows 端 AI（或任何第一次接触本项目的人/agent）
> 写于：2026-09-13 深夜
> 目的：让你在不读几十轮对话的前提下，完整理解这个项目做了什么、为什么这么做、现在卡在哪、下一步怎么走

---

## 1. 项目是什么

**目标**：钢铁侠里的贾维斯——**听、执行、说**。

**原始需求（2026-08-14 立项）**：
1. 实时视觉 + 自然语音对话（像正常聊天，不是"按一下说一句"）
2. **综合情绪识别**（环境 / 对象 / 表情）
3. 后期：机器人本体 + 传动模块

**硬约束**：
- **本地跑，不付云端 API 费**（这是立项初衷——Gemini Live ~$0.02/min、OpenAI Realtime ~$0.25-0.35/min）
- 硬件：目标机是 **Windows 台式（RTX 5060 Ti 16GB）**，开发/前端是 **Mac（M1 Pro 16GB）**

---

## 2. 物理架构

```
┌─────────────────────┐         ┌──────────────────────────────────────┐
│  Mac (M1 Pro 16GB)  │  SSH    │  Windows (i5-12600KF / 5060 Ti 16GB) │
│                     │ 隧道    │                                      │
│  jarvis_omni.py     │◄───────►│  wrapper  :9060  (FastAPI, Python)   │
│  ├ 麦克风采集        │         │    └─► llama-server :19060 (C++)     │
│  ├ 双工循环          │         │         └─ MiniCPM-o 4.5 (int4)      │
│  ├ ASR (SenseVoice) │         │                                      │
│  ├ 播放器            │         │  Claude Code (headless, stream-json) │
│  └ CC 桥             │         │                                      │
└─────────────────────┘         └──────────────────────────────────────┘
```

**为什么跨机**：Mac 没有 NVIDIA GPU 跑不动 9B 模型；Windows 有 5060 Ti 但没有麦克风使用场景。两端通过 SSH 隧道连（`win` 是 SSH 配置里的别名，地址 10.20.70.180，校园网内网）。

**安全**：wrapper 与 C++ server 都绑 `127.0.0.1`，Mac 走 SSH 隧道访问 → 防火墙保持开启、校园网零暴露。

---

## 3. 关键决策的历史（为什么是现在这样）

### 3.1 为什么选 MiniCPM-o 4.5

| 候选 | 结论 |
|---|---|
| Gemini Live / OpenAI Realtime | 体验好但**要付费**，违背立项初衷 |
| Qwen3.8-27B | 27B 稠密 VLM，**无语音输出、非全双工** |
| Qwen3-Omni | 30B 本地要 79GB+ 显存，跑不动 |
| **MiniCPM-o 4.5** | **Apache-2.0 免费，9B 端到端全双工全模态**（同时看视频+听语音+说话+情绪识别），**是当时开源里唯一能本地跑全双工的**。情绪识别 MELD 52.4，超 GPT-4o-realtime 的 33.2 |

### 3.2 为什么走 llama.cpp 的 C++ 移植，而不是官方 Python 实现

**因为显存**：
- 官方权重是 bfloat16，9B ≈ **18GB** > 16GB 显存
- **官方不提供任何量化版本**
- llama.cpp 有 GGUF int4（≈6GB），是当时**唯一能塞进 16GB 的路径**

**代价**：吃第三方移植的 bug（见第 5 节，我们踩了一整轮）。

**⚠️ 事后验证**：这个选择在**性能上是对的**。2026-09-13 实测 Qwen3-TTS（只有 0.6B）在 Windows 上用 PyTorch 原生推理，**RTF 高达 4.6-8.3**（生成 1 秒语音要 5-8 秒），诊断是"GPU 利用率仅 4-13%、功耗 28-81W——**GPU 在睡觉**，瓶颈是逐 token 小 kernel 的**主机发射开销**（Windows WDDM）"。如果当初用官方 PyTorch 跑 9B，会直接跑不动。

### 3.3 为什么让 Claude Code 当"执行"的大脑

语音模型（9B）做不了复杂任务（联网搜索、读写文件、多步推理）。方案：
- **MiniCPM-o**：耳朵 + 嘴 + 闲聊
- **Claude Code**：干活（headless 子进程，`--input-format stream-json` 常驻）
- 任务结果**回灌**给 MiniCPM-o 播报（试图统一音色）——**这条路已证明走不通，见第 6 节**

参考实现：sreyas-endor/jarvis（常驻 `claude` 子进程模式）。关键发现：`--bare` 会跳过 plugin sync 和 CLAUDE.md 自动发现 → **内置 WebSearch/WebFetch 在 bare 下不存在**，必须显式挂 MCP。

---

## 4. 组件详解

### 4.1 Mac 侧

| 文件 | 作用 |
|---|---|
| **`jarvis_omni.py`**（~1100 行） | 主客户端。双工循环引擎 + 麦克风采集 + ASR 门控 + 播放器 + 任务路由 + 结果回灌 |
| **`claude_bridge.py`** | Claude Code 常驻桥。持久 `claude --input-format stream-json` 子进程，warm turn ~1-1.5s |
| **`run.sh`** | 一键启动：查 Windows 后端 → 建 SSH 隧道 → 探测双工模式 → 拉起客户端（日志 tee 到 `/tmp/jarvis-client.log`） |
| `jarvis_state.py` | 任务状态记录（小） |
| `jarvis_demo.py` | Phase 1 的最小闭环（SenseVoice + CC + `say`），保留作降级路径 |
| `test_*.py` | 见 4.3 |

**`jarvis_omni.py` 的核心循环（双工铁律）**：

```
每 1.0 秒一拍，严格交替：
  prefill(1s 音频或文本) ──► decode ──► 消费 SSE（边收边播）
```
- **静音也必须照发**——节拍一断，模型就退回 LISTEN 永不开口
- **块长必须 1.0 秒**——这是官方训练单元，改成 0.7s 会导致模型退化成重复循环（实测过）
- 插话 = `player.flush()` + `POST /omni/break`

### 4.2 Windows 侧

| 路径 | 作用 |
|---|---|
| `E:\jarvis-voice\app\minicpmo_cpp_http_server.py`（~2900 行） | FastAPI wrapper。转发给 C++、扫音频、管会话、注入人设 |
| `E:\jarvis-voice\app\run-wrapper.bat` | 启动脚本（环境变量 + 命令行参数） |
| `E:\jarvis-voice\app\llama-omni-new\` | **当前使用的**二进制（VS2022 编译，带自制补丁） |
| `E:\jarvis-voice\app\llama.cpp-omni\` | 旧二进制（回退用，未动） |
| `E:\jarvis-build\llama.cpp-omni\` | 编译源码树（v1.0.22 + 补丁） |
| `E:\jarvis-build\build_vs2022.bat` | 编译脚本 → `cmake --build build-vs2022 --config Release --target llama-server -j 8` |
| `E:\jarvis-voice\app\JarvisOmniServer.task.bak.xml` | 计划任务备份 |
| `E:\tts-test\` | TTS 实测环境（Qwen3-TTS 已判死刑但环境留着；Kokoro 可用） |

**服务由计划任务 `JarvisOmniServer` 托管**（LogonTrigger+20s / ExecutionTimeLimit PT0S 无限 / RestartOnFailure 3次·1分 / StartWhenAvailable / **StopOnIdleEnd false**）。

### 4.3 测试脚本（Mac 侧）

| 脚本 | 测什么 |
|---|---|
| `test_loop_structure.py` | **AST 结构回归**——防止"代码块缩进串位导致 ASR 每 10 拍才采样一次"那类语义 bug |
| `test_dedup_fix.py` | 音频送达（含打断/重开会话后的**文件名复用**场景） |
| `test_broadcast.py` | 播报完整度量化（产出音频 ÷ 文本预期） |
| `test_duplex_client.py` | 无麦双工代码路径 |
| `test_asr.py` | SenseVoice 单独测 |

---

## 5. 踩过的坑（完整清单，**别再踩**）

### 5.1 🔴 双工协议类

| 坑 | 根因 | 状态 |
|---|---|---|
| **节拍一断模型就不开口** | 双工要求严格交替 prefill→decode，静音也要发 | 已理解，遵守 |
| **块长改 0.7s 导致模型退化成重复循环** | `chunk_ms` 直接推导 mel 单元（700ms=70帧 vs 训练 100 帧）→ 分布外 | 已回退 1.0s，**别改** |
| **`listen_prob_scale` 是错的工具** | 全局常数偏置，分不清"该继续"和"该闭嘴"。0.3 档换来严重自言自语 | 回退 1.0 |
| **`force_listen_count` 我们设 3，官方是 0** | 每次会话开局白等 3 秒；且该分支有上游 PR #94 修过的"不写 KV"bug | **未修，待办** |

### 5.2 🔴 C++ 移植的缺陷

**我们的二进制 = `tc-mb/llama.cpp-omni` tag `v1.0.22`（61d8393, 2026-04-29）**，之后该分支冻结。上游后续修复都在不兼容的 master 上。

| 缺陷 | 根因 | 状态 |
|---|---|---|
| **缺 listen 守门** | 官方 Python `modeling_minicpmo.py:3237-3239` 有 `if last_id == listen_token and not current_turn_ended: last_id = tts_bos_token`，**C++ 所有分支都没有**。后果：模型一句话说一半就切 LISTEN | **已自行补丁并编译**（只改 `omni.cpp` 两处，日志可见 `[守门] 句中 <|listen|> -> tts_bos`） |
| **`length_penalty` 缺失** | 官方 demo 用 1.1（注释原文："suppress turn_eos → model 更不容易结束当前 turn，倾向更长输出"），我们用 1.0 = 完全不压 | **已改**（日常 1.1，播报 1.5） |
| **`top_k` 用错** | 我们传 100（那是**非双工**路径的值），官方双工 demo 用 **20** | 未改，待办 |
| **wav 文件名复用 → 永久丢音** | 文件名 = `wav_{wav_turn_base + wav_idx}`；打断时 `wav_idx` 归零而双工**不**递增 `wav_turn_base`，init 时归零 → 编号回到起点。而 wrapper 按**文件名**去重 → 新音频判"已发送"永久丢弃 | **已修**（见 5.4） |

### 5.3 🔴 我（Claude）自己犯的错

| 错 | 教训 |
|---|---|
| **代码块缩进串位** | 插入诊断块时把"用户语音收集 + ASR 触发"整块掉进了 `if beats >= 10:` → 每 10 秒才采样一次用户的话，任务主线全废。`py_compile` 抓不到（语法合法语义全错） | **已加 AST 结构回归测试** |
| **多次二手引用出错** | 把错文件名、错行号、编造的 issue 内容写进交接文档。**这个项目上至少三次** | 规矩：**交付物里的出处/行号一律先亲自核实** |
| **误判"退化"** | 把"音频没送达"（wav 去重 bug）误判成"模型上下文退化"，差点去改滑动窗口 | 规矩：**先区分"模型没说"和"说了没传到"**，服务端日志能区分 |

### 5.4 ✅ 已修复的重要问题（按时间）

1. **wrapper 每轮固定死等 1.0 秒** → 双工节拍被拖到 1.17s/块 > 1s 音频块 → 播放缓冲持续抽干。改为双信号等待（`generation_done.flag` + 静默窗口），降到 **0.46s/块**
2. **回声自我打断** → 外放被麦克风拾到 → 判插话 → 掐断模型。修法：助手出声时门槛抬到 0.15 + 需连续 2 块 + `recently_spoke(0.3)` 覆盖混响
3. **ASR 自我循环** → 助手的声音被识别成"用户说的"。修法：`_excise_echo` 按播放时刻挖除采样
4. **音频回传（2026-09-13 深夜重构）**：
   - **旧**：C++ 写 wav 文件 → wrapper 每 50ms `os.listdir` 扫目录 → 按文件名去重
   - **新**：C++ 内存队列 `audio_queue` → `POST /v1/stream/audio_poll` 取走 → 直塞 SSE
   - **根除了一整类 bug**：文件名复用、写一半被读、先标记后入队丢音、轮询延迟抖动

### 5.5 🔴 已证伪 / 走不通的（别再试）

- **`max_new_speak_tokens_per_chunk` 调大**：我们 26，官方 20，**已经更宽松**。不是瓶颈
- **`length_penalty` 在官方双工路径不存在**：C++ 那个分支是移植时自加的，但**有用**（作用于 `turn_eos`）
- **`force_speak` 这类补丁**：全 GitHub 搜不到，是我们的原创需求，无先例
- **comni-2.0 不是升级路线**：属于 master 那套**另一套双工引擎**（DuplexPipeline + WebSocket），维护者 issue #74 原话："two different duplex engines… locking to feat/web-demo for now, rather than backporting across incompatible architectures"
- **"文本 → speech token 的桥"不存在**：调研确认这类工具要专门训练，没有现成实现

---

## 6. 🔴 现在的核心问题：结构性障碍

### 6.1 已证实的事实

**端到端语音模型（realtime model）结构上无法"朗读指定文本"。**

**LiveKit 官方文档原文**（https://docs.livekit.io/agents/models/realtime）：

> *"Realtime models don't offer a method to directly generate speech from a text script, such as with the `say` method. You can produce a response with `generate_reply(instructions='...')` but **the output isn't guaranteed to precisely follow any provided script**. If your application requires the use of specific scripts, **consider using the model with a separate TTS instance instead**."*

**我们的实测完全一致**：

```
[回灌] 交给 Omen Alpha 播报: I'll search for today's news about Dublin
[Omen Alpha] 都柏林今天
🤖 '的天气预报显示晴朗，气温适中。没有特别新闻事件哦。'   ← 自己编的，跟原文无关
```

**它只会"回应"，不会"朗读"。** 我们之前调的 `length_penalty`、人设措辞、覆盖率松手，全是在给一条不存在的路铺砖。

### 6.2 由此产生的连锁问题

| 症状 | 机理 |
|---|---|
| **回复前几个字被吞 / 内容不对** | 上述结构性问题的表现 |
| **卡顿（播放断口）** | 每块音频 1.000s，到达间隔中位数 **994ms** → 供给刚好 1.0x、**零余量**，第一块播完第二块还没到就断 |
| **无法打断** | 播放与采集**跨进程**（甚至跨机），我们手写的"按播放时刻挖除麦克风采样"本质上是在**手工重造 AEC**。社区文章诊断（runedge.ai）：*"The AEC reference lives in the wrong place… aligning them across process boundaries and Python's scheduling jitter is exactly the alignment problem that makes echo cancellation fail"* |
| **答非所问** | 9B 模型能力上限（问"心情怎么样"答"我在呢，随时准备帮你"） |

### 6.3 TTS 选型实测（2026-09-13）

| | Qwen3-TTS 0.6B | **Kokoro 82M** |
|---|---|---|
| 首块延迟 | **无流式接口** | **128–158 ms** |
| 26 字总耗时 | 29,129 ms | **128 ms** |
| RTF | 4.6–8.3 ❌ | **0.02–0.12** ✅ |
| 显存 | 2.51 GB | **0.81 GB** |
| 中文 | ✅ | ✅（misaki-zh） |
| 克隆 | ✅ | ❌（有预设音色，可后接 RVC） |

**Qwen3-TTS 在这台 Windows 机上判死刑**：GPU 利用率只有 4-13%，瓶颈是逐 token 小 kernel 的**主机发射开销**（Windows WDDM）。除非迁到 WSL2/Linux，否则不通。

**显存账本**：MiniCPM-o 11.5G + Kokoro 0.81G + 系统 1.5G ≈ **13.8G / 16G → 混合方案成立** ✅

**免费云 TTS 全部不支持克隆**：Edge-TTS（作者自认 *"a very bad idea to use this library for anything serious"*，微软会封）、Google 免费档（**要绑卡**、超额自动扣费）。

**⚠️ 被忽略的一个维度**：Mac 侧有 Python 3.12/3.13 + uv，内存 56% 空闲，**PyTorch 2.14 支持到 3.14**。**TTS 完全可以跑在 Mac 客户端**——零 Windows 显存、零音频网络传输、延迟更低。

---

## 7. 正在做的架构决策

### 三条路

| | 架构 | 优点 | 代价 |
|---|---|---|---|
| **A** 混合 | MiniCPM-o 管听+闲聊，独立 TTS 管说 | 保留双工无缝感 + 情绪 | 双机两模型，链路最长 |
| **B** 全三段式·Mac | **Mac：SenseVoice + TTS + CC** / Windows 只跑 CC | **最简单**：零 SSH 音频传输、零显存冲突、**打断 = 停 TTS**（本地进程天然对齐） | 丢双工无缝感 |
| C 全三段式·Win | 全在 Windows | 集中 | 显存 + 网络传输都不划算 |

### 关键论据

**走 B 的理由**：用户原始需求里真正难满足的两项（自然对话、能打断），**双工并没有真的给**——
- 打断：跨进程手工 AEC，注定不可靠
- 自然对话：9B 会答非所问、会改写内容

**而且 B 能拿回音色克隆**：CosyVoice2-0.5B 是"双向流式（首包 ~150ms）+ 零样本克隆"一体，之前卡在"4-6GB 与 9B 抢显存"，**挪到 Mac 就没这个问题**。

**不丢情绪识别**：SenseVoice **本身就带 SER**（官方 README：*"speech emotion recognition (SER), and audio event detection (AED)"*），Mac 上已通过 `sherpa_onnx` 在用。真正会丢的只有"视频/表情情绪"（MiniCPM-o 的 vision 编码器），那是项目 Phase ④ 的事。

**可借鉴的架构范本**：`fluxions-ai/vui`（★763，2026-09-12 活跃）——它把"听→执行→说"全做成了成品，特别是：
- **句级 TTS 分块 + backpressure**（治卡顿）
- **并行 "thoughts" LLM 说 filler**（"让我查一下…"）保持对话
- **CC 任务侧车**：结果 POST 回来**直接朗读**
- **TTS 跑 CPU**（不抢 GPU）

⚠️ 但它 **TTS 是英文专用**（HF 模型卡 `language: [en]`），ASR 默认也是 `.en` —— **不能直接用，但架构值得抄**。

---

## 8. Windows 端的职责边界

**你负责**：
- 跑 wrapper + C++ server（计划任务托管）
- **编译**（`E:\jarvis-build\build_vs2022.bat`）
- 配合实测、报数据

**注意事项**：
- ⚠️ **改任何东西前先备份**（我们的惯例是 `.bak-<日期>` 后缀）
- ⚠️ **不要动 `E:\jarvis-voice\app\llama.cpp-omni\`**（回退用）
- ⚠️ `.ps1` 脚本**必须 ASCII-only**（PowerShell 5.1 按 GBK 读 UTF-8 会乱码）；`.bat` 需要 CRLF
- ⚠️ **杀进程必须按命令行点名**，不能用 `/IM python.exe` 全杀：
  ```powershell
  Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*minicpmo_cpp_http_server*' -or $_.Name -eq 'llama-server.exe' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
  ```
  （`schtasks /End` 只解注册不杀进程树，会留孤儿 llama-server 占着 19060）
- ⚠️ `/health` 通 **不等于**模型就绪——wrapper 在 uvicorn 启动**之前**做完预初始化，所以通=就绪；**但反过来，`/health` 通也不代表 C++ 子进程还活着**（健康检查不反映子进程死活，我们被这个坑过）

**服务操作**：
```powershell
schtasks /Run /TN JarvisOmniServer     # 启
schtasks /End /TN JarvisOmniServer     # 停（记得补点名杀，见上）
```

---

## 9. 关键路径速查

**Windows**：
```
E:\jarvis-voice\app\minicpmo_cpp_http_server.py     wrapper（Mac 侧有同名副本，改动要同步）
E:\jarvis-voice\app\run-wrapper.bat                 启动脚本
E:\jarvis-voice\app\llama-omni-new\build\bin\Release\   当前二进制（8 个文件）
E:\jarvis-voice\app\server.log                      运行日志
E:\jarvis-build\llama.cpp-omni\                     编译源码（v1.0.22 + 补丁）
E:\jarvis-build\build_vs2022.bat                    编译
E:\tts-test\                                        TTS 实测环境
```

**Mac**：
```
~/jarvis-voice/jarvis_omni.py                       主客户端
~/jarvis-voice/minicpmo_cpp_http_server.py          wrapper 副本
~/jarvis-voice/run.sh                               启动
/tmp/jarvis-client.log                              客户端日志
~/Obsidian/SecondBrain/Projects/实时视觉语音机器人.md   项目主笔记（740 行完整历史）
```

**启动**：Mac 上 `cd ~/jarvis-voice && ./run.sh`（会自动查后端、建隧道、探模式、拉客户端）

**C++ 关键参数**（wrapper 传给 C++）：
- `--top-k 100`（**应为 20**，待改）
- `--temp 0.7 --top-p 0.8 --min-p 0.0 --repeat-penalty 1.05 --repeat-last-n 512`
- 运行时可通过 `POST /v1/stream/update_session_config` 改：`listen_prob_scale` / `force_listen_count` / `max_new_speak_tokens_per_chunk` / `tts_temperature`（**注意：它会清 KV**）
- `POST /v1/stream/decode` 的请求体可带 `length_penalty`（**持久设置，不传不会恢复默认**）

**新增端点（2026-09-13）**：`POST /v1/stream/audio_poll` → `{"success":true,"chunks":[<base64 int16 PCM>...],"count":N}`

---

## 10. 待办清单

**高优先级**：
1. **架构决策**（第 7 节三条路）——需要用户拍板
2. 在 Mac 上实测：SenseVoice 现有能力、Kokoro/CosyVoice2 的安装与首包延迟
3. `top_k` 100 → 20（对齐官方双工 demo）
4. `force_listen_count` 3 → 0（官方默认；且该分支有"不写 KV"bug）

**中优先级**：
5. 抄 vui 的三个设计：句级 TTS 分块 + backpressure、并行 filler LLM、VAD 驱动轮次
6. 试听 Kokoro 的 `v1.1-zh` 中文专训版（100 名中文专业发音人数据，音质可能更好）

**已封存**：
- Qwen3-TTS（环境留在 `E:\tts-test`，哪天迁 Linux/WSL2 再复活）
- 用双工模型朗读文本（结构性不可能）

---

## 11. 给接手者的三条告诫

1. **别信二手引用**。这个项目上至少三次因为转抄别人的结论（错文件名、错行号、编造的 issue 内容）而走弯路。**任何出处、行号、版本号，先亲自核实再说。**
2. **先区分"模型没说"和"说了没传到"**。我们在这上面误判过一次——把"音频没送达"当成"模型上下文退化"，差点去改核心的滑动窗口。**服务端日志能区分这两者。**
3. **别在"结构性不可能"的路上调参**。花一小时确认"这条路存在吗"，比花一周调参数值得。**这个项目最大的教训就是：我们用一个月验证了一个本该第一天用半天验证的假设。**
