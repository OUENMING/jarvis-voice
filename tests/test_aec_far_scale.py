#!/usr/bin/env python3
"""回归：**喂给 AEC 的 far 参考必须与原始音频同尺度，不能饱和**。

**真机 bug（2026-09-20 16:1x 抓到）**：`~/.jarvis/bargein-audio/*-far.wav` 显示
far 参考 **RMS 32750 / 32767，54% 的样本贴在 ±32767** —— 那不是音频，是**方波**。

根因在 `AecGate.accept()`：

    far44 = player.far_slice(...)                      # int16，±32767
    far16 = resample_poly(far44.astype(np.float32))    # ← 漏了 / 32768，仍是 ±32767
    far_i16 = np.clip(far16 * INT16, ...)              # ← 再乘 32768 → 全部截到满幅

后果链条（就是用户报的「自己打断自己」）：

    AEC 拿到垃圾参考 → 回声一点没消 → 麦克风里全是它自己的声音
    → VAD 起音 → 判定为用户插话 → 打断自己 → 再播 → 再被自己打断 …

⚠️ **这不是新 bug**：`git show HEAD:jarvis_voice/aec.py` 里就有 ——
**生产的 AEC 从上线起就没拿到过可用的 far 参考。** 之前的 34–36 dB ERLE
全是 `tests/aec_probe.py` 自己造参考测的，**没走过这条路径**。

⚠️ **老测试为什么没抓到**：`test_aec_gate.py` 只断言到 `player.far_slice()`
（原始镜像，尺度本来就是对的），**从没看 `accept()` 内部真正交给 AEC 的那份**。
本测试补的就是这个缝：用**间谍后端**接住 `process()` 收到的 far。

⚠️ 离线，不开任何音频设备（直接调 `Player._cb`）。
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis_voice.aec import AecGate                          # noqa: E402
from jarvis_voice.config import Config                        # noqa: E402
from jarvis_voice.player import Player                        # noqa: E402
from jarvis_voice.tts.base import AudioFormat                 # noqa: E402

BLK = 4410          # ⚠️ 必须与 `accept()` 每次消耗的 far 帧数一致。
# 为什么：`accept(1600@16k)` 每次读 **4410** 个 44.1k far 帧。若 `_cb` 每次只推进
# 1024 帧，读指针就比镜像快 4.3 倍 → 读到的是**互相重叠的错位窗** → 与源相关性崩掉。
# （`test_aec_gate.py` 里那个集成测试用的就是 1024，所以它只能查长度和幅度，
#   查不了"far 到底是不是那段音频" —— 本测试补的正是这一点。）
FAIL = []


def check(label, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {label}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(label)


class SpyBackend:
    """接住 `accept()` 真正交给 AEC 的 (near, far)。本测试的核心观测点。"""

    name = "spy"

    def __init__(self):
        self.seen = []

    def process(self, near_i16, far_i16):
        self.seen.append((near_i16.copy(), far_i16.copy()))
        return near_i16

    def reset(self):
        pass

    @property
    def speech_probability(self):
        return 0.0


def rms_db(x):
    return 10 * np.log10(max(float(np.mean(np.asarray(x, np.float64) ** 2)), 1e-12))


def build(src16, blocks=None):
    """建真 Player + 真 AecGate，只把后端换成间谍。返回 (gate, spy, src44)。

    ⚠️ `block_frames=BLK=4410` —— 让扬声器每 `_cb` 推进的帧数**等于** `accept()`
    每次消耗的 far 帧数，两者同步。不同步的话 far 读指针会跑到错位的地方。
    """
    p = Player(AudioFormat(sample_rate=44100, channels=1), block_frames=BLK)
    for _ in range(200):                       # 静置，模拟真机场景
        p._cb(np.zeros((BLK, 1), np.int16), BLK, None, None)
    from scipy.signal import resample_poly
    src44 = resample_poly(src16.astype(np.float32) / 32768.0, 44100, 16000)
    src44 = np.clip(src44 * 32768, -32768, 32767).astype(np.int16)
    p.write(src44.tobytes())
    g = AecGate(Config(), p)
    spy = SpyBackend()
    g.be = spy
    n = blocks if blocks is not None else len(src44) // BLK
    for _ in range(n):
        p._cb(np.zeros((BLK, 1), np.int16), BLK, None, None)
        g.accept(np.zeros(1600, np.float32))
    return g, spy, src44


SENSE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models",
    "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17", "test_wavs", "zh.wav")

print("=== ① 喂给 AEC 的 far 必须与源音频同尺度（±3 dB 内）===")
import wave                                                    # noqa: E402
with wave.open(SENSE, "rb") as w:
    src16 = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
g, spy, src44 = build(src16)
check("间谍确实收到了东西（否则下面全是空转）", len(spy.seen) > 10, True)
far_all = np.concatenate([f for _, f in spy.seen])
near_all = np.concatenate([n for n, _ in spy.seen])
src_db, far_db = rms_db(src44), rms_db(far_all)
print(f"    源音频 RMS {src_db:.1f} dB | 喂给 AEC 的 far RMS {far_db:.1f} dB")
check(f"far 电平与源同档（差 {abs(src_db - far_db):.1f} dB < 3）",
      abs(src_db - far_db) < 3.0, True)

print("\n=== ② 绝对不能饱和 —— 这正是真机那份「方波」的判据 ===")
sat = float(np.mean(np.abs(far_all) >= 32700)) * 100
print(f"    |far| ≥ 32700 的样本占比 = {sat:.1f}%  (真机坏样本是 54%)")
check("far 不饱和（贴满幅 < 1%）", sat < 1.0, True)
check("far 峰值不是恒定的满幅", int(np.abs(far_all).max()) < 32767, True)

print("\n=== ③ far 必须真的是**那段**音频（尺度对了但内容错也不行）===")
# ⚠️ 不能直接对齐比：① far 是 16k，源是 44.1k ② far 读的是 `emit - 150ms`（物理对齐）。
#    所以必须**先降到同一采样率，再搜滞后**。直接比会得到相关系数 ≈0（我第一版就是）。
from scipy.signal import resample_poly                            # noqa: E402
src16 = resample_poly(src44.astype(np.float32), 160, 441).astype(np.int16)


def best_cc(a, b, max_lag):
    """10ms 包络上搜滞后，返回 (最大相关, 滞后样本数)。"""
    n = min(len(a), len(b))
    w = 160
    k = n // w
    ea = np.sqrt((a[:k * w].astype(np.float64) ** 2).reshape(k, w).mean(1) + 1e-9)
    eb = np.sqrt((b[:k * w].astype(np.float64) ** 2).reshape(k, w).mean(1) + 1e-9)
    ea, eb = ea - ea.mean(), eb - eb.mean()
    mk = max(1, max_lag // w)
    c = np.correlate(ea, eb, "full")[k - 1 - mk: k + mk]
    i = int(np.argmax(c)) - mk
    return float(c[np.argmax(c)] / (np.linalg.norm(ea) * np.linalg.norm(eb) + 1e-12)), i * w


cc, lag = best_cc(far_all, src16, int(0.3 * 16000))
print(f"    与源音频最佳相关 = {cc:.3f}（滞后 {lag / 16:.0f} ms，"
      f"预期 ≈ -150ms 的 far 读偏移 + 缓冲）")
check("far 与源高度相关（> 0.9）", cc > 0.9, True)

print("\n=== ④ 近端路径本来是对的（对照，确认不是两边一起错）===")
check("near 是静音 → RMS 极低", rms_db(near_all) < -60, True)

print("\n=== ⑤ 静音期 far 就该是静音（别把「补零」误判成 bug）===")
p2 = Player(AudioFormat(sample_rate=44100, channels=1))
for _ in range(200):
    p2._cb(np.zeros((BLK, 1), np.int16), BLK, None, None)
g2 = AecGate(Config(), p2)
spy2 = SpyBackend()
g2.be = spy2
for _ in range(5):
    p2._cb(np.zeros((BLK, 1), np.int16), BLK, None, None)
    g2.accept(np.zeros(1600, np.float32))
far2 = np.concatenate([f for _, f in spy2.seen])
check("没播东西时 far 全是 0（真机那 10 个 −120 dB 的样本是正常的）",
      int(np.abs(far2).max()), 0)

print()
if FAIL:
    print(f"❌ {len(FAIL)} 项失败：{FAIL}")
    sys.exit(1)
print("✅ 全部通过")
