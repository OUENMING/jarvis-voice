"""TTS 工厂：按配置选后端。

原则（docs/RESEARCH-NATURALNESS-20260916.md §6）：换 TTS 只是换"谁来念"，
**不改变"念什么"**。所有后端都不改写文本。
"""
from ..config import Config
from .base import AudioFormat, TTSBackend, TTSError
from .fish import FishTTS
from .say import SayTTS

__all__ = ["AudioFormat", "TTSBackend", "TTSError", "FishTTS", "SayTTS", "make_tts"]


def make_tts(cfg: Config) -> TTSBackend:
    provider = (cfg.tts_provider or "fish").lower()
    if provider == "fish":
        return FishTTS(voice_id=cfg.fish_voice, model=cfg.fish_model,
                       sample_rate=cfg.fish_sample_rate, latency=cfg.fish_latency,
                       temperature=cfg.fish_temperature)
    if provider == "say":
        return SayTTS(voice=cfg.say_voice)
    raise TTSError(f"未知 TTS 后端: {provider}（可选 fish | say）")
