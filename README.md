# jarvis-voice

级联式语音助手。**戴着耳机说话，它自动接话；播报中直接插话即可打断。**

```
麦克风 → AEC(免提时) → VAD(Silero) → ASR(SenseVoice 本地) → Claude Code 常驻子进程
      → 切句 → 清洗 → TTS(Fish Audio 云) → 扬声器
                 ↑ 填充音/承接句（盖住思考期）
                 ↑ 异步工具（长任务转后台，回合立刻收尾）
```

Claude Code 是"脑"——它**有工具**（Bash/Edit/Read + 搜索 + 浏览器），所以助手真的能查东西、改文件、开网页，不只是聊天。

## 跑起来

| 命令 | 作用 |
|---|---|
| `./jarvis.sh start --dashboard` | 耳机模式 + 可视化仪表盘 |
| `./jarvis.sh start --speaker-aec` | 免提 + 软件 AEC：**免提也能插话打断**（自动切内置扬声器）|
| `./jarvis.sh start --speaker` | 免提但**无 AEC**：播出时不听麦克风、不能插话（对照用）|
| `./jarvis.sh start --fresh` | 不复用上次对话（**默认会 resume**，见坑 #2）|
| `./jarvis.sh status` / `./jarvis.sh stop` | 看有几个实例在跑 / 全停 |

⚠️ **一定用 `jarvis.sh` 而不是手动 `pkill`**（见坑 #1）。
⚠️ **`--speaker-aec` 只消「我们自己播的」回声。** 环境里别人的声音（网课、视频、别人的语音）消不掉——那需要说话人分离，是另一条路（`docs/RESEARCH-AEC-20260919.md` §2.4）。放视频请用耳机。

前置：`~/.jarvis/fish.env` 里有 `FISH_API_KEY`，且 `claude` CLI 已登录。

**环境变量**（`python -m jarvis_voice --help` 也能看）：

| 变量 | 用途 |
|---|---|
| `JARVIS_AUDIO_MODE` / `JARVIS_TTS` / `JARVIS_VAD` / `JARVIS_ASR` | 四种后端/模式的开关 |
| `JARVIS_{INPUT,OUTPUT}_DEVICE` | 设备名子串（免提必须两个都切，否则麦克风听不到自己）|
| `JARVIS_BARE=0` | 非 bare：拿到 skills + 懒加载工具，每轮 +156ms |
| `JARVIS_BRAIN_COMPACT_WINDOW` | **脑的上下文窗口。不设 = 自动压缩永不触发**（见坑 #3）|

## 记忆（脑认识主人 + 长期记忆）

`claude --bare` **跳过 CLAUDE.md 自动发现和 auto-memory**（一手实测 2.1.266：bare 下无法恢复），所以"脑认识主人"必须**显式注入**。

| 文件 / 机制 | 作用 |
|---|---|
| `~/.jarvis/persona.md` | **人工维护**的身份与世界事实（"Brightspace = UCD 课程平台"这类）。改完重启生效 |
| `~/.jarvis/memory.md` | 说「**记住 X**」时**确定性追加**一行（去重、绝不自动删）。它也会注入系统提示，所以保持精简 |
| `--resume`（**默认开**） | 会话连续性：重启记得上次聊的。要清就加 `--fresh` |

启动时把 [口语规则] + persona + memory 合成 `~/.jarvis/system_prompt.md`，用 `--system-prompt-file` 喂给 claude（一手验证：内容真进上下文）。

⚠️ **`memory.md` 的写入下次启动才进系统提示**（`_compose_system_prompt` 只在启动跑）——会话内不等它，CC 本来就看得见这轮对话。

**第二大脑（Obsidian）按需热挂**：启动时 Obsidian 没开 → 工具缺席（秒失败，不拖启动）；后来起来了说一句「**连上第二大脑**」即可热重连（走 `mcp_reconnect` 控制帧），**不用重启脑进程**。

## 代码地图

