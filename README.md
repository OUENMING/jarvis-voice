# jarvis-voice

级联式语音助手。**戴着耳机说话,它自动接话;播报中直接插话即可打断。**

```
麦克风 → VAD(Silero) → ASR(SenseVoice 本地) → Claude Code 常驻子进程
      → 切句 → 清洗 → TTS(Fish Audio 云) → 扬声器
```

Claude Code 是"脑"——它**有工具**(Bash/Edit/Read + Tavily 搜索 + 浏览器),所以助手真的能查东西、改文件、开网页,不只是聊天。

## 跑起来

```bash
./jarvis.sh start --dashboard      # 耳机模式 + 可视化仪表盘
./jarvis.sh start --speaker        # 免提(需 JARVIS_OUTPUT_DEVICE=MacBook,否则输出仍走耳机)
./jarvis.sh start --fresh          # 不复用上次对话（**默认会 resume**，见下）
./jarvis.sh status                 # 看有几个实例在跑
./jarvis.sh stop
```

⚠️ **一定用 `jarvis.sh` 而不是手动 `pkill`** —— 详见下面「踩过的坑」。

需要 `~/.jarvis/fish.env` 里有 `FISH_API_KEY`,以及 `claude` CLI 已登录。

## 记忆（脑认识主人 + 长期记忆）

`claude --bare` **跳过 CLAUDE.md 自动发现和 auto-memory**（一手实测 2.1.266：无法在 bare 下恢复），
所以"脑认识主人"必须**显式注入**。三样东西：

| 文件 / 机制 | 作用 |
|---|---|
| `~/.jarvis/persona.md` | **人工维护**的身份与世界事实（"Brightspace = UCD 课程平台"这类）。改完重启生效 |
| `~/.jarvis/memory.md` | 脑听到「**记住 X**」时自己追加的可写记忆（一行一条）。定期人工压缩 |
| `--resume`（**默认开**） | 会话连续性：重启记得上次聊的。要清就加 `--fresh` |

启动时把 [口语规则] + persona + memory 合成 `~/.jarvis/system_prompt.md`，用
`--system-prompt-file` 喂给 claude（一手验证：内容真进上下文）。

**第二大脑（Obsidian）按需热挂**：启动时 Obsidian 没开 → 工具缺席（秒失败，不拖启动）；
Obsidian 起来后说一句「**连上第二大脑**」即可热重连（走 `mcp_reconnect` 控制帧），**不用重启脑进程**。

## 代码地图

| 路径 | 作用 |
|---|---|
| `jarvis_voice/orchestrator.py` | 主循环 + 状态机 + 打断检测(接线中心) |
| `jarvis_voice/session.py` | 状态(IDLE/THINKING/SPEAKING) + `turn_id` 轮次作废 |
| `jarvis_voice/player.py` | sounddevice 回调播放,支持 `flush`/`cut_tag` |
| `jarvis_voice/vad.py` | VAD 门 + 噪声底信噪比门限(防"没出声自己说话") |
| `jarvis_voice/asr.py` | SenseVoice(本地) / Fish(云),含专名纠正表 |
| `jarvis_voice/tts/` | `base`(抽象) / `fish`(WebSocket 流式) / `say`(兜底) |
| `jarvis_voice/filler.py` | backchannel + 停口令判定 |
| `jarvis_voice/fillers.py` | 填充音音频片(预渲染,盖住思考期空白) |
| `jarvis_voice/commands.py` | 元命令("清理一下上下文"/"暂停监听") |
| `jarvis_voice/sanitize.py` | 朗读前的确定性清洗(markdown/URL/emoji/空行) |
| `jarvis_voice/events.py` + `dashboard.py` | 事件总线 + 本地仪表盘(只绑 127.0.0.1) |
| `jarvis_voice/config.py` | 全部配置与阈值 |
| `claude_bridge.py` | 常驻 `claude` 子进程(stream-json、可中断、会话持久化) |

## 说几句就能做的事

- **打断**:播报中直接开口(耳机模式)
- **"停一下"**:就地闭嘴,不消耗一轮 CC
- **"清理一下上下文"**:清空 CC 的会话记忆(不重启进程)
- **"开个新窗口"**:"脑"用浏览器工具做
- **"暂停监听"/"继续监听"**
- **"记住 X"**：写进 `~/.jarvis/memory.md`，下次还记着
- **"连上第二大脑"**：热重连 Obsidian 笔记库（Obsidian 得开着）

## ⚠️ 踩过的坑(改代码前先看)

1. **杀进程用 `jarvis.sh`**。macOS 上真实进程名是 `.../MacOS/Python`(**大写 P**), `pkill -f "python -m jarvis_voice"` **永远匹配不到**,只会杀掉 zsh 包装,留下**孤儿实例继续抢麦克风**。曾同时跑 3 个,对着旧代码说话还以为新功能没生效。

2. **`--resume` 默认关闭，而且它很危险**。会话是**链式**的：每次恢复都继承上一次的全部
   上下文 → 一旦链条被污染（比如跑过测试），助手会带着**完全陌生的记忆**醒来。
   真机踩过两次。要跨天记忆时显式加 `--resume`，并先想清楚隔离。

3. **别用 `print` 调试实时音频路径**。`Player._cb` 是实时线程。

4. **`claude --bare` 下没有 WebSearch/WebFetch/Write**。工具集是刻意的最小集(启动快),搜索走 Tavily MCP。

5. **Fish 的 `model` 必须放 HTTP header**,放 body 会静默掉回付费模型 → 402。

6. **SenseVoice 对任何非语音都会"幻觉"出文本**(静音→`그。`、白噪声→`Yeah.`)。文本层过滤无效,只能在音频层拦(见 `vad.py` 的信噪比门限)。

7. **`--bare` 下没有 CLAUDE.md / auto-memory**，而且 `--add-dir` **不**注入"所加目录"的 CLAUDE.md（注的是 `~/.claude/CLAUDE.md`——那是编码指令，对语音脑是污染）。脑的常识只能靠 `--system-prompt-file` 显式喂（见「记忆」）。

## 安全

- `--disallowedTools` 拦了 31 条破坏性命令(递归删除/sudo/强制 push/管道执行等)。
  ⚠️ **这是防手滑的护栏,不是沙箱** —— `bash -c "rm -rf /"` 之类能绕。
- 免提模式(无 AEC)会**关闭自动打断并改为半双工**,防止麦克风听到音箱里的自己。
- 仪表盘只绑 `127.0.0.1`,代码里 `assert` 强制。
