# 项目审计与接管请求 · JARVIS 全双工语音助手（MiniCPM-o 4.5 + Claude Code）

> 你是一名被请来做**独立审计**的高级 Agent。本文件由原开发 Agent 撰写，是**交接材料，不是结论**。
> 请把本文档里的一切都当作**待验证的线索**，而不是事实。

---

## 0. 你的角色与硬规矩（先读）

**你的任务是诊断，不是修 bug。** 在完成审计并给出书面结论之前，不要修改任何代码。

1. **不要相信本文档的结论。** 本文档明确区分了「已实测验证」与「推测」，请把推测部分全部重新验证，把已验证部分抽样复验。
2. **优先证据，后猜测。** 每个判断都要能指向：文件路径+行号 / 日志原文 / 命令输出 / API 响应。
3. **优先最小可验证实验，后大规模改代码。**
4. **如果发现原方案方向就是错的，直接说，不要继续打补丁。**
5. 🔴 **绝对不要删除用户数据。** 特别是 `C:\Users\Owen\.claude\projects\*\*.jsonl`（会话记录）—— **只读不删**。任何删除前先备份。
6. 🔴 **绝不整文件 dump 含密钥的配置**（`~/.claude/settings*.json`、`~/.cc-switch/`、`~/.claude-mem/settings.json`）。只挑键名取值。
7. **不用通配符清目录。**
8. **不要代填凭据。** 需要 key/密码就停下来。
9. **每步做完先自证再报成功**，贴真实原始输出，不写"应该成功了"。
10. **不确定就停。** 宁可回报"卡住了"。

**协调条款**：用户（Owen）随时可能在 Mac 上跑真机测试。任何**重启服务、发测试请求**的动作，先确认没有测试在进行，或先回报申请。

---

## 1. 项目最终目标

在**全本地硬件**上跑一个实时视觉+语音助手（代号 JARVIS / 人设名 "Omen Alpha"）：

1. **像真人一样自然的中文语音对话**（全双工：边说边听，可随时插话打断）
2. **能靠语音让 Claude Code 真的干活**（用户说"帮我查一下 X"，助手去执行并把结果用**同一个人声音色**播报回来）
3. 后期扩展：情绪识别、视觉输入、机器人本体

**参照物**：MiniCPM-o 4.5 官方在线 demo `https://minicpmo45.modelbest.cn/` —— 用户明确说"我想要的就像这个"。

**动机**：不想付云端 API 费（Gemini Live ~$0.02/min、OpenAI Realtime ~$0.25-0.35/min），也不想被供应商锁定。

---

## 2. 技术栈与架构

### 三层，跨两台机器

```
┌─ Mac (M1 Pro 16GB, macOS) ──────────────┐
│  jarvis_omni.py   客户端                     │
│   ├─ 麦克风采集 → 1 秒切片 → 有界队列          │
│   ├─ 每拍: prefill(音频) → decode → 消费 SSE  │
│   ├─ 边收边播 (24kHz 常驻输出流)              │
│   ├─ SenseVoice 做 ASR → 任务路由             │
│   └─ 任务结果回灌给模型 → 用模型音色播报        │
│  claude_bridge.py  Claude Code headless 常驻桥 │
│  jarvis_state.py   任务状态                    │
│  run.sh            启动器(建隧道/健康检查/重启)  │
└────────────────── SSH 隧道 ───────────────┘
          ↓ 转发 9060 / 9061 / 19060
┌─ Windows (i5-12600KF / RTX 5060 Ti 16GB) ─┐
│  :9060  minicpmo_cpp_http_server.py (FastAPI) │
│          wrapper，管理会话 / 扫 WAV / 拼 SSE    │
│  :19060 llama-server.exe  (C++, 预编译 CUDA)   │
│          真正的模型 + TTS + 声码器，全 GPU      │
│  :9061  break server                          │
└───────────────────────────────────────────┘
```

