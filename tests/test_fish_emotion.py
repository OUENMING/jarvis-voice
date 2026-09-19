#!/usr/bin/env python3
"""验证 Fish bracket 情感标签到底有没有用 —— 把"听不出来"变成数字。

假设（来自 GitHub issue #1280，🟡 且是 self-hosted S2-Pro）：
  `[happy]` 这类标签是**放大器**而非**覆盖器** ——
  文本本身带情绪时有效，中性文本上几乎无效。

方法：对同一句做「情感文本 vs 中性文本」×「无标签/标签」的对照，
      再用 librosa 算 F0 统计（情绪在声学上主要体现为音高的均值与波动）。
      F0 差异 ≈ 0 说明标签没起作用。

用法：.venv/bin/python test_fish_emotion.py
输出：/tmp/fish_emo/ 下 8 个 wav（自己听）+ 一张 F0 对照表
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import os
import wave

import httpx
import numpy as np
import librosa

from jarvis_voice.tts.fish import load_api_key, REST_URL

VOICE = "5c353fdb312f4888836a9a5680099ef0"   # 女大学生
OUT = "/tmp/fish_emo"

CASES = [
    # (标签, 文本, 类别)
    ("—",         "会议是明天下午三点。",            "中性"),
    ("[happy]",   "会议是明天下午三点。",            "中性"),
    ("[sad]",     "会议是明天下午三点。",            "中性"),
    ("—",         "今天真是太棒了！",                "情绪"),
    ("[happy]",   "今天真是太棒了！",                "情绪"),
    ("[sad]",     "今天真是太棒了！",                "情绪"),
    ("—",         "我把 config 的超时改成六十秒了。", "任务汇报"),
    ("[happy]",   "我把 config 的超时改成六十秒了。", "任务汇报"),
    ("(breath)",  "我，嗯，(breath) 我想想。",        "副语言"),
]


def tts(text: str) -> np.ndarray:
    h = {"Authorization": f"Bearer {load_api_key()}",
         "Content-Type": "application/json", "model": "s2.1-pro-free"}
    b = {"text": text, "format": "pcm", "reference_id": VOICE, "latency": "balanced"}
    with httpx.Client(timeout=60) as c:
        r = c.post(REST_URL, headers=h, json=b)
        r.raise_for_status()
        return np.frombuffer(r.content, dtype=np.int16)


def f0_stats(pcm: np.ndarray, sr: int = 44100) -> dict:
    y = pcm.astype(np.float32) / 32768.0
    f0 = librosa.yin(y, fmin=70, fmax=420, sr=sr, frame_length=2048)
    v = f0[(f0 > 70) & (f0 < 420)]
    if v.size < 5:
        return {"mean": float("nan"), "std": float("nan"), "range": float("nan")}
    return {"mean": float(np.mean(v)), "std": float(np.std(v)),
            "range": float(np.percentile(v, 95) - np.percentile(v, 5))}


def save_wav(path: str, pcm: np.ndarray, sr: int = 44100):
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def main():
    os.makedirs(OUT, exist_ok=True)
    for f in os.listdir(OUT):
        os.remove(os.path.join(OUT, f))
    print(f"{'类别':<6} {'标签':<10} {'时长':>6} {'F0均值':>8} {'F0波动':>8} {'F0幅度':>8}  文本")
    print("-" * 92)
    prev: dict[str, dict] = {}
    for i, (tag, text, cat) in enumerate(CASES):
        try:
            pcm = tts(f"{tag} {text}" if tag != "—" else text)
        except Exception as e:
            print(f"{cat:<6} {tag:<10}  ❌ {type(e).__name__}: {str(e)[:50]}")
            continue
        st = f0_stats(pcm)
        dur = len(pcm) / 44100
        fn = f"{OUT}/{i}_{cat}_{tag.strip('[]()') or 'none'}_{len(text)}.wav"
        save_wav(fn, pcm)
        print(f"{cat:<6} {tag:<10} {dur:5.2f}s {st['mean']:7.1f}Hz {st['std']:7.1f} "
              f"{st['range']:7.1f}  {text}")
        prev.setdefault(cat, {})[tag] = st

    print("\n=== 同类别内，加标签 vs 不加标签 的 F0 差异 ==="
          "（≈0 = 标签没起作用）")
    for cat, d in prev.items():
        base = d.get("—")
        if not base:
            continue
        for tag, st in d.items():
            if tag == "—":
                continue
            dm = st["mean"] - base["mean"]
            ds = st["std"] - base["std"]
            verdict = ("❌ 几乎无差别" if abs(dm) < 6 and abs(ds) < 4
                       else "✅ 有可测差别")
            print(f"  {cat:<6} {tag:<10} ΔF0均值 {dm:+7.1f}Hz  ΔF0波动 {ds:+7.1f}  {verdict}")


if __name__ == "__main__":
    main()
