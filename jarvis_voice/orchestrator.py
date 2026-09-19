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
from .config import JARVIS_HOME, Config
from .commands import match as match_meta
from .echoguard import EchoGuard
from .events import BUS
from .filler import is_backchannel, is_stop_command
from .fillers import FillerClips
from .player import Player
from .sanitize import has_speakable, sanitize_for_speech
from .session import Session, State
from .tts import make_tts
from .vad import VadGate

# 口语化强约束 system prompt —— 实测有效（docs/RESEARCH-NATURALNESS-20260916.md §6.1）
SYSTEM_PROMPT = (
    "你是 Omen（欧文的私人语音助手）。你说的每句话都会被**逐字朗读**出来，所以必须按口语写。\n"
    "0. **身份**：你的名字叫 **Omen**。**欧文是主人的名字，不是你的**——绝不要说'我是欧文'、\n"
    "   也不要说自己叫欧文。被问'你是谁'就答 Omen。\n"
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
    "    '开个新窗口''打开…''看看这个网页'时，用浏览器工具做，做完用一句话回报结果。"
)

# 第二大脑（Obsidian）热重连。server 名须与 mcp-jarvis.local.json 里的 **完全一致**。
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
                                  resume=cfg.resume_session)

        self.utt_q: queue.Queue = queue.Queue(maxsize=8)
        self.sent_q: queue.Queue = queue.Queue(maxsize=64)
        self._stop = threading.Event()
        self._paused = threading.Event()   # set = 暂停监听（仪表盘按钮控制）
        # ⚠️ 填充音用**独立的 TTS 实例**，不复用 self.tts：
        # 启动时的预渲染线程与 TTSThread 会**同时**调 synthesize，而 FishTTS 的
        # `_ws_client` 是共享单例 —— 两个线程跑同一 WebSocket 流会交错损坏（审计发现）。
        # 加粗锁会更糟（预渲染会阻塞延迟敏感的 TTS 线程），所以干脆拆开实例。
        self.fillers = FillerClips(make_tts(cfg)) if cfg.filler_enabled else None
        self._filler_timer: threading.Timer | None = None
        self._hd_gated = False             # 半双工门控：正在因为"自己在播"而不听
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
                  # 第二大脑看门狗：Obsidian 起来后自动热重连（不必为此说话）
                  threading.Thread(target=self._memory_watchdog, name="vault-watch",
                                   daemon=True)):
            t.start()
            self._threads.append(t)

    def stop(self):
        self._stop.set()
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
        if self.cfg.barge_in:
            self.log("JARVIS · 耳机模式（说完自动接话；播报中直接插话即可打断）Ctrl+C 退出")
        else:
            self.log("JARVIS · 免提模式（无 AEC → 半双工：播出时不听麦克风，**不能插话打断**）")
            self.log("        仍可用仪表盘的「打断」按钮手动打断。Ctrl+C 退出")
        self.log("=" * 64)
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
                if self.aec is not None:
                    chunk = self.aec.accept(chunk)
                # 麦克风电平（仪表盘电平表），节流 ~12 次/秒
                now = time.time()
                if now - last_level > 0.08:
                    rms = float(np.sqrt(np.mean(np.square(chunk))))
                    if self.aec is not None:
                        # 播放期间多报一个 WebRTC 自己的语音概率 ——
                        # 留着事后定 aec_min_speech_prob 的阈值（先测量，再设门限）。
                        BUS.emit("level", rms=rms,
                                 speech_prob=round(self.aec.speech_probability, 3))
                    else:
                        BUS.emit("level", rms=rms)
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
                if not speaking:
                    if silence_since is None:
                        silence_since = now
                    burst_started_at = None              # 爆发结束
                    fired_for_this_speech = False
                else:
                    silence_since = None
                    if burst_started_at is None:
                        # 只有"静够久之后"的新爆发才武装；否则视为同一次说话的延续
                        if (prev_silence is None
                                or (now - prev_silence) * 1000 >= self.cfg.interrupt_min_gap_ms):
                            burst_started_at = now
                    elif (self.cfg.barge_in          # 免提模式：不做自动打断
                          and st is not State.IDLE
                          and not fired_for_this_speech
                          and burst_started_at >= self.session.turn_started_at
                          and (now - burst_started_at) * 1000 >= self.cfg.interrupt_confirm_ms):
                        self.log(f"⚡ [打断-触发] state={st.value} 爆发已持续 "
                                 f"{(now-burst_started_at)*1000:.0f}ms")
                        self._do_interrupt()
                        fired_for_this_speech = True
                prev_silence = silence_since if silence_since is not None else prev_silence
        except KeyboardInterrupt:
            self.log("\n[退出]")
        finally:
            self.stop()

    # ---------- 打断 ----------
    def _do_interrupt(self):
        self._cancel_filler()
        # ⚠️ 顺序要紧：**先作废轮次，再清缓冲**。
        # 反过来的话，落在"flush 之后、作废之前"的 TTS 写入不会被再清掉 →
        # 打断后仍会漏播一小段旧句（审计发现）。
        self.session.interrupt()
        played = self.player.flush()
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

    def _run_meta(self, meta, heard: str) -> None:
        """执行控制助手自身的元命令（见 commands.py 的说明：这些**不能**送进 CC）。"""
        self.log(f"[meta] {meta.name} ← {heard!r}")
        BUS.emit("meta", cmd=meta.name, text=heard)
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

    # ---------- 填充音 ----------
    def _arm_filler(self, turn: int):
        """本轮开始后 delay_ms 内还没出首句 → 播一段填充音盖住空白。

        实测动机：闲聊首句 1–2.5s，**工具调用 5.3–12s**。后者是纯静音灾难。
        """
        self._cancel_filler()
        if not (self.fillers and self.fillers.ready()):
            return

        def fire():
            # 三重校验：还是这一轮、还在思考、且一次都还没出声
            if not self.session.is_current(turn) or self.session.state is not State.THINKING:
                return
            clip = self.fillers.pick()
            if not clip:
                return
            self.player.write_filler(clip)
            BUS.emit("filler", delay_ms=self.cfg.filler_delay_ms)

        self._filler_timer = threading.Timer(self.cfg.filler_delay_ms / 1000.0, fire)
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
            return
        r = self.asr.transcribe(utt)
        if not r.text:
            return
        # ---- 回声文本护栏 ----
        # 内置扬声器场景：AEC 残余越过 VAD 门限被转写。若放它进脑，助手就**回应自己**
        # （HANDOVER §1 的验收口径"不能凭空自言自语"）。放在**最前** ——
        # 是回声的话，不解元命令、不打断、不送 CC。
        if self.cfg.echo_guard_enabled:
            hit, score, match = self.echo_guard.check(r.text)
            if hit:
                self.log(f"[echo] 判为回声（相似度 {score:.2f}）→ 丢弃: {r.text!r}")
                BUS.emit("echo_suppressed", text=r.text, score=round(score, 3),
                         match=match[:60], total=self.echo_guard.suppressed)
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
            self._run_meta(meta, r.text)
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
