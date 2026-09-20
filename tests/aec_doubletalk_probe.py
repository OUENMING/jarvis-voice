#!/usr/bin/env python3
"""双讲保真实测 —— 播放时你念一句话，看 AEC 有没有把你也消掉。

设计要点：
  - **两轮**，每轮 15 秒，中间隔 5 秒 → 不依赖按键，你听到声音就念
  - **12 秒静默预备** → 给你时间看说明/准备好（同时录一段当"无人声"参照）
  - far 用 zh.wav + en.wav 拼接（**接缝处交叉淡化** —— 避免 np.tile 造成的阶跃伪影）
  - 同时跑 AEC-only 与 AEC+NS 两条，因为 **NS 是最可能伤语音的那一环**

产出：dt_{raw,aec,aec_ns}_r{1,2}.wav
用法：.venv/bin/python dt_probe.py
"""
import os
import sys
import time
import wave

import numpy as np
import sounddevice as sd
from scipy.signal import resample_poly

from pywebrtc_audio import AudioProcessor, EchoCanceller

RATE, FRAME = 16000, 160
SENSE = os.path.expanduser(
    "~/jarvis-voice/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17")
OUT = "/tmp/aec-probe"


def _read(p):
    with wave.open(p, "rb") as w:
        sr, ch, n = w.getframerate(), w.getnchannels(), w.getnframes()
        a = np.frombuffer(w.readframes(n), dtype=np.int16)
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1).astype(np.int16)
    if sr != RATE:
        a = np.clip(resample_poly(a.astype(np.float32), RATE, sr), -32768, 32767).astype(np.int16)
    return a


def far_bed(seconds=15.0):
    """zh + en 拼接，接缝交叉淡化 20ms —— 避免循环拼接的阶跃（前一次踩到过）。"""
    a, b = _read(f"{SENSE}/test_wavs/zh.wav"), _read(f"{SENSE}/test_wavs/en.wav")
    xf = int(RATE * 0.02)
    seam = a[-xf:] * np.linspace(1, 0, xf) + b[:xf] * np.linspace(0, 1, xf)
    sig = np.concatenate([a[:-xf], seam.astype(np.int16), b[xf:]])
    want = int(RATE * seconds)
    while sig.size < want:                       # 不够就再拼一次（同样交叉淡化）
        tail, head = sig[-xf:], sig[:xf]
        s2 = tail * np.linspace(1, 0, xf) + head * np.linspace(0, 1, xf)
        sig = np.concatenate([sig[:-xf], s2.astype(np.int16), sig[xf:]])
    return sig[:want]


def rms(x):
    x = x.astype(np.float64)
    return float(np.sqrt(np.mean(x * x))) if x.size else 0.0


def one_round(far, tag, *, with_ns):
    ec = EchoCanceller(sample_rate=RATE)
    ap = AudioProcessor(sample_rate=RATE, echo_cancellation=True,
                        noise_suppression=True) if with_ns else None
    n = len(far) // FRAME
    fb, rb, eb, ab = [], [], [], []
    with sd.Stream(samplerate=RATE, blocksize=FRAME, channels=1, dtype="int16") as st:
        for i in range(n):
            s = i * FRAME
            ff = far[s:s + FRAME]
            st.write(ff.reshape(-1, 1))          # 先 render
            nf, _ = st.read(FRAME)               # 再 capture（WebRTC 官方顺序）
            nf = nf.flatten()
            fb.append(ff.copy()); rb.append(nf.copy())
            eb.append(ec.process(nf, ff))
            if ap is not None:
                ab.append(ap.process(nf, ff))
    cat = lambda b: np.concatenate(b)
    out = {"far": cat(fb), "raw": cat(rb), "aec": cat(eb)}
    if ap is not None:
        out["aec_ns"] = cat(ab)
    for k, v in out.items():
        with wave.open(f"{OUT}/dt_{k}_{tag}.wav", "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
            w.writeframes(v.tobytes())
    return out


def main():
    os.makedirs(OUT, exist_ok=True)
    far = far_bed(15.0)

    print("=" * 68, flush=True)
    print("双讲保真实测", flush=True)
    print("=" * 68, flush=True)
    print("\n请念这一句（念 2-3 遍，每遍之间稍停）：\n", flush=True)
    print("    帮我把那个文件移到备份目录里，原目录就别留了\n", flush=True)
    print("现在开始 12 秒预备（保持安静，这一段用作稳态参照）…\n", flush=True)
    # ⚠️ **先打印再 sleep**（ocr 2026-09-20 报的）：原写法是 `sleep(3)` 之后才 print
    # `{s} 秒后开始播放`，于是每一条都比实际晚了 3 秒 —— 第一句说"12 秒后"时其实只剩 9 秒，
    # 最后一句说"3 秒后"时应该**立刻**开口。照提示念的人会整体晚 3 秒，
    # 双讲段与提示时序错位 → 测量结论不可信。
    for s in (12, 9, 6, 3):
        print(f"    {s} 秒后开始播放 → 听到声音就开始念", flush=True)
        time.sleep(3)

    _, _, _, _ = None, None, None, None
    # 预备段：只录不说话（拿稳态 ERLE 的干净参照）
    sil = np.zeros(RATE * 12, dtype=np.int16)
    _ = one_round(sil, "pre", with_ns=False)
    print("\n>>> 第 1 轮：现在开始播！听到就念 <<<\n", flush=True)
    r1 = one_round(far, "r1", with_ns=True)
    import time; time.sleep(5)
    print("\n>>> 第 2 轮：再来一次 <<<\n", flush=True)
    r2 = one_round(far, "r2", with_ns=True)

    print("\n" + "=" * 68)
    print("数值（决策级事件总量；双讲段包含你的语音，所以绝对值会比纯回声高）")
    print("=" * 68)
    for lbl, d in (("第1轮", r1), ("第2轮", r2)):
        print(f"\n[{lbl}]  far={rms(d['far']):8.1f}  raw={rms(d['raw']):8.1f}  "
              f"aec={rms(d['aec']):8.1f}  aec_ns={rms(d['aec_ns']):8.1f}")
    print(f"\nwav 已存到 {OUT}/dt_*.wav")
    return 0


if __name__ == "__main__":
    sys.exit(main())
