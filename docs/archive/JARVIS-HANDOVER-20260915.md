# JARVIS 全量交接文档（2026-09-15）

> **面向**：任何一个第一次接手本项目的 AI（Claude Code / ZCode / 其他）或人。
> **目的**：让你不读几十轮对话，就能完整接手——知道这项目要什么、现在跑到哪、为什么长这样、哪些路已经证伪、下一步该干什么。
> **作者**：Mac 侧 Claude Code（Owen 的会话），2026-09-15 22:30 GMT+1 写。
> **前身**：`docs/JARVIS-HANDOVER.md`（2026-09-13，Windows 向）——**部分内容已过时**，以本文为准。

**引用规矩（本项目血泪换来的）**：本文所有结论都标了来源或验证状态。**⚠️ 标记 = 写本文时未亲自核实**。转抄任何行号/版本号/issue 内容前，请自己再核一遍（这个项目因为二手引用出错至少三次）。

---

## 1. 项目目的与需求

### 1.1 一句话

钢铁侠里的贾维斯：**听得见、能干活、说得出**。本地跑，不付云端 API 费。

### 1.2 原始需求（2026-08-14 立项）

1. **实时视觉 + 自然语音对话**——像正常聊天，不是"按一下说一句"
2. **综合情绪识别**（环境 / 对象 / 表情）
3. 后期：机器人本体 + 传动模块

### 1.3 硬约束

| 约束 | 内容 |
|---|---|
| **成本** | 本地推理，不付云端 API 费（立项初衷：Gemini Live ~$0.02/min、OpenAI Realtime ~$0.25-0.35/min 太贵） |
| **硬件** | 目标机 = Windows 台式（i5-12600KF / **RTX 5060 Ti 16GB**）；开发机 = Mac（M1 Pro 16GB，无 NVIDIA） |
| **模型** | 必须开源可本地跑（见 §4.1 选型） |
| **体验底线** | 能打断；不会自说自话；被叫到会回应 |

### 1.4 隐性验收口径（从历史对话里提炼的）

- 响应要"像人"：首音延迟越低越好，目前前台模型首音 **1.10s**（实测）
- 音色统一：所有输出（闲聊、任务结果）用同一个声音
- 静默期不能自言自语（这条花了整整一周才定位，见 §6）

---

## 2. 现场状态（2026-09-15 22:30 **实测**）

> 🛑 **2026-09-15 23:5x 状态变更**：Owen 要求停机，Windows 侧**所有进程已停止**（guard → 计划任务 → 按命令行点名杀；复查 CLEAN，9060/8006/22400/19060 均无监听，显存从 11,672 MiB 降到 **~1,450 MiB**）。
> ⚠️ **停机后它们自己回来了**，两次：23:58:00 `DemoWorkerGuard` 把 guard 重新拉起（父进程 = `svchost.exe`，即任务计划宿主）。→ **5 个计划任务现已全部 `/Disable`**，越过 23:59:00 的触发点复测为 CLEAN。
> **要恢复任一套栈，必须先 `/ENABLE` 对应任务**（`schtasks /Change /TN <名> /ENABLE`），否则起来也会被半路掐掉。
> 下面 2.1–2.3 描述的是**停机前**的现场，价值在于**为什么会长成这样**；重新拉起前请先读 §6.5（看门狗死循环）。

### 2.1 Windows（10.20.70.180，SSH 别名 `win`）停机前跑着**两套栈**

> 本节数据：2026-09-15 22:16–22:40 经 `ssh win` 实测（只读，未动任何进程）。

**两套栈的顶层进程全部在 22:16:20 同时启动**（即整套栈在那个时刻被重启过一次）：

**栈 A · 自研 HTTP wrapper（旧主线，现为回退）**

| 进程 | 端口 | 说明 |
|---|---|---|
| `minicpmo_cpp_http_server.py`：PID **11312**（cmd 启动器）→ **24476**（真解释器） | 127.0.0.1:**9060** | `--duplex`，日志可见 v4 看门狗（闲置 60s / 剪枝静默 8s） |
| `llama-server.exe`：**PID 每分钟都在换**（观察到 14376 → 11176 → 14440） | 127.0.0.1:19060 | `llama-omni-master` 构建。**被看门狗每 60s 杀一次重启一次**——详见下方红框 |

**栈 B · 官方 Comni demo（新探索，Plan B）**

