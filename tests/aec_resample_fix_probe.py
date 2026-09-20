#!/usr/bin/env python3
"""U-1 续：验证修法 —— 把重采样从「生产侧」移到「消费侧」。

根因（上一测）：resample_poly 输出长度 = ceil(L*160/441)。
  生产侧块长（TTS 来的）不是 441 的整数倍 → 每块多半/少半样本 → 累积漂移。

洞察：**消费侧块长是固定的**（MicStream 给 100ms@16k = 1600 样本）。
  1600 * 441 / 160 = 4410 —— **恰好是 441 的 10 倍，整数**。
  → 只要读指针永远落在 441 的整数网格上，输出长度精确 = 1600，零漂移。

再验两个变体：
  C1：直接重采样需要的 4410 个（无余量）
  C2：带 50ms 余量、丢弃边界（消除 resample_poly 的边缘瞬态）
  C3：余量再大一点（100ms），看是否还有收益
"""
import sys
import numpy as np
from scipy.signal import resample_poly

sys.path.insert(0, "/Users/owen/jarvis-voice")
from pywebrtc_audio import EchoCanceller  # noqa: E402

UP, DOWN = 160, 441
RATE44, RATE16 = 44100, 16000
SENSE = "/Users/owen/jarvis-voice/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"


def load16(path):
    import wave
    with wave.open(path, "rb") as w:
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        if w.getframerate() != RATE16:
            x = resample_poly(x.astype(np.float32), RATE16, w.getframerate()).astype(np.int16)
    return x


def rms(x):
    x = x.astype(np.float64)
    return float(np.sqrt(np.mean(x * x))) if x.size else 0.0


def far_by_consumer_side(x44, near16, chunk16=1600, margin44=0):
    """消费侧重采样：每次按固定的 chunk16 从连续窗口取 far。

    读指针按 44.1k 绝对帧推进，**永不由累加输出长度决定** —— 这是零漂移的关键。
    """
    out = []
    pos = 0                       # 44.1k 绝对帧读指针
    total16 = 0
    while total16 < len(near16):
        n16 = min(chunk16, len(near16) - total16)
        # 需要消费的 44.1k 样本数：严格按 n16 换算（可整除，因为 n16 是 160 的倍数）
        need = n16 * DOWN // UP   if (n16 * DOWN) % UP == 0 else None
        if need is None:
            # 非整除：用分数推进，仍然不漂（取整只影响这一次的边界）
            need = round(n16 * DOWN / UP)
        s = max(0, pos - margin44)
        e = min(len(x44), pos + need + margin44)
        if e - s < 2:
            out.append(np.zeros(n16, np.int16))
            pos += need
            total16 += n16
            continue
        r = resample_poly(x44[s:e].astype(np.float32), UP, DOWN)
        # 窗口起点 s 对应 16k 域的位置
        off = round((pos - s) * UP / DOWN)
        seg = r[off:off + n16]
        if len(seg) < n16:
            seg = np.concatenate([seg, np.zeros(n16 - len(seg), np.float32)])
        out.append(seg.astype(np.int16))
        pos += need
        total16 += n16
    return np.concatenate(out)


def erle(near, ref16, skip_s=1.5):
    ec = EchoCanceller(sample_rate=RATE16)
    y = ec.process(near, ref16[:len(near)])
    s = int(skip_s * RATE16)
    return 20 * np.log10(max(rms(near[s:]), 1e-12) / max(rms(y[s:]), 1e-12))


def main():
    x16 = load16(f"{SENSE}/test_wavs/zh.wav")
    # ⚠️ 空数组会让下面 `concatenate([x16, x16])` 永远是空的 → **死循环空转占满 CPU**
    # （ocr 2026-09-20 报的）。wav 读失败/重采样结果为空都会走到这里。
    if x16.size == 0:
        raise SystemExit(f"读不到音频或为空: {SENSE}/test_wavs/zh.wav")
    while len(x16) < RATE16 * 12:
        x16 = np.concatenate([x16, x16])
    x16 = x16[:RATE16 * 12]
    x44 = resample_poly(x16.astype(np.float32), RATE44, RATE16).astype(np.int16)

    far_whole = resample_poly(x44.astype(np.float32), UP, DOWN).astype(np.int16)
    D, g = 3765, 0.35
    rng = np.random.default_rng(7)
    n = len(far_whole)
    near = (g * np.concatenate([np.zeros(D, np.float32), far_whole.astype(np.float32)])[:n]
            + rng.standard_normal(n) * 80).astype(np.int16)

    base = erle(near, far_whole)
    print("=" * 74)
    print("消费侧重采样 vs 生产侧（近端块固定 1600 = 100ms@16k）")
    print("=" * 74)
    print(f"{'方案':<40} {'长度':>8} {'漂移':>6} {'ERLE':>9} {'Δ':>8}")
    print("-" * 74)
    print(f"{'A. 整段（基准，不可实现）':<40} {len(far_whole):>8} {'—':>6} {base:>8.2f}dB {'—':>8}")

    for lbl, mg in [("C1. 消费侧，无余量", 0),
                    ("C2. 消费侧，50ms 余量丢边界", int(0.050 * RATE44)),
                    ("C3. 消费侧，100ms 余量丢边界", int(0.100 * RATE44))]:
        fc = far_by_consumer_side(x44, near, chunk16=1600, margin44=mg)
        e = erle(near, fc)
        print(f"{lbl:<40} {len(fc):>8} {len(fc)-len(near):>+6} {e:>8.2f}dB {e-base:>+7.2f}dB")

    print()
    print("非整除块长（1600 的边角，验证分数推进不漂）：")
    for ck in (1584, 1552, 1280):     # 99/97/80 ms
        fc = far_by_consumer_side(x44, near, chunk16=ck, margin44=int(0.05*RATE44))
        e = erle(near, fc)
        print(f"{'  块='+str(ck)+f' ({ck/16:.1f}ms)':<40} {len(fc):>8} "
              f"{len(fc)-len(near):>+6} {e:>8.2f}dB {e-base:>+7.2f}dB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
