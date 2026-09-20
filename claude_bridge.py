"""
常驻 Claude Code stream-json 桥（jarvis 项目模式）

参考 sreyas-endor/jarvis：持久 `claude --input-format stream-json` 子进程，
warm turn ~1-1.5s（对比每轮冷启 claude -p 的 ~1s 进程成本 + 无复用）。

中断（2026-09-16 一手实测，claude 2.1.266，见 test_interrupt_probe.py）：
  往 stdin 写 {"type":"control_request","request_id":<uuid>,"request":{"subtype":"interrupt"}}
  → CLI 回 {"type":"control_response","response":{"subtype":"success","request_id":<echo>,
                                                         "response":{"still_queued":[...]}}}
  → 在途 turn **约 10ms 内被取消**，收尾 result 带 terminal_reason="aborted_tools"（工具期）
    或 "aborted_streaming"（流式期）；**进程与会话均存活**，下一轮照常。
  能力由 system/init 的 capabilities 声明（实测 ["interrupt_receipt_v1",
  "interrupt_cancel_queued_v1","msg_lifecycle_v1"]）。
  ⚠️ 中断会往 stdout 灌合成 user 事件（"[Request interrupted by user for tool use]"），
    下游不得误当新输入。本模块在 _read_turn 里显式忽略之。
  ⚠️ 判别"被打断"只看 terminal_reason，不看 subtype——真错误同样是
    subtype="error_during_execution"，但 terminal_reason 不是 aborted_*。

用法:
    from claude_bridge import ClaudeBridge
    bridge = ClaudeBridge(system_prompt="...", allowed_tools=["WebSearch"])
    bridge.start()
    for ev in bridge.ask("你好"):
        ...  # {"type":"sentence"|"tool"|"done"|"interrupted"|"error", ...}
    bridge.interrupt()   # 可在另一线程调用，打断在途 turn
    bridge.stop()
"""
import json
import os
import queue
import subprocess
import threading
import time
import uuid

# 会话持久化：让"进程重启就失忆"变成"重启还记得"。
# 实测（2026-09-16）：`--resume <session_id>` 在 stream-json 常驻模式下有效。
SESSION_PATH = os.path.expanduser("~/.jarvis/session.json")


def _wipe_session_file():
    try:
        os.remove(SESSION_PATH)
    except OSError:
        pass