| 进程 | 端口 | 说明 |
|---|---|---|
| `gateway.py`：PID **2020** → **9156** | **0.0.0.0:8006** | 官方网关（`E:\jarvis-build\miniCPM-o-demo-comni`，venv = `demo-venv`） |
| `worker.py`：PID **21992** → **14140** | **0.0.0.0:22400** | `/health` 回报 `worker_status: idle / model_loaded: true / kv_cache_length: 21` |
| `llama-server.exe`：PID **14632**（22:16:22，由 worker 拉起） | 0.0.0.0:19060 | `E:\jarvis-build\llama-demo\` 构建。**真正装了模型的那个**（宿主内存 5,926 MB；GPU 已用 11,402 / 16,311 MiB） |

**✅ 已澄清**：之前看到的"每个组件两个相同 PID"**不是重复启动**——是 venv 的 `python.exe` 启动器 → 真实解释器的**父子进程**，属正常现象。

**🔴 真正的元凶：wrapper 的「会话纪律看门狗」在空载时死循环重启引擎（已查实，2026-09-15 23:55）**

栈 A 的引擎 PID 一直在变（我先后看到 14376 → 11176 → 14440），**不是偶发，是每 60 秒一次的死循环**：

| 证据 | 数值 |
|---|---|
| `E:\jarvis-voice\MASTER-VERIFY\migrate-live.log` 写入区间 | 22:16:38 → 23:52:55（**96.3 分钟**） |
| `POST /omni/session/reset` 次数 | **96** |
| `启动 C++ llama-server` 次数 | **98** |
| 看门狗日志行数 | 193 |

**96 次 / 96.3 分钟 = 恰好每分钟一次。**

**机制**（源码 `E:\jarvis-voice\app\minicpmo_cpp_http_server.py` ~L319 `session_watchdog()`）：

```python
_IDLE_RESET_SEC = float(os.environ.get("OMNI_IDLE_RESET_SEC", "60"))
...
if _pruned_flag and idle >= _RESET_GRACE_SEC: reason = "剪枝后静默"
elif idle >= _IDLE_RESET_SEC:                 reason = f"闲置 {idle:.0f}s"
if reason:
    resp = requests.post(f"http://127.0.0.1:{port}/omni/session/reset", timeout=200.0)
    _last_activity_ts = time.time()          # ← 计时从"端点返回"重新开始
```

1. `mark_session_activity()` **只在有请求进来时**（用户语音 / 模型出声 / 播报）刷新时间戳 —— **没有客户端连接时永远无人刷新**
2. 于是 60s 后必然触发 `闲置 60s → 重置会话`
3. `/omni/session/reset` 因为「进程内快重置会让音频变哑」已被回滚成**整进程重启**（杀 llama-server + 重载 11GB 模型，15–18s），**但端点立即返回**（`full reinit 已在后台开始`）→ 计时立刻重新开始 → 60s 后再来一次

**后果**：整晚引擎在被反复杀/重载（11GB 模型每分钟过一次），GPU 反复吞吐。**这也同时解释了两件事**：① 22:32 那个只有 179MB 的引擎（刚启动还没加载完就被下一次重启干掉）；② `server.log`（JarvisOmniServer）22:16:38 后再无写入 + 那条 `[Errno 10048] bind 9060` —— **两个任务都抢 9060，JarvisMigrateTest 赢（它就是 migrate-live.log 的写入者），JarvisOmniServer 绑不上直接退出**。

**两个 bug**：
1. **看门狗没有「是否存在活跃会话」的判断** → 空载也重置，等于自转
2. **重手段配快节奏**：整进程重启（15–18s）是为「偶发重置」设计的，却按 60s 的 idle 节奏调用（进程内快路径才是为这个节奏设计的，而它被证明会让音频变哑）

⚠️ **勘误**：本文档早先一版把 22:32 的引擎更替归因为"第二个 wrapper 实例的收尾逻辑杀了引擎"——**该结论已被上述证据推翻**。

**⚠️ 结构性冲突（不是偶发）**：两套栈的引擎**都硬编码 19060**——demo 的 `config.py` 原文「C++ llama-server 端口：默认 19060 + gpu_id」。**两套栈不可能长期共存**，必须二选一。

**计划任务（共 5 个，全部已 `/Disable`，见 §2 顶部停机说明）**：

| 任务名 | 动作 | 触发器 |
|---|---|---|
| `JarvisOmniServer` | `E:\jarvis-voice\app\run-wrapper.bat` | LogonTrigger（+ RestartOnFailure 3次·1分） |
| `JarvisMigrateTest` | `E:\jarvis-voice\MASTER-VERIFY\mig_run.bat` | TimeTrigger |
| `DemoGateway` | `E:\jarvis-build\run-demo-gateway.bat` | TimeTrigger |
| `DemoWorker` | `E:\jarvis-build\run-demo-worker.bat` | TimeTrigger |
| **`DemoWorkerGuard`** | `E:\jarvis-build\run-demo-guard.bat` | TimeTrigger |

⚠️ **注意 `DemoWorkerGuard` 这个名字** —— 我第一轮清理时按"DemoGuard"猜名去查，查不到就以为不存在，**结果 23:58:00 它把 guard 又拉起来了**（guard 的 .bat 是死循环，会把 worker 从 error 里重启 → 等于整栈保活）。**教训：任务名不要猜，用 `Get-ScheduledTask` 按动作路径枚举。**

### 2.2 Mac 侧

- 客户端 `jarvis_omni.py` 已改为 **`OMNI_TRANSPORT=ws`（默认）**，走官方 Comni WS 协议（新文件 `comni_ws.py`）
- **SSH 隧道当前未建立**（`pgrep -f "ssh -N -L"` 无结果）——`./run.sh` 会自己建
- 最后一次客户端实跑：**09-15 03:31**，日志 `/tmp/jarvis-client.log`（结论见 2.3）

### 2.3 哪条链路是活的（最后一次实跑证据）

**Plan B（WS）链路本身是通的**——这是当晚的进展：

```
[comni] 连接 ws://127.0.0.1:8006/ws/duplex/adx_jarvis
[comni] prepared（人设 621 tokens, 会话 20260915_033054_adx_jarvis）
[omni] init ok (官方 WS)
[reset] 会话已重置（官方 full_reinit，0.3s）
```

**但同一次跑暴露了三个问题**（见 §6 问题 1、2、3）：

1. 模型**说英文且跑偏**：`<|tts_bos|>Hello! I was wondering if you could help me write a rap.`
2. **控制 token 泄漏到文本**（`<|tts_bos|>` 被当正文打出来）
3. `kv_cache_length` 每拍 +11（32→43→54→124→137）——**静默期污染 KV 的现象在官方栈上同样存在**

**副产品（重要）**：官方 WS 每个事件都带 `kv_cache_length` / `cost_*_ms` / `wall_clock_ms` / `is_listen` / `end_of_turn`，**诊断能力比 HTTP wrapper 强一个量级**——以前只能靠 grep 服务端日志猜。

### 2.4 🔴 已知安全问题（写本文时实测）

| 端口 | 绑定 | 防火墙 | 结论 |
|---|---|---|---|
| 19060 | 0.0.0.0 | ✅ 有 Block 规则 `JARVIS raw llama-server API (19060)` | 安全（Mac 直连实测不通） |
| 9060 | 127.0.0.1 | —（本就只听 loopback） | 安全（Mac 直连实测不通） |
| **8006** | **0.0.0.0** | ❌ **无规则** | 🔴 **Mac 不经隧道直连 `http://10.20.70.180:8006/health` 返回 200** |
| **22400** | **0.0.0.0** | ❌ **无规则** | 🔴 **直连返回 200，且回的是完整 worker 状态** |

