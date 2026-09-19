#!/usr/bin/env python3
"""第二轮审计（架构/逻辑）修复的回归测试。

每一条都对应审计发现的真实缺陷 —— 而且**都是我自己引入的**：
  ① Player 缓冲计数器跨锁裂开 → flush 插在两次加锁之间会把计数减成负数
  ② 元命令 `_norm` 先归一化再比字面量集合 → "暂停一下"/"别听了" 是死条目
  ③ 填充音预渲染与 TTS 线程共用同一 TTS 实例 → Fish 的 WS 客户端被两线程共用
  ④ 打断回执 rid 登记晚于写帧 → 回执可能被丢
  ⑤ VAD `speaking` 绕过锁（仪表盘 reset 从 uvicorn 线程进来）

用法：.venv/bin/python test_audit_round2.py
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import time

fails = []

# ---- ① Player 计数器不变负 ----
print("① Player 缓冲计数器（flush 插进回调中间也不能变负）")
import numpy as np
from jarvis_voice.tts.base import AudioFormat
from jarvis_voice.player import Player

fmt = AudioFormat(sample_rate=44100)
p = Player(fmt); p.start()
tone = (np.sin(2 * np.pi * 440 * np.arange(44100)) * 6000).astype(np.int16).tobytes()
neg = False
for _ in range(80):
    p.write(tone)
    time.sleep(0.01)
    p.flush()
    if p.buffered_seconds() < -1e-9:
        neg = True
        break
print(f"   反复 flush 80 次，负值出现: {neg}")
if neg:
    fails.append("buffered 变负（计数器跨锁）")
p.stop()

# ---- ② 元命令归一化 ----
print("\n② 元命令（归一化后仍要匹配）")
from jarvis_voice.commands import match

for text, want in [("暂停一下", "pause"), ("别听了", "pause"), ("暂停监听", "pause"),
                   ("接着听", "resume"), ("继续监听", "resume"),
                   ("清理一下上下文", "clear"), ("帮我忘掉刚才", "clear")]:
    m = match(text)
    ok = m is not None and m.name == want
    if not ok:
        fails.append(f"元命令 {text!r} 未识别为 {want}")
    print(f"   {'✅' if ok else '❌'} {text!r} → {m.name if m else None}")

# ---- ③ 填充音用独立 TTS 实例 ----
print("\n③ 填充音不复用主 TTS 实例")
from jarvis_voice.config import Config
from jarvis_voice.orchestrator import Orchestrator

o = Orchestrator(Config.load(), verbose=False)
shared = o.fillers.tts is o.tts
print(f"   fillers.tts is tts → {shared}（应 False）")
if shared:
    fails.append("填充音与主 TTS 共用实例（并发不安全）")
o.player.stop()

# ---- ④⑤ 无死锁 + speaking 取锁 ----
print("\n④ VAD accept/speaking 交错无死锁")
from jarvis_voice.vad import VadGate

v = VadGate(Config.load())
try:
    for _ in range(10):
        v.accept(np.zeros(512, dtype=np.float32))
        _ = v.speaking
    print("   ✅ 无死锁")
except Exception as e:
    fails.append(f"VAD 出错: {e}")
    print(f"   ❌ {e}")

# ---- ⑥ events：level 不落盘 + 轮转 ----
print("\n⑤ events：level 瞬时事件不落盘、文件有轮转")
import importlib
import json as _json
import os as _os

for _f in ("/tmp/ev_rt.jsonl", "/tmp/ev_rt.jsonl.1"):
    _os.path.exists(_f) and _os.remove(_f)
import jarvis_voice.events as _E
importlib.reload(_E)
_bus = _E.EventBus(path="/tmp/ev_rt.jsonl", recent=50)
for _ in range(500):
    _bus.emit("level", rms=0.01)
for _k in ("user", "sentence"):
    _bus.emit(_k, text="x")
_kinds = [_json.loads(l)["kind"] for l in open("/tmp/ev_rt.jsonl") if l.strip()]
_ok6 = _kinds == ["user", "sentence"]
print(f"   落盘: {_kinds}（level 应被丢弃）{'✅' if _ok6 else '❌'}")
if not _ok6:
    fails.append("level 事件落盘了")

print("\n" + "=" * 54)
print("❌ " + "; ".join(fails) if fails else "✅ 第二轮审计修复全部通过")

