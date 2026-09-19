#!/usr/bin/env python3
"""WO-AEC-01 §2.1 / §2.2 的验收断言。ZCode 改完后立刻跑这个判通过与否。

不接主链路、不出声（Player 只建流不 start，等于静音输出）。
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


print("=" * 70)
print("§2.1 config.py —— 三种模式的派生属性（P-1 的验收）")
print("=" * 70)
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
print("=" * 70)
print("§2.2 player.py —— far 镜像（P-2 的验收）")
print("=" * 70)
sys.path.insert(0, REPO)
try:
    from jarvis_voice.player import Player
    from jarvis_voice.tts.base import AudioFormat
except Exception as e:
    print(f"  ❌ 导入失败: {e}")
    sys.exit(1)

import numpy as np  # noqa: E402

p = Player(AudioFormat(sample_rate=44100, channels=1))

# 1) 写进去、但一帧都没播 → far_slice 不许读到"未来"（这是 P-2 的正例）
p.write((np.arange(44100, dtype=np.int16)).tobytes(), tag="t")
emitted = p.emit_frames()
try:
    got = p.far_slice(0, 44100)
    n_future = len(got)
except Exception as e:
    n_future, got = -1, f"<异常 {e}>"
check("一帧未播时 far_slice(0, 44100) 返回空（不读未来）", n_future, 0)

# 1b) 模拟"全播出去了" → 同样的调用现在必须读得到
p._emitted = 44100                      # 直接注入，避免依赖真实音频时序
check("注入 emit=44100 后同一次调用读满 44100",
      len(p.far_slice(0, 44100)), 44100)
# 1c) 越过 emit 边界只拿到已播部分
p._emitted = 20000
check("emit=20000 时 far_slice(0, 44100) 只给 20000（硬边界）",
      len(p.far_slice(0, 44100)), 20000)

# 2) flush() 之后仍未播放 → 依然不许读到"未来"（P-2）
p._emitted = 0
p.write((np.arange(44100, dtype=np.int16)).tobytes(), tag="t")
p.flush()
try:
    n_after = len(p.far_slice(p.emit_frames(), 1000))
except Exception as e:
    n_after = f"<异常 {e}>"
check("flush() 后仍读不到未来（P-2）", n_after, 0)

# 3) 环缓冲滚动后数据正确（用可预测序列）
p2 = Player(AudioFormat(sample_rate=44100, channels=1))
cap = int(44100 * 3.0)
seq = (np.arange(cap * 3, dtype=np.int64) % 30000).astype(np.int16)
for i in range(0, len(seq), 4410):                # 分块写，模拟 TTS
    p2.write(seq[i:i + 4410].tobytes(), tag="t")
p2._emitted = len(seq)
try:
    tail = p2.far_slice(len(seq) - 8820, 8820)     # 取最后 200ms
    want = seq[len(seq) - 8820:len(seq)]
    ok = len(tail) == len(want) and bool(np.array_equal(np.asarray(tail), want))
    if not ok:
        print(f"     len(tail)={len(tail)} want={len(want)}")
except Exception as e:
    ok, tail = False, f"<异常 {e}>"
check("环滚动 3 圈后最后 200ms 与朴素拼接一致", ok, True)

# 3b) 已被环覆盖的过去 → 只能拿到还留着的那部分
got_old = p2.far_slice(0, 8820)
check("读已被覆盖的起点 → 返回空（不读已滚掉的过去）", len(got_old), 0)

p.stop()
p2.stop()

print()
print("=" * 70)
if FAIL:
    print(f"❌ 失败 {len(FAIL)} 项: {FAIL}")
    sys.exit(1)
print("✅ 全部通过")
