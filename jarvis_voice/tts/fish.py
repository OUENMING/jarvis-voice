"""Fish Audio TTS 后端（s2.1-pro-free + 预设音色）。

实测要点（2026-09-16，见 docs/RESEARCH-TTS-COST-20260916.md 与本次会话记录）：
  - ⚠️ **`model` 必须放在 HTTP header**；放 body 会掉回付费默认模型 → 402。
  - ⚠️ **WebSocket 比 REST 流式快约 2.4 倍**（首包 ~1.1s vs ~2.6s）——默认走 WS。
  - ⚠️ **免费档无延迟保证**：实测首包 340ms–5.2s 大幅波动（官方博客自称 70–90ms，免费档拿不到）。
  - PCM 输出实测 = **44100Hz / 单声道 / int16**。
  - `chunk_length` 下限是 100（<100 返回 400）。
  - 端点：REST `POST /v1/tts`；WS `wss://api.fish.audio/v1/tts/live`。

不做的事：**不改写文本**（硬需求：逐字忠实）。`[happy]`/`(breath)` 等标记原样透传。
"""
import os
import threading
from typing import Iterable, Iterator

import httpx

from .base import AudioFormat, TTSError

REST_URL = "https://api.fish.audio/v1/tts"


def load_api_key(explicit: str | None = None) -> str:
    """优先显式传入 → 环境变量 FISH_API_KEY → ~/.jarvis/fish.env 里读。"""
    if explicit:
        return explicit
    if os.environ.get("FISH_API_KEY"):
        return os.environ["FISH_API_KEY"]
    env_path = os.path.expanduser("~/.jarvis/fish.env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("FISH_API_KEY="):
                    return line.split("=", 1)[1].strip()
    raise TTSError("未找到 FISH_API_KEY（环境变量或 ~/.jarvis/fish.env）")


class FishTTS:
    """Fish Audio。默认走 WebSocket 流式（更低首包）。"""

    def __init__(self, voice_id: str, model: str = "s2.1-pro-free",
                 api_key: str | None = None, sample_rate: int = 44100,
                 latency: str = "balanced", use_websocket: bool = True,
                 temperature: float = 0.3, timeout: float = 60.0):
        self.name = f"fish:{model}"
        self.voice_id = voice_id
        self.model = model
        self.api_key = load_api_key(api_key)
        self.audio_format = AudioFormat(sample_rate=sample_rate)
        self.latency = latency
        # 实测（test_fish_emotion_repeat.py）：temperature 默认 0.7 时，
        # **同一句重复生成**的时长变异 7.6%、F0 变异 8.0%（人声重复 <2%）。
        # 降到 0.1 后时长变异 → 2.1%。语音助手要的是**稳定**，不是多变。
        self.temperature = temperature
        self.use_websocket = use_websocket
        self.timeout = timeout
        self._client: httpx.Client | None = None
        self._ws_client = None          # 复用同一个 SDK 客户端，别每句新建（泄漏）
        self._init_lock = threading.Lock()   # 保护懒初始化（多线程可能同时进来）

    # ---------- REST 流式（回退路径 / 单句已知文本） ----------
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "model": self.model}          # ⚠️ 必须在 header

    def _synthesize_rest(self, text: str) -> Iterator[bytes]:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout)
        body = {"text": text, "format": "pcm", "reference_id": self.voice_id,
                "latency": self.latency, "temperature": self.temperature,
                "sample_rate": self.audio_format.sample_rate}
        with self._client.stream("POST", REST_URL, headers=self._headers(), json=body) as r:
            if r.status_code != 200:
                detail = r.read()[:200].decode("utf-8", "replace")
                raise TTSError(f"Fish REST {r.status_code}: {detail}")
            # ⚠️ REST 的块长度**任意**（实测 76/81 是奇数，如 1363/1369 字节），
            # 而我们的格式是 int16 = 每样本 2 字节。直接 `np.frombuffer` 会抛
            # `ValueError: buffer size must be a multiple of element size`，
            # **整句哑掉**（真机踩到：填充音能响、正文全静音 —— 因为填充音是
            # 预渲染 WAV，块长恒定；正文走这条路）。
            # WS 路的块是 40960 字节（偶数）所以一直没暴露。
            #
            # ⚠️ 不能简单丢弃余字节 —— 那会让**后续所有样本错位半个**（噪音）。
            # 必须**跨块携带**：把上一块多出来的那个字节拼到下一块前面。
            carry = b""
            for chunk in r.iter_bytes():
                buf = carry + chunk
                even = len(buf) - (len(buf) % 2)
                if even:
                    yield buf[:even]
                carry = buf[even:]
            # 流结束时可能剩半个样本 —— 丢掉（不足一个样本，听不出来）
            if carry:
                print(f"[tts] REST 流尾剩 {len(carry)} 字节（半个样本），已丢弃", flush=True)

    # ---------- WebSocket 流式（默认，更快） ----------
    def _synthesize_ws(self, texts: Iterable[str]) -> Iterator[bytes]:
        from fishaudio import FishAudio
        from fishaudio.types.tts import TTSConfig
        key = self.api_key
        os.environ.setdefault("FISH_API_KEY", key)
        # ⚠️ 复用客户端：旧版每句 `FishAudio(...)` 新建且从不关闭，
        # 长会话会累积 socket（审计发现）。
        if self._ws_client is None:
            with self._init_lock:          # 双检锁：两线程同时进来只会建一个
                if self._ws_client is None:
                    self._ws_client = FishAudio(api_key=key)
        cfg = TTSConfig(format="pcm", latency=self.latency,
                        temperature=self.temperature, normalize=True)
        yield from self._ws_client.tts.stream_websocket(
            iter(list(texts)), reference_id=self.voice_id, model=self.model, config=cfg)

    # ---------- 对外 ----------
    def synthesize(self, text: str) -> Iterator[bytes]:
        if self.use_websocket:
            produced = False
            try:
                for ch in self._synthesize_ws([text]):
                    produced = True
                    yield ch
                return
            except Exception as e:
                if produced:
                    # ⚠️ 已经吐过音频了：**绝不能回退 REST** —— 那会把整句重念一遍，
                    # 用户听到重复（审计发现）。宁可这句不完整，也不要重复。
                    print(f"[tts] WS 中途失败，已产出音频，不回退（避免重念）: "
                          f"{type(e).__name__}: {e}", flush=True)
                    return
                print(f"[tts] WS 失败，回退 REST: {type(e).__name__}: {e}", flush=True)
        yield from self._synthesize_rest(text)

    def synthesize_many(self, texts: Iterable[str]) -> Iterator[bytes]:
        """多句一次连接（热连接，省去每句握手）。"""
        if self.use_websocket:
            yield from self._synthesize_ws(texts)
        else:
            for t in texts:
                yield from self._synthesize_rest(t)

    def close(self):
        if self._client is not None:
            self._client.close()
            self._client = None
        if self._ws_client is not None:
            try:
                self._ws_client.close()
            except Exception:
                pass
            self._ws_client = None
