# OCR 全仓审查（2026-09-20）

> 工具：阿里 open-code-review（`ocr` v1.12.7），走 cc-switch → deepseek，**实花费 0**。
> 范围：`ocr scan --exclude '.gitignore,**/*.json,**/__pycache__/**'` → **31 文件 / 5,506 行**。
> 产出：**125 条**（critical 1 / high 16 / medium 72 / low 36）+ 一份 `project_summary`。
> 口径：🟢 回原码复现过 / 🔴 未核实。
>
> **项目规矩：`ocr` 的建议必须逐条回原码核实**（它在 camlife-lite 那轮给过 3 条错建议，
> 见全局记忆 `alibaba-open-code-review`）。本文档只记**已复现**的。

---

## 0. 最重要的一条：它抓到了**三条我刚写进去的 bug**

本轮改动（承接句 / 异步工具 / 清空命令 / 记忆写入）是同一晚写的，**没经过独立审查**。
`ocr` 在其中抓出 3 个真缺陷 —— 全部已复现、已修、已补 red-green 测试：

| # | 位置 | 缺陷 | 复现 |
|---|---|---|---|
| 1 | `claude_bridge.py` `ask()` | `_awaiting` 置位**晚于** `_drain_until_result()` → 排空时残留帧被判成「非应答轮」流进 `_async_q` → ① 排空永远等不到 `result`，白等 5s 超时 ② **上一轮的残句被异步线程念出来** | 🟢 新增 spy 测试，旧顺序得 `[False]`、新得 `[True]`（red-green）|
| 2 | `commands.py` `_is_clear` | 加了「以『上下文』结尾即成立」后**否定句也命中** → 说「**别**清空上下文」会**真把上下文清掉** | 🟢 5 条否定句用例，旧逻辑全错 |
| 3 | `vad.py` `_drain` | `_pre_snap` 只在**门限通过**的分支里取走清空 → 被拒的段留下**陈旧快照**给下一段（`max_speech_duration` 切出的段没有 False→True 跳变，不会刷新），把无关音频拼到段首 = 注释里警告的「首字吐两遍」 | 🟢 旧写法跑完 `_pre_snap` 是 `<ndarray shape=(22400,)>`，新写法是 `None`（red-green）|

另有一条是**我写的注释自相矛盾**（说「单独出现即成立」但代码已改成「以它结尾」），已在自审时修掉。

**教训**：同一晚写的一大片改动，自己审不出来。**代码审查要在改动之后立刻跑一次**，
而不是攒到"有空再说"。

---

## 1. 已核实为真、已修（8 项）

