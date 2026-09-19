#!/usr/bin/env python3
"""填充音验证：预渲染 → 延迟触发 → 被真句子剪切。

  .venv/bin/python test_filler.py

验证点：
  ① 预渲染后 ready() 为真，且**不重复渲染**（第二次跑应命中缓存，快很多）
  ② 触发后播放器确实收到了 tag="filler" 的音频
  ③ 真句子到达时，未播的填充音被**切掉**（不会把真内容往后推）
  ④ 首句先到（未超时）时，填充音**不播**
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import time

from jarvis_voice.config import Config
from jarvis_voice.orchestrator import Orchestrator
from jarvis_voice.session import State

cfg = Config.load()
orch = Orchestrator(cfg, verbose=True)
fails = []

# ---- ① 预渲染 ----
t0 = time.perf_counter()
orch.fillers.ensure(log=print)
first_ms = (time.perf_counter() - t0) * 1000
print(f"\n① 预渲染 {first_ms:.0f}ms | ready={orch.fillers.ready()} | {len(orch.fillers.clips)} 条")
if not orch.fillers.ready():
    fails.append("预渲染失败，无可用片段")
t0 = time.perf_counter()
orch.fillers.ensure(log=lambda *a: None)
print(f"   二次调用（应命中缓存） {(time.perf_counter()-t0)*1000:.0f}ms")

orch.player.start()

# ---- ② 延迟触发 ----
print(f"\n② 延迟触发（delay={cfg.filler_delay_ms}ms）")
turn = orch.session.begin_turn()
orch.session.set_state(State.THINKING)
orch._arm_filler(turn)
time.sleep(0.15)
mid = orch.player.buffered_seconds()
time.sleep(cfg.filler_delay_ms / 1000.0 + 0.4)
after = orch.player.buffered_seconds()
print(f"   触发前缓冲 {mid:.2f}s → 触发后 {after:.2f}s")
if after <= mid or after <= 0:
    fails.append("填充音未触发（播放器没收到音频）")

# ---- ③ 被真句子剪切 ----
print("\n③ 真句子到达 → 切掉未播填充音")
orch._cancel_filler()
cut = orch.player.cut_tag("filler")
left = orch.player.buffered_seconds()
print(f"   切掉 {cut:.2f}s | 剩余缓冲 {left:.2f}s（应 ≈0）")
if cut <= 0:
    fails.append("cut_tag 没切到东西")

# ---- ④ 首句先到则不播 ----
print("\n④ 首句先到（不等满 delay）→ 不应播填充音")
orch.player.flush()
turn2 = orch.session.begin_turn()
orch.session.set_state(State.THINKING)
orch._arm_filler(turn2)
time.sleep(0.2)
orch._cancel_filler()            # 模拟首句到达
time.sleep(cfg.filler_delay_ms / 1000.0 + 0.3)
buf = orch.player.buffered_seconds()
print(f"   缓冲 {buf:.2f}s（应为 0）")
if buf > 0.05:
    fails.append("首句先到时仍播了填充音")

orch.player.stop()
print("\n" + "=" * 50)
print("❌ " + "; ".join(fails) if fails else "✅ 填充音全部通过")
