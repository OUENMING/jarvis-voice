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
        # ⚠️ 必须加锁：填充音的**预渲染线程**与 TTSThread 会同时用同一个实例，
        # 两者都写 self._proc → 可能杀错/漏杀子进程（审计发现）。
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
