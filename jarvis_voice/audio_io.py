"""麦克风输入与播放设备。

MicStream：sounddevice 回调 → 有界队列（满则**丢最老**，不让延迟无界增长）。
回调里只做拷贝入队，绝不阻塞——回调线程阻塞会导致 CoreAudio 丢帧。
"""
import queue

import numpy as np
import sounddevice as sd

from .config import Config


def input_devices() -> list[tuple[int, str, int]]:
    """(index, name, max_input_channels) —— 只列有输入通道的设备。"""
    return [(i, d["name"], d["max_input_channels"])
            for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]


def output_devices() -> list[tuple[int, str, int]]:
    return [(i, d["name"], d["max_output_channels"])
            for i, d in enumerate(sd.query_devices()) if d["max_output_channels"] > 0]


def resolve_device(spec: str | int | None, want_output: bool = True) -> int | None:
    """把设备规格解析成 sounddevice 的索引。

    支持：None/"-"（默认设备）、纯数字（索引）、名字子串（不区分大小写）。
    ⚠️ 免提模式必须把**输出**也切到内置扬声器——只关打断而输出仍走耳机的话，
    麦克风根本听不到，等于还是耳机场景，测不出真免提的问题。
    """
    if spec is None or spec == "" or spec == "-":
        return None
    if isinstance(spec, int) or (isinstance(spec, str) and spec.isdigit()):
        return int(spec)
    key = str(spec).lower()
    for i, d in enumerate(sd.query_devices()):
        chans = d["max_output_channels"] if want_output else d["max_input_channels"]
        if chans > 0 and key in d["name"].lower():
            return i
    return None


class MicStream:
    def __init__(self, cfg: Config, device: int | None = None):
        self.cfg = cfg
        self.device = device
        self.q: queue.Queue[np.ndarray] = queue.Queue(maxsize=cfg.mic_queue_max)
        self._stream: sd.InputStream | None = None
        self.dropped = 0

    def _cb(self, indata, frames, time_info, status):
        chunk = indata[:, 0].copy()
        try:
            self.q.put_nowait(chunk)
        except queue.Full:
            # 丢最老，保住低延迟（宁可丢一段旧音频，也不要延迟堆积）
            self.dropped += 1
            try:
                self.q.get_nowait()
            except queue.Empty:
                pass
            try:
                self.q.put_nowait(chunk)
            except queue.Full:
                pass

    def start(self):
        self._stream = sd.InputStream(
            samplerate=self.cfg.sample_rate, channels=1, dtype="float32",
            blocksize=self.cfg.mic_blocksize, device=self.device, callback=self._cb)
        self._stream.start()
        return self

    def read(self, timeout: float = 0.5) -> np.ndarray | None:
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self) -> list[np.ndarray]:
        """取空当前所有待处理块（打断时用于丢弃陈旧音频）。"""
        out = []
        while True:
            try:
                out.append(self.q.get_nowait())
            except queue.Empty:
                return out

    def stop(self):
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