⏱ **复测记录**：2026-09-15 22:31（Owen 重启防火墙之后）从 Mac 再测一次 —— **8006 / 22400 仍然可达**；Windows 上 `Get-NetFirewallRule -Action Block` 依然只有 19060 那一条。**即：目前没有任何东西挡住这两个端口。**

**官方 Comni 栈默认绑 `0.0.0.0` 且无鉴权**——校园网内任何人可开双工会话、驱动机器人、听它的输出。之前的自研栈是刻意绑 loopback 的（`--host 127.0.0.1`），官方栈没有这个习惯。**处理建议**（未做，等 Owen 拍板）：

```powershell
New-NetFirewallRule -DisplayName "JARVIS block Comni gateway (8006)" -Direction Inbound -Action Block -Protocol TCP -LocalPort 8006
New-NetFirewallRule -DisplayName "JARVIS block Comni worker (22400)" -Direction Inbound -Action Block -Protocol TCP -LocalPort 22400
```

（Windows 防火墙规则优先级：**显式 Block > 显式 Allow > 默认**，所以即使有 python.exe 的 Allow 规则也会被压住。SSH 隧道走的是 Windows 本机 loopback，不受入站规则影响——19060 已是这个模式，隧道使用正常。）

---

## 3. 架构

### 3.1 全貌

```
┌───────────────────────── Mac (M1 Pro) ─────────────────────────┐
│  jarvis_omni.py                                                │
│   ├ MicCapture（sounddevice，1.0s 块，有界队列丢最老）            │
│   ├ 双工循环（严格交替 prefill→decode，见 §3.3）                  │
│   ├ SenseVoice ASR（并行转写，用于任务路由）                       │
│   ├ DuplexPlayer（边收边播，含回声地板/AEC 近似）                  │
│   └ claude_bridge.py ←→ `claude --input-format stream-json`     │
└───────────────┬─────────────────────────────────────────────────┘
                │ SSH 隧道（win，10.20.70.180）
┌───────────────▼──────────────── Windows (5060 Ti 16GB) ─────────┐
│  【Plan A】minicpmo_cpp_http_server.py :9060（自研 wrapper）      │
│            └→ llama-server :19060（master + 4 补丁）             │
│  【Plan B】gateway.py :8006 → worker.py :22400                  │
│            └→ llama-server :19060（llama-demo 构建，持 GPU）      │
│            模型同为 MiniCPM-o 4.5 GGUF Q4_K_M（ctx 8192）        │
└─────────────────────────────────────────────────────────────────┘
```

### 3.2 两条路线的关系

- **Plan A**：自研 wrapper。为了绕开 C++ 移植的缺陷和补 text 通路，写了大量补丁（10 处 wrapper + 4 处 C++）。
- **Plan B**：直接跑**官方 MiniCPM-o-Demo（Comni）**的 gateway/worker，客户端只实现官方 WS 协议（`comni_ws.py`）。**分工差异**：音频取回/排序去重/人设注入/会话生命周期/看门狗全由官方 worker 负责，客户端只管"按 1 秒发块 + 播放"。

**切 Plan B 的理由（⚠️ 该决策发生在 09-15 凌晨，未经我核实原始讨论，以下为从代码/日志反推）**：官方 demo 的会话纪律（每会话 `full_reinit`、pause/闲置释放、KV 剪枝当结束信号）恰好是我们花一周手搓的东西；且官方栈自带 `kv_cache_length` 等遥测。**代价**：自研 wrapper 上 10+ 处补丁的经验/修复在 Plan B 上需要重新验证是否适用。

### 3.3 双工协议的铁律（**改一个字都要先读这段**）

1. **每 1.0 秒一拍，严格交替** `prefill → decode → 消费输出`。不是"喂完音频再猛敲 decode"（那是单工节奏，模型永远不开口）。
2. **静音也必须照发**——节拍一断，模型退回 LISTEN 不再开口。⚠️ 但 2026-09-15 实测反过来也成立：**长时间静音节拍会污染 KV**（连送 ~15 拍就"失聪"）→ 现行折中：静默 >6s 暂停送节拍（`IDLE_PAUSE_SEC`），开口自动恢复。
3. **块长必须是 1.0s**。这是官方训练单元——改成 0.7s 会让模型退化成重复循环（实测过）。改 `chunk_ms` 会直接改变 mel 帧数（700ms=70 帧 vs 训练 100 帧）→ 分布外。
4. **打断** = 停播 + 通知服务端（Plan A 是 `POST /omni/break`；Plan B 是下一块带 `force_listen=true`）。

