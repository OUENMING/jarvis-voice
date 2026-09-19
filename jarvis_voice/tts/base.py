"""TTS 后端抽象。

设计约束（来自 docs/RESEARCH-NATURALNESS-20260916.md §6）：
  - **只做"怎么说"，不做"说什么"** —— 后端**绝不改写文本**。
  - 输出统一为**裸 PCM**（方便直接喂 sounddevice，且打断时可按帧截断）。
  - 后端必须能被**中途停止**（barge-in 需要）。

风格/情感由**生成侧(prompt)** 决定；本层只负责把给定文本念出来。
Fish 的 `[happy]` / `(breath)` 之类标记属于"文本的一部分"，
是否注入由上层决定；本层**原样透传**，不清洗、不解释。
"""
from dataclasses import dataclass
from typing import Iterator, Protocol, runtime_checkable


@dataclass(frozen=True)
class AudioFormat:
    sample_rate: int
    channels: int = 1
    sample_width: int = 2      # 字节数，2 = int16

    @property
    def bytes_per_frame(self) -> int:
        return self.channels * self.sample_width


@runtime_checkable
class TTSBackend(Protocol):
    name: str
    audio_format: AudioFormat

    def synthesize(self, text: str) -> Iterator[bytes]:
        """把一段文本转成 PCM 块流。可被中途放弃（生成器 close = 停止）。"""
        ...

    def close(self) -> None:
        ...


class TTSError(RuntimeError):
    pass
