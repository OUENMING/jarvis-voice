"""jarvis-voice：级联语音助手（耳机 + 云优先 TTS）。

链路：麦克风 → Silero/TEN VAD → SenseVoice ASR → Claude Code（常驻 stream-json，可中断）
      → 切句 → TTS（云优先，say 兜底）→ 耳机
"""
__version__ = "0.1.0"