---

## 4. 决策史（为什么是现在这样）

### 4.1 为什么是 MiniCPM-o 4.5（2026-09-13 复核，结论仍有效）

| 候选 | 裁决 |
|---|---|
| Gemini Live / OpenAI Realtime | 体验好但**要付费**，违背立项初衷 |
| Qwen3-Omni-30B-A3B | 权重 30B，INT4 约 20GB，**16GB 装不下**，且无原生全双工 |
| SALMONN-omni（NeurIPS 2025） | **权重从未发布**（GitHub issue #113/#119 催更一年仍 open），且纯语音无视觉 |
| **MiniCPM-o 4.5** | **Apache-2.0、9B、端到端全双工全模态**（同时看视频+听语音+说话+情绪），INT4 仅 11GB，llama.cpp-omni 生态活跃。情绪识别 MELD 52.4 > GPT-4o-realtime 33.2 |

### 4.2 为什么走 llama.cpp 的 C++ 移植

**因为显存**：官方权重 bfloat16 9B ≈ 18GB > 16GB；**官方不提供任何量化版**。llama.cpp GGUF int4 ≈ 11GB 是当时唯一能塞进 16GB 的路径。
**事后验证对了**：2026-09-13 实测 Qwen3-TTS（仅 0.6B）用 PyTorch 在 Windows 上 RTF 高达 4.6-8.3（GPU 利用率 4-13%），若当初用官方 PyTorch 跑 9B 会直接跑不动。
**代价**：吃第三方移植的 bug（缺 listen 守门、wav 编号复用丢音等，见 §7.2）。

### 4.3 为什么让 Claude Code 当"执行"的大脑

9B 语音模型做不了复杂任务（联网搜索、读写文件、多步推理）。分工：

- **MiniCPM-o**：耳朵 + 嘴 + 闲聊 + 情绪
- **Claude Code**（headless 常驻子进程，`--input-format stream-json`）：干活，warm turn ~1-1.5s
- 任务结果回灌给 MiniCPM-o 播报（**这条路已证伪，见 §7.4**）

**关键坑**：`--bare` 会跳过 plugin sync 和 CLAUDE.md 自动发现 → **内置 WebSearch/WebFetch 在 bare 下不存在**，必须显式挂 MCP（项目里用 `mcp-search.json`）。

### 4.4 TTS 选型实测（2026-09-13，Windows 本机）

| | Qwen3-TTS 0.6B | **Kokoro 82M** |
|---|---|---|
| 首块延迟 | 无流式接口 | **128–158 ms** |
| 26 字总耗时 | 29,129 ms | **128 ms** |
| RTF | 4.6–8.3 ❌ | **0.02–0.12** ✅ |
| 显存 | 2.51 GB | **0.81 GB** |
| 克隆 | ✅ | ❌（预设音色，可后接 RVC） |

Qwen3-TTS 在这台 Windows 上判死刑（GPU 在睡觉，瓶颈是 WDDM 下逐 token 小 kernel 的主机发射开销；迁 WSL2/Linux 可复活）。免费云 TTS 全部不支持克隆。
**显存账本**：MiniCPM-o 11.5G + Kokoro 0.81G + 系统 1.5G ≈ 13.8G / 16G → 混合方案理论上成立。

### 4.5 master 迁移（2026-09-14 完成）

