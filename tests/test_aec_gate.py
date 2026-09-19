#!/usr/bin/env python3
"""WO-AEC-01 §2.1 / §2.2 的验收断言 + 2026-09-19 真机 bug 的回归测试。

不接主链路、不出声（用 `Player._cb` 直接驱动，等价于回调干活但不碰声卡）。

⚠️ 回归测试必测：**空闲不推进 far 镜像**。原实现里 far 只在 `write()` 时增长，
而读指针按"回调已输出帧数"定位 —— 两个时钟。空闲 15 秒后读指针远超镜像数据量，
`far_slice` 永远返回空 → AEC 参考全零 → 扬声器声音原样进麦克风（真机踩到）。
"""
import os
import subprocess
import sys
import textwrap

REPO = "/Users/owen/jarvis-voice"
FAIL = []


def check(label, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {label}: got={got} want={want}")
    if not ok:
        FAIL.append(label)


print("=" * 72)
print("§2.1 config.py —— 三种模式的派生属性（P-1 的验收）")
print("=" * 72)
CODE = textwrap.dedent("""
    import sys; sys.path.insert(0, %r)
    from jarvis_voice.config import Config
    c = Config.load()
    print(f"{c.barge_in}|{c.half_duplex}|{getattr(c, 'aec', 'MISSING')}")
""" % REPO)

for mode, want in [("headphones", "True|False|False"),
                   ("speaker", "False|True|False"),
                   ("speaker_aec", "True|False|True")]:
    env = dict(os.environ, JARVIS_AUDIO_MODE=mode)
    r = subprocess.run([f"{REPO}/.venv/bin/python", "-c", CODE],
                       capture_output=True, text=True, env=env, cwd=REPO)
    got = (r.stdout.strip().splitlines() or ["<空>"])[-1]
    check(f"JARVIS_AUDIO_MODE={mode}", got, want)
    if r.returncode != 0:
        print("    stderr:", r.stderr.strip()[:300])

print()
print("=" * 72)
print("§2.2 player.py —— far 镜像（走真的 _cb，不是直接调内部方法）")
print("=" * 72)
sys.path.insert(0, REPO)
from jarvis_voice.player import Player            # noqa: E402
from jarvis_voice.tts.base import AudioFormat     # noqa: E402
import numpy as np                                # noqa: E402

BLK = 1024


def spin(p, blocks):
    """驱动回调 blocks 次（等价于扬声器跑了这么多 block）。返回最后一次的 outdata。"""
    for _ in range(blocks):
        out = np.zeros((BLK, 1), dtype=np.int16)
        p._cb(out, BLK, None, None)
    return out

# ---- 0) 回归：空闲不推进镜像，且读指针不会跑到镜像数据之外 ----
p = Player(AudioFormat(sample_rate=44100, channels=1))
spin(p, 646)                                     # ≈15s 空闲（模拟切模式前的静置）
idle_frames = p.emit_frames()
check("空闲 15s 后 emit_frames 已推进", idle_frames > 600 * BLK, True)

p.write(np.zeros(4410, dtype=np.int16).tobytes())   # 现在 TTS 说话
spin(p, 5)
read = max(0, p.emit_frames() - int(0.150 * 44100))
got = p.far_slice(read, 4410)
check("★回归：空闲后 far_slice 仍拿得到 4410 帧（原实现返回 0）", len(got), 4410)

# ---- 1) 镜像 = 实际送出去的 PCM（含静音）----
p2 = Player(AudioFormat(sample_rate=44100, channels=1))
tone = (np.sin(np.arange(4410) * 0.05) * 20000).astype(np.int16)
p2.write(tone.tobytes())
spin(p2, 5)
n = p2.emit_frames()
have0 = p2.far_slice(0, 4410)          # tone 在这一轮的帧区间 [0, 4410)
check("刚播出的那一块能原样取回（镜像 = 扬声器实际输出）",
      len(have0) == 4410 and bool(np.array_equal(np.asarray(have0), tone)), True)
check("播完之后补的是静音（镜像含静音，不是空档）",
      bool(np.array_equal(np.asarray(p2.far_slice(4410, 500)), np.zeros(500, np.int16))), True)

# ---- 2) flush() 不清镜像（已经播出去的仍在），也不制造"未来" ----
p2.flush()
check("flush() 后 emit_frames 不回退", p2.emit_frames() >= n, True)
check("flush() 后仍能取到已播部分", len(p2.far_slice(n - 4410, 4410)), 4410)

# ---- 3) 环形滚动：最后 200ms 与朴素拼接一致 ----
cap = int(44100 * 3.0)
p3 = Player(AudioFormat(sample_rate=44100, channels=1))
seq = (np.arange(cap * 2, dtype=np.int64) % 30000).astype(np.int16)
for i in range(0, len(seq), 4410):
    p3.write(seq[i:i + 4410].tobytes())
    p3._cb(np.zeros((4410, 1), dtype=np.int16), 4410, None, None)
want = seq[len(seq) - 8820:]
have = p3.far_slice(p3.emit_frames() - 8820, 8820)
check("环滚动 2 圈后最后 200ms 与朴素拼接一致",
      len(have) == len(want) and bool(np.array_equal(np.asarray(have), want)), True)

# ---- 4) 已被覆盖的过去 → 返回空 ----
check("读已被覆盖的起点 → 返回空", len(p3.far_slice(0, 8820)), 0)

print()
print("=" * 72)
print("集成：AecGate × 真 Player —— far 参考到底喂进去没有")
print("=" * 72)
from jarvis_voice.aec import AecGate          # noqa: E402
from jarvis_voice.config import Config        # noqa: E402
import wave                                   # noqa: E402
from scipy.signal import resample_poly        # noqa: E402

SENSE = f"{REPO}/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/test_wavs/zh.wav"

# ---- 回归 2：accept 被连续快调（主循环赶工 / 麦克风积压排空）----
# 旧实现自由推进 `_far_read += need44`，没有播放跟随时读指针会跑到播放位置之前
# → far_slice 长期返回空。锚定到 emit_frames 之后不该再发生。
# ⚠️ 断言必须看 **far 本身**，不能看 accept 的返回长度 ——
#    返回长度恒等于近端块长（far 空了会被补零），那样旧实现也能"通过"。
p5 = Player(AudioFormat(sample_rate=44100, channels=1))
spin(p5, 200)
reads5 = []
_r5 = p5.far_slice


def _spy5(a, n):
    r = _r5(a, n)
    reads5.append(len(r))
    return r


p5.far_slice = _spy5
g5 = AecGate(Config(), p5)
for _ in range(20):
    g5.accept(np.zeros(1600, np.float32))
check("★回归：连续 20 次快调 accept，far 每块都给满（旧实现后期变 0）",
      reads5[-1] == 4410 and min(reads5) == 4410, True)

# ---- 边播边说：far 参考里应当有真实音频 ----
p4 = Player(AudioFormat(sample_rate=44100, channels=1))
with wave.open(SENSE, "rb") as w:
    src16 = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
src44 = resample_poly(src16.astype(np.float32), 44100, 16000).astype(np.int16)

reads = []
_real = p4.far_slice


def _spy(a, n):
    r = _real(a, n)
    reads.append(r)
    return r


p4.far_slice = _spy
gate = AecGate(Config(), p4)
spin(p4, 200)                                  # 静置（真机场景）
n_before = len(reads)
p4.write(src44.tobytes())
for _ in range(len(src44) // 4410):
    p4._cb(np.zeros((1024, 1), np.int16), 1024, None, None)
    gate.accept(np.zeros(1600, np.float32))

after = [r for r in reads[n_before:] if len(r) > 0]
check("播放期间 far_slice 每次都给满 4410 帧（无空窗）",
      all(len(r) == 4410 for r in after) and len(after) > 20, True)
amp = float(np.abs(np.concatenate(after)).mean())
check(f"far 参考里是真实音频不是静音（平均幅度 {amp:.0f} > 100）", amp > 100, True)

for q in (p, p2, p3, p4, p5):
    q.stop()

print()
print("=" * 72)
if FAIL:
    print(f"❌ 失败 {len(FAIL)} 项: {FAIL}")
    sys.exit(1)
print("✅ 全部通过")
