"""主循环编排：把 VAD / ASR / Claude Code / TTS / 播放 串成一条可打断的链路。

并发模型（见 plan §8）：**线程 + 有界队列**。全栈阻塞式，不混 asyncio。

    MicThread(主线程)        BrainThread            TTSThread
    ────────────────         ───────────            ─────────
    mic.read() → VAD         utt_q.get()            sent_q.get()
    · 完整语音段 → utt_q       → ASR                   → sanitize
    · vad.speaking & 非IDLE    → bridge.ask()          → tts.synthesize()
      → 确认窗 → ⚡打断          → sentence → sent_q     → player.write()

打断（耳机方案，无需 AEC）：flush 缓冲 → session.interrupt() → brain.interrupt()
"""
import os
import queue
import socket
import threading
import time
from dataclasses import replace

import numpy as np

from claude_bridge import ClaudeBridge

from .asr import make_asr
from .aec import AecGate
from .audio_io import MicStream, resolve_device
from .bargein_trace import BargeinTrace
from .config import JARVIS_HOME, Config
from .commands import match as match_meta
from .echoguard import EchoGuard, similarity
from .events import BUS
from .filler import is_backchannel, is_false_interruption, is_stop_command
from .fillers import TEXTS as FILLER_TEXTS, TOOL_TEXTS, FillerClips, tool_tag
from .player import Player
from .sanitize import has_speakable, sanitize_for_speech
from .session import Session, State
from .tts import make_tts
from .vad import VadGate

# 口语化强约束 system prompt —— 实测有效（docs/RESEARCH-NATURALNESS-20260916.md §6.1）
SYSTEM_PROMPT = (
    "你是 Aries（欧文的私人语音助手）。你说的每句话都会被**逐字朗读**出来，所以必须按口语写。\n"
    "0. **身份**：你的名字叫 **Aries**。**欧文是主人的名字，不是你的**——绝不要说'我是欧文'、\n"
    "   也不要说自己叫欧文。被问'你是谁'就答 Aries。\n"
    "规则：\n"
    "1. 第一句必须极短（≤12 字），先给结论或应答，再展开。\n"
    "2. 禁止书面语结构：不要'首先/其次/最后/综上所述/值得注意的是/总的来说'。\n"
    "3. 禁止 markdown、列表、编号、括号注释、emoji、代码块。\n"
    "4. 用说话的方式组织：短句、可停顿，必要时用'那个''就是'这类语气词。\n"
    "5. 数字按口语读法写：'三件'而不是'3 件'，'下午五点'而不是'17:00'。\n"
    "6. 需要列举时用'第一…然后…还有个…'，不要用 1. 2. 3.。\n"
    "7. 有不确定就直说，不要编。代码和长内容只说'内容我发到你屏幕上'。\n"
    "8. 主人没提出任务时，不要自己演搜索或汇报过程。\n"
    "9. **能直接答就别调工具**——调用工具会让你晚好几秒才开口（实测：闲聊 1s，调工具 7.7s），\n"
    "   只有真需要外部信息时才用。\n"
    "10. 主人说'停''别说了''安静'时**不用回应**——系统会直接让你闭嘴。\n"
    "11. 你**能操作浏览器**（开新窗口/标签页、导航、点击、填表、读页面）。主人说\n"
    "    '开个新窗口''打开…''看看这个网页'时，用浏览器工具做，做完用一句话回报结果。\n"
    "12. ⛔ **浏览器里撞上登录页就停手。** 看到 SSO / 登录表单 / 'Sign in' 页面时：\n"
    "    **不要**填账号密码、**不要**找表单提交（`form.submit()` / `requestSubmit()`）、\n"
    "    **不要**挨个点按钮试。**直接说一句**「XX 的登录过期了，你去浏览器里登一下」，然后结束。\n"
    "    实测教训（2026-09-19）：让它自己试登录，**一次烧掉 28 步工具调用**、七十多秒，全在摸索\n"
    "    登录表单。主人的浏览器里登一次就永久解决，机器试一次都不该试。\n"
    "13. **Microsoft / Google / 学校 SSO 的二次验证（2FA）机器过不去** —— 那不是你能碰的，\n"
    "    直接请主人来。\n"
    "14. **多步任务：开口先给一句「我去做 X」，再开始调工具。** ≤12 字，说**意图**不说结果。\n"
    "    例：「好，我上去看看。」「我看看有没有开着的页面。」\n"
    "    ⚠️ 这条**覆盖**你默认的「不叙述例行工具调用」——那个默认对**多步任务**不适用。\n"
    "    （实测 2026-09-19：26 个工具回合里你只有 2 次这么做。而工具链中位 3 步 / 最长 28 步、\n"
    "    每步 4.5 秒 —— 不说这一句，主人要盯着十几秒的静音。）\n"
    "15. **但只在开始一个新阶段时说，不要每次调用都说。** 说多了比静音更烦。\n"
    "16. **超过约 5 秒的活，用 Bash 的 `run_in_background` 跑**（多个 curl、大文件处理、\n"
    "    串行好几步的计算）。启动后**立刻**回一句人话，然后你这一轮就结束 —— 跑完系统\n"
    "    会通知你，你那时再汇报结果。（实测：前台等 6 步命令要静默 69 秒。）\n"
    "17. **主人听到的是「你在干什么」和「结果是什么」**，不是你怎么做到的。所以：\n"
    "    启动时说意图（「这个我放后台跑，好了喊你」），跑完说结果（「查到了，明天阴天，不下雨」）。\n"
    "    ⛔ 唯一硬护栏：任务 ID、文件名、shell 命令、退出码、路径都**别念**——那些是给你看的。\n"
    "    不好的例子：「已启动后台任务（ID b3tbmiy67，运行 `sleep 20; echo BACKGROUND_DONE`）」"
)

# 登录页哨兵：navigate 的目标 URL 命中这些标记就判为"撞上登录页"。
# 用途是**观测**（发 login_required 事件），不是硬中断 —— 硬中断要在消费生成器的
# 循环里调 brain.interrupt()，有死锁风险（见 _login_target 的注释）。
#
# ⚠️ host 与 path **分开匹配**：单纯子串匹配的话，"auth" 会误命中 `author`、
# `authentication-guide`；按 host 前缀 / path 段匹配才准。
LOGIN_HOST_MARKERS = (
    "sso.", "login.", "signin.", "sign-in.", "idp.", "adfs.", "auth.",
    "login.microsoftonline", "accounts.google", "shibboleth",
)
LOGIN_PATH_MARKERS = ("/login", "/signin", "/sign-in", "/sso", "/auth/", "/idp", "/adfs")

MEMORY_SERVER = "obsidian-vault"
OBSIDIAN_PORT = 27124


