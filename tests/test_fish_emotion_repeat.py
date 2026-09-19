#!/usr/bin/env python3
"""严谨版：把「标签效应」和「随机波动」分开。

n=1 时无法判断 F0 差异来自标签还是采样随机性（Fish temperature 默认 0.7）。
本脚本对每个条件重复 N 次，比较：
  · 标签**之间**的均值差（想要的效果）
  · 同一标签**内部**的标准差（噪声）
若 组间差 ≈ 组内噪声 → 标签效果不成立。

用法：.venv/bin/python test_fish_emotion_repeat.py [N=3]
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import os
import statistics as st
import sys
import wave

import httpx
import numpy as np
import librosa

from jarvis_voice.tts.fish import load_api_key, REST_URL

VOICE = "5c353fdb312f4888836a9a5680099ef0"
OUT = "/tmp/fish_emo_repeat"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 3

TEXTS = {
    "中性":     "会议是明天下午三点。",
    "情绪":     "今天真是太棒了！",
    "任务汇报": "我把 config 的超时改成六十秒了。",
}
TAGS = ["—", "[happy]", "[sad]"]


def tts(text: str) -> np.ndarray:
    h = {"Authorization": f"Bearer {load_api_key()}",
         "Content-Type": "application/json", "model": "s2.1-pro-free"}
    with httpx.Client(timeout=60) as c:
        r = c.post(REST_URL, headers=h,
                   json={"text": text, "format": "pcm",
                         "reference_id": VOICE, "latency": "balanced"})
        r.raise_for_status()
        return np.frombuffer(r.content, dtype=np.int16)


def f0(pcm: np.ndarray) -> float:
    y = pcm.astype(np.float32) / 32768.0
    v = librosa.yin(y, fmin=70, fmax=420, sr=44100, frame_length=2048)
    v = v[(v > 70) & (v < 420)]
    return float(np.mean(v)) if v.size >= 5 else float("nan")


def save(path, pcm):
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(44100)
        w.writeframes(pcm.tobytes())


def main():
    os.makedirs(OUT, exist_ok=True)
    for f in os.listdir(OUT):
        os.remove(os.path.join(OUT, f))

    data: dict[str, dict[str, list[float]]] = {}
    for cat, text in TEXTS.items():
        data[cat] = {}
        for tag in TAGS:
            vals = []
            for i in range(N):
                s = f"{tag} {text}" if tag != "—" else text
                try:
                    pcm = tts(s)
                except Exception as e:
                    print(f"  ❌ {cat}/{tag}: {type(e).__name__}")
                    continue
                vals.append(f0(pcm))
                save(f"{OUT}/{cat}_{tag.strip('[]') or 'none'}_{i}.wav", pcm)
            data[cat][tag] = vals
            if vals:
                print(f"  {cat:<6} {tag:<9} n={len(vals)}  "
                      f"F0均值 {st.mean(vals):6.1f}Hz  组内SD {st.pstdev(vals):5.2f}")

    print("\n=== 组间差 vs 组内噪声（关键判据）===")
    print("  若 |组间差| 不明显大于 组内SD → 标签效果分不清，等于无效\n")
    for cat, d in data.items():
        base = d.get("—") or []
        if not base:
            continue
        noise = st.pstdev(base)
        print(f"  【{cat}】基线组内SD={noise:.2f}Hz")
        for tag, vals in d.items():
            if tag == "—" or not vals:
                continue
            diff = st.mean(vals) - st.mean(base)
            pooled = (st.pstdev(vals) + noise) / 2 if len(vals) > 1 else noise
            ratio = abs(diff) / pooled if pooled > 0.01 else float("inf")
            verdict = ("✅ 可信" if ratio >= 2.0 else
                       "⚠️ 可疑（可能只是噪声）" if ratio >= 1.0 else
                       "❌ 无效（淹没在噪声里）")
            print(f"     {tag:<9} 组间差 {diff:+7.1f}Hz  vs 合并噪声 {pooled:5.2f}  "
                  f"比值 {ratio:4.1f}×  {verdict}")


if __name__ == "__main__":
    main()