### 模型与推理

- **MiniCPM-o 4.5**（9B，端到端全双工全模态，Apache-2.0），Q4_K_M GGUF
- `--duplex` 模式启动，**模式在服务端启动时锁定**
- 音色克隆：固定参考音 `E:\jarvis-voice\app\llama.cpp-omni\tools\omni\assets\default_ref_audio.wav`
- 人设（"Omen Alpha"）通过 `voice_clone_prompt` 前缀注入，格式：
  `"<|im_start|>system\nStreaming Duplex Conversation! {PERSONA}\n<|audio_start|>"`
- **双工协议**：客户端严格交替 `POST /omni/streaming_prefill`(1 秒音频) → `POST /omni/streaming_generate`(SSE) 循环。
  **静音也必须发**，节拍一断模型就退回 listen 永不开口。
- 模型每拍自主决定输出 `<|speak|>` 还是 `<|listen|>`

### 关键文件清单

| 文件 | 位置 | 作用 |
|---|---|---|
| `jarvis_omni.py` | Mac `~/jarvis-voice/` | **客户端主程序**，双工节拍 / 播放 / ASR / 任务路由 / 结果回灌 |
| `run.sh` | Mac `~/jarvis-voice/` | 启隧道、健康检查、`--restart`（按命令行点名杀进程再重启 Windows 服务） |
| `test_duplex_client.py` | Mac `~/jarvis-voice/` | 无麦测试，用 WAV 驱动真实双工代码路径 |
| `claude_bridge.py` | Mac `~/jarvis-voice/` | Claude Code headless 常驻桥（`--bare` + `MAX_THINKING_TOKENS=0`） |
| `minicpmo_cpp_http_server.py` | Win `E:\jarvis-voice\app\` | **wrapper**：会话管理、WAV 目录扫描、SSE 拼接、人设注入 |
| `run-wrapper.bat` | Win `E:\jarvis-voice\app\` | 启动脚本 + 所有调参环境变量（**CRLF 行尾**） |
| `server.log` | Win `E:\jarvis-voice\app\` | wrapper + C++ 全部日志 |
| `wav_timing.log` | Win `E:\jarvis-voice\app\` | **音频产出时序的金标准**（每块 WAV 的写入/发送时刻与间隔） |
| `llm_debug\llm_text.txt` | Win `...\output_9060\` | **模型的原始输出文本**（不是客户端看到的） |
| `tts_wav\` | Win `...\output_9060\` | 生成的音频分块；同目录下 `generation_done.flag` |
| `llama.cpp-omni\` | Mac `~/jarvis-voice/` | **C++ 源码 checkout，停在 PR #29**（比 Windows 上跑的二进制新/旧关系不明，见 §5 争议） |

### 运行方式

```bash
# Mac 上
cd ~/jarvis-voice
./run.sh              # 建隧道 + 健康检查 + 启动客户端（真机带麦）
./run.sh --restart    # 重启 Windows 后端（End→点名杀→验 CLEAN→Run→轮询健康）
OMNI_CHUNK_SEC=1.0 ./run.sh          # 可覆盖块长
```

### 环境变量（`run-wrapper.bat`，Windows 侧）

| 变量 | 当前值 | 作用 |
|---|---|---|
| `OMNI_LISTEN_PROB_SCALE` | `0.6` | 压制模型"转去听"的概率（见 §4） |
| `OMNI_MAX_SPEAK_TOKENS` | `60` | 每拍 speak token 上限 |
| `MODE_FLAG` | `--duplex` | 模式开关 |
| `PYTHONUTF8` / `PYTHONIOENCODING` | `1` / `utf-8` | 中文日志 |
| `REGISTER_URL` | `off` | 关闭服务注册 |

Mac 侧客户端环境变量：`OMNI_BASE`、`OMNI_CHUNK_SEC`、`OMNI_BARGEIN_RMS`、`OMNI_BARGEIN_RMS_SPEAKING`、`OMNI_BARGEIN_CHUNKS`、`OMNI_ECHO_MARGIN`、`OMNI_OUTPUT_LATENCY`、`OMNI_TASK_SPEECH`、`OMNI_HINT_PAIR_WINDOW`

### 外部依赖

- Claude Code CLI（headless 子进程）
- SenseVoice-Small（Mac 本地 ASR，CPU）
- SSH 隧道（Mac → Windows），**防火墙保持开启，校园网零暴露**
- Windows 计划任务 `JarvisOmniServer`（LogonTrigger +20s）
- Syncthing（Mac↔Windows 同步 Obsidian vault）

---

## 3. 失败历史（按时间线，含"哪些被证伪"）

### 阶段 A：M1 Pro 原型（已废弃）
- 半双工，15-28s/轮，RTF 2-4x，太慢。
- **教训**：M1 Pro 上"边收边播"必然断续，这是算力问题，不是代码问题。

### 阶段 B：后端搬到 5060 Ti
- **零编译路线**：不从源码编译（当时判断 sm_120 会踩 PTX 坑），改从 Comni 安装包提取预编译 CUDA `llama-server.exe`。
- 37/37 层全上 GPU，首音 1.10s。

### 阶段 C：双工客户端改写
- **发现**：服务端被锁成 `--duplex`，而客户端还在调单工 API → **`./run.sh` 当时是坏的**。
- 重写 `jarvis_omni.py`（432→744 行），加入双工节拍循环。

### 阶段 D：第一次真机测试 → 用户报三个缺陷
> ①说话卡，有时字说一半被切断 ②CC 结果音色与聊天不一致 ③CC 的语音被 ASR 识别成用户说的话

**已修复且已验证的：**
- 🎯 **「卡」的一半原因 = wrapper 每轮固定死等 1.0 秒**
  - 位置：`minicpmo_cpp_http_server.py` 的 `_streaming_generate_duplex` 收尾
  - 原代码：`no_new_wav_count >= 10` + `await asyncio.sleep(0.1)` → 每轮无论有没有音频都白等 1000ms
  - **实测数据**：每块周期 1.17s → 0.46s；`wav_timing.log` 的 `interval_from_last_ms` 从 1243–1496ms（**0.8x 实时**）变成 475ms（**2.4x 实时**）
  - ⚠️ **过程中推翻的两个旧判断**：① 声码器不是瓶颈（C++ `T2W线程` 日志 `RTF=0.16–0.30`）② 早先"约 950ms 是 wrapper 扫盘"是**错的**，`write_to_send_delay_ms` 实测只有 23–98ms
  - 修法：改成双信号 —— 检测 `generation_done.flag` 的 mtime + 200ms 兜底静默窗口
- 🎯 **「音色不一致」已解决并验证**
  - 机制：`POST /v1/stream/prefill` 接受 `text` 字段，C++ 把它当 `user_text` 直接 `eval_string` → 等于注入一个文本用户轮次，模型用自己的克隆音色回应
  - wrapper 原来**没有转发** `text`（Pydantic 静默丢弃），已补
  - 措辞实测三选一，`（背景：后台助理刚刚汇报——{结果}。）` 最稳；实测产出 1.40s / 2.24s 音频，两次都成
- 🎯 **「回声自我打断」做了缓解**（未真机验证）
  - 机制：Mac 外放 → Mac 麦克风拾到 → RMS 过门槛 → 客户端判"用户插话" → `flush()` + `/omni/break` → 模型说到一半被自己掐断
  - 修法：双阈值（助手出声时 0.02→0.15）+ 连续 2 块迟滞 + 播放后 300ms 混响冷却

### 阶段 E：第二次真机测试
**新暴露的（部分是我自己引入的）：**
- 🔴 **我在 `_run_asr` 里打错一个括号** —— `len(text.strip("。.，, ") < 2)` 应为 `len(text.strip(...)) < 2`。
  后果：ASR 每次跑到这行就抛 `TypeError`，**后面的任务路由一行都没执行**。已修。
- 🔴 **模型从不输出 `<task>` 标签** —— 拉 `llm_debug/llm_text.txt` 看原始输出，模型说的全是自然口语（"嗯哼，让我查一下哈。"），**一次 `<task>` 都没有**。人设里写的"遇到任务先应一句「好的」再输出 `<task>…</task>`"只实现了前半句。
  - 应对（**未真机验证**）：新增"意图路由" —— 模型说「让我查一下」时，把它配对到用户最近一句原话，派给 Claude Code。
  理由：模型虽不吐标签，但这句**会带着音频传回客户端**。

### 阶段 F：第三次真机测试（当前状态）
用户反馈：**"还是很奇怪，会卡顿"** + **"不回应我说话了直接"**

- 诊断输出：`⏱ 10 拍: 1005ms/拍 | 说话 2 拍 产出比 0.90x ❌缓冲在抽干`
- 我据此把 `max_new_speak_tokens_per_chunk` 从 26 调到 60 —— **调完实测证明这个改动无效**（见下）
- **无麦定量实验推翻了我的假设**：模型每拍**固定产出约 1.00 秒音频**，处理只用 0.67s（产出比 1.49x）。
  换成 0.5s 块长结果一样（仍是 1.00s/拍）。
  → **token 上限根本不是瓶颈，模型每拍本来就只吐约 25 个 T2W token（正好一个处理窗）**
- **新的（仍未验证的）假设**：模型每拍说满约 1s 就切 `<|listen|>`，所以句子被切成碎片、中间夹静音。
- 实时找到的旋钮 `listen_prob_scale`（C++ 里 `logits[<|listen|>] += (scale-1)*2`），已设 **0.6**，日志确认 `listen_prob_scale set to 0.6000` 生效。**尚未真机验证效果。**

---

## 4. 当前真正的问题

### Core Problem

> ⚠️ **本节原表述已被独立审计推翻，下面是修正版。** 原文写的是"双工一拍墙钟约 1 秒，
> 而模型每拍只产约 1 秒音频，所以是零余量、无法跨拍连续" —— **这是错的**。审计实测每拍墙钟
> 只有 **0.30–0.90 秒（快于实时）**，并拿到了横跨 9 拍的 **8.56 秒连续语音**。碎片化的
> 决定因素不是节拍，而是**模型会不会切 `<|listen|>`**。

**修正后的 Root cause（已确认）：**

> 运行中的 C++ 二进制（v1.0.22）**缺少官方参考实现的 listen 守门**。官方 Python 版在
> "本 turn 还没结束"时会把采样出的 `<|listen|>` 强制改写成 `tts_bos`（继续说话）；C++ 版
> **无条件接受 `<|listen|>` 并 `break` 结束本拍**（`current_turn_ended` 全文件只写不读）。
> 因此模型每说完一个 chunk 就可以合法地转去听，一句话被拦腰切成长约 1 秒的碎片、中间夹静音拍。
>
> **直接复现**：审计把 `listen_prob_scale` 设回默认 1.0，单词 "Omen" 被劈成
> `…我是O` + 一拍静音 + `men Alpha。` —— 与用户听到的"一个字说一半被切断"完全同形。

**次生问题（三条叠加，互相独立）：**

1. 🔴 客户端 ASR 采集与触发曾被缩进 bug 挤进 `if beats >= 10:`，变成每 10 秒才采样一次用户语音 ——
   任务主线因此几乎完全废掉。（**已修**）
2. 模型**从不输出 `<task>` 标签**（XML 对 9B 语音模型分布外），任务分流机制选错。
   （**已改**：人设删掉标签指令，改走客户端 ASR + 规则路由）
3. 意图正则曾接不住模型的实际话术（它说「好的，我**为你查**一下。」）。（**已修**）

**尚未定位的部分**：跨会话退化的根因（"跑一段后全 LISTEN"）。审计的 Run4 显示
**会话内上下文状态对开口行为影响很大**（scale=0.2 在干净会话里产出 8.56s 连续语音，
在受污染会话里只产出 1.2s），与该现象可能同源，但未做长时实验。

### 候选根因（按我的置信度排序，全部需要独立验证）

| # | 候选 | 我的置信度 | 证据 / 反证 |
|---|---|---|---|
| 1 | **C++ 缺一道 Python 有的 listen 守门** —— 官方参考实现 `openbmb/MiniCPM-o-4_5/modeling_minicpmo.py` **第 3237-3239 行**（⚠️ 已由独立审计从 HuggingFace 下载原文核实；本文档早期版本写的 `modeling_minicpmo_unified.py:5104-5106` 是错的，那是转抄未验证的二手引用）：<br>`if last_id.item() == self.listen_token_id and (not self.current_turn_ended): last_id = torch.tensor([self.tts_bos_token_id], ...)`<br>C++ 侧（`omni.cpp`）：`current_turn_ended` **全文件只写不读**（9271/9450/9491 置位，无任何读取点）；采样到 `<|listen|>` 直接 `break`（9474-9514）；`tts_bos_token_id` 仅 4071-4074 初始化，注释写着"用于双工模式强制继续说话"，生成循环从未使用 | **已确认** | 官方 Python 原文 + 两份 C++ 代码副本 + 行为实验三重印证。审计已用 scale=1.0 直接复现：单词 "Omen" 被劈成 `…我是O` + 一拍静音 + `men Alpha。` |
| 2 | **二进制版本落后（已坐实）** —— `llama-server.exe` = `tc-mb/llama.cpp-omni` tag **v1.0.22**（commit `61d8393`，2026-04-29 14:50:59 +0800）；exe 文件时间戳同日 21:57。上游 **PR #47 合入 2026-06-02、PR #78 合入 2026-07-01，均晚于构建日期 → 物理上不可能包含**。PR #101（base `bench/huawei`）与 #108 **从未合入**。 | **已确认** | 独立审计取证：exe 时间戳 + tag 提交时间 + 行为特征比对（`LISTEN during listen state, skip flush` 字符串、`sampling` 回执字段等逐条命中）+ GitHub API 查 PR 合入时间 |
| 3 | 模型自身的训练节拍就是 1 秒/单元，全双工下"每拍 1 秒、拍间切换"是设计行为，不是 bug | 中 | 官方 README 明确 1.0 秒/1000ms 节奏；官方 issue #3 说 0.5s 单元会掉智能 |
| 4 | 客户端播放/缓冲策略问题（无预缓冲、`blocksize=2400` 仅 100ms、拍间无补偿） | 中 | 未做过对照实验 |
| 5 | `listen_prob_scale=0.6` 能救 | 未知 | **改完还没测过** |
| 6 | 用户声学环境（外放+内置麦）导致回声混入，间接影响模型行为 | 低 | `_excise_echo` 未在真机上验证过是否真的削掉了回声 |

### 🔴 已知的、与结论无关但会影响你判断的坑

- 本机**三个版本互不一致**（已由审计厘清关系）：Windows 二进制 = **v1.0.22 (61d8393, 2026-04-29)**；
  Mac 源码 checkout = **`feat/web-demo` @ `5202b7b`（PR #29 merge, 2026-04-23）** —— 两者是**近亲**，
  `tts_bos_token_id` 初始化位置仅差 10 行、双工逻辑同构，**都缺 listen 守门**；上游 master 则是另一个世界。
  **任何"读 master 源码得出的结论"都必须在本机二进制上实测确认。**
- 已发生过一次真实冲突：另一份调研读 master 说"双工路径会丢弃 `text` 字段"，但本机实测 `text` 注入**有效**（模型复述了注入内容）。**以实测为准，但这条争议没有最终裁定。**

---

## 5. 故障树（按层，含状态标记）

| 层 | 项 | 状态 |
|---|---|---|
| **环境** | Windows 无 CUDA toolkit / cmake / VS（`where nvcc/cmake/cl` 全部 not found） → **无法从源码重编译** | 已确认 |
| | Windows OpenSSH 会话 System32 PATH 损坏（`%SystemRoot%` 不展开） | 已确认 |
| | Windows 有系统代理？（上游 PR #38 报告 Clash/V2Ray 会污染本地 httpx 导致 502） | **未测试** |
| **依赖** | Claude Code headless 桥可用 | 已确认 |
| | SenseVoice ASR 可用 | 已确认 |
| **配置** | 人设注入（`voice_clone_prompt`）生效 | 已确认（C++ 日志打印出完整人设） |
| | `max_new_speak_tokens_per_chunk` 运行时覆盖生效 | 已确认（日志回执） |
| | `listen_prob_scale` 运行时覆盖生效 | 已确认（日志回执 `set to 0.6000`） |
| **前端/客户端** | 双工节拍循环 | 已确认（无麦测试通过） |
| | `MicCapture` 分块 + 时间戳 | 已确认（单元级） |
| | `DuplexPlayer` 播放 + 打断 | 已确认（无麦） |
| | `_excise_echo` 回声明除 | **未测试**（只有单元级验证，从未在真机确认削掉回声） |
| **后端** | wrapper SSE 拼接 / WAV 扫描 | 已确认（有日志） |
| | wrapper 尾部等待逻辑（现为 flag 双信号） | 已确认（实测 1.17s→0.46s）；但**曾有过"整句音频永久丢失"的事故**，是否还有别的丢音路径 → **未穷尽** |
| **API** | `/omni/streaming_prefill` 接受 `text` | 已确认（实测模型复述了注入内容） |
| | `/omni/streaming_generate` SSE 事件格式 | 已确认 |
| | `update_session_config` **会清 KV cache、丢失 omni_init 的 system prompt** | **未测试**（上游 PR #38 报告，若为真影响人设持久性） |
| **状态管理** | 服务端"跨会话退化"：跑一段后从会应答退化到全 LISTEN，重启恢复 | **已确认存在，但无量化基线，根因未知** |
| **并发/异步** | 上游 PR #108 移除了 stream_decode/stream_prefill 的外层阻塞锁（死锁源） | 本机二进制是否含此修复 → **未测试** |
| | 客户端 `handle_task` 后台线程 + 主循环并发 | 已确认（用 `task_result_q` 解耦） |
| **AI/Prompt** | 模型不输出 `<task>` 标签 | 已确认（`llm_text.txt` 原始输出为证） |
| | 模型每拍只吐约 1s 音频 | 已确认（无麦定量，两种块长一致） |
| | 模型开口率 20–30% | 已确认 |
| **用户流程** | "说一句话 → 得到自然回应" | **失败**（卡顿） |
| | "下指令 → Claude Code 执行 → 语音播报结果" | **从未真正跑通**（意图路由新加，未验证） |

---

## 6. 关键证据位置 —— 如果只检查 5 个地方，查这里

1. **`E:\jarvis-voice\app\llama.cpp-omni\tools\omni\output_9060\llm_debug\llm_text.txt`**
   —— 模型的**原始输出**。客户端看到的文本是残缺的（wrapper 把文本和 WAV 按序号配对，会错位）。
   要判断"模型到底说了什么 / 有没有吐标签"，这是唯一可信来源。

2. **`E:\jarvis-voice\app\wav_timing.log`**
   —— 每块 WAV 的写入时刻、发送时刻、发送延迟、**距上一块的间隔**、音频时长。
   "产出跟不跟得上实时"在这里一眼可见。当前基线：间隔 ~475ms / 音频 1.12s。

3. **Mac `~/jarvis-voice/llama.cpp-omni/tools/omni/omni.cpp` 的双工 LISTEN 分支**（约 9440–9520 行）
   —— 对照 Python 参考实现 `modeling_minicpmo_unified.py:5104-5106` 的守门。
   **注意：这份源码停在 PR #29，不等于 Windows 上跑的那个二进制。**

4. **Mac `~/jarvis-voice/jarvis_omni.py` 的 `_duplex_loop`**
   —— 节拍、播放、ASR 门控、任务路由、结果回灌全在这里。
   每 10 拍会打印一行 `⏱` 诊断（拍均耗时 / 说话拍数 / 说话时产出比）。**让用户跑一次并收集这些行，是最快的现场数据。**

5. **`E:\jarvis-voice\app\server.log`**
   —— wrapper + C++ 的交错日志。搜这些关键字：
   `T2W线程`（声码器 RTF）、`LLM Duplex:`（说话/听决策）、`本次消耗`（每拍 prefill token）、
   `c++ finish stream_prefill`、`结束扫描`、`生成性能总结`

**其他证据**：
- `E:\jarvis-voice\app\llama.cpp-omni\tools\omni\output_9060\tts_wav\`（音频分块 + `generation_done.flag`）
- Mac `/tmp/measure_split.py`（拆 prefill vs generate 耗时）
- Mac `/tmp/measure_sustain.py`（端到端产出速率）
- Mac `/tmp/measure_beatsize.py`（不同块长的产出比对照）
- Mac `~/jarvis-voice/test_duplex_client.py`（无麦回归测试）
- Obsidian 项目笔记：`~/Obsidian/SecondBrain/Projects/实时视觉语音机器人.md`（**全部历史调研与结论**）

---

## 7. 架构级问题（请主动质疑，不要假设原架构是对的）

1. **客户端 1 秒节拍 == 模型 1 秒生成上限。** 模型每拍最多吐 1 秒音频，而一拍墙钟也是 1 秒。
   **这是零余量设计** —— 只要任何一拍模型少说一点，播放缓冲就永久性落后。
   → 请判断：这个"1 拍 = 1 秒音频"的耦合是否是根本缺陷？有没有别的调度方式？
2. **靠自然语言标签做工具调用。** 人设让一个 9B 语音模型输出 `<task>…</task>`，实测它根本不吐。
   → 这是不是从一开始就选错了机制？（对比：正规做法是 function calling / 结构化输出）
3. **wrapper 把文本和 WAV 按序号配对**（`global_text_send_idx` 对每个 WAV +1）。
   → 这必然错位。客户端拿到的文本和音频不对应，"意图路由"建立在这个残缺文本上是否可靠？
4. **跨机器三层 + SSH 隧道 + 目录轮询传音频**（C++ 写 WAV → wrapper 扫目录 → base64 塞 SSE）。
   → 这是不是过度复杂？有没有更直接的路径（比如 WebRTC，上游有 WebRTC 集成文档）？
5. **零编译路线**（用别的项目的预编译二进制）。
   → 这导致源码与二进制版本脱节、无法打补丁。是否应该承认这条路已经走到头、改为自建编译？
6. **回声明除是"朴素手工 AEC"。** 靠播放时刻估算挖掉麦克风采样，没有参考信号对齐、没有滤波器。
   → 够用吗？还是应该上 `livekit` 的 WebRTC AEC3（需 10ms 帧 + 参考信号）？
7. **是否应该放弃当前实现重写？** 请给出明确判断和理由。

---

## 8. 不要做什么（已证明无效或高风险）

1. **不要再调 `max_new_speak_tokens_per_chunk`。**
   —— 已实测证明它不是瓶颈：模型每拍本来就只吐约 25 个 token（1.00s 音频），
   从 26 调到 60 **没有任何效果**。
2. **不要把块长改成 0.5 秒。**
   —— 官方 issue #3 明确说 0.5s 单元"会轻微降低模型智能"，是留给下一个模型版本的。
   实测 0.5s 块长的产出仍是 1.00s/拍，没有改善。
3. **不要假设 wrapper 的尾部等待还能继续缩短。**
   —— 它**已经丢过一次整句音频**（模型说了话，客户端一个字节都没收到）。
   任何缩短必须先构造"尾音晚于本轮结束"的场景做对照实验。
4. **不要在没有验证的情况下断言"双工模式下 `text` 字段会被丢弃"。**
   —— 读 master 源码得出的这个结论与本机实测结果相反。本机实测它是**有效**的。
5. **不要去找 macOS 系统级 AEC。**
   —— 已核实：sounddevice 的 `CoreAudioSettings` 没有 AEC 项，PortAudio 的 `pa_mac_core.h` 也没暴露
   VoiceProcessingIO，没有支持它的 PortAudio Python 绑定。唯一可用是 `livekit` 的 WebRTC AEC3。
6. **不要盲目重编译 C++。**
   —— Windows 上没有任何编译工具链（已实测），安装 CUDA Toolkit + VS Build Tools 约 10GB；
   且当初走"零编译路线"正是因为担心 sm_120 的 PTX 问题。要做的话请先写清楚风险和回退方案。
7. **不要在服务运行中重启或发测试请求而不先确认。**
   —— 用户可能正在做真机测试。
8. **不要把本文档里的推测当成事实传播。** 特别是 §4 表格里置信度"中"及以下的每一条。

---

## 9. 你的审计任务

按顺序执行，**每一步都要留下证据**：

1. **通读项目**：先把 §2 的架构和 §5 的关键文件实际读一遍。**不要跳读。**
2. **识别版本**：确定 Windows 上那个 `llama-server.exe` 到底是哪个 commit/release 构建的
   （线索：`server.log` 启动行有 `build: 74 (4d28373)`）。判断它到底含不含 PR #47 / #78 / #101 / #108。
   **这一条会直接决定"该不该重编译"这个结论。**
3. **复验关键假设**（至少这几条）：
   - 模型每拍真的只产 1 秒音频吗？（用 `wav_timing.log` + 自己的实验）
   - 模型真的从不输出 `<task>` 吗？（看 `llm_text.txt`，并考虑是否存在服务端过滤）
   - 双工下 `text` 字段真的生效吗？（构造纯文本 prefill 实验）
   - `listen_prob_scale` 真的影响说话连续性吗？（有无对照）
   - 回声明除真的削掉了回声吗？（真机或合成回声实验）
4. **定位 root cause**：给出你版本的 Core Problem，明确标注哪些是**证据支持**、哪些是**推断**。
   如果定不了，就写 `Root cause unknown` 并列出候选和证据。
5. **判断架构是否值得继续**：明确回答"修补 vs 重构"，给理由。
6. **给出最小可验证修复方案**：一次只改一处，每处都要说清楚"预期看到什么变化"。
7. **先验证，再修改。** 不要在没有对照实验的情况下改动任何东西。
8. **如果发现原方案方向就是错的，直接说**，不要继续打补丁。

---

## 10. 交付物

一份报告，包含：

```
## 1. 我实际验证了什么（列出实验 + 原始输出）
## 2. 我推翻/确认了原 Agent 的哪些结论
## 3. Root cause（或 Root cause unknown + 候选清单）
## 4. 故障树更新（每条标注：已确认/高概率/可能/已排除/未测试）
## 5. 架构判断：修补 vs 重构（附理由）
## 6. 最小可验证修复方案（排序，每条含预期观测）
## 7. 如果只能做一件事，做哪件
```

**再次强调：优先诊断，后修改；优先证据，后猜测；优先最小可验证实验，后大规模改代码。**
