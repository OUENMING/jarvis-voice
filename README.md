# jarvis-voice · 本地语音贾维斯

> 戴着耳机跟电脑说话，它自动接话、播报中直接插话就能打断 —— 而且**真的能干活**（查资料 / 改文件 / 开网页）。

[![License: MIT](https://img.shields.io/badge/License-MIT-green?style=flat-square)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.14-blue?style=flat-square)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-macOS%20Apple%20Silicon-lightgrey?style=flat-square)](#环境要求)
[![Brain](https://img.shields.io/badge/Brain-Claude%20Code-orange?style=flat-square)](https://claude.com/claude-code)
[![ASR](https://img.shields.io/badge/ASR-SenseVoice%20local-success?style=flat-square)](https://github.com/FunAudioLLM/SenseVoice)

---

## 项目介绍

### 为什么做这个

「钢铁侠的贾维斯」难的**不是语音，是让它能干活、还能把结果一字不差地念出来**。

这条硬需求直接定了架构：**端到端语音模型结构上做不到忠实逐字朗读**
（这个假设花了一个月才验证完，本该半天），所以走**级联** ——
语音只负责进出口，"思考"整个交给 **Claude Code 常驻子进程**。

代价换来的是：脑**天然拥有** Claude Code 的全部工具（Bash / Edit / Read / 搜索 / 浏览器），
不是只会聊天的玩具。而且"脑"是可替换的 —— 换个 `--model` 就换智力水平。

### 核心功能

- **免提也能打断** — 软件 AEC（`pywebrtc-audio` + 自己的 far 参考），本机稳态 **ERLE 34–36 dB**
- **工具轮不等静默** — 承接句（按工具类别选句子）+ **异步工具**（长任务转后台、回合立刻收尾）
- **本地元命令** — 「清空上下文 / 暂停 / 记住 X / 连第二大脑」在编排层就地处理，不消耗一轮 CC
- **长期记忆** — `persona.md`（人工）+ `memory.md`（说「记住 X」确定性追加），启动时注入系统提示
- **回声护栏** — 把自己播出去的话（含填充音）从转写里拦掉，防"助手回应自己"
- **本地仪表盘** — 对话流 / 工具调用 / 电平 / 状态，只绑 `127.0.0.1`

### 适用场景

- 想要一个**能干活**的语音助手（不是只会聊天）
- 学习**级联语音架构**（VAD → ASR → LLM → TTS）的完整可跑参考
- 想拿 Claude Code 当"脑"塞进别的交互形态（本项目把 `claude` 当常驻子进程用）

## 实测性能（M1 Pro 16GB）

| 指标 | 值 | 条件 |
|---|---|---|
| 闲聊首句 | **p50 2.1s** | 无工具调用 |
| TTS 首包 | **478ms** | Fish REST（换 REST 前是 1559ms）|
| 打断延迟 | **400ms** | 语音起点 → 停（修前 600ms）|
| AEC 稳态 ERLE | **34–36 dB** | 免提，本机 |
| 工具轮首句 | p50 **14.3s** / p90 49.3s | ⚠️ 修承接句与异步工具**之前**测的，见「已知限制」|

## 功能清单

| 功能名称 | 功能说明 | 技术栈 | 更新时间 |
|---------|---------|--------|----------|
| VAD 门 | Silero VAD + 噪声底信噪比门限 + 900ms 预滚 | sherpa-onnx | 2026-09-20 |
| ASR | SenseVoice（本地、免费）+ 专名纠正表 | sherpa-onnx | 2026-09-20 |
| 打断 | VAD 起音触发，带静音门槛防抖动误判 | 自研状态机 | 2026-09-20 |
| 打断误判恢复 | 起音先暂停（留缓冲），转写若是「嗯」这类应答就**接着播** | 自研（LiveKit 同款语义）| 2026-09-20 |
| 免提 AEC | WebRTC AEC3 + far 参考锚在播放时钟上 | pywebrtc-audio | 2026-09-19 |
| 承接句 | 按工具类别播预渲染短句（覆盖 98% 工具调用）| 自研 | 2026-09-20 |
| 异步工具 | 长任务转后台，回合提前收尾、跑完自动汇报 | Claude Code 后台任务 | 2026-09-20 |
| 填充音 | 预渲染音频盖住思考期空白 | 自研 | 2026-09-20 |
| 回声文本护栏 | 字符级 LCS 判"自己刚说的话又被转写回来" | 自研 | 2026-09-20 |
| 元命令 | 清空上下文 / 暂停 / 记住 X / 连第二大脑 | 自研 | 2026-09-20 |
| 长期记忆 | persona + memory 注入系统提示 | Claude Code `--system-prompt-file` | 2026-09-20 |
| 会话持久化 | `--resume` 跨重启记得上次聊的 | Claude Code | 2026-09-18 |
| 本地仪表盘 | 对话流 / 工具 / 电平 / 状态，SSE 推送 | FastAPI + uvicorn | 2026-09-20 |
| 口语化约束 | prompt 层强约束（首句 60→9 字） | 自研 | 2026-09-16 |

## 技术栈

| 技术 | 版本 | 用途 | 官网 |
|------|------|------|------|
| Python | 3.14 | 全部实现 | https://www.python.org |
| Claude Code | 2.1.278 | **脑**：常驻 `claude` 子进程（stream-json）| https://claude.com/claude-code |
| sherpa-onnx | 1.13.7 | Silero VAD + SenseVoice ASR（**本地**）| https://github.com/k2-fsa/sherpa-onnx |
| SenseVoice | 2024-07-17 | ASR 模型（中英日韩粤）| https://github.com/FunAudioLLM/SenseVoice |
| pywebrtc-audio | 0.2.0 | WebRTC AEC3（免提回声消除）| https://pypi.org/project/pywebrtc-audio |
| Fish Audio | s2.1-pro-free | 云 TTS（REST 流式）| https://fish.audio |
| sounddevice | 0.5.6 | PortAudio 绑定（麦克风 / 扬声器）| https://python-sounddevice.readthedocs.io |
| FastAPI + uvicorn | 0.141 / 0.52 | 本地仪表盘 | https://fastapi.tiangolo.com |
| numpy / scipy | 2.5 / 1.18 | 音频处理、重采样 | https://numpy.org |

### 技术架构

```
麦克风 ──► AEC(免提时) ──► VAD(Silero) ──► ASR(SenseVoice 本地)
             ▲                                      │
             │ far 参考                              ▼
        Player 播放时钟                    Claude Code 常驻子进程（脑）
             ▲                                      │
             │                              切句 ──► 清洗
             │                                      │
             └────────── TTS(Fish 云) ◄─────────────┘
                          ▲
                  填充音 / 承接句（盖住思考期）
                  异步工具（长任务转后台，回合立刻收尾）
```

**并发模型**：线程 + 有界队列，**全栈阻塞式**，不混 asyncio。
五个线程：主循环（麦克风）/ `BrainThread` / `TTSThread` / `AsyncThread` / 第二大脑看门狗。

## 项目结构

```
jarvis-voice/
├── jarvis_voice/
│   ├── orchestrator.py        # 主循环 + 状态机 + 打断检测（接线中心）
│   ├── config.py              # 全部配置与阈值（env 可覆盖）
│   ├── session.py             # 状态（IDLE/THINKING/SPEAKING）+ 轮次作废
│   ├── audio_io.py            # MicStream + 设备解析
│   ├── aec.py                 # 免提 AEC 门（far 参考锚在播放时钟上）
│   ├── player.py              # sounddevice 回调播放 + far 镜像
│   ├── vad.py                 # VAD 门 + 信噪比门限 + 预滚缓冲
│   ├── asr.py                 # SenseVoice（本地）/ Fish（云）+ 专名纠正
│   ├── echoguard.py           # 回声文本护栏（含填充音短窗口通道）
│   ├── filler.py              # backchannel + 停口令判定
│   ├── fillers.py             # 填充音/承接句池（预渲染，按工具类别分池）
│   ├── commands.py            # 元命令匹配
│   ├── sanitize.py            # 朗读前的确定性清洗
│   ├── events.py              # 事件总线
│   ├── dashboard.py           # 本地仪表盘（只绑 127.0.0.1:8848）
│   ├── __main__.py            # 入口（参数解析 + 仪表盘生命周期）
│   └── tts/                   # base / fish(REST 流式) / say(兜底)
├── claude_bridge.py           # 常驻 claude 子进程（stream-json、可中断、非应答轮分流）
├── jarvis.sh                  # 启停脚本（**杀进程必须用它**）
├── tests/                     # 29 个独立测试脚本
├── docs/                      # 调研 / 施工单 / 修复记录（索引见 docs/README.md）
├── models/                    # VAD + ASR 模型（230MB，**不入库**，见「安装」）
└── requirements.txt
```

## 环境要求

- **macOS + Apple Silicon**（必须在 Mac 上跑：兜底 TTS 用系统 `say`，设备选择依赖 CoreAudio）
- Python **3.14**
- `claude` CLI 已登录（脑）
- Fish Audio API key（TTS）
- ~700MB 磁盘（`.venv` 447M + `models/` 230M）

## 安装

```bash
# 1. 克隆 + 依赖
git clone https://github.com/OUENMING/jarvis-voice.git
cd jarvis-voice
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 2. 模型（230MB，不入库）—— 从 k2-fsa/sherpa-onnx 的 asr-models release 拿
mkdir -p models && cd models
curl -SL -O https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2
tar xvf sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2
rm sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2
#    代码读的是 model.int8.onnx（239MB）。包解出来的目录名带 -int8-，默认路径不带，
#    所以要么改名（下面这句），要么设 JARVIS_ASR_MODEL 指过去。
mv sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17 \
   sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17
mkdir -p vad && curl -SL -o vad/silero_vad.onnx \
   https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx
cd ..

# 3. 凭据与个人数据（都不入库，放 ~/.jarvis/）
mkdir -p ~/.jarvis
echo 'FISH_API_KEY=你的key' > ~/.jarvis/fish.env && chmod 600 ~/.jarvis/fish.env
#    persona.md —— 人工维护的"身份与世界事实"，脑开机就读它。不写也能跑，只是它不认识你
#    memory.md  —— 说「记住 X」时自动追加；不建会自动跳过

# 4. MCP —— 「查资料 / 开网页」全靠它，不配就只有 Bash/Edit/Read
cp mcp-jarvis.example.json mcp-jarvis.local.json   # 这个文件名已在 .gitignore 里
#    填 key 后，三个 server 按需取舍：
#      tavily        搜索（https://tavily.com 拿 key）
#      browser       浏览器自动化（本项目用的是 claude-code-browser，需填绝对路径）
#      obsidian-vault 第二大脑（Obsidian 得开着，token 在 Local REST API 插件里）
#    也可用 JARVIS_MCP_CONFIG=<路径> 指到别处。

# 5. 跑
./jarvis.sh start --dashboard      # 耳机模式 + 仪表盘 http://127.0.0.1:8848
```

⚠️ **免提真生效**必须把**输入输出都切到内置设备**，否则麦克风听不到自己、等于还是耳机。
`./jarvis.sh start --speaker-aec` 会自动设 `JARVIS_OUTPUT_DEVICE=MacBook`。

## 使用说明

| 命令 | 作用 |
|---|---|
| `./jarvis.sh start --dashboard` | 耳机模式 + 仪表盘 |
| `./jarvis.sh start --speaker-aec` | 免提 + AEC：**免提也能插话打断** |
| `./jarvis.sh start --speaker` | 免提但无 AEC：播出时不听麦克风（对照用）|
| `./jarvis.sh start --fresh` | 不复用上次对话 |
| `./jarvis.sh status` / `stop` | 看有几个实例在跑 / 全停（**别用 pkill**，见开发笔记 #1）|

**环境变量**（`python -m jarvis_voice --help` 也能看）：

| 变量 | 用途 |
|---|---|
| `JARVIS_AUDIO_MODE` / `JARVIS_TTS` / `JARVIS_VAD` / `JARVIS_ASR` | 后端 / 模式开关 |
| `JARVIS_{INPUT,OUTPUT}_DEVICE` | 设备名子串（免提必须两个都切）|
| `JARVIS_BARE=0` | 非 bare：拿到 skills + 懒加载工具，每轮 +156ms |
| `JARVIS_BRAIN_COMPACT_WINDOW` | 脑的上下文窗口。**不设 = 自动压缩永不触发**（见开发笔记 #3）|

### 说几句就能做的事

| 说 | 会发生什么 |
|---|---|
| *（播报中直接开口）* | 打断 |
| 「停一下」 | 就地闭嘴，不消耗一轮 CC |
| 「清空上下文」 | 清空 CC 的会话记忆（不重启进程）|
| 「记住 X」 | 追加进 `~/.jarvis/memory.md`，并把这句也送 CC |
| 「暂停监听」/「继续监听」 | 不再听 / 重新听 |
| 「连上第二大脑」 | 热重连 Obsidian 笔记库（Obsidian 得开着）|

### 跑测试

29 个**独立脚本**（不是 pytest），用法 `.venv/bin/python tests/test_x.py`。

```bash
for t in tests/test_*.py; do .venv/bin/python "$t" >/dev/null 2>&1 \
  && echo "✅ $t" || echo "❌ $t"; done
```

⚠️ 部分测试会**真实出声**（`test_orchestrator` / `test_spoken_style` / `test_tts` / `test_filler`）
—— 跑之前先确认音量或戴耳机。

## 开发笔记

1. **杀进程必须用 `jarvis.sh`**。macOS 上真实进程名是 `.../MacOS/Python`（**大写 P**），`pkill -f "python -m jarvis_voice"` **永远匹配不到**，只杀掉 zsh 包装，留下**孤儿实例继续抢麦克风**。曾同时跑 3 个，对着旧代码说话还以为新功能没生效。

2. **`--resume` 默认开，是把双刃剑**。会话**链式**继承上下文：好处是跨天记得上次聊的；代价是**一旦链条被污染**（比如跑过测试），助手会带着陌生记忆醒来 —— 真机踩过两次。要干净重来加 `--fresh`。

3. **走代理时自动压缩根本不会武装**。Claude Code 判定压缩的第一道闸是「窗口来源 ≠ auto」，而走代理拿不到服务端窗口表 → 直接放弃。实测 **250+ 个脑会话零压缩**（其中一个跨 73 小时 / 215 轮 / 37 万 token 也没压）。必须显式设 `CLAUDE_CODE_AUTO_COMPACT_WINDOW` —— 不设的话撞上限后**无法恢复**（reactive 兜底会被代理改写的错误文案挡住）。

4. **音频探针绝不能和被测应用抢同一个设备**。同进程开 `OutputStream`(44100) + `Stream`(16000 双向) → macOS 把设备切到 16000 → 44100 的数据慢放 2.76× → **265Hz 变成 96Hz（男声）**，还丢帧。这条踩了整晚，而且**误导了当时全部音频判断**。

5. **别用 `print` 调试实时音频路径**。`Player._cb` 是实时线程。

6. **SenseVoice 对任何非语音都会"幻觉"出文本**（静音 → `그。`、白噪声 → `Yeah.`）。文本层过滤无效，只能在**音频层**拦（`vad.py` 的信噪比门限）。

7. **填充音会被麦克风收回去转写**：≤2 字的插话碎片里 **47%** 紧跟在填充音之后。任何**播出去的短音频**都必须登记进回声护栏（`note_spoken(text, filler=True)`），否则助手会回应自己。

8. **Fish 的 `model` 必须放 HTTP header**，放 body 会静默掉回付费模型 → 402。

9. **切句器会被换行卡死**（本项目最难查的一个 bug）：消费掉一句后缓冲往往以 `\n` 开头，而 `\n` 在句末标点集合里 → `idx=0 < NEXT_SENT_MIN` → **立刻 break，之后永不切**。真机证据：**41/41 条 >120 字的句子全部是每轮最后一句**。表现是首句正常、后半段要等整段生成完才开始合成。

10. **`--bare` 下 `--add-dir` 不注入所加目录的 CLAUDE.md**（注的是 `~/.claude/CLAUDE.md` —— 那是编码指令，对语音脑是污染）。脑的常识只能靠 `--system-prompt-file` 显式喂。

11. **`lsof -ti:8848` 会连客户端一起列出**（也就是你的浏览器）。释放端口时必须加 `-sTCP:LISTEN`，否则 `stop` 会把浏览器杀掉。

12. **元命令匹配一律取「宁可漏判不可误判」**。放宽判据时配的三道护栏（疑问句 / 长句 / 否定句）**每一道都有真机原话作依据** —— 其中「别清空上下文」曾经会**真的把上下文清掉**。

## 已知限制

- **必须 macOS**：兜底 TTS 用系统 `say`，设备选择依赖 CoreAudio
- **工具轮首句延迟**：表里的 14.3s 是**修承接句与异步工具之前**测的。两个机制都已实现并离线验证，但**真机效果尚未验收**
- **打断误判恢复的窗口值（800ms）没在真机上调过**：它是按链路时延推的。误判撤回时，
  一句话中间会多一个约 0.8s 的顿 —— 这个代价要 A/B 才知值不值
- **对话人味只到约七成**：缺重叠说话 / 副语言交换 / 轮次协商（`docs/PLAN-HUMANNESS-20260920.md`）。而且**助手每轮说 75 字 vs 用户 10 字（7.5 倍）** —— 一次说太多
- **免提只能消自己的回声**：环境里别人的声音（网课、视频）它消不掉，那需要说话人分离。放视频请用耳机
- **上下文只增不减**：`--resume` 链式累积，修了压缩窗口但仍会跑在 100–180K 区间（首字比 <20K 时慢约 1.5 倍）
- **情绪识别只通了一半**：ASR 拿到情绪标签，但**没有驱动 TTS**（Fisher 的情感标签实测无效）

## 路线图

| 阶段 | 内容 | 状态 |
|---|---|---|
| P0 | 修切句器的换行卡死 | ✅ 已做 |
| P1 | **打断误判恢复** —— VAD 起音先暂停（保留缓冲）、800ms 内看转写是不是「嗯」，是就恢复播放（照抄 LiveKit `false_interruption_timeout` 语义）| ✅ 已做（🔴 真机未验收）|
| P2 | **轮次协商** —— 说一段、停一下、等反应（要 A/B 量，可能纯亏时间）| ⬜ 待量 |
| P3 | 助手发 backchannel（用户说话时「嗯」）| ⬜ 优先级低（用户话长中位只有 8 字）|
| — | 情感层 / 视觉 / iPad 前端 | ⬜ |

## 文档

调研、施工单、修复记录都在 [`docs/`](docs/README.md)。几篇最值得看的：

| 文档 | 内容 |
|---|---|
| [`docs/HANDOVER-20260918.md`](docs/HANDOVER-20260918.md) | **冷启动入口**：架构 / 文件地图 / 未解决问题 / 红线 |
| [`docs/FIXES-VOICE-20260920.md`](docs/FIXES-VOICE-20260920.md) | 语音层修复 + **上下文膨胀实测** + 自动压缩未武装的根因 |
| [`docs/FIXES-AUDIO-20260920.md`](docs/FIXES-AUDIO-20260920.md) | AEC 上线后的音频修复 + **已证伪假设清单** |
| [`docs/PLAN-HUMANNESS-20260920.md`](docs/PLAN-HUMANNESS-20260920.md) | 对话「人味」方案 |
| [`docs/OCR-REVIEW-20260920.md`](docs/OCR-REVIEW-20260920.md) | 第三方代码审查的核实账 |

## License

MIT

Copyright (c) 2026 OUENMING
