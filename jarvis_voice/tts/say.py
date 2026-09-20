"""macOS `say` 兜底后端（零依赖、断网可用）。

修掉 jarvis_demo.py 的一个真 bug：原 `Speaker.flush()` 只清队列，
**正在跑的 `say` 子进程会继续播完**。这里**持有子进程句柄**，`stop()` 真能终止。

实现方式：让 `say` 合成到临时 WAV，再按 PCM 块读出 —— 这样它和云后端
走同一条播放路径（统一为裸 PCM），不用为兜底单独写一套播放逻辑。
"""
import os
import subprocess
import tempfile
import threading
import wave
from typing import Iterator

from .base import AudioFormat, TTSError

SR = 22050  # say 的 LEI16 输出用 22050 足够，且省内存


class SayTTS:
    def __init__(self, voice: str = "Tingting", rate: int = 190,
                 sample_rate: int = SR, chunk_bytes: int = 4096):
        self.name = f"say:{voice}"
        self.voice = voice
        self.rate = rate
        self.sample_rate = sample_rate
        self.audio_format = AudioFormat(sample_rate=sample_rate)
        self.chunk_bytes = chunk_bytes
        self._proc: subprocess.Popen | None = None
        self._interrupted = False       # stop() 置位，让 yield 循环提前结束
        # ⚠️ 锁保护的是一组**实例级**的可变状态（`_proc` / `_interrupted`）。
        #
        # 🔴 **本类当前不支持「多线程共用同一个实例」**（ocr 2026-09-20 报的，
        #    已回原码核实结论为**潜伏、当前不可达**）：
        #    · `orchestrator` 给填充音预渲染用的是 `make_tts(cfg)` 的**另一个实例**
        #      （`orchestrator.py:131`，注释里写明了这是刻意的）
        #    · `self.tts.stop()` **从没被调用过** —— 唯一的调用点是 `close()`
        #    所以现在不会有两个线程同时压同一个实例。
        #
        # ⚠️ 但**一旦有人改接线**（比如把 `self.tts` 也交给预渲染线程，或让打断去调
        #    `self.tts.stop()`），这里就会坏，而且是静默地坏：
        #      ① `synthesize()` 开头无条件 `_interrupted = False` → 并发 `stop()` 的
        #         打断标志被吞掉，前一次的 `_proc` 句柄丢失、再也杀不掉
        #      ② `_proc` 只记「最近一个」→ 重叠渲染时 `stop()` 只能终止后写入的那个
        #      ③ 实例级 `_interrupted` 分不清「本次调用」还是「上一次/并发」→
        #         会把真实的非零退出掩盖成「已打断」
        #    **要支持并发就得把打断做成调用级状态**（每次 `synthesize` 生成自己的
        #    Event/token 传进 `_render`），光加锁不够。在那之前，请保持"一个实例一个
        #    使用者"。
        self._lock = threading.Lock()

    def _render(self, text: str) -> bytes:
        """把整句合成到内存。

        ⚠️ `proc.wait()` 必须在**锁外**等待：锁内等待的话 `stop()` 会阻塞在
        取锁上，反而打不断正在渲染的 say（这会导致死锁式的"停不下来"）。
        """
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            path = tf.name
        try:
            proc = subprocess.Popen(
                ["say", "-v", self.voice, "-r", str(self.rate),
                 "--data-format", f"LEI16@{self.sample_rate}", "-o", path, text],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with self._lock:
                self._proc = proc              # 只锁这一下，让 stop() 拿得到句柄
            rc = proc.wait()                   # 锁外等，stop() 才能插进来
            if self._interrupted:
                raise TTSError("已打断")
            if rc != 0:
                raise TTSError(f"say 退出码 {rc}")
            with wave.open(path, "rb") as w:
                return w.readframes(w.getnframes())
        finally:
            with self._lock:
                if self._proc is proc:
                    self._proc = None
            try:
                os.unlink(path)
            except OSError:
                pass

    def synthesize(self, text: str) -> Iterator[bytes]:
        """整句在锁内渲染完，再把 PCM 分块吐出去。

        锁**不跨越 yield** —— 否则消费方卡住会连带阻塞别的调用方。
        ⚠️ 但 yield 循环里**必须检查 `_interrupted`**：改成"先渲染后吐块"之后，
        `stop()` 没法再通过 `_proc = None` 打断，得靠这个标志
        （回归测试抓到：旧实现下 stop() 后仍会吐完 141 个块）。
        """
        with self._lock:
            self._interrupted = False
            self._proc = None
        pcm = self._render(text)               # 内部自己管锁（锁外等待子进程）
        n = self.chunk_bytes
        for i in range(0, len(pcm), n):
            if self._interrupted:
                return
            yield pcm[i:i + n]

    def stop(self):
        """打断：终止在跑的 say 子进程，并让正在吐块的生成器提前结束。"""
        with self._lock:
            self._interrupted = True
            p = self._proc
            self._proc = None
        # 等待放在锁外：render 线程的 finally 也要拿这把锁，锁内等会互相卡住
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=1)
            except subprocess.TimeoutExpired:
                p.kill()

    def close(self):
        self.stop()
