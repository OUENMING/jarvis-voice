#!/usr/bin/env python3
"""审计修复的回归测试。

覆盖审计发现的几条（每条都对应一个真实的失败场景）：
  ① `stop()` 在队列满时**不能挂死**（旧版用无超时 put → Ctrl+C 关不掉）
  ② `Session.finish_turn` 是**原子**的（旧版 is_current+set_state 两段式会把新轮踩成 IDLE）
  ③ VAD 的 `accept()` 与 `reset()` **可并发**（仪表盘"恢复监听"走 uvicorn 线程）
  ④ 筛选逻辑：'只看对话' 应隐藏工具卡

用法：.venv/bin/python test_audit_fixes.py
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import queue
import threading
import time

fails = []

# ---- ① stop() 不挂死 ----
print("① stop() 在队列满时不挂死")
from jarvis_voice.config import Config
from jarvis_voice.orchestrator import Orchestrator

orch = Orchestrator(Config.load(), verbose=False)
# 把两个队列灌满
filled = 0
for q, mx in ((orch.utt_q, 8), (orch.sent_q, 64)):
    while True:
        try:
            q.put_nowait(b"x"); filled += 1
        except queue.Full:
            break
print(f"   灌满队列（{filled} 项），调用 stop()…")
done = threading.Event()
t0 = time.perf_counter()


def call_stop():
    orch.stop()
    done.set()


threading.Thread(target=call_stop, daemon=True).start()
ok = done.wait(timeout=3.0)
dt = (time.perf_counter() - t0) * 1000
print(f"   stop() 返回={ok}  耗时 {dt:.0f}ms")
if not ok:
    fails.append("stop() 挂死（队列满时阻塞在 put）")
try:
    orch.player.stop()
except Exception:
    pass

# ---- ② finish_turn 原子性 ----
print("\n② Session.finish_turn 原子性")
from jarvis_voice.session import Session, State

s = Session()
t1 = s.begin_turn(); s.set_state(State.SPEAKING)
t2 = s.begin_turn(); s.set_state(State.SPEAKING)     # 新轮插入
r_old = s.finish_turn(t1)                            # 旧轮收尾 → 必须失败
r_new = s.finish_turn(t2)
print(f"   旧轮 finish_turn={r_old}（应 False）  新轮={r_new}（应 True）")
if r_old or not r_new:
    fails.append("finish_turn 非原子：旧轮把新轮状态踩掉了")

# ---- ③ VAD accept/reset 并发 ----
print("\n③ VAD accept() 与 reset() 并发安全")
import numpy as np
from jarvis_voice.vad import VadGate

v = VadGate(Config.load())
err = []


def hammer_accept():
    for _ in range(300):
        try:
            v.accept(np.zeros(512, dtype=np.float32))
        except Exception as e:
            err.append(f"accept: {type(e).__name__}: {e}")
            return


def hammer_reset():
    for _ in range(100):
        try:
            v.reset()
        except Exception as e:
            err.append(f"reset: {type(e).__name__}: {e}")
            return


ts = [threading.Thread(target=hammer_accept) for _ in range(2)] + \
     [threading.Thread(target=hammer_reset) for _ in range(2)]
for t in ts:
    t.start()
for t in ts:
    t.join(timeout=20)
print(f"   4 线程并发 300 accept / 100 reset，异常 {len(err)} 个")
if err:
    fails.append(f"VAD 并发出错: {err[0]}")

# ---- ④ 筛选逻辑 ----
print("\n④ 仪表盘筛选逻辑")
from jarvis_voice import dashboard

h = dashboard.PAGE
if "k==='tool'" not in h or "kind==='tool'" not in h:
    fails.append("筛选逻辑未修复")
print(f"   '只看对话' 分支为隐藏工具卡: {'k===\'tool\'' in h}")

print("\n" + "=" * 54)
print("❌ " + "; ".join(fails) if fails else "✅ 审计修复全部通过")
