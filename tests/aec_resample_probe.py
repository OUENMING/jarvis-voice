#!/usr/bin/env python3
"""U-1 测量：分段 resample_poly 会不会毁掉 far 参考，进而拉低 ERLE？

背景（WO-AEC-01 §2.3）：
  实测探针里 far 是**整段一次性**降采样的；真实管线里 far 来自
  `Player.write()`，是**一块一块**进来的（TTS 块大小不定）。
  若分段重采样在块边界产生阶跃，AEC 的参考信号就脏了。

设计（确定性，不需要房间）：
  1. 造一个 44.1k 的近语音信号 far44（把 16k 的 zh.wav 升到 44.1k）
  2. 造 near16 = far_whole16 延迟 D + 增益 g + 噪声   ← 模拟声学回声路径
  3. far 参考的两个版本：
       A. far_whole = resample_poly(far44, 160, 441)   ← 探针用的（正确）
       B. far_chunk = 按块 resample_poly 后拼接         ← 真实管线的做法
  4. 同一个 near，分别喂 A / B，比稳态 ERLE

  额外测：块大小**随机**（模拟 TTS 真实块）时的样本数漂移。

判据（WO-AEC-01 §2.3）：差值 >3 dB 就必须改设计（保留重叠 + 丢边界）。
"""
import sys
import numpy as np
from scipy.signal import resample_poly

sys.path.insert(0, "/Users/owen/jarvis-voice")
from pywebrtc_audio import EchoCanceller  # noqa: E402

UP, DOWN = 160, 441          # 44.1k → 16k
RATE44, RATE16 = 44100, 16000
SENSE = "/Users/owen/jarvis-voice/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"


def load16(path):
    import wave
    with wave.open(path, "rb") as w:
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        if w.getnchannels() > 1:
            x = x.reshape(-1, w.getnchannels()).mean(axis=1).astype(np.int16)
        if w.getframerate() != RATE16:
            x = resample_poly(x.astype(np.float32), RATE16, w.getframerate()).astype(np.int16)
    return x


def to44(x16):
    """16k → 44.1k（制造一个真实的 44.1k 源）"""
    return resample_poly(x16.astype(np.float32), RATE44, RATE16).astype(np.int16)


def rms(x):
    x = x.astype(np.float64)
    return float(np.sqrt(np.mean(x * x))) if x.size else 0.0


def resample_whole(x44):
    """A：整段一次性（探针做法）"""
    return resample_poly(x44.astype(np.float32), UP, DOWN).astype(np.int16)


def resample_chunked(x44, sizes):
    """B：按块重采样后拼接（真实管线做法）。sizes = 每块样本数（44.1k 域）"""
    out, i = [], 0
    while i < len(x44):
        n = sizes[len(out) % len(sizes)]
        blk = x44[i:i + n]
        if len(blk) < 2:
            break
        out.append(resample_poly(blk.astype(np.float32), UP, DOWN))
        i += n
    return np.concatenate(out).astype(np.int16) if out else np.zeros(0, np.int16)


def erle_steady(near, ref16, skip_s=1.5):
    """喂同一个 near，用 ref16 当 far，返回稳态 ERLE（跳过收敛期）。"""
    ec = EchoCanceller(sample_rate=RATE16)
    y = ec.process(near, ref16[:len(near)])
    s = int(skip_s * RATE16)
    return 20 * np.log10(max(rms(near[s:]), 1e-12) / max(rms(y[s:]), 1e-12)), y


def main():
    x16 = load16(f"{SENSE}/test_wavs/zh.wav")
    # 拼到 ~12 秒，避免太短
    while len(x16) < RATE16 * 12:
        x16 = np.concatenate([x16, x16])
    x16 = x16[:RATE16 * 12]
    x44 = to44(x16)

    print("=" * 74)
    print(f"far 源：{len(x44)} samples @44.1k = {len(x44)/RATE44:.2f}s")
    print("=" * 74)

    # ---- 造 near：far 的延迟+衰减+噪声版本（模拟声学回声）----
    far_whole = resample_whole(x44)
    D = 3765                      # 实测探针测到的真实延迟
    g = 0.35
    rng = np.random.default_rng(7)
    n = len(far_whole)
    near = (g * np.concatenate([np.zeros(D, np.float32), far_whole.astype(np.float32)])[:n]
            + (rng.standard_normal(n) * 80)).astype(np.int16)

    print(f"near 构造：far_whole 延迟 {D} 样本 × {g} + 白噪声(σ=80)")
    print(f"  far RMS={rms(far_whole):.1f}  near RMS={rms(near):.1f}\n")

    # ---- 块大小方案 ----
    schemes = [
        ("固定 10ms  (441)", [441]),
        ("固定 100ms (4410)", [4410]),
        ("固定 23.2ms(1024帧播放块)", [1024]),
        ("随机 20–200ms（模拟 TTS）", None),
    ]

    print(f"{'参考版本':<34} {'长度':>8} {'漂移':>7} {'稳态ERLE':>10}")
    print("-" * 74)

    base_erle, _ = erle_steady(near, far_whole)
    print(f"{'A. 整段（探针做法）':<34} {len(far_whole):>8} {'—':>7} {base_erle:>9.2f}dB")

    for label, sizes in schemes:
        if sizes is None:
            sizes = list(rng.integers(882, 8820, size=64))   # 20–200ms @44.1k
        fc = resample_chunked(x44, sizes)
        drift = len(fc) - len(far_whole)
        # ⚠️⚠️ **两侧必须裁到同一个时间片**（ocr 2026-09-20 报的）。
        # 原写法两个分支给 `near_i` 赋的都是 `near`，等于原始意图（长度不齐时同步裁剪）
        # **根本没生效**；而 `fc` 比 `near` 短时，AEC 后半段没有参考信号
        # → ERLE 被**系统性拉低** → 这个探针报出来的数字不可信。
        # （这个探针是 `docs/PROBE-AEC-RESULTS-20260919.md` 里「重采样必须在消费侧、
        #   生产侧掉 13.15 dB」那条结论的来源，所以对齐不是小事。）
        n = min(len(near), len(fc))
        near_i = near[:n]
        fc_i = fc[:n]
        e, _ = erle_steady(near_i, fc_i)
        print(f"B. 分段 {label:<24} {len(fc):>8} {drift:>+7} {e:>9.2f}dB"
              f"   Δ={e-base_erle:+.2f}dB")

    print()
    print("=" * 74)
    print("参考信号本身的失真（不跑 AEC，纯看 far 差多少）")
    print("=" * 74)
    for label, sizes in schemes:
        if sizes is None:
            sizes = list(rng.integers(882, 8820, size=64))
        fc = resample_chunked(x44, sizes)
        L = min(len(fc), len(far_whole))
        d = fc[:L].astype(np.float64) - far_whole[:L].astype(np.float64)
        ratio = rms(d) / max(rms(far_whole[:L]), 1e-12)
        print(f"  {label:<34} 误差RMS/参考RMS = {ratio:8.4%}  "
              f"({20*np.log10(max(ratio,1e-12)):6.1f} dB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