- 旧：`v1.0.22`（2026-04-29，该分支后冻结）+ 3 处本地补丁
- 新：上游 `master`（+260 commits），我们的补丁 3 项被上游取代（length_penalty / 滑窗 / wav 编号根因）
- **崩溃根因是上游 bug**：`server-omni.cpp` 悬垂引用，**一行修复**（详见 `E:\jarvis-voice\MASTER-VERIFY-RESULT-02.md`）
- 现役：`E:\jarvis-voice\app\llama-omni-master\`（master + 4 处 C++ 补丁）

### 4.6 采样参数现状

- 日常 `length_penalty = 1.1`（官方 demo 默认），播报 1.5
- `top_k 100 → 40` 待做（原计划对齐官方双工 demo 的 20，一次只改一参）
- 双工 TTS 采样**写死在 C++**（temp 0.8 / top_p 0.85 / top_k 25，CLI 改不动）

---

## 5. 文件地图

### 5.1 Mac 侧 `~/jarvis-voice/`（35 项）

| 路径 | 作用 | 备注 |
|---|---|---|
| **`jarvis_omni.py`**（1430 行） | 主客户端：双工循环 + 麦克风 + ASR + 播放器 + 任务路由 + 回灌 + 护栏 | 核心文件 |
| **`claude_bridge.py`** | Claude Code 常驻桥（stream-json 双向） | 独立可用，`python claude_bridge.py` 冒烟 |
| **`comni_ws.py`**（新增 09-15） | 官方 Comni WS 协议传输层（Plan B） | 含 worker idle 等待 + 退避重连 |
| `jarvis_state.py` | `~/.jarvis/state.json` 任务状态层（闲聊层 ↔ 任务层共享） | |
| `jarvis_demo.py` | Phase 1 最小闭环（SenseVoice + claude + say） | 降级参考 |
| `minicpmo_cpp_http_server.py` | wrapper 本地副本（**部署在 Windows，两边可能不同步，以 Windows 为准**） | |
| `persona.txt` | Omen Alpha 人设（621 tokens，注入给模型） | ⚠️ 见 §6 问题 4 |
| `run.sh` | 一键启动（查后端 → 建隧道 → 探模式 → 拉客户端） | `--status` / `--restart` / `--logs` |
| `omni-server.sh` / `omni-server.log` / `omni-server.pid` | 本地 M1 服务启停（历史） | 路径固定，勿移动 |
| `assets/default_ref_audio.wav` / `assets/omen_ref_16k.wav` | 音色参考音频（Plan B 用，**必须 16k 单声道裸 float32**） | |
| `models/minicpmo45-gguf/` | MiniCPM-o GGUF（~8.3G，Mac 开发用） | |
| `models/sherpa-onnx-sense-voice-.../` | SenseVoice ASR | |
| `llama.cpp-omni/` | 源码 checkout（分支 `feat/web-demo` @ `5202b7b`，含 1 处未提交 `omni.cpp` 改动） | ⚠️ 不是 master |
| `test_*.py` | 回归测试：`test_loop_structure.py`（AST 结构）、`test_loop_names.py`（**先用后赋**顺序敏感检查，09-15 新增）、`test_dedup_fix.py`、`test_broadcast.py`、`test_duplex_client.py`、`test_asr.py` | 改主循环后必跑前两个 |
| `*.bak-*`（9 个） | 各阶段备份（idlepause / reset / mdstrip / broadcast / retry / selftalk / echofloor / planB + run.sh.bak-planB） | 回退用 |
| `docs/` | 见 5.3 | |
| `.venv/` | Python 虚拟环境 | |
| `mcp-search.json` | Claude Code 桥挂的搜索 MCP 配置 | |

### 5.2 Windows 侧 `E:\`（10.20.70.180）

| 路径 | 作用 |
|---|---|
| `E:\jarvis-voice\app\minicpmo_cpp_http_server.py` | Plan A wrapper（~2900 行，10 处补丁） |
| `E:\jarvis-voice\app\run-wrapper.bat` | Plan A 启动脚本 |
| `E:\jarvis-voice\app\llama-omni-master\` | **现役 C++ 产物**（master + 4 补丁） |
| `E:\jarvis-voice\app\llama-omni-new\` | 旧 v1.0.22 产物（**勿动**） |
| `E:\jarvis-voice\app\llama.cpp-omni\` | 更旧的回退（**勿动**） |
| `E:\jarvis-voice\app\server.log` | Plan A 服务日志 |
| `E:\jarvis-voice\models\minicpmo45-gguf\` | GGUF 权重 |
| `E:\jarvis-voice\MASTER-VERIFY\` | master 验证实验区（t3_ab.py、dump 分析脚本、migrate-live.log） |
| `E:\jarvis-voice\MASTER-VERIFY-RESULT.md` / `-02.md` | master 验证结论（**崩溃根因 + 一行修复在这**） |
| `E:\jarvis-voice\JARVIS-HANDOVER.md` | 09-13 版交接（Windows 侧副本） |
| `E:\jarvis-build\llama.cpp-omni\` | 编译源码树（含 `master-audioq` 分支、`local-patches-v1.0.22-20260913.patch` 全量补丁备份） |
| `E:\jarvis-build\build_master.bat` | 增量编译（~27s）→ 产物**改名 `llama-server.exe`** 部署（先停栈，exe 占用无法覆盖） |
| `E:\jarvis-build\miniCPM-o-demo-comni\` | **官方 MiniCPM-o-Demo 源码**（Comni 版，Plan B） |
| `E:\jarvis-build\demo-venv\` | Plan B 的 Python 环境 |
| `E:\jarvis-build\run-demo-gateway.bat` / `run-demo-worker.bat` / `run-demo-guard.bat` | Plan B 启动脚本（guard = worker 报 error 就重启） |
| `E:\jarvis-build\demo-gateway.log` / `demo-worker.log` / `demo-guard.log` | Plan B 日志 |
| `E:\jarvis-build\llama-demo\build\bin\Release\llama-server.exe` | **Plan B 的 C++ 引擎（当前唯一占 GPU 的进程）** |
| `E:\jarvis-build\ws_test.py` / `win_comni_test.py` / `comni_ws.py` | Plan B 协议测试脚本 |
| `E:\tts-test\` | TTS 实测环境（Qwen3-TTS 判死，环境留着；Kokoro 可用） |
| `E:\jarvis-build\procdump\procdump64.exe` | 崩溃 dump 工具 |

### 5.3 文档与记忆系统

| 位置 | 内容 |
|---|---|
| `~/Obsidian/SecondBrain/Projects/实时视觉语音机器人.md` | **项目主笔记**（现状/决策/待办，单一真源） |
| 同目录 `-工程日志.md` | 时间线全记录（9/07→，~110KB） |
| 同目录 `-调研存档.md` | 8/14 立项调研原文 |
| 同目录 `JARVIS-架构方案池-20260914.md` | 方案池与决策记录（含"全进 CC"定稿方案） |
| `~/jarvis-voice/docs/JARVIS-HANDOVER.md` | 09-13 版交接（Windows 向，部分过时） |
| `docs/AUDIT-REPORT-20260913.md` | 独立审计报告（版本鉴定、listen 守门出处验证） |
| `docs/AUDIT-HANDOVER-PROMPT.md` | 给审计 Agent 的任务书（含红线） |
| `docs/WORKORDER-01/02-*.md` | 给 Windows 侧 AI 的工单 |
| `docs/README-M1Pro-Phase2-历史.md` | M1 Pro 原型历史（已归档） |
| claude-mem | 跨会话语义记忆（MCP：`mcp-search`），可用 `get_observations([ID])` / mem-search skill |
| `~/.claude/projects/-Users-owen-jarvis-voice/memory/` | Claude Code 自动记忆（本目录） |

---

## 6. 当前未解决的问题（按优先级，2026-09-15 现场）

### 🔴 1. Plan B 上模型说英文 / 跑偏 + 控制 token 泄漏

证据（09-15 03:31 客户端日志）：`<|tts_bos|>Hello! I was wondering if you could help me write a rap.`
- `<|tts_bos|>` 是控制 token，被当正文打印/播出
- 中文人设 + 中文输入，输出却是英文 → 疑与 Plan B 未注入正确人设/参考音色有关（日志显示 prepared 621 tokens，人设是进去了）
- **未验证**：是否与 `assets/omen_ref_16k.wav` 的格式（官方要求**裸 float32 16k 单声道**，不是 WAV 文件）有关

### 🔴 2. 静默污染 KV（**本项目最顽固的问题**）

- 现象：「静默不理人」+「自言自语/碎碎念」是同一个病的两面
- 机理：C++ 每拍实跑 ~12 轮 decode×2 token（不是自称的 1:1）+ 每拍静音音频 ~11 token ≈ **40 token/s 进 KV** → 每 ~2.3 分钟撞 ctx 上限触发 FORCE 滑窗剪枝（正常线 6144 从未生效）
- **Plan B 上依然存在**（kv_cache_length 每拍 +11 是铁证）
- 上游有同类报告：demo #28/#49/#30、tc-mb #88（ctx=8192 时"说话事件数"4 倍波动）/ #90，**无官方根治**
- 现行缓解：闲置暂停（6s 停送节拍）、会话重置、反自言自语护栏。**治本方案未做**（让 C++ 真按 1:1 跑，动核心路径）

### 🟡 3. 进程内快重置会让音频路径变哑（**已回滚，谜未解**）

- `/v1/stream/session_reset`（移植官方 `reset_octx_for_session`）实测 1.9–2.3s 完成，但**之后模型对音频输入完全不回应**（encoder 正常、prefill 正常，就是不应答）；真·进程重启则正常（✅ 7.08s）
- 已排除：麦克风音量（合成音衰减到同电平对照同样失败）
- 候选原因（**均未验证**）：`audition_whisper_clear_kv_cache()` 与编码器 streaming 状态不同步 / 双工线程重建时序 / `n_keep=0` 与音频路径交互
- 现状：`/omni/session/reset` 默认走**进程重启（18s）**；快路径降级为 `OMNI_FAST_RESET=1` 开关（默认关）

### 🟡 4. `persona.txt` 有一处语病（疑似编辑残留）

当前内容含：`…不要替主人说话。播报请把下面这段话念给主人听如果收到来自工具系统的消息（…），按消息里说的做；…`
`播报请把下面这段话念给主人听` 与后半句之间**缺标点、语义不通**，像是编辑时把旧的"播报模板"残留粘进了人设。人设是 621 token 注入给模型的，**语病会直接影响模型行为**，建议修。

### 🔴 5. 栈 A 的看门狗在空载时死循环重启引擎（**现场最该先修的**）

见 §2.1 红框。**每 60 秒杀一次引擎 + 重载 11GB 模型**，从 22:16 起持续到被人为停止（96 次/96 分钟）。两个 bug：

1. 看门狗**没有「是否存在活跃会话」的判断** —— 没有任何客户端时也照常计时、照常重置
2. **重手段配快节奏** —— 整进程重启（15–18s）按 60s 的 idle 节奏调用

**修法方向**（未实施）：① 加"有活跃会话才计时"的门闩（比如最近 N 分钟内有连接/请求）② 空载时用零成本的"什么都不做"，而不是重置 ③ 或把 `OMNI_IDLE_RESET_SEC` 调到一个与整进程重启相称的量级（如 900s）。

**次要问题**：两套栈的引擎**都硬编码 19060**（demo `config.py` 原文「默认 19060 + gpu_id」）→ **不可能共存，必须二选一**；重启窗口里旧/新引擎会短暂同时占位。

### 🟢 6. 未修但明确的小项

- master 的 text 通路缺失导致「不播报」（Plan A 上，~1h 工作量）——⚠️ Plan B 上是否同样存在未验证
- `top_k 100 → 40` 采样 A/B 未做
- 上游 issue 待追加 2 条（人设覆盖不全 / `--host` 被忽略 → #114）

---

## 7. 踩过的坑（红线清单，**别再踩**）

### 7.1 双工协议

| 坑 | 根因 | 状态 |
|---|---|---|
| 节拍一断模型就不开口 | 双工要求严格交替 | 已理解 |
| 块长改 0.7s → 模型退化成重复循环 | 700ms=70 帧 vs 训练 100 帧，分布外 | 已回退，**别改** |
| `listen_prob_scale` 是错的工具 | 全局常数偏置，分不清"该继续"和"该闭嘴" | 回退 1.0 |
| 静音节拍污染 KV | 见 §6.2 | 缓解中 |
| `update_session_config` 在 master 上是**空壳**（只改 `media_type`，不清 KV） | 上游差异 | 已知 |

### 7.2 C++ 移植缺陷

| 缺陷 | 状态 |
|---|---|
| **缺 listen 守门**（官方 Python `modeling_minicpmo.py:3237-3239` 有，C++ 所有分支都没有）→ 模型一句话说一半切 LISTEN | **已自行补丁并编译**（只改 `omni.cpp` 两处） |
| `length_penalty` 缺失 | 已改（日常 1.1 / 播报 1.5） |
| wav 文件名复用 → 永久丢音 | master 已在上游修复（编号递增移进 T2W 线程） |
| 音频回传靠"写 wav 文件 + 50ms 轮询扫目录 + 按文件名去重" | **已重构**为 C++ 内存队列 + `POST /v1/stream/audio_poll`（根除一整类 bug） |
| master 崩溃（`0xC0000005` 读地址 `0x63`） | **上游悬垂引用 bug，一行修复**；dump 分析文件在 `E:\jarvis-voice\MASTER-VERIFY\` |
| master 的 index-0 prefill 协议差异（duplex 卡 prefill done） | 已破案并修 |

**⚠️ 已知的 dump 分析红线**：`procdump -ma`（14GB 全量 dump）曾引发整机内存耗尽（dwm 崩溃、黑屏、远程重启）。**只用 `-mm` + 流式分析脚本**；任何进程 commit 超 ~20GB 立即介入。

### 7.3 施工中犯过的错（给 AI 的教训）

| 错 | 教训 |
|---|---|
| **代码块缩进串位**：插入诊断块时把"用户语音收集 + ASR 触发"整块掉进 `if beats >= 10:` → 每 10 秒才采样一次 | `py_compile` 抓不到（语法合法语义全错）→ 已加 AST 结构回归测试 |
| **先用后赋**：护栏补丁在 `n_audio` 赋值前引用它 → 首拍 `UnboundLocalError` → 双工循环崩到单工 | AST 也抓不到 → 已加 `test_loop_names.py`（顺序敏感名字检查） |
| **多次二手引用出错**：把错文件名、错行号、编造的 issue 内容写进交接文档（**至少三次**） | **交付物里的出处/行号一律先亲自核实** |
| **误判"退化"**：把"音频没送达"（wav 去重 bug）误判成"模型上下文退化"，差点去改滑动窗口 | **先区分"模型没说"和"说了没传到"**——服务端日志能区分 |

### 7.4 已证伪 / 走不通的（**别再试**）

- **用端到端语音模型"朗读指定文本"**——LiveKit 官方文档原文：*"Realtime models don't offer a method to directly generate speech from a text script… the output isn't guaranteed to precisely follow any provided script"*。我们的实测完全一致（让它念"都柏林今天的新闻"，它自己编了一段天气预报）。**它只会"回应"，不会"朗读"**。这是本项目花一个月验证的一个本该半天验证的假设。
- **`max_new_speak_tokens_per_chunk` 调大**：我们 26，官方 20，已经更宽松，不是瓶颈
- **`force_speak` 这类补丁**：全 GitHub 搜不到，无先例
- **comni-2.0 不是升级路线**：属 master 那套另一套双工引擎（DuplexPipeline + WebSocket），维护者 issue #74 原话："two different duplex engines… locking to feat/web-demo for now"
- **"文本 → speech token 的桥"不存在**：要专门训练，无现成实现
- **用双工模型朗读工具结果**：见上，结构性不可能 → 现行方案是"用 `say` 合成指令音频喂进去，让它用自己的嗓音念内容"（实测模型念出 9.7s）

---

## 8. 关键参数、开关与运维

### 8.1 客户端环境变量（`jarvis_omni.py` 顶部）

| 变量 | 默认 | 说明 |
|---|---|---|
| `OMNI_TRANSPORT` | **`ws`** | `ws`=官方 Comni（Plan B）；`http`=自研 wrapper（Plan A 回退） |
| `OMNI_WS_URL` | `ws://127.0.0.1:8006/ws/duplex/adx_jarvis` | 官方网关 |
| `OMNI_CHUNK_SEC` | `1.0` | **别改** |
| `OMNI_BARGEIN_RMS` | `0.02` | 助手不在说话时的插话阈值（实测用户说话峰值 0.068） |
| `OMNI_BARGEIN_RMS_SPEAKING` | `0.05` | 助手说话时（回声底 0.021）——原为 0.15，**高出 7 倍永远触发不了**，已降 |
| `OMNI_IDLE_PAUSE_SEC` | `6.0` | 静默多久停送节拍（**别调大**，15 拍就污染到失聪） |
| `OMNI_SELF_TALK_MAX_BEATS` | `5` | 连续几拍"只有模型出声"→ 强制打断 |
| `OMNI_SELF_TALK_RESET_AFTER` | `2` | 第 2 次触发直接重置会话 |
| `OMNI_MIC_TARGET_RMS` | `0.07` | 对齐官方 LUFS 前端（−23 LUFS ≈ 0.07） |
| `OMNI_START_DELAY` | `1.5` | 播放预缓冲（0.9 会断续，代价是每句晚 1.5s 出声） |
| `OMNI_POST_SPEECH_MUTE_SEC` | `0.6` | 播完后的数字静音期（切断回声自激） |
| `OMNI_LENGTH_PENALTY` | `1.1` | 日常；播报走 `OMNI_RESULT_LENGTH_PENALTY=1.5` |
| `OMNI_FAST_RESET` | 关 | 进程内快重置（**会让音频变哑，默认关**） |

