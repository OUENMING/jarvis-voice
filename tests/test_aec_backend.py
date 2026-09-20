#!/usr/bin/env python3
"""AEC 后端可切：`webrtc`（生产默认）vs `speex`（线性、不压近端）。

**背景**：真机定案「打断时识别差 = WebRTC AEC3 双讲时削掉近端高频」。
离线 20 个合成双讲场景（660 字）比转写错误率：speex 线性 **8.0%** vs webrtc **39.8%**。
但合成≠真机 ⇒ 需要能在**真机上**切换后端 A/B 一次。
本测试守的是「这个切换开关本身不会坏事」。

⚠️ 离线，不开任何音频设备（fake player + fake/真后端，不建音频流）。
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis_voice.aec import AecGate, _make_backend, _SpeexBackend   # noqa: E402
from jarvis_voice.config import Config                               # noqa: E402

FAIL = []


def check(label, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {label}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(label)


class FakePlayer:
    def __init__(self, n=44100 * 5):
        self._e = 0
        self._far = ((np.arange(n) % 20000) + 1).astype(np.int16)

    def emit_frames(self):
        return self._e

    def far_slice(self, s, n):
        s, e = max(0, int(s)), min(self._far.shape[0], max(0, int(s)) + int(n))
        return np.zeros(0, np.int16) if s >= self._far.shape[0] else self._far[s:e].copy()

    def advance(self, f):
        self._e += f


print("=== ① 后端名写错必须**响亮**报错（不能静默退回 webrtc）===")
print("   静默退回是最坏的一种：你以为在测 speex，其实还在跑 webrtc")
try:
    _make_backend(Config(aec_backend="nope"))
    check("抛 ValueError", False, True)
except ValueError as e:
    check("ValueError 里列出可选值", "webrtc" in str(e) and "speex" in str(e), True)

print("\n=== ② 两个后端都跑通 `accept()` 全路径 ===")
for kind in ("webrtc", "speex"):
    cfg = Config(audio_mode="speaker_aec", aec_backend=kind)
    p = FakePlayer()
    g = AecGate(cfg, p)
    rng = np.random.default_rng(0)
    lens = []
    for _ in range(3):
        p.advance(4410)
        x = (rng.standard_normal(1600) * 0.05).astype(np.float32)
        lens.append(g.accept(x).shape[0])
    check(f"{kind}: 输出长度恒等于输入（不打乱下游时钟）", set(lens), {1600})
    check(f"{kind}: backend_name 如实上报", g.backend_name, kind)
    check(f"{kind}: raw_slice 有数据", g.raw_slice(1600).size, 1600)
    check(f"{kind}: far_slice 与 raw 等长", g.far_slice(1600).size, 1600)
    check(f"{kind}: speech_probability 是 float", isinstance(g.speech_probability, float), True)

print("\n=== ③ `reset()` 后两个 slice 必须报「还没数据」，不是「一片安静」===")
print("   返回一大段全零会被误读成'有数据只是安静' —— 误导比报错更糟")
p = FakePlayer()
g2 = AecGate(Config(audio_mode="speaker_aec"), p)
p.advance(4410)
g2.accept(np.zeros(1600, np.float32))
check("reset 前有数据", g2.far_slice(100).size, 100)
g2.reset()
check("reset 后 raw_slice 为空", g2.raw_slice(100).size, 0)
check("reset 后 far_slice 为空", g2.far_slice(100).size, 0)
check("reset 后 fed 归零（slice 的可用量判据）", g2.fed, 0)

print("\n=== ④ Speex 非整帧输入：余数透传 + 可观测（不静默出错）===")
sb = _SpeexBackend(16000, 3200)
rng = np.random.default_rng(1)
x = (rng.standard_normal(1650) * 3000).astype(np.int16)
y = sb.process(x, x.copy())
check("输出长度 == 输入长度", y.shape[0], 1650)
check("余数 1650-10*160=50 记进 unframed", sb.unframed, 50)
# 生产实际路径：mic_blocksize=1600 = 10 整帧 → 不该有透传
sb2 = _SpeexBackend(16000, 3200)
sb2.process(np.zeros(1600, np.int16), np.zeros(1600, np.int16))
check("整帧输入（生产实际块长）余数为 0", sb2.unframed, 0)

print("\n=== ⑤ `aec_backend` 能从环境变量覆盖（真机 A/B 就靠它）===")
os.environ["JARVIS_AEC_BACKEND"] = "speex"
try:
    check("JARVIS_AEC_BACKEND=speex 生效", Config.load().aec_backend, "speex")
    del os.environ["JARVIS_AEC_BACKEND"]
    check("默认值是 webrtc（不设就不改变现有行为）", Config.load().aec_backend, "webrtc")
finally:
    os.environ.pop("JARVIS_AEC_BACKEND", None)

print()
if FAIL:
    print(f"❌ {len(FAIL)} 项失败：{FAIL}")
    sys.exit(1)
print("✅ 全部通过")