| # | 严重度 | 位置 | 问题 | 验证方式 |
|---|---|---|---|---|
| 1 | **critical** | `events.py` `_rotate` | `close() → replace() → open()` 三步挤在一个 try；任一步失败被 `except OSError: pass` 吞掉，而 `_fh` 已指向**关闭了的**句柄 → 下次 `emit()` 抛 **ValueError**（不是 OSError，拦不住）→ 逃进**实时麦克风主循环** | 🟢 red-green：旧实现抛 `ValueError` |
| 2 | high | `events.py` `emit` | `json.dumps` 遇不可序列化 `**data`（bytes/set）抛 **TypeError**，同样的逃逸路径 | 🟢 bytes/set/自定义对象三例 |
| 3 | high | `events.py` `close` | 不置 `None` → `BUS` 是**模块级单例**，收尾后仪表盘线程再 emit 就写到已关闭句柄 | 🟢 |
| 4 | high | `claude_bridge.py` | 见 §0-1 | 🟢 red-green |
| 5 | high | `commands.py` | 见 §0-2 | 🟢 red-green |
| 6 | high | `commands.py` `_remember_object` | `_REMEMBER_HEADS` **按长度倒序**匹配 → 「**帮我记**下明天买牛奶」先命中「帮我记」，payload 变成 **`下明天买牛奶`**（多残字）；「麻烦帮我记住X」→ `住X`。正解是取「**消耗前缀最远**」的那个 | 🟢 三条用例 |
| 7 | high | `vad.py` `_drain` | 见 §0-3；另 `snap[lo:hi]` 的 `hi` 未钳 → Python 超界切片**不报错只截断**，拼进的是尾巴 | 🟢 red-green |
| 8 | high | `sanitize.py` ×2 | ① 强调正则带 `DOTALL` + 无边界 → `a_b_c`→`abc`、`2*3*4`→`234`（**吞实义文本**，而模块 docstring 写的是"不丢任何实义文本"）；② 裸 URL 用 `\S+` → 中文没空格，`详见 https://x.com，这是说明` 整段被替换成「链接」，**「，这是说明」丢失** | 🟢 四条全部本机复现 |
| 9 | high | `jarvis.sh` | `lsof -ti:8848` **连客户端一起列出**（= 用户浏览器）→ `./jarvis.sh stop` 可能把浏览器杀掉。加 `-sTCP:LISTEN` | 🟢 命令语义 |
| 10 | high | `orchestrator.py:314` | **TOCTOU**：`set_mode()`（仪表盘线程）会在运行期把 `self.aec` 置 `None`，而主循环 `if self.aec is not None: self.aec.accept()` 两次读取之间被置空 → `AttributeError` 冲出 `run()`（它只捕 `KeyboardInterrupt`）→ **麦克风主循环静默终止** | 🟢 引用快照到局部即可；已修 |
| 11 | medium | `orchestrator.py` `_play_filler` | 回滚**无条件**置 `_filler_played_turn = None` → 会抹掉**更新的那一轮**刚设的标记 → 那一轮能再播一次 | 🟢 改成「只在仍属于自己这轮时才回滚」 |
| 12 | medium | `echoguard.py` | docstring 声称有锁，**代码里没有** —— 而本轮加「填充音回声」后，写侧多了一条 **`Timer` 线程**（`_play_filler` → `note_spoken`），与 BrainThread 的 `check()` 并发遍历同一个 list | 🟢 加锁 + `check` 内先快照再算相似度 |

---

## 1b. 第二批（同一天晚些时候补修的 5 条 high）

| # | 位置 | 问题 | 验证 / 处理 |
|---|---|---|---|
| 13 | `tests/measure_preamble.py` | `print(f"…{with_pre/total*100}…" if total else "…")` —— **f-string 先求值整个表达式**再选分支，`if total` 保护不了除法 → 没有工具回合时 `ZeroDivisionError`。而这**是 P0 的验收脚本** | 🟢 改成显式的 if/else。修完能跑出 **9/46 = 20%**（基线 8%，目标 >50%） |
| 14 | `tests/aec_resample_fix_probe.py` | `while len(x16) < N: x16 = concatenate([x16, x16])` —— `x16` 为空时永远是空的 → **死循环空转占满 CPU** | 🟢 加空数组前置校验 |
| 15 | `tests/aec_resample_probe.py` | `if/else` 两个分支给 `near_i` 赋的都是 `near`，原始意图（长度不齐时同步裁剪）**根本没生效** → A/B 在**不同时间片**上比 ERLE → 它报出的数字被系统性拉低。⚠️ 这个探针正是 `PROBE-AEC-RESULTS-20260919.md` 里「重采样必须在消费侧、生产侧掉 13.15 dB」那条结论的来源 | 🟢 改成两侧裁到 `min(长度)`，并把这条来源关系写进注释 |
| 16 | `tests/aec_doubletalk_probe.py` | 倒计时 `sleep(3)` **之后**才打印「12 秒后开始播放」→ 每条都晚 3 秒，最后一句说「3 秒后」时该立刻开口。照提示念话的人整体晚 3 秒 → 双讲段与提示错位 | 🟢 改成先打印再 sleep；`import time` 提到模块顶层 |
| 17 | `jarvis_voice/__main__.py`（两条）| ① 所有 flag 用 `in argv` 判断 → **拼错静默忽略**（最坏是 `--fressh` 拼错后**恢复了被污染的会话链**，正是该文件 docstring 警告的事）② 仪表盘线程没有停止/回收，且 `serve()` **先 print「[仪表盘] http://…」再 bind** → 端口被占时用户已经看到"已启动" | 🟢 ① 认不得的参数报错退出（码 2），`sys.exit(main())` 让退出码真的生效；② 改用 `uvicorn.Server` 句柄 + `wait_ready()`（**确认真起来了**才宣告）+ `shutdown()`，线程内异常自己报出来。测试含「随机端口能起能关」与「端口被占时 `wait_ready` 必须为 False」 |
| 18 | `jarvis_voice/tts/say.py` | docstring 声称「填充音预渲染线程与 TTSThread 会同时用同一个实例」 —— **回原码核实结论是"潜伏、当前不可达"**：`orchestrator.py:131` 给预渲染用的是**另一个实例**，且 `self.tts.stop()` **从没被调用过**（唯一调用点是 `close()`） | ⚪ **不改实现**，只把**假契约**改成真契约 + 写明「一旦改接线会怎么坏、要怎么修（打断做成调用级状态，光加锁不够）」。理由：它是**兜底 TTS**，生产走 fish |