### 8.2 Windows 运维

```powershell
schtasks /Run /TN JarvisOmniServer     # 启（Plan A）
schtasks /End /TN JarvisOmniServer     # 停——⚠️ 只解注册，进程树变孤儿
# ✅ 正确杀法（按命令行点名，绝不能用 /IM python.exe 全杀）：
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*minicpmo_cpp_http_server*' -or $_.Name -eq 'llama-server.exe' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

- 编译：`E:\jarvis-build\build_master.bat`（增量 ~27s）→ 产物**改名 `llama-server.exe`** 部署
- `.ps1` 脚本**必须 ASCII-only**（PowerShell 5.1 按 GBK 读 UTF-8 会乱码）；`.bat` 需要 CRLF
- 日志 UTF-8 须 `-Encoding UTF8` 读；ssh 远程最稳 = `-EncodedCommand` + base64(UTF-16LE)
- `/health` 通 **≠** 模型就绪的对称陷阱：wrapper 在 uvicorn 启动前做完预初始化（所以通=就绪），**但反过来，`/health` 通不代表 C++ 子进程还活着**

### 8.3 诊断方法（省时间的）

```bash
grep -o "\[[^]]*\]" /tmp/jarvis-client.log | sort | uniq -c   # 一眼看出"有没有真的派活/回灌"
grep -E "slide|FORCE" <服务端日志>                             # 上下文崩溃时刻与频率
watch -n1 'curl -s http://127.0.0.1:22400/health'             # Plan B：kv_cache_length 实时（HTTP 栈没有这能力）
```

---

## 9. 待办与路线

### 立即（拿到项目先做这三件）

1. **修栈 A 看门狗的空载死循环**（§6.5：每 60s 重启一次引擎，整晚不停）—— 或至少把 `OMNI_IDLE_RESET_SEC` 调大、把两套栈二选一
2. **堵 8006/22400 的防火墙**（§2.4，安全；22:31 复测仍可达）
3. **修 `persona.txt` 的语病**（§6.4）

### 短期

4. 定位 Plan B 上的"英文跑偏 + `<|tts_bos|>` 泄漏"（§6.1）
5. 解决静默污染 KV 的治本方案（C++ 真 1:1，或把官方"KV 剪枝即结束会话"纪律完整搬过来）
6. 解 §6.3 的谜（进程内重置为何哑掉音频）
7. 真机验收三项：①派活能听见播报 ②说话能打断 ③静置 30s 再说话仍有反应

### 中期（见 `JARVIS-架构方案池-20260914.md`）

8. **单脑 vs 双脑决策**：方案池 9/14 定稿是「全进 Claude Code、不分流模型」（贾维斯 = CLAUDE.md 人设的语音化身），配套五件套：应答词过滤器 / 语音模式人设约束 / 会话滚动策略 / 打断三层 / Smart Turn v2 + TEN VAD。**当前实跑仍是双脑**（MiniCPM-o 前台 + CC 后台）。调研显示 TML / GPT-Live / Qwen-Audio-Agent 全是"前台快 + 后台干"
9. 采样 A/B：`top_k 100→40`（一次只改一参）
10. Phase ③ 情感层（EMA 惯性 μ≈0.8 / 离散标签驱动 TTS）→ Phase ④ 视觉 → iPad 前端

### 已封存

- Qwen3-TTS（环境留 `E:\tts-test`，迁 Linux/WSL2 可复活）
- 用双工模型朗读文本（结构性不可能）

---

## 10. 给接手者的三条告诫

1. **别信二手引用。** 这个项目上至少三次因为转抄别人的结论（错文件名、错行号、编造的 issue 内容）而走弯路。**任何出处、行号、版本号，先亲自核实再说。**
2. **先区分"模型没说"和"说了没传到"。** 我们在这上面误判过一次——把"音频没送达"当成"模型上下文退化"，差点去改核心的滑动窗口。**服务端日志能区分这两者。**
3. **别在"结构性不可能"的路上调参。** 花一小时确认"这条路存在吗"，比花一周调参数值得。**这个项目最大的教训：我们用一个月验证了一个本该第一天用半天验证的假设。**

**补充两条本项目特有的工作方式**：

4. **改任何东西前先备份**（惯例：`.bak-<日期>` 或 `.bak-<主题>` 后缀），本项目 Mac 侧现在有 9 个 `.bak` 文件就是这习惯的产物。
5. **一次只改一个变量**，改完立刻真机验证并记录原始输出（不写"应该没问题"）。

---

## 附：本文档中标注"未验证"的清单（接手时优先复核）

- §2.4 8006/22400 至今未被封（Owen 09-15 22:31 重启防火墙后复测仍可达）
- §3.2 切 Plan B 的完整决策理由（从代码反推，未找到原始讨论记录）
- §6.1 英文跑偏是否与参考音色格式有关
- §6.3 进程内重置哑掉音频的根因
- §6.5 看门狗死循环的确切起止时间（只能从 96 次/96 分钟推断每分钟一次）
- §6.6 Plan B 上是否也存在 master 的 text 通路缺失

**已澄清（不必再查）**：
- Windows 上"每个组件两个 PID" = venv 启动器 → 真实解释器的父子进程，正常现象
- 引擎 PID 频繁变化 = 看门狗每 60s 重启一次，不是被别的实例杀掉（早先一版的判断已推翻）