| 路径 | 作用 |
|---|---|
| `jarvis_voice/orchestrator.py` | 主循环 + 状态机 + 打断检测（接线中心）|
| `jarvis_voice/session.py` | 状态（IDLE/THINKING/SPEAKING）+ `turn_id` 轮次作废 |
| `jarvis_voice/player.py` | sounddevice 回调播放；支持 `flush`/`cut_tag`；**AEC 的 far 参考镜像** |
| `jarvis_voice/aec.py` | 免提 AEC 门（far 参考锚在播放时钟上）|
| `jarvis_voice/vad.py` | VAD 门 + 噪声底信噪比门限 + **预滚缓冲**（治首字判错）|
| `jarvis_voice/echoguard.py` | 回声文本护栏（含**填充音**的短窗口通道）|
| `jarvis_voice/asr.py` | SenseVoice（本地）/ Fish（云），含专名纠正表 |
| `jarvis_voice/tts/` | `base`（抽象）/ `fish`（REST 流式）/ `say`（兜底）|
| `jarvis_voice/filler.py` | backchannel + 停口令判定 |
| `jarvis_voice/fillers.py` | 填充音/承接句音频池（预渲染，按工具类别分池）|
| `jarvis_voice/commands.py` | 元命令（清空上下文 / 暂停 / 记住 X / 连第二大脑）|
| `jarvis_voice/sanitize.py` | 朗读前的确定性清洗（markdown/URL/emoji/空行）|
| `jarvis_voice/events.py` + `dashboard.py` | 事件总线 + 本地仪表盘（只绑 127.0.0.1）|
| `jarvis_voice/config.py` | 全部配置与阈值 |
| `claude_bridge.py` | 常驻 `claude` 子进程（stream-json、可中断、会话持久化、**非应答轮分流**）|

## 说几句就能做的事

| 说 | 会发生什么 |
|---|---|
| *（播报中直接开口）* | 打断 |
| 「停一下」 | 就地闭嘴，不消耗一轮 CC |
| 「清空上下文」 | 清空 CC 的会话记忆（不重启进程）|
| 「记住 X」 | 追加进 `~/.jarvis/memory.md`，并把这句也送 CC（本会话内它也知道）|
| 「暂停监听」/「继续监听」 | 不再听 / 重新听麦克风 |
| 「连上第二大脑」 | 热重连 Obsidian 笔记库（Obsidian 得开着）|
| 「开个新窗口」 | 脑用浏览器工具做 |

## ⚠️ 踩过的坑（改代码前先看）

1. **杀进程用 `jarvis.sh`**。macOS 上真实进程名是 `.../MacOS/Python`（**大写 P**），`pkill -f "python -m jarvis_voice"` **永远匹配不到**，只杀掉 zsh 包装，留下**孤儿实例继续抢麦克风**。曾同时跑 3 个，对着旧代码说话还以为新功能没生效。

2. **`--resume` 默认开，是把双刃剑**。会话是**链式**的：每次恢复都继承上次全部上下文。好处是跨天记得上次聊的；代价是**一旦链条被污染**（比如跑过测试），助手会带着**陌生的记忆**醒来——真机踩过两次。要干净重来加 `--fresh`。
   ⚠️ **链条只增不减**：实测一个会话跨 **73 小时 / 215 轮 / 37 万 token** 仍在用。上下文越大首字越慢（200–300K 时 p50 1969ms vs <20K 的 1295ms）。

3. **走代理时自动压缩根本不会武装** —— 必须显式设窗口，否则上下文只增不减，且撞上限后**无法恢复**（reactive 兜底会被代理改写的错误文案挡住）。见 `docs/FIXES-VOICE-20260920.md` §4.1。

4. **别用 `print` 调试实时音频路径**。`Player._cb` 是实时线程。

5. **`claude --bare` 下没有 WebSearch/WebFetch/Write**。工具集是刻意的最小集（启动快），搜索走 MCP。

6. **Fish 的 `model` 必须放 HTTP header**，放 body 会静默掉回付费模型 → 402。

7. **SenseVoice 对任何非语音都会"幻觉"出文本**（静音→`그。`、白噪声→`Yeah.`）。文本层过滤无效，只能在音频层拦（`vad.py` 的信噪比门限）。

8. **填充音会被麦克风收回去转写**（≤2 字碎片里 47% 紧跟在填充音之后）。任何**播出去的短音频**都必须走 `echo_guard.note_spoken(text, filler=True)`，否则助手会自己回应自己。

9. **`--bare` 下 `--add-dir` 不注入所加目录的 CLAUDE.md**（注的是 `~/.claude/CLAUDE.md`——那是编码指令，对语音脑是污染）。脑的常识只能靠 `--system-prompt-file` 显式喂。

## 安全

- `--disallowedTools` 拦了 31 条破坏性命令（递归删除/sudo/强制 push/管道执行等）。
  ⚠️ **这是防手滑的护栏，不是沙箱** —— `bash -c "rm -rf /"` 之类能绕。
- 免提模式（无 AEC）会**关闭自动打断并改为半双工**，防止麦克风听到音箱里的自己。装了 AEC（`--speaker-aec`）后这两个限制都解除。
- 仪表盘只绑 `127.0.0.1`，代码里 `assert` 强制。
