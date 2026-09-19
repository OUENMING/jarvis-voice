#!/usr/bin/env python3
"""TTS 层验证：Fish（WebSocket）+ say 兜底。

  .venv/bin/python test_tts.py              # Fish，生成并播放
  .venv/bin/python test_tts.py --say        # say 兜底
  .venv/bin/python test_tts.py --ttfa       # 只测首包延迟（不播）

验证点：① 后端可切换 ② PCM 格式正确（能直接喂 sounddevice）③ 首包延迟
        ④ say 的 stop() 真能打断（jarvis_demo 做不到的事）
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import sys
import time

import numpy as np
import sounddevice as sd

from jarvis_voice.config import Config
from jarvis_voice.tts import make_tts

SENTENCES = ["你好，我是 Aries。", "今天都柏林的天气还行，", "要不要我帮你查一下明天要不要带伞？"]


def play_pcm(chunks, fmt, label=""):
    """把 PCM 块边收边播（真正的流式播放）。"""
    stream = sd.OutputStream(samplerate=fmt.sample_rate, channels=fmt.channels,
                             dtype="int16", blocksize=0)
    stream.start()
    t0 = time.perf_counter(); first = None; total = 0
    try:
        for ch in chunks:
            if first is None:
                first = (time.perf_counter() - t0) * 1000
            stream.write(np.frombuffer(ch, dtype=np.int16).reshape(-1, fmt.channels))
            total += len(ch)
    finally:
        stream.stop(); stream.close()
    dur = total / fmt.bytes_per_frame / fmt.sample_rate
    print(f"  {label}首包 {first:6.0f}ms | 音频 {dur:4.2f}s | {total//1024}KB | 全程 {(time.perf_counter()-t0):.2f}s")
    return first


def main():
    cfg = Config.load()
    use_say = "--say" in sys.argv
    only_ttfa = "--ttfa" in sys.argv
    if use_say:
        cfg = Config(**{**cfg.__dict__, "tts_provider": "say"})

    tts = make_tts(cfg)
    print(f"后端: {tts.name} | 格式: {tts.audio_format}")

    if use_say:
        print("\n=== say 兜底 ===")
        for s in SENTENCES:
            play_pcm(tts.synthesize(s), tts.audio_format, label=f"[{s[:10]}…] ")
        print("\n=== 打断验证：say 播长文本中途 stop() ===")
        gen = tts.synthesize("这是一段很长的话，用来验证 stop() 能不能真的把它打断。" * 3)
        t0 = time.perf_counter(); n = 0
        for ch in gen:
            n += 1
            if n == 3:
                tts.stop()
                print(f"  已播 {n} 块后调用 stop()，耗时 {(time.perf_counter()-t0):.2f}s")
                break
        left = sum(1 for _ in gen)
        print(f"  stop() 后生成器剩余块数: {left}（应为 0 = 真的停了）")
        tts.close()
        return

    print("\n=== Fish：逐句流式播放 ===")
    for s in SENTENCES:
        try:
            play_pcm(tts.synthesize(s), tts.audio_format, label=f"[{s[:10]}…] ")
        except Exception as e:
            print(f"  ❌ {type(e).__name__}: {e}")

    print("\n=== Fish：多句一次连接（热连接）===")
    try:
        play_pcm(tts.synthesize_many(SENTENCES), tts.audio_format, label="[3句] ")
    except Exception as e:
        print(f"  ❌ {type(e).__name__}: {e}")

    print("\n=== Fish：首包延迟 ×5 ===")
    xs = []
    for i in range(5):
        t0 = time.perf_counter(); first = None
        for _ in tts.synthesize("好的，我这就去办。"):
            if first is None:
                first = (time.perf_counter() - t0) * 1000
        xs.append(first)
        print(f"  第{i+1}次 {first:6.0f}ms")
    xs.sort()
    print(f"  → 中位 {xs[len(xs)//2]:.0f}ms  最小 {xs[0]:.0f}  最大 {xs[-1]:.0f}")
    tts.close()


if __name__ == "__main__":
    main()
