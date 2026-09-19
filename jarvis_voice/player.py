"""PCM 播放器：可即时打断（barge-in 的关键件）。

设计要点：
  - 用 sounddevice 的**回调式** OutputStream，回调里从内部缓冲取数据填空。
  - 缓冲空了就输出**静音**——所以 **`flush()` 只需清空缓冲**，一个 block 内
    立即静音（44.1kHz / 1024 帧 ≈ 23ms）。
  - **不用 `abort()`**：它要重建 stream，且不能从回调里调用。清缓冲更快更安全。
  - 回调内**只做拷贝，绝不阻塞、绝不分配大对象**（阻塞回调 = 爆音/丢帧）。
  - `played_frames` 记录真实播放位置 → OpenAI `audio_end_ms` 的对应物（截断/日志用）。
"""
import threading
from collections import deque
from typing import Iterator

import numpy as np
import sounddevice as sd

from .tts.base import AudioFormat

BLOCK_FRAMES = 1024

# AEC 的 far 参考镜像保留多久。2s 够用（AEC3 自估的延迟在 200ms 量级），3s 留余量。
AEC_FAR_SEC = 3.0


class Player:
    def __init__(self, fmt: AudioFormat, device: int | None = None,
                 block_frames: int = BLOCK_FRAMES):
        self.fmt = fmt
        self.block_frames = block_frames
        # 缓冲里存 (音频, 标签)。标签用于**按来源剪切**——
        # 典型场景：填充音还在播时真句子到了，要把填充音从队列里切掉，
        # 否则它会把真正的内容往后推（1.5s 的回答 + 0.5s 填充 = 更差）。
        self._buf: deque[tuple[np.ndarray, str]] = deque()
        self._lock = threading.Lock()
        self._uptime = 0          # 音频流总运行帧数（含静音）
        self._played_audio = 0    # **实际输出过音频**的帧数 ← 本轮播到第几秒
        self._underruns = 0       # 只在"播过音频之后又断供"时计数（真卡顿）
        self._had_audio = False
        # 缓冲里现有帧数。维护成计数器而不是每次遍历 deque：
        # `buffered_seconds()` 每 20ms 被调一次，全量遍历会与**实时音频回调**
        # 抢同一把锁，长回答时可能 xrun/爆音（审计发现）。
        self._buffered = 0
        self._device = device
        # ---- far 参考镜像（AEC 用：我们**确切知道**在播什么）----
        # ⚠️⚠️ **必须在音频回调里写，用 `outdata` 本身** —— 不能用 `write()` 里塞进来的
        # 音频。原因：`write()` 记的是"TTS 往队列里放了多少帧"，而回调才是"扬声器真的
        # 出了多少帧"。空闲时回调照样按实时输出**静音**并推进，两者是不同的时钟：
        #   空闲 15s → 回调已出 661500 帧，而镜像里只有 0 帧
        #   → 读指针被设到 654885（远超镜像数据量）→ `far_slice` 永远返回空
        #   → **AEC 拿到的 far 参考是全零 → 什么都没消 → 扬声器的声音原样进麦克风**。
        # 真机踩到过（2026-09-19，日志 session #7：切到 speaker_aec 时本 session 还
        # 没说过话，`_far_total=0` 而 `_emitted` 已 66 万）。
        # 用 `outdata` 之后：镜像 ≡ 实际播放信号（含静音），**只有一个时钟**。
        self._far_cap = int(self.fmt.sample_rate * AEC_FAR_SEC)
        self._far = np.zeros(self._far_cap, dtype=np.int16)   # 环形数组，O(1) 追加
        self._far_w = 0           # 回调累计输出过的帧数（绝对帧号 = 播放位置）
        self._far_lock = threading.Lock()
        self._stream: sd.OutputStream | None = None
        self._open_stream()

    def _open_stream(self):
        """建立 OutputStream。

        ⚠️ sounddevice 的流在**创建时**绑定设备，之后系统默认输出变了它**不会跟**
        —— 真机表现：用户把默认输出切到耳机，声音仍从扬声器出。
        所以必须能 `reopen()`（仪表盘的设备下拉就是走这条路）。
        """
        self._stream = sd.OutputStream(
            samplerate=self.fmt.sample_rate, channels=self.fmt.channels, dtype="int16",
            blocksize=self.block_frames, device=self._device, callback=self._cb)

    def reopen(self, device: int | None) -> None:
        """换输出设备：停旧流、清缓冲、按新设备重建（**保持原运行状态**）。"""
        was_running = self._stream is not None and self._stream.active
        self._device = device
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
        except Exception:
            pass
        with self._lock:
            self._buf.clear()
            self._buffered = 0
        self._had_audio = False
        # 换设备 = 换了一条声学路径，旧 far 参考全部作废（帧号也从 0 重来）。
        with self._far_lock:
            self._far[:] = 0
            self._far_w = 0
        self._open_stream()
        if was_running:
            self._stream.start()

    @property
    def device(self):
        return self._device

    # ---- 回调（实时线程）----
    def _cb(self, outdata, frames, time_info, status):
        filled = 0
        with self._lock:
            while filled < frames and self._buf:
                chunk, tag = self._buf[0]         # 形状 (N, channels)
                n = chunk.shape[0]
                need = frames - filled
                if n <= need:
                    # ⚠️ outdata[..., 0] 是 1 维，必须切出 chunk[:, 0] 也取 1 维，
                    # 否则 (N,1)→(N,) 广播失败，回调抛异常 → 音频流直接停（踩过）
                    outdata[filled:filled + n, 0] = chunk[:, 0]
                    filled += n
                    self._buf.popleft()
                else:
                    outdata[filled:filled + need, 0] = chunk[:need, 0]
                    self._buf[0] = (chunk[need:], tag)
                    filled += need
            if filled < frames:
                outdata[filled:, 0] = 0              # 缓冲空 → 静音
                if self._had_audio:                  # 只在"播过之后又断供"才算卡顿
                    self._underruns += 1
                    self._had_audio = False
            else:
                self._had_audio = True
            # ⚠️ 计数必须与上面的 pop **同锁**更新。分两次加锁的话，
            # `flush()`/`cut_tag()` 可能插在两次加锁之间（它们会把 _buffered 置 0），
            # 随后这里再 `-= filled` → **计数变负** → `is_playing()` 在音频仍在播时
            # 返回 False → 半双工门控漏开门（麦克风听到自己）+ TTS 收尾提前退出
            # （播报中被踩成 IDLE）。审计发现。
            self._played_audio += filled
            self._buffered -= filled
        self._uptime += frames          # 仅统计用，单写者，不必在锁内
        # far 参考镜像：**看 `outdata` 本身** —— 它就是真正送去扬声器的 PCM
        # （缓冲空时填的静音也在里面）。写索引在数据写完之后才推进，读侧凭锁看到
        # 的一定是完整的帧。见 __init__ 里的长注释：这里是"只有一个时钟"的关键。
        self._mirror_far(outdata[:, 0])

    # ---- 生命周期 ----
    def start(self):
        self._stream.start()
        return self

    def stop(self):
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass

    # ---- 写 / 控制 ----
    def write(self, pcm: bytes, tag: str = "audio"):
        """tag 用于事后按来源剪切（如把还在排队的填充音切掉）。"""
        if not pcm:
            return
        # ⚠️ int16 = 每样本 2 字节，块长必须是偶数。
        # 真机踩到（2026-09-19）：Fish 改走 REST 后块长变成**任意**（76/81 是奇数），
        # `np.frombuffer` 抛 ValueError，被 `_tts_loop` 的异常护栏吞掉 →
        # **正文全静音，而填充音照响**（填充音是预渲染 WAV，块长恒定）——
        # 现象极具误导性，查了很久。这里显式拦住并给出可诊断的信息，
        # 别再让它以一个看不懂的 numpy 报错消失在日志里。
        if len(pcm) % 2:
            raise ValueError(
                f"Player.write 收到奇数长度 {len(pcm)} 字节（int16 须为偶数）—— "
                f"上游 TTS 没有按样本对齐。看 tts/fish.py 的块切分。")
        arr = np.frombuffer(pcm, dtype=np.int16)
        rem = arr.shape[0] % self.fmt.channels
        if rem:
            arr = arr[:-rem]
        with self._lock:
            buf = arr.reshape(-1, self.fmt.channels)
            self._buf.append((buf, tag))
            self._buffered += buf.shape[0]

    def _mirror_far(self, pcm: np.ndarray) -> None:
        """把**实际送往扬声器的** PCM 写进环形数组。绝对帧号 f 恒映射到 `f % _far_cap`。

        ⚠️ 由音频回调调用（实时线程）—— 只做切片赋值，不分配、不阻塞。
        注意这里**不重采样** —— 重采样必须在消费侧做（见 aec.py 的说明）。
        """
        n = pcm.shape[0]
        if n == 0:
            return
        with self._far_lock:
            if n >= self._far_cap:
                pcm, n = pcm[-self._far_cap:], self._far_cap
            start = self._far_w % self._far_cap
            end = start + n
            if end <= self._far_cap:
                self._far[start:end] = pcm
            else:
                k = self._far_cap - start
                self._far[start:] = pcm[:k]
                self._far[:end - self._far_cap] = pcm[k:]
            self._far_w += n

    def emit_frames(self) -> int:
        """扬声器**累计输出过**的绝对帧数（44.1k）= 当前播放位置。

        AEC far 读指针的基准。单写者（回调）+ CPython 整数读写原子 → 不必加锁
        （同 `_uptime` 的理由）。
        """
        return self._far_w

    def far_slice(self, start_frame: int, n_frames: int) -> np.ndarray:
        """取绝对帧区间 [start_frame, start_frame + n_frames) 的 far 参考（44.1k int16）。

        **唯一的硬边界**：不读已滚掉的过去（截到 `_far_w - _far_cap`）。
        这里**没有"不读未来"这一条** —— 镜像里的每一帧都已经被扬声器播出去了
        （回调写的），所以镜像帧号 ≡ 播放位置 ≡ `_far_w`，三者同一个时钟。
        返回**实际可用**的部分（可能短于请求，也可能为空）。读指针只增不减。
        """
        with self._far_lock:
            total = self._far_w
            lo = max(0, int(start_frame), total - self._far_cap)
            hi = min(int(start_frame) + max(0, int(n_frames)), total)
            if hi <= lo:
                return np.zeros(0, dtype=np.int16)
            return self._far[np.arange(lo, hi) % self._far_cap].copy()

    def cut_tag(self, tag: str) -> float:
        """把缓冲里该标签的音频整段切掉，返回切掉的秒数。

        用途：填充音还在播时真句子到了 —— 立刻把它从队列里拿走，
        真正的内容紧跟着播，不被填充音往后推。
        （已在回调里、正在播的那一个 block 切不掉，约 23ms，可忽略）
        """
        cut = 0
        with self._lock:
            keep: deque[tuple[np.ndarray, str]] = deque()
            for arr, t in self._buf:
                if t == tag:
                    cut += arr.shape[0]
                else:
                    keep.append((arr, t))
            self._buf = keep
            self._buffered -= cut
        return cut / self.fmt.sample_rate

    def write_filler(self, pcm: bytes, tag: str = "filler"):
        self.write(pcm, tag=tag)

    def feed(self, chunks: Iterator[bytes], tag: str = "audio"):
        """把 TTS 的块流喂进来（阻塞，调用方应在独立线程）。"""
        for ch in chunks:
            self.write(ch, tag)

    def flush(self) -> float:
        """⚡ 打断：清空未播缓冲 → 一个 block 内静音。返回**本轮已播秒数**。"""
        played = self.played_seconds()
        with self._lock:
            self._buf.clear()
            self._buffered = 0
        self._had_audio = False
        return played

    def played_seconds(self) -> float:
        """**本轮**已播音频秒数（OpenAI `audio_end_ms` 的对应物）。"""
        with self._lock:
            return self._played_audio / self.fmt.sample_rate

    def reset_position(self):
        """新一轮开始时调用，把"播到第几秒"归零。"""
        with self._lock:
            self._played_audio = 0
            self._had_audio = False

    def uptime_seconds(self) -> float:
        return self._uptime / self.fmt.sample_rate

    def buffered_seconds(self) -> float:
        with self._lock:
            return self._buffered / self.fmt.sample_rate

    def is_playing(self) -> bool:
        return self.buffered_seconds() > 0

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