**high/critical 最终结清：17/17**（16 条改代码 + 1 条改错误的契约说明）。

---

## 2. 未修（**reviewer 的意见对，但不在本轮范围**）

⚠️ **先看清楚这一档的性质**：剩下的 **108 条**里绝大多数是 **medium/low 的风格级、
理论级**建议（大范围 `except: pass`、`time.time()` vs `monotonic`、参数校验风格…）。
它们**不是"没修完的 bug"**，而是"可以更稳，但当前不咬人"。逐条列在
`tmp/ocr-jarvis.json` 的 `comments[]` 里，按 `severity` 筛即可。
下面是**判断过、决定先不动**的主要几类：

| 项 | 说明 | 为什么先不动 |
|---|---|---|
| 大量 `except OSError: pass` 静默 | `project_summary` 的 Top-1 主题（磁盘满/权限变更无痕） | **风格级**整改，会动很多文件；且不是一律要记日志（有些就是该静默）。建议单独一单，逐处判断 |
| 时序判据用 `time.time()` 而非 `monotonic()` | `echoguard` 窗口 / barge-in 判据 / `session` 起点 | **NTP 校时或改系统时钟**才会出问题；这台机日常不跳时钟。低风险，记着 |
| `config.py` 布尔 env 只认 `"1"`；非法值静默回退 | 与 docstring「env 可覆盖一切」不符 | 真问题，但是配置健壮性，非当前痛点 |
| `tts/fish.py` REST 回退路径缺锁 | 懒初始化 check-then-act | 生产主路径就是 REST，所以**值得看**；但 🔴 **未核实影响面**（要确认哪两处会并发），不敢照 ocr 的说法直接改 |
| `session.set_state` 是无守卫的公开写入口 | 与「检查-后-使用必须原子」的文档相矛盾 | 现有调用方都自己保证顺序；加守卫要先定语义 |
| `audio_io` 名字子串匹配失败静默回退默认设备 | 配置写错不会有提示（而"免提必须切到目标设备"是本模块前提） | 真问题，小修；排在后面 |

---

## 3. 它的 `project_summary` 五个主题（供参考，非逐条核实）

1. **异常被静默吞掉** → 故障不可观测（全仓反复出现 `except: pass`）
2. **并发与 TOCTOU**：`orchestrator` 的 `self.aec is not None` → `self.aec.accept()` 之间可被
   `set_mode()` 置 None；`session.set_state` 是无守卫的公开写入口；`echoguard` 无锁却跨线程
3. **时序用墙上时钟**（应改 `monotonic`）
4. **配置解析校验缺失 + 静默回退**
5. **子串匹配导致误命中**

第 2 条里 `orchestrator` 那个 TOCTOU 与 `echoguard` 无锁 **已修**（见 §1 的 10/12）；
`session.set_state` 是无守卫的公开写入口这条**未修**（它现在的调用方都自己保证顺序，
加守卫要先想清楚语义，不急着动）。

---

## 4. 复现与产物

```bash
# 全仓扫描（约 25 分钟 / 125 条 / 实花费 0）
ocr scan --exclude '.gitignore,**/*.json,**/__pycache__/**' \
         --format json --output <path>          # ⚠️ --output 要的是**文件**，父目录须存在

# 结果在本次会话的临目录：tmp/ocr-jarvis.json（含 comments[] 与 project_summary）
```

**怎么读它**：`comments[]` 每条有 `path / start_line / end_line / severity / category /
content / existing_code`。`severity=critical` 与 `high` 优先看，**但每一条都要回原码核实**。