class Orchestrator:
    def __init__(self, cfg: Config, verbose: bool = True):
        self.cfg = cfg
        self.verbose = verbose
        self.session = Session()
        # 回声文本护栏（装 AEC 之前的纯文本兜底，零延迟）。判据见 echoguard.py。
        self.echo_guard = EchoGuard(threshold=cfg.echo_guard_threshold,
                                    min_len=cfg.echo_guard_min_len,
                                    window_s=cfg.echo_guard_window_s)
        self.mic = MicStream(cfg, device=resolve_device(getattr(cfg, "input_device", ""), False))
        self.vad = VadGate(cfg)
        self.asr = make_asr(cfg)
        self.tts = make_tts(cfg)
        self.player = Player(self.tts.audio_format,
                             device=resolve_device(getattr(cfg, "output_device", ""), True))
        # AEC 门：只在 speaker_aec 模式建。必须在 Player 之后（far 参考来自它）。
        self.aec = AecGate(cfg, self.player) if cfg.aec else None
        sp_file = self._compose_system_prompt()   # 口语规则 + persona.md + memory.md → 落盘
        self.brain = ClaudeBridge(system_prompt="" if sp_file else SYSTEM_PROMPT,
                                  system_prompt_file=sp_file,
                                  model=cfg.brain_model,
                                  disallowed_tools=list(cfg.bash_deny),
                                  bare=cfg.brain_bare,
                                  compact_window=cfg.brain_compact_window,
                                  resume=cfg.resume_session)

        self.utt_q: queue.Queue = queue.Queue(maxsize=8)
        self.sent_q: queue.Queue = queue.Queue(maxsize=64)
        self._stop = threading.Event()
        self._paused = threading.Event()   # set = 暂停监听（仪表盘按钮控制）
        # ⚠️ 填充音用**独立的 TTS 实例**，不复用 self.tts：
        # 启动时的预渲染线程与 TTSThread 会**同时**调 synthesize，而 FishTTS 的
        # `_ws_client` 是共享单例 —— 两个线程跑同一 WebSocket 流会交错损坏（审计发现）。
        # 加粗锁会更糟（预渲染会阻塞延迟敏感的 TTS 线程），所以干脆拆开实例。
        self.fillers = (FillerClips(make_tts(cfg),
                                    texts={FillerClips.GENERIC: list(FILLER_TEXTS),
                                           **TOOL_TEXTS})
                        if cfg.filler_enabled else None)
        self._filler_timer: threading.Timer | None = None
        self._filler_played_turn: int | None = None   # 本轮已播过 —— tool 路径与兜底只能二选一
        self._filler_lock = threading.Lock()
        self._hd_gated = False             # 半双工门控：正在因为"自己在播"而不听
        self._last_rejected = 0            # vad.rejected 的上次值（只在变化时上报，见主循环）
        # 待决的自动打断（VAD 起音已暂停、还在等转写判断）。None = 没有待决的。
        self._pending_bargein_at: float | None = None
        # P2 轮次协商：只在本轮记「第几句」，用锁是因为 TTSThread 是唯一写者，
        # 但 `_tts_loop` 每轮都从队列取，跨轮复用同一组字段。
        self._yield_turn: int | None = None
        self._yield_count = 0
        self._yield_lock = threading.Lock()
        # 上一条填充音/承接句的**时刻**（时间门用，见 `filler_min_gap_ms`）。
        # 0.0 = 还没播过（第一次一定放行）。
        self._filler_last_at = 0.0
        # 诊断轨迹（常驻记录电平 + VAD 状态，只在关键时刻落盘）。
        # 纯观测，不影响行为；写盘失败一律吞掉。见 bargein_trace.py。
        self.trace = BargeinTrace(enabled=cfg.bargein_trace)
        self._trace_onset_done = False     # 一次「开口」只落一次盘
        self._trace_utt_path: str | None = None   # 本次打断音频存到哪（存完置 None）
        # 「非应答轮」= CC 自己起的轮次（后台任务跑完后的汇报）。见 docs/WORKORDER-ASYNC-01.md
        self._async_pending: list[str | None] = []   # 攒着的句子；None = 该轮结束哨兵
        self._async_turn: int | None = None          # 我们给自主轮分配的 turn id
        self._async_spoke = False                    # 这一轮自主轮是否已出过声
        self._async_last_ev = 0.0                    # 最后一次收到自主轮事件的时刻（看门狗用）

        self._shutdown_started = False     # 关机按钮防重复触发
        self._threads: list[threading.Thread] = []

    # ---------- 日志 ----------
    def log(self, *a):
        if self.verbose:
            print(*a, flush=True)

    def _set_state(self, s: State):
        """改状态并广播（仪表盘要看到 听/想/说 的切换）。"""
        self.session.set_state(s)
        BUS.emit("state", value=s.value)

    def _compose_system_prompt(self) -> str | None:
        """把 [口语规则] + persona.md + memory.md 合成一份系统提示并**写盘**，返回文件路径。

        为什么要写盘而不是拼进 argv（`--system-prompt`）：值会出现在 `ps` 里（个人数据泄露），
        且 `--bare` 下 `--add-dir` / auto-memory **都不能用**（一手实测 2.1.266），
        `--system-prompt-file` 是唯一可靠通道。文件内容**替换**默认提示（与原语义一致）。

        两个文件任一缺失/为空都不报错——降级为"只有口语规则"，绝不让记忆问题拖垮启动。
        """
        parts = [SYSTEM_PROMPT]
        notes: list[str] = []
        for label, path in (("persona", self.cfg.persona_file),
                            ("memory", self.cfg.memory_file)):
            if not path:
                continue
            try:
                with open(os.path.expanduser(path), encoding="utf-8") as f:
                    txt = f.read().strip()
            except OSError:
                continue
            if txt:
                parts.append(txt)
                notes.append(f"{label} {len(txt)}字")
        if not notes:
            return None
        self.log(f"[记忆] 载入 {' · '.join(notes)}")
        out = JARVIS_HOME / "system_prompt.md"
        try:
            JARVIS_HOME.mkdir(parents=True, exist_ok=True)
            tmp = str(out) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("\n\n".join(parts) + "\n")
            os.replace(tmp, out)          # 原子落盘
        except OSError as e:
            self.log(f"[记忆] 写盘失败，退回纯口语规则：{e}")
            return None
        return str(out)

    # ---------- 第二大脑（Obsidian）热重连 ----------
    @staticmethod
    def _port_open(port: int = OBSIDIAN_PORT) -> bool:
        """本地端口有没有人在听（= Obsidian 是否在跑）。"""
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                return True
        except OSError:
            return False

    def reconnect_memory(self) -> bool:
        """热重连第二大脑，并广播结果。元命令 / 仪表盘 / 看门狗 **共用这一个入口**。"""
        ok = self.brain.mcp_reconnect(MEMORY_SERVER)
        BUS.emit("meta_result", cmd="reconnect", ok=ok)
        return ok

    def _memory_watchdog(self, interval: float = 30.0):
        """Obsidian **上线时自动**热重连第二大脑（边缘触发：只在 down→up 连一次）。

        动机：启动时 Obsidian 常没开 → obsidian-vault 被标记 failed，且**整个会话不再自动重试**
        （一手实测 2.1.266）。用户不该为"把 Obsidian 打开"这件事还得专门说一句命令。
        """
        was_up = self._port_open()
        while not self._stop.wait(interval):
            up = self._port_open()
            if up and not was_up:
                self.log("[记忆] 检测到 Obsidian 上线 → 热重连第二大脑")
                self.reconnect_memory()
            was_up = up

    # ---------- 生命周期 ----------
    def start(self):
        self.log(f"[启动] TTS={self.tts.name} | VAD={self.cfg.vad_backend} | 脑={self.cfg.brain_model}")
        BUS.emit("session_start", tts=self.tts.name, vad=self.cfg.vad_backend,
                 brain=self.cfg.brain_model, mode=self.cfg.audio_mode,
                 barge_in=self.cfg.barge_in)
        # 填充音**预渲染**（后台，别拖慢启动）。没有它，工具调用那 5–12s 就是纯静音。
        if self.fillers:
            threading.Thread(target=self.fillers.ensure,
                             kwargs={"log": self.log}, daemon=True).start()
        self.brain.start()
        self.player.start()
        self.mic.start()
        for t in (threading.Thread(target=self._brain_loop, name="brain", daemon=True),
                  threading.Thread(target=self._tts_loop, name="tts", daemon=True),
                  # 非应答轮：CC 后台任务跑完会自己起一轮汇报，这里把它接成语音
                  threading.Thread(target=self._async_loop, name="async", daemon=True),
                  # 第二大脑看门狗：Obsidian 起来后自动热重连（不必为此说话）
                  threading.Thread(target=self._memory_watchdog, name="vault-watch",
                                   daemon=True)):
            t.start()
            self._threads.append(t)

    def stop(self):
        self._stop.set()
        # 退出时把待决的打断清掉并解除暂停：留着的话缓冲排不空，
        # TTS 那条「等播完再回 IDLE」的收尾循环要一直等到超时才退（虽然有 _stop 兜底）。
        self._pending_bargein_at = None
        try:
            self.player.resume()
        except Exception:
            pass
        # ⚠️ 先把未闭合的语音段交出来再收设备：`VadGate.flush()` 此前**无人调用**
        # （审计发现），退出时最后半句会被丢掉。
        try:
            for _ in self.vad.flush():
                pass
        except Exception:
            pass
        # ⚠️ 必须用 put_nowait：若 TTS 卡在网络合成（Fish 首包实测可达 5s），线程停在
        # player.write，队列会被填满 → 无超时的 put() **永远阻塞** → Ctrl+C 关不掉，
        # 只能 kill 进程（审计发现）。
        for q in (self.utt_q, self.sent_q):
            try:
                q.put_nowait(None)
            except queue.Full:
                pass
        self.mic.stop()
        self.player.stop()
        try:
            self.brain.stop()
        except Exception:
            pass
        try:
            self.tts.close()
        except Exception:
            pass
        try:
            self.asr.close()          # 此前漏了：FishASR 的 httpx 连接不关会泄漏
        except Exception:
            pass

    def run(self):
        """主线程 = 麦克风循环 + VAD + 打断检测。"""
        self.start()
        self.log("=" * 64)
        # ⚠️ 按 `audio_mode` **三路**分，不能按 `barge_in` 两路 ——
        # `barge_in` 在 headphones 与 speaker_aec 下都是 True，两路分会把
        # `--speaker-aec` 显示成「耳机模式」（真机踩到：2026-09-20 起机时横幅写错）。
        # 这行是**用户唯一会看到的模式说明**，写错等于骗人。
        if self.cfg.audio_mode == "headphones":
            self.log("JARVIS · 耳机模式（说完自动接话；播报中直接插话即可打断）Ctrl+C 退出")
        elif self.cfg.audio_mode == "speaker_aec":
            self.log("JARVIS · 免提 + AEC（说完自动接话；**播报中直接插话即可打断**）Ctrl+C 退出")
        else:
            self.log("JARVIS · 免提模式（无 AEC → 半双工：播出时不听麦克风，**不能插话打断**）")
            self.log("        仍可用仪表盘的「打断」按钮手动打断。Ctrl+C 退出")
        self.log("=" * 64)
        # ---- 观测：把 AEC 对齐的未知量 B 报出来 ----
        # `player.py` 在 2026-09-20 之前**从不读 `stream.latency`**，所以
        # `aec_stream_delay_ms` 取多少一直靠推断（子代理审查 + ocr 都点出这点）。
        # 启动时报出设备真实延迟，B 就从「未知」变「已知」。
        _olat = self.player.output_latency_ms
        self.log(f"[音频] 输出延迟 B = {_olat:.1f}ms" if _olat is not None
                 else "[音频] 输出延迟 = 读不到（设备没报）")
        self.log(f"       AEC = {'开' if self.aec is not None else '关'}"
                 f" | far 读偏移 = {self.cfg.aec_stream_delay_ms}ms"
                 f" | VAD 预滚 = {self.cfg.vad_pre_roll_ms}ms")
        # 诊断轨迹落在哪 —— 用户报「打断不灵」时要看这个文件，不打出来就找不到
        if self.trace.enabled:
            self.log(f"       打断诊断轨迹 → {self.trace.path}"
                     f"（平时零盘 IO，只在播放中检测到语音/武装/待定/撤回/提交时落盘）")
        burst_started_at: float | None = None   # 本次"语音爆发"的起点
        fired_for_this_speech = False
        silence_since: float | None = None      # 当前静音段从何时开始（None = 正在说话）
        prev_silence: float | None = None       # 上一段静音的起点（用来算静了多久）
        last_level = 0.0
        try:
            while not self._stop.is_set():
                chunk = self.mic.read(timeout=0.2)
                if chunk is None:
                    continue
                # ---- AEC：先消掉我们自己播的回声，再进电平表与 VAD ----
                # ⚠️ 必须在电平表**之前** —— 放在之后的话，仪表盘显示的是回声，会误导。
                #
                # ⚠️⚠️ **先把引用快照到局部变量**（ocr 2026-09-20 报的 TOCTOU）：
                # `set_mode()` 由**仪表盘线程**调用，会在运行期把 `self.aec` 置成 `None`
                # （见本文件 `set_mode`）。原来写的是 `if self.aec is not None: self.aec.accept()`
                # —— 两次读取之间被置空就抛 `AttributeError`，而 `run()` 只捕
                # `KeyboardInterrupt`，异常会冲出 while 循环 → **麦克风主循环静默终止**
                # （其余线程还活着，表现为"应用看着正常但不再响应语音"，最难查的一类故障）。
                aec = self.aec
                if aec is not None:
                    chunk = aec.accept(chunk)
                # ⚠️ RMS **每块都算一次**：下面电平表是节流的（~12 次/秒），
                # 但诊断轨迹要每块都记（10 次/秒，与块长对齐），所以提到节流之外。
                # 1600 个 float 的 RMS ≈ 微秒级，主循环块预算是 100ms，可以忽略。
                chunk_rms = float(np.sqrt(np.mean(np.square(chunk))))
                # 麦克风电平（仪表盘电平表），节流 ~12 次/秒
                now = time.time()
                if now - last_level > 0.08:
                    rms = chunk_rms
                    if aec is not None:
                        # 播放期间多报一个 WebRTC 自己的语音概率 ——
                        # 留着事后定 aec_min_speech_prob 的阈值（先测量，再设门限）。
                        # `dropped`/`xrun` 也带上：mic 队列溢出与采集侧溢出是两回事，
                        # 不区分就查不出音频卡顿的根因（ocr 点出的观测盲区）。
                        BUS.emit("level", rms=rms,
                                 speech_prob=round(aec.speech_probability, 3),
                                 dropped=self.mic.dropped, xrun=self.mic.status_flags)
                    else:
                        BUS.emit("level", rms=rms,
                                 dropped=self.mic.dropped, xrun=self.mic.status_flags)
                    last_level = now

                # ---- 半双工（免提模式）----
                # 没有 AEC，播出时麦克风听到的就是**我们自己**。只关打断不够：
                # 自己的声音还会被转写、送去 CC，变成助手自问自答。
                # 所以播出期间**完全不喂 VAD**，出来时 reset 一次丢掉残留状态。
                if self.cfg.half_duplex and self.player.is_playing():
                    if not self._hd_gated:
                        self._hd_gated = True
                        self.vad.reset()
                    continue
                if self._hd_gated:
                    self._hd_gated = False
                    self.vad.reset()          # 重新干净地开始听
                utts = self.vad.accept(chunk)
                # ---- 观测：SNR 门限丢了多少段（**这是「要很大声才能打断」的盲区**）----
                # 段被 `vad._passes_gate` 丢掉时是**完全静默**的，不分播放中还是空闲。
                # 播放期间丢 = 可能正是主人在插话却被门限吃掉 → 打断失灵。
                # 只在计数变化时报（低频），并标明当时是否在播放。
                if self.vad.rejected != self._last_rejected:
                    d = self.vad.rejected - self._last_rejected
                    self._last_rejected = self.vad.rejected
                    self.log(f"[drop] SNR 门限丢了 {d} 段"
                             f"（累计 {self.vad.rejected}）"
                             f"{'  ⚠️ 当时正在播放' if self.player.is_playing() else ''}"
                             f" | 噪声底={self.vad._floor:.5f}")
                if self._paused.is_set():
                    continue      # 暂停：丢弃语音，也不触发打断（麦克风仍读，防设备缓冲溢出）
                for utt in utts:
                    try:
                        self.utt_q.put_nowait(utt)
                    except queue.Full:
                        self.log("[warn] 语音队列满，丢弃一段")

                # ---- 打断检测 ----
                # 三次迭代才定下来的判据（每次都是真机日志逼出来的）：
                #   1) 按"此刻是否忙"武装 → 说长句时把自己的后半句当插话打断
                #   2) 改按"爆发起点晚于本轮起点" → 仍会误判，因为 **VAD 切分长句时
                #      `speaking` 会 True→False→True 抖动**，那一抖被当成"新爆发"
                #   3) 最终：新爆发必须**前面静够久**（>= interrupt_min_gap_ms）才算新开口。
                #      静得太短 = 同一次说话的延续 → 不武装。
                st = self.session.state
                speaking = self.vad.speaking
                now = time.time()
                # 诊断轨迹：**每块都记**（10 次/秒，只进内存环形缓冲，不碰盘）。
                # 为什么值得常驻：用户报「要喊几遍才打断」时，最该看的那个数
                # （那一刻麦克风多响、VAD 有没有翻）在 events.jsonl 里**没有留痕**
                # —— `level` 被刻意标成瞬时事件不落盘。见 bargein_trace.py。
                #
                # `far_rms` = 扬声器侧电平 → 有了它才能算**真实房间的 ERLE**：
                # `20log10(far/mic)`（只在"没人在说话"的时段取）。离线探针报的
                # 34–36 dB 是探针环境的数，真机可能完全不同 —— 打断识别差时
                # 第一件要确认的就是这个。
                _far = 0.0
                if aec is not None:
                    try:
                        _far = self.player.far_rms(chunk.shape[0])
                    except Exception:
                        _far = 0.0          # 诊断量，取不到就算了
                self.trace.note(chunk_rms, speaking, self.player.is_playing(), _far)
                if not speaking:
                    if silence_since is None:
                        silence_since = now
                    burst_started_at = None              # 爆发结束
                    fired_for_this_speech = False
                    self._trace_onset_done = False       # 下一次开口可以再落一次盘
                else:
                    silence_since = None
                    if burst_started_at is None:
                        # 只有"静够久之后"的新爆发才武装；否则视为同一次说话的延续
                        # 🆕 观测：**武装**也记一条。用户报「要喊几遍才打断」时，
                        # 这条能立刻区分两种原因：
                        #   有 `[打断-武装]` 但没有 `[打断-触发]` → 卡在确认窗
                        #   连 `[武装]` 都没有 → 卡在**静音门槛**（说得太密没静够）
                        _gap = -1.0 if prev_silence is None else (now - prev_silence) * 1000
                        _armed = _gap < 0 or _gap >= self.cfg.interrupt_min_gap_ms
                        if _armed:
                            burst_started_at = now
                            self.log(f"🎤 [打断-武装] state={st.value} 前静 {_gap:.0f}ms")
                        # 🆕 **这就是"用户在试图打断"的定义** —— 播放中 VAD 说有人说话。
                        # 无论武装没武装、成没成，都落一次盘（每次开口只落一次）。
                        if self.player.is_playing() and not self._trace_onset_done:
                            self._trace_onset_done = True
                            self.trace.dump("speech_during_playback",
                                            state=st.value, armed=_armed,
                                            gap_ms=round(_gap))
                    elif (self.cfg.barge_in          # 免提模式：不做自动打断
                          and st is not State.IDLE
                          and not fired_for_this_speech
                          and burst_started_at >= self.session.turn_started_at
                          and (now - burst_started_at) * 1000 >= self.cfg.interrupt_confirm_ms):
                        self.log(f"⚡ [打断-触发] state={st.value} 爆发已持续 "
                                 f"{(now-burst_started_at)*1000:.0f}ms")
                        # ⚠️ 这里是**唯一**走宽限窗口的路径（明说的路径直接提交）：
                        # 只暂停、不销毁，等转写判断是真打断还是 backchannel。
                        self._begin_bargein()
                        fired_for_this_speech = True
                # ---- 待决打断超时 → 提交 ----
                # 窗口内没等到任何转写（用户一直不出声，或 ASR 没吐出段）→ 当成真打断。
                # 少了这一步，助手会**永远停在暂停状态**（缓冲还在，但再也不出声）。
                if (self._pending_bargein_at is not None
                        and (now - self._pending_bargein_at) * 1000 >= self.cfg.bargein_grace_ms):
                    if st is State.IDLE:
                        # 本轮已经自然结束（理论上不该发生：暂停期间缓冲排不空、
                        # TTS 收尾会一直等 → 状态不会回 IDLE。留作护栏）。
                        # 这时再提交只会白加一次打断计数 + 给空闲的脑发一帧 interrupt。
                        self.log("↩️ [打断-撤回] 本轮已自然结束，不需要提交")
                        self._pending_bargein_at = None
                        self.player.resume()
                    else:
                        self.log(f"⚡ [打断-超时] {self.cfg.bargein_grace_ms}ms 内没有转写 → 提交")
                        self.trace.dump("timeout", state=st.value)
                        self._commit_interrupt()
                prev_silence = silence_since if silence_since is not None else prev_silence
        except KeyboardInterrupt:
            self.log("\n[退出]")
        finally:
            self.stop()

    # ---------- 打断 ----------
    # ⚠️ 分两步：**暂停**（可撤销）与**提交**（不可撤销）。
    # 只有「VAD 起音触发的自动打断」走两步 —— 因为那一刻还不知道用户是真打断
    # 还是只应了一声「嗯」。明说的路径（「停一下」/ 仪表盘按钮 / 清空上下文）
    # **直接提交**，它们没有歧义。见 docs/PLAN-HUMANNESS-20260920.md P1。
    def _begin_bargein(self):
        """VAD 起音 → 先暂停（**保留缓冲**），开一个宽限窗口等转写来判断。"""
        if self._pending_bargein_at is not None:
            return                          # 窗口已经开着，别重复开
        self._pending_bargein_at = time.time()
        self.player.pause()
        self.log(f"⚡ [打断-待定] 已暂停，等 {self.cfg.bargein_grace_ms}ms 内的转写判断")
        BUS.emit("bargein_pending", grace_ms=self.cfg.bargein_grace_ms)
        self.trace.dump("bargein_pending", grace_ms=self.cfg.bargein_grace_ms)

    def _resolve_bargein(self, text: str, is_echo: bool = False):
        """转写到了（或为空）→ 判定刚才是误判还是真打断。**没有待决窗口时是 no-op。**

        `is_echo` 由调用方传进来（回声护栏的结论）—— 是回声同样意味着**误判**，
        而且那是最常见的一类：`'那个。'` / `'让我想想。'` 是我们自己填充音的回声。
        """
        if self._pending_bargein_at is None:
            return
        waited = (time.time() - self._pending_bargein_at) * 1000
        self._pending_bargein_at = None
        if is_echo or is_false_interruption(text):
            self.player.resume()
            why = "回声" if is_echo else "应答词/非语音"
            self.log(f"↩️ [打断-撤回] {waited:.0f}ms 后判为误判（{why}：{text!r}）→ 接着播")
            BUS.emit("bargein_reverted", text=text, waited_ms=round(waited),
                     reason=why)
            # ⚠️ 这里的 kwarg 不能叫 `reason` —— `dump(reason, **extra)` 的**第一个形参**
            # 就叫这个名，重名会 `TypeError: got multiple values for argument 'reason'`。
            # （测试抓到的；生产里同样会炸。）
            self.trace.dump("revert", text=text[:40], waited_ms=round(waited), why=why)
            return
        self.log(f"⚡ [打断-确认] {waited:.0f}ms 后判为真打断（{text[:24]!r}）")
        self.trace.dump("commit", text=text[:40], waited_ms=round(waited))
        self._commit_interrupt()

    def _do_interrupt(self):
        """**立即**打断（无宽限）。明说的路径与仪表盘按钮用这个。"""
        self._pending_bargein_at = None
        self._commit_interrupt()

    def _commit_interrupt(self):
        self._cancel_filler()
        # ⚠️ 顺序要紧：**先作废轮次，再清缓冲**。
        # 反过来的话，落在"flush 之后、作废之前"的 TTS 写入不会被再清掉 →
        # 打断后仍会漏播一小段旧句（审计发现）。
        self.session.interrupt()
        played = self.player.flush()        # flush 会一并清掉 paused 标志
        self.brain.interrupt()
        self._drain(self.sent_q)
        self.log(f"⚡ [打断] 已播 {played:.2f}s | 累计打断 {self.session.interrupts} 次")
        BUS.emit("interrupt", played_s=round(played, 2), count=self.session.interrupts)

    @staticmethod
    def _drain(q: queue.Queue):
        while True:
            try:
                q.get_nowait()
            except queue.Empty:
                return

    @staticmethod
    def _login_target(ev: dict) -> str | None:
        """这次工具调用是不是在往登录页走？是就返回目标 host（用于回报给主人）。

        只认 `mcp__browser__navigate` —— 那是最清晰的信号。`find`/`computer` 也可能
        落在登录页上，但从入参看不出 URL（只有关键词/坐标），**不猜**。

        ⚠️ **只观测，不中断**。要硬停就得在这个位置调 `_do_interrupt()`，而它会调
        `self.brain.interrupt()` —— 我们此刻正踩在消费 `gen` 的循环里，有死锁风险。
        先用事件确认「提示词规则 12 有没有起作用」，有证据再谈硬停。
        """
        if ev.get("name") != "mcp__browser__navigate":
            return None
        url = str((ev.get("input") or {}).get("url", "") or "")
        if not url:
            return None
        try:
            from urllib.parse import urlparse
            host = (urlparse(url).hostname or "").lower()
            path = (urlparse(url).path or "").lower()
        except Exception:
            return None
        if any(host.startswith(m) or m in host for m in LOGIN_HOST_MARKERS) \
                or any(p in path for p in LOGIN_PATH_MARKERS):
            return host or url[:60]
        return None

    def _note_spoken(self, text: str) -> None:
        """记一句「实际要念出去」的文本，供回声护栏比对。

        ⚠️ 传进来的必须是**实际送 TTS 的字符串**（即已经过 sanitize 的那份）。
        若拿未清洗的原文，比对的基准就不是真正会从扬声器出来的内容。
        """
        if self.cfg.echo_guard_enabled:
            self.echo_guard.note_spoken(text)

    # ---------- 手动控制（仪表盘按钮） ----------
    def pause(self):
        """暂停监听：闭嘴 + 不再处理语音。麦克风继续读（否则设备缓冲会溢出）。"""
        self._paused.set()
        if self.session.state is not State.IDLE:
            self._do_interrupt()
        self._set_state(State.IDLE)
        self._drain(self.utt_q)
        BUS.emit("paused", value=True)
        self.log("[控制] ⏸ 已暂停监听")

    def resume(self):
        self._paused.clear()
        self._drain(self.utt_q)        # 丢掉暂停期间积压的语音
        self.vad.reset()
        BUS.emit("paused", value=False)
        self.log("[控制] ▶ 已恢复监听")

    def interrupt_now(self):
        """立刻打断（不依赖 VAD），供仪表盘按钮用。"""
        if self.session.state is not State.IDLE:
            self._do_interrupt()

    def shutdown(self, delay: float = 0.4) -> None:
        """⏻ **完全关闭**：杀脑进程 + **直接结束本进程**。

        ⚠️ 刻意**不**依赖主循环的优雅退出（原先只置 `_stop` 等 `run()` 收尾）：一旦主循环
        卡在任意一步（设备读、网络合成、锁），就永远关不掉。这里在**独立线程**里：
          ① `mic/player.stop()` 释放设备 ② `brain.stop()` 关 stdin → claude 退出（**避免留孤儿脑**）
          ③ `os._exit(0)` 直接结束进程。
        tts/asr 的 socket 交给 OS 回收（进程没了，麦克风指示灯自然灭）。
        延迟（默认 0.4s）只为让仪表盘的 HTTP 响应先发回去。
        """
        if self._shutdown_started:
            return
        self._shutdown_started = True
        if self.session.state is not State.IDLE:
            try:
                self._do_interrupt()
            except Exception:
                pass
        BUS.emit("shutdown")
        self.log("[控制] ⏻ 正在关闭：杀脑进程 + 结束本进程…")

        def _go():
            time.sleep(delay)
            for fn in (self.mic.stop, self.player.stop, self.brain.stop):
                try:
                    fn()
                except Exception:
                    pass
            self.log("[控制] ⏻ 退出进程")
            os._exit(0)
        threading.Thread(target=_go, name="shutdown", daemon=True).start()

    def set_mode(self, mode: str) -> bool:
        """**运行时**切耳机/免提 —— 同时**联动输出设备**。

        为什么必须联动（真机踩到）：用户点「免提」后，声音**仍从原设备出**，
        而免提的语义就是"用内置扬声器"。更坑的是用户听到内置扬声器那种又薄又尖
        的音色，以为是"换了个模型在说话"——其实是设备没切。
        `barge_in`/`half_duplex` 是 cfg 的派生属性，replacing cfg 后自动生效。
        """
        if mode not in ("headphones", "speaker", "speaker_aec"):
            return False
        self.cfg = replace(self.cfg, audio_mode=mode)
        if mode in ("speaker", "speaker_aec"):
            self._drain(self.utt_q)
            self.vad.reset()
            builtin = self._find_device(("macbook", "built-in", "内置"))
            if builtin is not None:
                self.player.reopen(builtin)
        else:
            self.player.reopen(None)
        # AEC 门跟着模式建/拆/重置。
        # ⚠️ 必须在 player.reopen() **之后** —— reopen 会把 far 绝对帧号归零，
        # 而 `AecGate._far_read` 是 44.1k 绝对帧指针，不重置就会指向不存在的过去。
        if self.cfg.aec:
            if self.aec is None:
                self.aec = AecGate(self.cfg, self.player)
            else:
                self.aec.reset()
        else:
            self.aec = None
        self.log(f"[控制] 模式 → {mode}（打断={'开' if self.cfg.barge_in else '关'}, "
                 f"半双工={'开' if self.cfg.half_duplex else '关'}, "
                 f"AEC={'开' if self.cfg.aec else '关'}）")
        BUS.emit("mode", value=mode, barge_in=self.cfg.barge_in,
                 half_duplex=self.cfg.half_duplex, aec=self.cfg.aec)
        return True

    @staticmethod
    def _find_device(keys: tuple[str, ...]) -> int | None:
        """按名字子串找输出设备（内置扬声器的名字各机型不同）。"""
        try:
            import sounddevice as sd
            for i, d in enumerate(sd.query_devices()):
                if d["max_output_channels"] > 0 and \
                        any(k in d["name"].lower() for k in keys):
                    return i
        except Exception:
            pass
        return None

    def set_output_device(self, idx: int | None) -> bool:
        """**运行时**换输出设备。

        为什么需要（真机踩到）：sounddevice 的流在**创建时**绑定设备，之后系统
        默认输出改成耳机它**不会跟** —— 用户切了耳机，声音仍从扬声器出。
        """
        try:
            self.player.reopen(idx)
        except Exception as e:
            self.log(f"[控制] 换输出设备失败: {type(e).__name__}: {e}")
            return False
        name = "系统默认"
        try:
            import sounddevice as sd
            name = sd.query_devices(idx)["name"] if idx is not None else \
                sd.query_devices(sd.default.device[1])["name"]
        except Exception:
            pass
        self.log(f"[控制] 输出设备 → {name}")
        BUS.emit("device", index=idx, name=name)
        return True

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def _speak_local(self, text: str):
        """不经 CC，直接念一句（元命令的确认语）。用当前轮次，TTSThread 照常处理。"""
        turn = self.session.begin_turn()
        self.player.reset_position()
        self._set_state(State.SPEAKING)
        self._note_spoken(text)            # 回声护栏的比对基准（元命令确认语也会念出去）
        self.sent_q.put((turn, text))
        self.sent_q.put((turn, None))

    # ---------- 记忆写入 ----------
    # `memory.md` 是**唯一**能跨「清空上下文 / 重启」留下来的地方（`--resume` 默认开，
    # 但 `/clear` 会把会话历史整个丢掉）。此前全库**没有写入路径** —— 它是个只读的死文件。
    #
    # ⚠️ 社区明确警告过的一点（🟡 LangChain，2026-06-24）：
    #    「若运行时缓存了 prompt，memory 的写入必须有**刷新路径**，否则系统存对了
    #      却一直拿旧上下文跑。」
    #    我们正是这样 —— `_compose_system_prompt()` 只在启动跑一次，所以这里写进去的
    #    内容**下次启动才进系统提示**。会话内不等它：CC 本来就看得见这轮对话。
    _MEMORY_MAX_LINES = 120       # 整份会进系统提示，不能无限长

    def _remember(self, text: str) -> tuple[bool, str]:
        """把一句话追加进 `memory.md`。返回 `(是否成功, 失败原因)`。

        去重：与已有条目做字符级相似度，直接复用 `echoguard.similarity`
        （同一个项目里已有实测过阈值的实现，不为这件事再写一个）。
        重复时**算成功**（用户的目标「让它记住」已经达成，只是不用再写一遍）。
        """
        path = os.path.expanduser(self.cfg.memory_file or "")
        if not path:
            return False, "没配 memory_file"
        try:
            with open(path, encoding="utf-8") as f:
                old = f.read()
        except OSError:
            old = ""
        lines = [ln[2:] for ln in old.splitlines() if ln.startswith("- ")]
        if any(similarity(text, ln) >= 0.75 for ln in lines):
            self.log(f"[记忆] 已存在，跳过: {text!r}")
            BUS.emit("memory_write", text=text, total=len(lines), duplicate=True)
            return True, ""
        if len(lines) >= self._MEMORY_MAX_LINES:
            # ⚠️ **绝不自动删**（硬规矩：不删用户数据）。只提醒，压缩交给人。
            self.log(f"[记忆] ⚠️ memory.md 已 {len(lines)} 条，超过 "
                     f"{self._MEMORY_MAX_LINES} —— 每次启动整份注入，该人工压缩了")
        entry = f"- {time.strftime('%Y-%m-%d')} {text}"
        try:
            with open(path, "a", encoding="utf-8") as f:
                if old and not old.endswith("\n"):
                    f.write("\n")
                f.write(entry + "\n")
        except OSError as e:
            self.log(f"[记忆] 写失败: {e}")
            return False, "文件写不进去"
        self.log(f"[记忆] + {entry}")
        BUS.emit("memory_write", text=text, total=len(lines) + 1)
        return True, ""

    def _run_meta(self, meta, heard: str) -> bool:
        """执行控制助手自身的元命令（见 commands.py 的说明：这些**不能**送进 CC）。

        返回 **True = 已处理完，别再送 CC**；**False = 这一句还要继续送 CC**。
        ⚠️ 只有「记住 X」成功时返回 False —— 见 `_brain_once` 里的说明。
        """
        self.log(f"[meta] {meta.name} ← {heard!r}")
        BUS.emit("meta", cmd=meta.name, text=heard)
        if meta.name == "remember":
            ok, why = self._remember(meta.payload)
            if not ok:
                # 写失败时**就地**告知并吃掉这一轮。
                # ⚠️ 不能既 `_speak_local` 又 fall through：`_speak_local` 会
                # `begin_turn()`，紧接着 CC 那轮再 `begin_turn()` 会把这条提示作废掉
                # （同一类坑见 `session.py` 里那段"状态被踩"的注释）。
                self._speak_local(f"没记上——{why}。")
                return True
            return False          # 写成功：仍送 CC，让它在**本会话内**也听见这句
        if meta.name == "clear":
            self._do_interrupt()
            ok = self.brain.clear_context()
            self.log(f"[meta] 清空上下文: {'成功' if ok else '失败'}")
            BUS.emit("meta_result", cmd="clear", ok=ok)
            self._speak_local(meta.reply if ok else "抱歉，刚才没清干净。")
        elif meta.name == "pause":
            self.pause()
            self._speak_local(meta.reply)
        elif meta.name == "resume":
            self.resume()
            self._speak_local(meta.reply)
        elif meta.name == "reconnect":
            # 热重连第二大脑（reconnect_memory 内含 BUS.emit 结果事件）
            ok = self.reconnect_memory()
            self._speak_local(meta.reply if ok else "没连上——Obsidian 可能没开着。")
        return True

    # ---------- 填充音 / 承接句 ----------
    def _play_filler(self, turn: int, tag: str = FillerClips.GENERIC,
                     trigger: str = "latency") -> bool:
        """真正播一条。返回是否播了。

        三重校验（原来写在 `_arm_filler.fire()` 里，tool 路径也要用，所以抽出来）：
        **还是这一轮 + 还在思考 + 距上一条够久**。
        第三条原本是「本轮一条都没播过」，2026-09-20 真机改成**时间门** ——
        因为兜底（2.0s）总抢在第一个工具事件（中位 3.4s）之前，
        「一轮一条」会让按工具类别选的承接句**永远没机会**。见 `filler_min_gap_ms`。
        """
        if not (self.fillers and self.fillers.ready()):
            return False
        with self._filler_lock:
            now = time.time()
            # ⚠️ 门**只在同一轮内**生效（`_filler_played_turn == turn`）——
            # 换了一轮就是新语境（用户又说了一句），不该被上一轮的播放时刻卡住。
            if (self._filler_played_turn == turn
                    and now - self._filler_last_at < self.cfg.filler_min_gap_ms / 1000.0):
                return False
            self._filler_last_at = now
            self._filler_played_turn = turn
        if not self.session.is_current(turn) or self.session.state is not State.THINKING:
            with self._filler_lock:
                # ⚠️ **只在标记仍属于自己这一轮时才回滚**（ocr 2026-09-20 报的）：
                # 无条件 `= None` 会抹掉**更新的那一轮**刚设下的标记 —— 于是新一轮
                # 还能再播一次填充音，"同一轮只播一次"的保证就破了。
                if self._filler_played_turn == turn:
                    self._filler_played_turn = None
                    self._filler_last_at = 0.0     # 没真播，时间门也不该推进
            return False
        clip, text = self.fillers.pick(tag)
        if not clip:
            return False
        self._cancel_filler()
        self.player.write_filler(clip)
        # ⚠️ 必须把**填充音的文本**也登记进回声护栏。填充音只 1–2 个字
        # （"那个……"/"嗯……"），被麦克风收回去后转写出来也正是 1–2 个字 ——
        # 正好落进 `echo_guard_min_len=6` 的保护里，不登记就永远拦不住。
        # 真机证据：≤2 字的插话碎片里 47% 紧跟在填充音之后（正常转写只有 22%）。
        self.echo_guard.note_spoken(text, filler=True)
        BUS.emit("filler", delay_ms=self.cfg.filler_delay_ms, text=text,
                 tag=tag, trigger=trigger, turn=turn)
        state = "承接句" if tag else "填充音"
        self.log(f"[{state}] {text!r}（trigger={trigger} tag={tag or 'generic'}）")
        return True

    def _arm_filler(self, turn: int):
        """**兜底路径**：本轮开始后 `filler_delay_ms` 内还没出首句 → 播通用池。

        实测动机：闲聊首句 1–2.5s，**工具调用 5.3–12s**。后者是纯静音灾难。
        2000ms 之后才播 ⇒ 大部分闲聊（p50 2.1s）根本不会插进来。
        """
        self._cancel_filler()
        if not (self.fillers and self.fillers.ready()):
            return
        self._filler_timer = threading.Timer(
            self.cfg.filler_delay_ms / 1000.0,
            lambda: self._play_filler(turn, FillerClips.GENERIC, "latency"))
        self._filler_timer.daemon = True
        self._filler_timer.start()

    def _arm_lead_in(self, turn: int, tag: str):
        """**工具路径**：首个 `tool` 事件且本轮还没出声 → 播按工具类别选的承接句。

        ⚠️ **不是 delay 0**（偏离 `PLAN-LATENCY-20260919.md` §P1 表的「delay 0」）：
        Roark 验收清单那条「工具 200ms 就返回 —— 填充音还该响吗？」
        （*a filler on a fast tool is a self-inflicted second of latency*）。
        delay 0 时快工具会让承接句播到一半就被 `_cancel_filler()` 切掉 → 用户听到
        **截断的半句话**，比静音更糟。300ms 宽限让真快工具完全不播；而本机工具步
        墙钟 p50 **4.50s**，300ms 在正常路径上可忽略。
        """
        self._cancel_filler()
        if not (self.fillers and self.fillers.ready()):
            return
        self._filler_timer = threading.Timer(
            self.cfg.filler_tool_delay_ms / 1000.0,
            lambda: self._play_filler(turn, tag, "tool"))
        self._filler_timer.daemon = True
        self._filler_timer.start()

    def _cancel_filler(self):
        t = self._filler_timer
        self._filler_timer = None
        if t:
            t.cancel()

    # ---------- BrainThread ----------
    # ⚠️ 线程必须"打不死"：任何异常只记录、继续跑。
    # 真机教训——一个写错的 `return` 让 BrainThread 静默死亡，
    # 应用看起来还在运行，却对任何语音再无响应（最难查的一类故障）。
    def _brain_loop(self):
        while not self._stop.is_set():
            try:
                self._brain_once()
            except Exception as e:
                self.log(f"[brain] 异常（已捕获，线程存活）: {type(e).__name__}: {e}")
                self._set_state(State.IDLE)
                time.sleep(0.1)

    def _brain_once(self):
        utt = self.utt_q.get()
        if utt is None:
            return
        if self._paused.is_set():
            return          # 暂停期间不处理（麦克风侧也在丢弃）
        if len(utt) / self.cfg.sample_rate < self.cfg.asr_min_utt_sec:
            # ⚠️ 这里是**静默丢弃** —— 以前丢了什么都不说，所以「说了助手没反应」
            # 这类现象无法从日志定位。报出来（带时长），让失败可见。
            self.log(f"[drop] 段太短 {len(utt)/self.cfg.sample_rate:.2f}s "
                     f"< asr_min_utt_sec={self.cfg.asr_min_utt_sec}s → 丢弃")
            return
        # 诊断：这一句是**打断**来的吗？是就把音频存下来（事后能复听/重转写）。
        # ⚠️ 判据要在 `_resolve_bargein` **之前**取 —— 它会把 `_pending_bargein_at` 清掉。
        # `state != IDLE` 或"有待定窗口"都算打断：前者是它正在播/想，后者是刚起音。
        if self.session.state is not State.IDLE or self._pending_bargein_at is not None:
            try:
                self._trace_utt_path = self.trace.save_pcm(utt, "bargein")
            except Exception:
                self._trace_utt_path = None
        r = self.asr.transcribe(utt)
        # 诊断：这一句如果是**打断**来的（或刚在待定窗口），把音频存下来 ——
        # 用户报「它一说话我打断识别就差」，光看电平只能猜到"有干扰"，
        # **听到音频**才能分清是"用户声音被压/变形"还是"混进了残余回声"还是
        # "段被截头去尾"。三种修法完全不同。
        if self._trace_utt_path is not None:
            try:
                with open(self._trace_utt_path + ".txt", "w", encoding="utf-8") as f:
                    f.write((r.text or "") + f"\n(ASR {r.latency_ms:.0f}ms)\n")
            except OSError:
                pass
            self.log(f"[打断] 音频已存 {self._trace_utt_path}  转写={r.text!r}")
            self._trace_utt_path = None
        # ---- 回声文本护栏 ----
        # 内置扬声器场景：AEC 残余越过 VAD 门限被转写。若放它进脑，助手就**回应自己**
        # （HANDOVER §1 的验收口径"不能凭空自言自语"）。放在**最前** ——
        # 是回声的话，不解元命令、不打断、不送 CC。
        #
        # ⚠️ 这里**只判一次**，结果同时给下面 `_resolve_bargein` 用：是回声同样意味着
        # 「这次打断是误判」——`'那个。'` / `'让我想想。'` 就是**我们自己填充音的回声**，
        # 真机 41 次早打断里出现过。判两次不但多算，还会让 `suppressed` 计数翻倍。
        hit, score, match = False, 0.0, ""
        if self.cfg.echo_guard_enabled:
            hit, score, match = self.echo_guard.check(r.text or "")
        # ⚠️⚠️ **待决的自动打断必须在这条链的最前面解决** —— 后面每个分支都会 `return`，
        # 放在它们之后的话，被回声护栏拦下的那次转写就**永远不解决**待决窗口 →
        # 主循环超时提交 → 助手还是停了。
        self._resolve_bargein(r.text or "", is_echo=hit)
        if hit:
            self.log(f"[echo] 判为回声（相似度 {score:.2f}）→ 丢弃: {r.text!r}")
            BUS.emit("echo_suppressed", text=r.text, score=round(score, 3),
                     match=match[:60], total=self.echo_guard.suppressed)
            return
        if not r.text:
            return
        busy = self.session.state is not State.IDLE
        if is_stop_command(r.text):
            # ⚠️ 必须**显式打断**，不能只靠主循环那条 300ms 的 VAD 中断计时器——
            # "停"往往短于 300ms，计时器可能压根没武装过 → 用户"说停停不下来"（审计发现）。
            self.log(f"[stop] {r.text!r} → 就地闭嘴")
            BUS.emit("stop", text=r.text)
            self._do_interrupt()
            return
        if is_backchannel(r.text) and busy:
            # 只在**忙**时用 backchannel 做门控（这才是它本来的用途）。
            # 空闲时用户答"对"是正经回答，**绝不能吞**——filler.py 的契约写明了
            # "只用于打断门控，绝不用于丢弃用户输入"（审计发现旧代码违背了它）。
            self.log(f"[ignore] backchannel（忙时）: {r.text!r}")
            BUS.emit("backchannel", text=r.text)
            return
        meta = match_meta(r.text)
        if meta:
            # 「记住 X」写成功时 `_run_meta` 返回 False → **继续往下送 CC**。
            # 否则 CC 在**本会话内**根本不知道这件事（我们把话截在了它前面），
            # 五分钟后再问「我下周三要干嘛」它会答不上来。
            # 写文件由我们保证，不给 CC 这个任务，所以它只会口头应一声。
            if self._run_meta(meta, r.text):
                return
        if busy:
            # 用户在我们说话时插了句**真话**。旧版直接 return = 静默丢弃用户输入。
            # 正确行为：人在这种时候会停下来听。所以要打断，然后处理这句新输入。
            self.log(f"[插话] 忙时收到真话 {r.text!r} → 打断并处理")
            self._do_interrupt()
        turn = self.session.begin_turn()
        self._set_state(State.THINKING)
        self._arm_filler(turn)          # 900ms 后还没出声就播填充音
        t0 = time.perf_counter()
        first = True
        self.log(f"你: {r.text}   (ASR {r.latency_ms:.0f}ms, 情感={r.emotion})")
        BUS.emit("user", text=r.text, asr_ms=round(r.latency_ms), emotion=r.emotion)
        abandoned = False
        gen = self.brain.ask(r.text)
        try:
            for ev in gen:
                if not self.session.is_current(turn):
                    # ⚠️ 用 continue **排空**，不是 break！
                    # break 会留下未消费的帧在 bridge 的 _pending 里 → 污染下一轮输出
                    # （真机实测：上一轮"数到40"的残句漏进了下一轮的回答）。
                    # bridge 内部规定"中断后必须 drain 完才能发下一条"。
                    if not abandoned:
                        self.log("[brain] 本轮已作废 → 排空旧帧")
                        abandoned = True
                    continue
                et = ev["type"]
                if et == "sentence":
                    # ⚠️ 在这里清洗，不是在 TTSThread —— 保证**屏幕上显示的字
                    # 和实际念出来的字是同一个字符串**（真机 bug：URL 被念成"链接"，
                    # 屏幕却显示完整网址，用户以为"对不上"）
                    clean = sanitize_for_speech(ev["text"])
                    if not has_speakable(clean):
                        continue
                    self._set_state(State.SPEAKING)
                    if first:
                        self._cancel_filler()
                        # ⚡ 真句子到了：把还在排队的填充音切掉，否则它会往后推
                        cut = self.player.cut_tag("filler")
                        if cut > 0.01:
                            self.log(f"[filler] 切掉剩余填充音 {cut:.2f}s")
                        self.player.reset_position()   # 本轮"播到第几秒"归零
                    first_ms = round((time.perf_counter() - t0) * 1000) if first else None
                    self._note_spoken(clean)           # 回声护栏的比对基准
                    self.sent_q.put((turn, clean))     # 队列里放的就是"要念的"
                    BUS.emit("sentence", text=clean, first_ms=first_ms,
                             raw=(ev["text"] if clean != ev["text"] else None))
                    if first:
                        self.log(f"[首句 {first_ms}ms] {clean}")
                        first = False
                    else:
                        self.log(f"        +{clean}")
                elif et == "tool":
                    self.log(f"  [工具] {ev.get('name')}")
                    BUS.emit("tool", name=ev.get("name"), input=ev.get("input"))
                    # 本轮一次都还没出声 = CC **没说** preamble → 我们补一句承接句。
                    # ⚠️ `first` 为假说明 CC 自己已经说过了 → **绝不补**（否则一句变两句）。
                    if first:
                        self._arm_lead_in(turn, tool_tag(ev.get("name") or ""))
                    # 登录页哨兵（**观测**）：规则 12 要求模型撞上登录页就停手，
                    # 这个事件用来验证它到底有没有照做。没有这个数就不知道规则有没有用。
                    _host = self._login_target(ev)
                    if _host:
                        self.log(f"[login] ⚠️ 奔向登录页 {_host} —— 规则 12 本应让它停手")
                        BUS.emit("login_required", host=_host, tool=ev.get("name"))
                elif et in ("done", "interrupted", "error"):
                    if et == "error":
                        self.log(f"  [错误] {ev.get('error')}")
                        BUS.emit("error", text=str(ev.get("error")))
                    self.sent_q.put((turn, None))   # 结束哨兵
                    BUS.emit("done", cost=ev.get("cost"), turns=ev.get("num_turns"))
                    break
        except Exception as e:
            self.log(f"[brain] 本轮异常 {type(e).__name__}: {e}")
            self._set_state(State.IDLE)
        finally:
            # ⚠️ 必须显式 close：`break` 只放弃迭代，**生成器里的 finally 不会立刻执行**，
            # bridge 的 _turn_lock 会一直被持有 → 下一次 ask() 直接返回 "busy"，
            # 表现是"助手突然不回应了"。此前只是碰巧被 GC 救了，时机不确定。
            gen.close()

    # ---------- AsyncThread（非应答轮）----------
    # 为什么需要单独一条线程：CC 在**后台任务跑完**后会自己起一轮汇报结果
    # （实测见 docs/WORKORDER-ASYNC-01.md §1：t=23.09 通知 → t=25.23 说结果）。
    # 那轮的帧由桥接分流到 `next_async()`，不走 `ask()` —— 所以编排器要有人接。
    #
    # ⚠️ **不使用 `_do_interrupt`**：自主轮不是用户发起的，没有"被打断"的对象；
    #    用户在说话时我们只是**先不播**（攒着），不去抢话。
    def _async_loop(self):
        while not self._stop.is_set():
            try:
                ev = self.brain.next_async(timeout=0.3)
                if ev is None:
                    self._async_watchdog()
                    self._flush_async()
                    continue
                self._async_last_ev = time.time()
                self._handle_async(ev)
            except Exception as e:
                # 与其它线程同规矩：异常只记录、继续跑（静默死线程是最难查的一类故障）
                self.log(f"[async] 异常（已捕获，线程存活）: {type(e).__name__}: {e}")
                time.sleep(0.1)

    def _handle_async(self, ev: dict):
        et = ev.get("type")
        if et == "system":
            sub = ev.get("subtype")
            # 两类都走这条：后台任务生命周期、以及 `compact_boundary`/`api_error`
            # 这类「系统事件」。用中性措辞，别一律叫「后台任务」。
            self.log(f"[async] 系统事件 {sub} {ev.get('task_id') or ''} "
                     f"{ev.get('status') or ''}".rstrip())
            BUS.emit("background_task", subtype=sub, task_id=ev.get("task_id"),
                     status=ev.get("status"), output_file=ev.get("output_file"),
                     summary=ev.get("summary"))
            return
        if et == "tool":
            self.log(f"  [async 工具] {ev.get('name')}")
            return
        if et == "sentence":
            clean = sanitize_for_speech(ev.get("text") or "")
            if has_speakable(clean):
                self._async_pending.append(clean)
            self._flush_async()
            return
        if et == "async_done":
            self._async_pending.append(None)      # 哨兵：自主轮到这儿结束
            self._flush_async()
            return

    def _flush_async(self):
        """把攒下的自主轮句子播出去。**忙就等**（用户在说话/在听时不去抢话）。

        允许续播的两种情形：① 会话空闲；② 非空闲但这轮**本来就是我们自己的**
        （自主轮可能分几批吐句子，中间不能因为"state 不是 IDLE"就卡死）。
        """
        if not self._async_pending:
            return
        if (self.session.state is not State.IDLE
                and not self.session.is_current(self._async_turn if self._async_turn is not None else -1)):
            return
        # ⚠️ **自愈**：自主轮可能已经被用户插话作废（`session.interrupt()` 换了 turn id）。
        # 不复位的话，下面会用那个**过期 id** 往 sent_q 里塞，TTS 侧 `is_current` 判假
        # → 句子被静默丢掉，而且 `_async_turn` 会永远卡着（既不播也不清）。
        # 复位成 None → 下面重新 `begin_turn()`，把攒下的句子在**新的一轮**里播出来
        # （后台结果即使被打断也仍然有用，不该丢）。
        if self._async_turn is not None and not self.session.is_current(self._async_turn):
            self._async_turn = None
            self._async_spoke = False
        if self._async_turn is None:
            self._async_turn = self.session.begin_turn()
            self._async_spoke = False
        turn = self._async_turn
        while self._async_pending:
            text = self._async_pending.pop(0)
            if text is None:                      # 该轮结束
                # ⚠️ `_async_spoke` 必须是**实例状态**，不能用本次调用的局部变量：
                # 句子和 async_done 常分两次调用到达（第一批句子排空后队列为空，
                # 下一次 flush 才拿到哨兵）—— 局部变量那时已经重置成 False，
                # 哨兵就永远发不出去 → 状态卡在 SPEAKING。
                if self._async_spoke:
                    self.sent_q.put((turn, None))  # TTS 播完会自己 finish_turn → IDLE
                self._async_turn = None
                return
            self._note_spoken(text)               # 回声护栏比对基准
            if not self._async_spoke:
                self._set_state(State.SPEAKING)
                self._async_spoke = True
            self.sent_q.put((turn, text))
            self.log(f"[async 说] {text}")

    def _async_watchdog(self):
        """收尾保护。三种情况：
        ① 轮次已被作废（用户插话打断）→ 这个自主轮过时了，**丢弃**攒着的句子
           （⚠️ 不丢的话它们会永远卡在 `_async_pending` 里：`_flush_async` 的头一道闸
              `is_current` 永远为假 → 既不播也不清，是个静默泄漏）
        ② 队列里还有句子 → 还没轮到收尾
        ③ 开了口却再没收到任何事件 → 兜底补哨兵，别把状态卡在 SPEAKING
        """
        if self._async_turn is None:
            return
        if not self.session.is_current(self._async_turn):
            self.log(f"[async] 自主轮已被作废 → 丢弃剩余 {len(self._async_pending)} 句")
            self._async_pending.clear()
            self._async_turn = None
            return
        if self._async_pending:
            return
        if time.time() - self._async_last_ev < self._ASYNC_IDLE_CLOSE_S:
            return
        self.log("[async] 自主轮超时未收尾 → 强制结束")
        turn, self._async_turn = self._async_turn, None
        self.sent_q.put((turn, None))

    _ASYNC_IDLE_CLOSE_S = 8.0

    # ---------- TTSThread ----------
    def _tts_loop(self):
        while not self._stop.is_set():
            try:
                self._tts_once()
            except Exception as e:
                self.log(f"[tts] 异常（已捕获，线程存活）: {type(e).__name__}: {e}")
                time.sleep(0.1)

    def _tts_once(self):
        item = self.sent_q.get()
        if item is None:
            return
        turn, text = item
        if text is None:                       # 本轮句子发完 → 等播完再回 IDLE
            while not self._stop.is_set() and self.player.is_playing():
                time.sleep(0.02)
            # ⚠️ 用原子的 finish_turn，不用 is_current + set_state 两段式：
            # 那样会在"判定通过"与"置 IDLE"之间被新轮插入，把正在播的新轮踩成 IDLE，
            # 导致该轮 barge-in 全程失灵（审计发现）。
            if self.session.finish_turn(turn):
                BUS.emit("state", value=State.IDLE.value)
            return
        if not self.session.is_current(turn):
            return
        # 清洗已在 BrainThread 完成（保证"屏幕显示的字 == 实际念出的字"），这里直接用
        gen = self.tts.synthesize(text)
        try:
            for ch in gen:
                if not self.session.is_current(turn):   # ⚡ 被打断 → 立刻停
                    break
                self.player.write(ch)
        finally:
            gen.close()
        self._maybe_yield_turn(turn)

    # ---------- P2：轮次协商（默认关）----------
    def _maybe_yield_turn(self, turn: int):
        """说满 N 句之后**主动停一下**，把插话的槽位让出来。

        ⚠️ 默认关闭（`turn_yield_ms=0`）。停顿是**净增加**的时间，而 CUI'25 实测
        「延迟 >4s 是头号体验杀手」——**用户不接话就是纯亏**。见 config 的注释与
        docs/PLAN-HUMANNESS-20260920.md P2：**先 A/B 量过再定值**。

        为什么不需要任何新机制：打断由主循环独立处理，用户在停顿里开口 →
        `_begin_bargein` → 提交 → `session.interrupt()` → 下一句的 `is_current`
        判假 → TTS 自然停。这里只负责"停一下"本身。
        """
        if self.cfg.turn_yield_ms <= 0:
            return
        with self._yield_lock:
            if self._yield_turn != turn:
                self._yield_turn, self._yield_count = turn, 0
            self._yield_count += 1
            n = self._yield_count
        if n != self.cfg.turn_yield_after_sentences:
            return
        if self.sent_q.empty():
            return                     # 后面没别的句子了，让给谁？
        # 等这一句真播完再停 —— 否则会把它从中间切断
        while (not self._stop.is_set() and self.player.is_playing()
               and self.session.is_current(turn)):
            time.sleep(0.02)
        if not self.session.is_current(turn):
            return                     # 等待期间被打断了，不用再让
        self.log(f"[P2] 第 {n} 句后让出 {self.cfg.turn_yield_ms}ms 等接话")
        BUS.emit("turn_yield", ms=self.cfg.turn_yield_ms, after_sentence=n)
        time.sleep(self.cfg.turn_yield_ms / 1000.0)
