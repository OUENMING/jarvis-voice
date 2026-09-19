#!/usr/bin/env python3
"""ASR 后端对比：SenseVoice（本地）vs Fish transcribe-1（云）。

  .venv/bin/python test_asr_compare.py                 # 用内置测试音频
  .venv/bin/python test_asr_compare.py a.wav b.wav     # 指定音频
  .venv/bin/python test_asr_compare.py --mixed         # 合成中英混说用例再比

⚠️ Fish 需要 **API credit**（与平台积分独立计费）。没有会 402——脚本会明确提示，
   不会静默失败。
⚠️ 方法学提醒：`--mixed` 的音频是 **TTS 合成的**，因此错误可能来自 TTS 发音而非 ASR。
   要下结论必须用**真人录音**（计划里的"100 条 Owen 语音样本"）。
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import glob
import io
import sys
import time
import wave

import numpy as np

from jarvis_voice.asr import FishASR, SenseVoiceASR
from jarvis_voice.config import Config

TESTWAV = "models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/test_wavs"
MIXED = [
    "帮我把 config.py 里的 timeout 改成六十秒。",
    "UCD 的 ECS4 专业今年收多少人？",
    "把那个 pull request merge 一下，然后 push 到 main 分支。",
    "我用的是 MacBook Pro，M1 Pro 芯片。",
]


def read_wav(p: str) -> np.ndarray:
    with wave.open(p) as f:
        return np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16).copy()


def resample_44100_to_16000(pcm: np.ndarray) -> np.ndarray:
    n = int(len(pcm) * 16000 / 44100)
    idx = (np.arange(n) * 44100 // 16000).astype(np.int64)
    return pcm[idx].astype(np.int16)


def synth_mixed() -> list[tuple[str, np.ndarray]]:
    """用 Fish TTS 合成中英混说音频（免费模型）。"""
    import httpx
    from jarvis_voice.tts.fish import load_api_key
    H = {"Authorization": f"Bearer {load_api_key()}", "Content-Type": "application/json",
         "model": "s2.1-pro-free"}
    out = []
    with httpx.Client(timeout=60) as c:
        for t in MIXED:
            r = c.post("https://api.fish.audio/v1/tts", headers=H,
                       json={"text": t, "format": "pcm",
                             "reference_id": "5c353fdb312f4888836a9a5680099ef0",
                             "latency": "balanced"})
            r.raise_for_status()
            out.append((t, resample_44100_to_16000(np.frombuffer(r.content, dtype=np.int16))))
    return out


def main():
    cfg = Config.load()
    if "--mixed" in sys.argv:
        print("合成中英混说用例（TTS 合成 → 仅供相对比较，非真人录音）…\n")
        items = synth_mixed()
    else:
        paths = [a for a in sys.argv[1:] if not a.startswith("--")]
        if not paths:
            paths = sorted(glob.glob(f"{TESTWAV}/*.wav"))[:3]
        items = [(p.split("/")[-1], read_wav(p)) for p in paths]

    print("加载 SenseVoice…")
    sv = SenseVoiceASR(cfg)
    fish_ok = True
    try:
        fish = FishASR(cfg)
    except Exception as e:
        fish, fish_ok = None, False
        print(f"⚠️ Fish ASR 不可用: {e}")

    print(f"\n{'参考/本地转写':<46} {'延迟':>8}  {'Fish 转写':<40} {'延迟':>8}")
    print("-" * 112)
    sv_times, fish_times = [], []
    for label, pcm in items:
        r1 = sv.transcribe(pcm)
        sv_times.append(r1.latency_ms)
        if fish_ok:
            try:
                r2 = fish.transcribe(pcm)
                fish_times.append(r2.latency_ms)
                f_text, f_ms = r2.text, f"{r2.latency_ms:.0f}ms"
            except Exception as e:
                f_text, f_ms = f"❌ {str(e)[:40]}", "-"
        else:
            f_text, f_ms = "(需 API credit)", "-"
        print(f"{r1.text[:44]:<46} {r1.latency_ms:6.0f}ms  {f_text[:38]:<40} {f_ms:>8}")
        if "--mixed" in sys.argv:
            print(f"   ↑ 原文: {label}")

    print("-" * 112)
    if sv_times:
        print(f"SenseVoice 延迟 中位 {sorted(sv_times)[len(sv_times)//2]:.0f}ms")
    if fish_times:
        print(f"Fish ASR  延迟 中位 {sorted(fish_times)[len(fish_times)//2]:.0f}ms")
    else:
        print("Fish ASR 未测到（无 API credit）。充值后重跑本脚本即可对比。")


if __name__ == "__main__":
    main()
