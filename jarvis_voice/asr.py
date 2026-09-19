"""SenseVoice ASR（本地、免费）。

口径：SenseVoice 是 **utterance 级**识别器，没有真正的流式部分结果——
一个语音段进来，一次 decode 出文本。因此端点判定（VAD）在前，ASR 在后。
"""
import re
import time
from dataclasses import dataclass

import numpy as np
import sherpa_onnx

from .config import Config

EMOTIONS = {"HAPPY", "SAD", "ANGRY", "NEUTRAL", "FEARFUL", "DISGUSTED", "SURPRISED"}
_EVENT_RE = re.compile(r"<\|([^|]+)\|>")


def apply_fixes(text: str, fixes) -> str:
    """专名纠正：确定性替换（见 config.asr_fixes 的说明）。

    ⚠️ 只做**整词**替换，不做模糊匹配 —— 宁可漏纠，不可把用户真说的话改掉。
    """
    for wrong, right in fixes or ():
        if wrong and wrong in text:
            text = text.replace(wrong, right)
    return text


@dataclass
class ASRResult:
    text: str
    emotion: str
    events: list[str]      # SenseVoice 的副语言标签（含非情感类，如 BGM/笑声）
    latency_ms: float
    pcm_sec: float


class SenseVoiceASR:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.asr = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=f"{cfg.asr_model_dir}/model.int8.onnx",
            tokens=f"{cfg.asr_model_dir}/tokens.txt",
            use_itn=True, num_threads=cfg.asr_num_threads)

    def transcribe(self, pcm16: np.ndarray) -> ASRResult:
        t0 = time.perf_counter()
        stream = self.asr.create_stream()
        stream.accept_waveform(self.cfg.sample_rate, pcm16)
        self.asr.decode_stream(stream)
        raw = stream.result.text or ""
        events = _EVENT_RE.findall(raw)
        emotion = next((t for t in events if t in EMOTIONS), "NEUTRAL")
        text = apply_fixes(_EVENT_RE.sub("", raw).strip(), self.cfg.asr_fixes)
        return ASRResult(
            text=text, emotion=emotion, events=events,
            latency_ms=(time.perf_counter() - t0) * 1000,
            pcm_sec=len(pcm16) / self.cfg.sample_rate)

    def close(self):
        """本地识别器无常驻连接，无需释放。留着是为了和 FishASR 接口一致
        （orchestrator 统一调 `asr.close()`）。"""


class FishASR:
    """Fish Audio `transcribe-1`（云，$0.36/音频小时，**需 API credit**）。

    实测约束（2026-09-16）：
      - 端点 `POST /v1/asr`，**multipart 或 msgpack**（不接受 base64 JSON）
      - 无 API credit 时 **402**；注意 API credit 与平台积分**独立计费**
      - 文档**只有批量端点，没有流式端点**——但厂商博客宣称"流式 TTFT 200–300ms" ⚠️ 矛盾
      - 返回 `text` / `duration` / `segments[]`（时间戳）/ `language_code`
      - 限制：≤20MB、≤60min、≥1s
    优点：中英 code-switching（厂商主打，🟡 自评）、时间戳、说话人分离。
    缺点：无情感标签（SenseVoice 有）、需上传到云、要花钱。
    """

    def __init__(self, cfg: Config, language: str | None = None):
        import os
        from .tts.fish import load_api_key
        self.cfg = cfg
        self.api_key = load_api_key()
        self.language = language
        self._client = None
        self.name = "fish:transcribe-1"

    def transcribe(self, pcm16: np.ndarray) -> ASRResult:
        import io
        import wave
        import httpx

        if self._client is None:
            self._client = httpx.Client(timeout=120)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2)
            w.setframerate(self.cfg.sample_rate)
            w.writeframes(pcm16.tobytes())
        files = {"audio": ("a.wav", buf.getvalue(), "audio/wav")}
        data = {"ignore_timestamps": "true"}
        if self.language:
            data["language"] = self.language
        t0 = time.perf_counter()
        r = self._client.post("https://api.fish.audio/v1/asr",
                              headers={"Authorization": f"Bearer {self.api_key}"},
                              files=files, data=data)
        ms = (time.perf_counter() - t0) * 1000
        if r.status_code != 200:
            raise RuntimeError(f"Fish ASR {r.status_code}: {r.text[:200]}")
        j = r.json()
        return ASRResult(text=apply_fixes((j.get("text") or "").strip(), self.cfg.asr_fixes),
                         emotion="NEUTRAL", events=[], latency_ms=ms,
                         pcm_sec=len(pcm16) / self.cfg.sample_rate)

    def close(self):
        if self._client:
            self._client.close()
            self._client = None


def make_asr(cfg: Config):
    """按配置选 ASR 后端。默认留在本地 SenseVoice（免费、~100ms、有情感标签）。"""
    provider = (getattr(cfg, "asr_provider", "sensevoice") or "sensevoice").lower()
    if provider == "fish":
        return FishASR(cfg)
    return SenseVoiceASR(cfg)