class ClaudeBridge:
    """常驻 claude 子进程，stream-json 双向通信。

    线程模型：
      - _pump（单读线程）：唯一 stdout 读者，把事件推入 _pending
      - ask（生成器）：同一时刻只允许一轮（_turn_lock）
      - interrupt：**不占 _turn_lock**，只短暂拿 _write_lock 写一帧 stdin
        → 一轮进行中也能打断（这正是旧版的最大缺陷：旧 ask 用 with self._lock
          把整个读循环包住，interrupt 根本挤不进去）
    """

    SENT_END = "。！？；!?\n"
    FIRST_SENT_MIN = 1     # 首句：≥1 字符即可发（"好的。" 不再被 idx<4 卡住）
    NEXT_SENT_MIN = 4      # 后续句：≥4 字符，避免碎句

    def __init__(self, system_prompt: str = "", model: str = "haiku",
                 allowed_tools: list[str] | None = None, cwd: str | None = None,
                 disallowed_tools: list[str] | None = None,
                 resume: bool = False, system_prompt_file: str | None = None,
                 bare: bool = True, compact_window: int = 0):
        """`resume=False` 是**刻意的默认**。

        ⚠️ 真机踩过：默认开启持久化时，**测试脚本与正式应用共用同一个
        `~/.jarvis/session.json`** —— 跑测试留下的"暗号/数到四十"被存进去，
        应用启动时把它们当自己的记忆拉回来，用户听到助手在复述**别人的对话**。
        所以持久化必须由**应用层显式开启**（orchestrator 传 cfg.resume_session），
        裸桥默认既不存也不恢复。
        """
        self.system_prompt = system_prompt
        self.system_prompt_file = system_prompt_file
        self.model = model
        self.allowed_tools = allowed_tools or []
        self.disallowed_tools = disallowed_tools or []
        self.resume = resume
        self.bare = bare
        self.compact_window = compact_window
        self.cwd = cwd or os.getcwd()
        self.proc: subprocess.Popen | None = None
        self.session_id: str | None = None
        self.capabilities: list[str] = []
        self.total_cost = 0.0

        self._write_lock = threading.Lock()   # 只锁 stdin 写
        self._pending_lock = threading.Lock()  # 保护 _pending
        self._turn_lock = threading.Lock()     # 一轮独占；interrupt 不碰它
        self._pending: list[dict] = []
        self._ready = threading.Event()        # init 到达时置位

        self._interrupt_rids: set[str] = set()  # 已发出、待回执的 interrupt request_id
        self._interrupted = False               # 本轮是否已被打断（由回执置位）
        self._turn_open = False                 # 已发消息但还没收到 result
        # 🆕 「非应答轮」通道（后台工具 / CC 自主续跑）—— 见 docs/WORKORDER-ASYNC-01.md
        # 为什么需要：CC 在后台任务完成后会**自己起一轮**汇报结果。那一轮的帧如果
        # 落进 `_pending`，会被下一个 ask() 当成它自己的回答吐出去，且那段的 result
        # 会把真正的这一轮**提前结束**。所以必须分流。
        self._awaiting = False                  # 有没有 ask() 正等着 result
        self._tasks: queue.Queue = queue.Queue()      # task_started/updated/notification/changed
        self._async_q: queue.Queue = queue.Queue()    # 非应答轮翻译后的事件
        self._async_buf = ""                          # 非应答轮的切句缓冲
        self._saved_id: str | None = None       # 已落盘的 session_id（避免重复写）
        self._control_lock = threading.Lock()   # 保护 _control
        self._control: dict[str, dict] = {}     # rid → {"ev": Event, "resp": dict}（通用控制帧回执）

    # ---------- 生命周期 ----------
    @staticmethod
    def _write_min_settings() -> str:
        """非 bare 模式用：写一份「插件全关」的 settings，返回路径。

        为什么要关：实测那 3 个插件（claude-mem / discernment-nudge / i-have-adhd）
        在**常驻会话**里值 400ms/轮 —— 关掉后非 bare 只比 bare 贵 156ms。
        ⚠️ **动态读**用户现有插件列表再逐个置 false，不硬编码名字 ——
        否则用户新装了插件就漏关（而且会静默地慢）。
        """
        import json
        from pathlib import Path
        src = Path.home() / ".claude" / "settings.json"
        plugins = {}
        try:
            if src.exists():
                plugins = {k: False for k in (json.loads(src.read_text()).get("enabledPlugins") or {})}
        except Exception:
            plugins = {}
        out = Path(os.environ.get("JARVIS_HOME", str(Path.home() / ".jarvis"))) / "brain-settings.json"
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps({"enabledPlugins": plugins}, indent=1))
        except Exception:
            return str(src)          # 写不了就退回用户原配置（宁可慢也别起不来）
        return str(out)

    def start(self):
        """起子进程。⚠️ 双向模式下 init 在首条消息后才返回，此处不等；ask() 负责等。"""
        cmd = [
            "claude",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--verbose",
            "--model", self.model,
            "--dangerously-skip-permissions",  # 受 allowed_tools 白名单约束
        ]
        # 启动模式：bare 快但**没有 skills / ToolSearch / WebSearch**。
        # 常驻会话实测（同一进程 6 轮稳态中位）：bare 929ms / 非bare 1483ms /
        # **非bare+插件全关 1085ms** —— 所以非 bare 时把插件关掉，只贵 156ms。
        if self.bare:
            cmd.append("--bare")
        else:
            cmd += ["--settings", self._write_min_settings()]
        # 记忆注入：优先**文件**（persona+memory 合成）。一手验证（2026-09-16，2.1.266）：
        #   ✅ --system-prompt-file 内容真进上下文（替换默认提示，与旧 --system-prompt 同语义）
        #   ✅ --append-system-prompt-file 也能追加
        #   ❌ --add-dir 在 --bare 下**不注入**所加目录的 CLAUDE.md（只注入 ~/.claude/CLAUDE.md，
        #      而那是编码指令，对语音脑是污染）；auto-memory 在 bare 下也无法恢复
        if self.system_prompt_file:
            cmd += ["--system-prompt-file", self.system_prompt_file]
        elif self.system_prompt:
            cmd += ["--system-prompt", self.system_prompt]
        if self.resume:
            sid = self._load_session()
            if sid:
                cmd += ["--resume", sid]
                print(f"[bridge] 恢复上次会话 {sid[:8]}…", flush=True)
        if self.allowed_tools:
            cmd += ["--allowedTools", ",".join(self.allowed_tools)]
        # 安全护栏：明确拒绝破坏性命令。带上 --dangerously-skip-permissions 时**依然生效**
        # （实测 2026-09-16）。每个模式单独一个 argv，避免空格被 shell/解析器拆错。
        for pat in self.disallowed_tools:
            cmd += ["--disallowedTools", pat]
        # 🔧 --bare 跳过 plugin sync 和 CLAUDE.md 自动发现 → 内置联网工具在 bare 下**不存在**。
        # 实测（同一句提问）：带 --bare 时 CC 只有 Bash/Read/Edit；不带时才有 WebSearch/WebFetch。
        # 但 bare 允许显式挂 MCP —— 所以只挂"搜索"这一个，不挂用户配的全部十个
        # （挂几个就要几份启动握手开销）。常驻进程只在 start() 握一次手，后续每轮不重复付。
        _mcp = os.environ.get("JARVIS_MCP_CONFIG") or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "mcp-jarvis.local.json")
        if _mcp and os.path.exists(_mcp):
            cmd += ["--mcp-config", _mcp]
        env = dict(os.environ, MAX_THINKING_TOKENS="0")
        # ⚠️ 必须显式告诉 CC「窗口有多大」，否则**自动压缩根本不武装**（见 config.py
        # `brain_compact_window` 的注释：走代理拿不到服务端窗口表 → 来源落到 auto →
        # 判定函数第一道闸直接 return）。实测证据：项目历史上 250+ 个会话零压缩。
        if self.compact_window:
            env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(self.compact_window)
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
            cwd=self.cwd, env=env)
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()
        return self.proc.pid

    # ---------- 会话持久化 ----------
    def _save_session(self):
        """把 session_id 落盘，供下次启动 --resume。只在 id 变化时写。

        ⚠️ `resume=False`（裸桥 / 测试）时**绝不落盘** —— 否则测试脚本会污染应用共用的
        `~/.jarvis/session.json`，下次应用启动就把**测试对话**当自己的记忆拉回来
        （真机踩过两次：助手复述"数到40/在的"）。原 docstring 就写了这条契约，
        但实现漏了这道闸，等于形同虚设（审计发现）。
        """
        if not self.resume or not self.session_id or self.session_id == self._saved_id:
            return
        try:
            os.makedirs(os.path.dirname(SESSION_PATH), exist_ok=True)
            tmp = SESSION_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"session_id": self.session_id, "model": self.model,
                           "saved_at": time.time()}, f)
            os.replace(tmp, SESSION_PATH)      # 原子落盘
            self._saved_id = self.session_id
        except OSError:
            pass

    def _load_session(self) -> str | None:
        """读回上次的 session_id。**模型不一致就不恢复**（换模型可能不兼容）。"""
        try:
            with open(SESSION_PATH) as f:
                d = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        if d.get("model") != self.model:
            return None
        return d.get("session_id") or None

    def forget_saved_session(self):
        """把落盘的会话一起忘掉。

        ⚠️ 元命令"清理上下文"必须调它：只发 `/clear` 的话，
        下次进程重启会用 `--resume` 把**旧会话**拉回来 —— 等于白清（实测发现的坑）。
        """
        _wipe_session_file()
        self._saved_id = None

    def _pump(self):
        """唯一的 stdout 读者：解析事件→按类型分发（init 唤醒 ready；control 帧就地处理；
        **任务生命周期**进 `_tasks`；轮内帧按「有没有 ask() 在等」分流）。"""
        for line in self.proc.stdout:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = ev.get("type")
            if etype == "system":
                sub = ev.get("subtype")
                if sub == "init":
                    self.session_id = ev.get("session_id")
                    self.capabilities = ev.get("capabilities") or []
                    self._save_session()          # 供下次 --resume
                    self._ready.set()
                    continue
                if sub in ("task_started", "task_updated",
                           "task_notification", "background_tasks_changed",
                           # ⚠️ 这两个**必须透出**，不能跟 status 一起丢掉：
                           #   · `compact_boundary` = 自动压缩**真的发生了** —— 这是
                           #     「窗口配对了没有」唯一的现场证据（见 config.brain_compact_window）
                           #   · `api_error` = 上游报错，属于「让失败可见」那一类
                           "compact_boundary", "api_error"):
                    self._tasks.put(ev)           # ⚠️ **绝不进 _pending**（见 ASYNC-01 §2）
                    continue
                continue                          # status 等：无信息量
            if etype == "control_response":
                resp = ev.get("response", {})
                rid = resp.get("request_id")
                # interrupt 的回执：置位本轮被打断标记
                if rid in self._interrupt_rids:
                    self._interrupted = True
                # 通用控制帧回执（mcp_reconnect 等）：唤醒等待者
                if rid:
                    with self._control_lock:
                        slot = self._control.get(rid)
                    if slot is not None:
                        slot["resp"] = resp
                        slot["ev"].set()
                continue
            # 轮内帧：有 ask() 在等 = 应答轮；否则是 **CC 自主续跑**（后台任务汇报）
            if self._awaiting:
                with self._pending_lock:
                    self._pending.append(ev)
            else:
                self._absorb_async(ev)
        # stdout EOF = 进程退出
        self._ready.set()

    def _absorb_async(self, ev: dict):
        """把**非应答轮**的帧翻成事件推进 `_async_q`。

        ⚠️ 刻意**只做「帧 → 句子」**，不复制 `_read_turn` 的排空/中断/超时逻辑 ——
        那些只对应答轮有意义（应答轮有 ask() 在等、会被 interrupt）。
        续跑的轮是 CC 自主的，没有超时语义，也不该被当成"上一轮残留"丢弃。
        """
        etype = ev.get("type")
        if etype == "stream_event":
            se = ev.get("event", {})
            if se.get("type") == "content_block_delta" and se["delta"].get("type") == "text_delta":
                self._async_buf += se["delta"].get("text", "")
                sents, self._async_buf = self._split_sentences(self._async_buf)
                for s in sents:
                    self._async_q.put({"type": "sentence", "text": s})
        elif etype == "assistant":
            for blk in ev.get("message", {}).get("content", []):
                if blk.get("type") == "tool_use":
                    self._async_q.put({"type": "tool", "name": blk.get("name"),
                                       "input": blk.get("input", {})})
        elif etype == "result":
            if self._async_buf.strip():
                self._async_q.put({"type": "sentence", "text": self._async_buf.strip()})
            self._async_buf = ""
            self.session_id = ev.get("session_id") or self.session_id
            self.total_cost += ev.get("total_cost_usd", 0.0)
            self._async_q.put({"type": "async_done",
                               "terminal_reason": ev.get("terminal_reason"),
                               "is_error": bool(ev.get("is_error"))})
        # `user` 帧（tool_result 回灌）在续跑轮里没有信息量，忽略

    def next_async(self, timeout: float | None = None) -> dict | None:
        """取一条**非应答轮**事件。没有则阻塞至多 `timeout` 秒，超时返回 None。

        两类事件混在一条流里（都能出话，顺序即语义）：
          · `{"type":"sentence"|"tool"|"async_done", ...}` —— CC 自主续跑的内容
          · `system/task_*` 原帧 —— 后台任务生命周期（`subtype` 区分）
        """
        t0 = time.time()
        while True:
            for q in (self._tasks, self._async_q):
                try:
                    return q.get_nowait()
                except queue.Empty:
                    pass
            if timeout is not None and time.time() - t0 >= timeout:
                return None
            time.sleep(0.02)

    def stop(self):
        if self.proc:
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()
                try:
                    self.proc.wait(timeout=3)  # 🔧 防僵尸
                except Exception:
                    pass
            self.proc = None

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    # ---------- 中断 ----------
    def interrupt(self) -> bool:
        """打断在途 turn。可在任意线程调用，不占 _turn_lock。

        实测：约 10ms 生效；会话与进程存活；被中断 turn 以
        result.terminal_reason=="aborted_tools"|"aborted_streaming" 收尾。
        不发 cancel_queued（默认 false）→ 排队中的用户消息得以存活（列在 still_queued）。
        """
        if not self.alive():
            return False
        rid = str(uuid.uuid4())
        frame = {"type": "control_request", "request_id": rid,
                 "request": {"subtype": "interrupt"}}
        # ⚠️ rid 必须在**写帧之前**登记：`_pump` 是并发读该集合的，
        # 若 control_response 在我们登记之前就被解析 → 回执被丢、`_interrupted` 永假。
        # （主判据是 terminal_reason，所以影响有限，但顺序本来就是错的 —— 审计发现。）
        with self._write_lock:
            self._interrupt_rids.add(rid)
            try:
                self.proc.stdin.write(json.dumps(frame) + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self._interrupt_rids.discard(rid)
                return False
        return True

    # ---- 通用控制帧（MCP 热重连等）----
    def _send_control(self, subtype: str, params: dict | None = None,
                      timeout: float = 10.0) -> dict | None:
        """发一个 control_request 并等它的 control_response，返回 response 对象或 None。

        ⚠️ 等待位必须在**写帧之前**登记：`_pump` 是并发读 stdout 的，回执可能在登记
        之前就到达 → 永远等不到（静默失败）。与 `interrupt()` 同一个坑。
        """
        if not self.alive():
            return None
        rid = str(uuid.uuid4())
        req: dict = {"subtype": subtype}
        if params:
            req.update(params)
        frame = {"type": "control_request", "request_id": rid, "request": req}
        slot = {"ev": threading.Event(), "resp": None}
        with self._control_lock:
            self._control[rid] = slot
        with self._write_lock:
            try:
                self.proc.stdin.write(json.dumps(frame) + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                with self._control_lock:
                    self._control.pop(rid, None)
                return None
        slot["ev"].wait(timeout)
        with self._control_lock:
            self._control.pop(rid, None)
        return slot["resp"]

    def mcp_reconnect(self, name: str, timeout: float = 25.0) -> bool:
        """**热重连**一个 MCP server —— 不用重启脑进程。

        用途：obsidian-vault 这类"后端服务按需才在"的 server。启动时 Obsidian 没开 →
        首次连接失败并被标记 failed、整个会话**不再自动重试**；等 Obsidian 起来后发这一帧
        即可接上。帧 schema 一手确认自 CC 2.1.266 二进制：
            {subtype:"mcp_reconnect", serverName:<string>}
        ⚠️ 非官方通道（公开 CLI 文档未列），**升级 CC 后需回归测试**。
        """
        resp = self._send_control("mcp_reconnect", {"serverName": name}, timeout)
        ok = bool(resp and resp.get("subtype") == "success")
        if ok:
            print(f"[bridge] MCP 已重连：{name}", flush=True)
        else:
            err = (resp or {}).get("error") or (resp or {}).get("subtype") or "超时/无响应"
            print(f"[bridge] MCP 重连失败：{name}（{err}）", flush=True)
        return ok

    def mcp_status(self, timeout: float = 8.0) -> list[dict]:
        """取所有 MCP server 的状态：[{"name","status"}]。schema 同上确认。"""
        resp = self._send_control("mcp_status", None, timeout)
        if resp and resp.get("subtype") == "success":
            return (resp.get("response") or {}).get("mcpServers") or []
        return []

    def clear_context(self, timeout: float = 20.0) -> bool:
        """清空会话上下文（**不重启进程**）。

        实测（2026-09-16）：把 `/clear` 当作普通 user 消息写进 stdin 即可，
        CLI 会回一个 `conversation_reset` 事件，会话历史立刻清空。
        实测有效：清空前"暗号是蓝色大象" → 清空后"我不知道。"

        必须先确保没有在途 turn（否则清空会被插在回答中间），所以先 interrupt。
        """
        self.interrupt()
        time.sleep(0.05)
        self._drain_until_result(timeout=2.0)
        try:
            for ev in self.ask("/clear", timeout=timeout):
                if ev.get("type") == "error":
                    return False
                if ev.get("type") in ("done", "interrupted"):
                    # ⚠️ 同时清掉落盘记录：否则下次进程重启会 --resume 把旧会话拉回来，
                    # 等于白清（实测发现的坑）。
                    self.forget_saved_session()
                    return True   # 非 error 即视为成功
        except Exception:
            return False
        return False

    def _drain_until_result(self, timeout: float = 5.0) -> bool:
        """排空"上一轮没收到 result 就被放弃"的残留帧，直到看见 result。

        为什么需要：调用方 `break`/close 放弃生成器后，CLI 仍会把上一轮的
        增量与 result 推进 stdout。若不排空，这些帧会被**下一轮**当成自己的内容
        消费掉——实测症状是上一轮的残句漏进下一轮的回答。
        """
        t0 = time.time()
        while time.time() - t0 < timeout:
            with self._pending_lock:
                while self._pending:
                    ev = self._pending.pop(0)
                    if ev.get("type") == "result":
                        return True
            time.sleep(0.02)
        return False

    # ---------- 请求 ----------
    def ask(self, text: str, timeout: float = 180.0):
        """发一条 user 消息，yield 事件流（生成器，阻塞至本轮结束）。
        进程死亡/写失败自动重启一次并重发。
        ⚠️ 生成器被中途丢弃时 _turn_lock 不会释放——调用方须消费到底。"""
        if not self._turn_lock.acquire(blocking=False):
            yield {"type": "error", "error": "busy: 已有一轮在途"}
            return
        try:
            self._interrupted = False          # 新一轮：清中断标记
            self._interrupt_rids.clear()
            if not self.alive():
                print("[bridge] 进程已死, 重启...", flush=True)
                self.stop()
                self.start()
            # ⚠️⚠️ `_awaiting` 必须在**排空之前**置位，不能等到写 stdin 时才置。
            # 排空要读的正是「上一轮残留的帧」——若此时 `_awaiting` 还是 False，
            # `_pump` 会把它们判成**非应答轮**灌进 `_async_q`，于是：
            #   ① 排空循环永远看不到 result → 白等满 5s 超时
            #   ② 上一轮的残句/result 被异步线程当成「CC 自主续跑」**念出来**
            # （ocr 2026-09-20 报的，是引入非应答轮通道时的回归。）
            self._awaiting = True
            if self._turn_open:
                # 上一轮被放弃（没等到 result）→ 先排空，否则残留帧会污染本轮
                print("[bridge] 上一轮未收尾 → 排空残留帧", flush=True)
                self._drain_until_result()
                self._turn_open = False
            user_ev = {
                "type": "user",
                "message": {"role": "user", "content": [{"type": "text", "text": text}]},
                "parent_tool_use_id": None,
                "session_id": self.session_id,
            }
            with self._write_lock:
                try:
                    self.proc.stdin.write(json.dumps(user_ev) + "\n")
                    self.proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    # 写失败=进程刚死: 重启后重发一次
                    print("[bridge] 写失败, 重启重发...", flush=True)
                    self.stop(); self.start()
                    self.proc.stdin.write(json.dumps(user_ev) + "\n")
                    self.proc.stdin.flush()
            self._turn_open = True             # 已发消息，等 result 收尾
            self._ready.wait(timeout=30)  # init 在首条消息后才返回

            yield from self._read_turn(timeout)
        finally:
            self._awaiting = False       # 兜底：生成器被中途丢弃时也要松手
            self._turn_lock.release()

    @staticmethod
    def _split_sentences(buf: str):
        """从 buf 切出完整句子。返回 (sentences, rest)。
        修三个缺陷：
          ① 取**最靠前**的句末标点，而非 SENT_END 顺序里第一个出现的；
          ② 首句门槛独立（旧版一律 idx<4 导致"好的。"被卡）;
          ③ 🆕 **先剥掉 `buf` 开头的句末标点/空白**（2026-09-20 从真机日志挖出来的）。

        ⚠️⚠️ 缺陷③ 的机制（**这是本文件里最难查的一个**）：
        消费掉一句之后，`buf` 往往以 `\\n` 开头 —— 模型输出段落之间就是换行。
        而 `\\n` **在 `SENT_END` 里**，于是 `buf.find('\\n') == 0` →
        `min(idxs) == 0` → `0 < NEXT_SENT_MIN(4)` → **立刻 break**。
        `buf` 从此不再缩小，后续每个 delta 都撞同一个 0 → **本轮剩下的内容永远切不开**，
        最后在 `result` 处被 `if buf.strip(): yield ...` **整块不切**地吐出去。

        真机证据（`~/.jarvis/events.jsonl`，125 轮）：**41 条 >120 字的 `sentence`
        事件里，41 条全部是每轮的最后一句**，且都含多个「。」与「\n」。
        这条 bug 会吃掉**每一段带换行的回答**的后半截粒度 —— 首句仍能提前出声
        （所以 `first_ms` 看起来正常），但**第二句往后要等整段生成完才开始合成**，
        切句本来就是为了避免这件事。
        """
        sents = []
        # ⚠️ 剥离**必须在循环里**做，不能只在进来时做一次：切掉一句之后 buf 又会以
        # `\n` 开头，第二次迭代照样撞 0。写成循环外的版本时我自己测出来还漏切
        # （`'…。\n所以…。\n但…。'` 只切出 2 句、剩下 26 字不动）。
        while True:
            # lstrip 集合 = SENT_END 全部字符 + 空白。开头的句末标点没有内容，剥掉不丢文本。
            buf = buf.lstrip(ClaudeBridge.SENT_END + " \t")
            idxs = [buf.find(c) for c in ClaudeBridge.SENT_END if c in buf]
            if not idxs:
                break
            idx = min(idxs)
            min_len = ClaudeBridge.FIRST_SENT_MIN if not sents else ClaudeBridge.NEXT_SENT_MIN
            if idx < min_len:
                break
            sent, buf = buf[: idx + 1].strip(), buf[idx + 1:]
            if sent:
                sents.append(sent)
        return sents, buf

    def _read_turn(self, timeout: float):
        """从 _pending 消费本轮事件直到 result；流式 delta→切句。
        被打断时 yield {"type":"interrupted"}；真错误 yield {"type":"error"}。"""
        buf = ""
        t0 = time.time()
        # 🔧 [修复残留] 丢弃上轮超时/错误残留的事件, 对齐到本轮
        # (残留里若有未消费的 result, 说明上轮其实完成了, 丢掉它)
        with self._pending_lock:
            while self._pending:
                if self._pending[0].get("type") == "result":
                    self._pending.pop(0)  # 丢弃旧 result
                else:
                    break  # 队列头是本轮新事件(如 system/status), 保留
        while True:
            with self._pending_lock:
                ev = self._pending.pop(0) if self._pending else None
            if ev is None:
                if not self.alive():
                    yield {"type": "error", "error": "process died"}
                    return
                if timeout and time.time() - t0 > timeout:
                    yield {"type": "error", "error": "timeout"}
                    return
                time.sleep(0.02)
                continue
            etype = ev.get("type")
            if etype == "stream_event":
                se = ev.get("event", {})
                if se.get("type") == "content_block_delta" and se["delta"].get("type") == "text_delta":
                    buf += se["delta"].get("text", "")
                    sents, buf = self._split_sentences(buf)
                    for s in sents:
                        yield {"type": "sentence", "text": s}
            elif etype == "assistant":
                # 完整 assistant 消息（含 tool_use 块）
                for blk in ev.get("message", {}).get("content", []):
                    if blk.get("type") == "tool_use":
                        yield {"type": "tool", "name": blk.get("name"),
                               "input": blk.get("input", {})}
            elif etype == "user":
                # 中断会灌合成 user 事件（tool_result 被拒 / "[Request interrupted...]"）
                # ——不是新输入，忽略
                continue
            elif etype == "result":
                self._turn_open = False        # 本轮正常收尾
                # ⚠️ 立刻松手：CC 可能在 result **之后马上**起一轮续跑（后台任务刚完成），
                # 那些帧必须走 `_absorb_async`，不能留在这里等 ask() 的 finally。
                self._awaiting = False
                if buf.strip():
                    yield {"type": "sentence", "text": buf.strip()}
                self.session_id = ev.get("session_id", self.session_id)
                self.total_cost += ev.get("total_cost_usd", 0.0)
                term = ev.get("terminal_reason")
                aborted = bool(term and term.startswith("aborted"))
                if aborted or self._interrupted:
                    yield {"type": "interrupted", "terminal_reason": term,
                           "cost": ev.get("total_cost_usd")}
                elif ev.get("is_error"):
                    yield {"type": "error", "error": ev.get("subtype"),
                           "detail": ev.get("errors")}
                else:
                    yield {"type": "done",
                           "ttft": ev.get("ttft_ms"),
                           "duration": ev.get("duration_ms"),
                           "cost": ev.get("total_cost_usd"),
                           "num_turns": ev.get("num_turns")}
                return


if __name__ == "__main__":
    # 冒烟测试：启动 → 两连问 → 停
    b = ClaudeBridge(system_prompt="你是语音助手，回复纯口语短句1-2句，禁markdown。")
    t0 = time.time()
    pid = b.start()
    print(f"[start] pid={pid} ({time.time()-t0:.2f}s)")
    for q in ("只回两个字:在的", "再来一个成语接龙,你先出"):
        t0 = time.time()
        for ev in b.ask(q):
            if ev["type"] == "sentence":
                print(f"  [{(time.time()-t0)*1000:5.0f}ms] {ev['text']}")
            elif ev["type"] == "done":
                print(f"  [done] ttft={ev['ttft']}ms cost=${ev['cost']:.4f} session={b.session_id[:8] if b.session_id else '?'}")
    b.stop()
    print("[stop] ok")
