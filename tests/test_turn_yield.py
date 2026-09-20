#!/usr/bin/env python3
"""P2「轮次协商」回归测试（见 docs/PLAN-HUMANNESS-20260920.md P2）。

**功能**：说满 N 句之后**主动停一下**，把插话的槽位让出来 —— 用户不必"抢"话，
而是被邀请接话。这是"人味"里**轮次协商**那一块（自然度调研指出我们缺的三件之一）。

**⚠️ 默认关闭，而且这是刻意的**：停顿是**净增加**的时间（3 句 × 600ms = +1.8s），
而 CUI'25 实测「延迟 >4s 是头号体验杀手」。**如果用户从不接话，这就是纯亏。**
计划里写死了：**先 A/B 量过再定值，甚至可能整体否掉。**

所以这个测试验的是**机制正确**（只在该停的时候停、只停一次、被打断就别停），
**不是**验"该不该开"——那个只能真机听。

⚠️ 离线：假播放器 + 假队列，不碰音频设备。
"""
import os
import queue
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis_voice import orchestrator as O              # noqa: E402
from jarvis_voice.config import Config                  # noqa: E402
from jarvis_voice.session import Session, State         # noqa: E402

import dataclasses                                       # noqa: E402

FAIL = []


def check(label, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {label}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(label)


class FakeBus:
    def __init__(self):
        self.events = []

    def emit(self, kind, **kw):
        self.events.append((kind, kw))


class FakePlayer:
    def __init__(self, playing=False):
        self.playing = playing
        self.flushed = 0

    def is_playing(self):
        return self.playing

    def flush(self):
        self.playing = False
        self.flushed += 1
        return 0.0


def make_orch(yield_ms, after=2, queued=0, playing=False):
    o = object.__new__(O.Orchestrator)
    o.cfg = dataclasses.replace(Config.load(),
                                turn_yield_ms=yield_ms,
                                turn_yield_after_sentences=after)
    o.session = Session()
    o.player = FakePlayer(playing)
    o.sent_q = queue.Queue()
    for i in range(queued):
        o.sent_q.put((1, f"第{i}句"))
    o._yield_turn = None
    o._yield_count = 0
    o._yield_lock = threading.Lock()
    o._stop = threading.Event()
    o.log = lambda *a, **k: None
    return o


O.BUS = FakeBus()


print("=== ① 默认关：`turn_yield_ms=0` → 一次都不让 ===")
o = make_orch(0, after=2, queued=5)
o.session.set_state(State.SPEAKING)
t = o.session.begin_turn()
for _ in range(4):
    o._maybe_yield_turn(t)
check("没有 turn_yield 事件", [k for k, _ in O.BUS.events], [])

print("\n=== ② 开启后：第 N 句之后让一次 ===")
O.BUS = FakeBus()
o = make_orch(60, after=2, queued=5)
o.session.set_state(State.SPEAKING)
t = o.session.begin_turn()
t0 = time.time()
o._maybe_yield_turn(t)          # 第 1 句 —— 不该让
mid = time.time() - t0
o._maybe_yield_turn(t)          # 第 2 句 —— 该让
elapsed = time.time() - t0
check("第 1 句不让", mid < 0.03, True)
check("第 2 句让了（≥60ms）", elapsed >= 0.055, True)
check("发了 turn_yield 事件",
      [k for k, _ in O.BUS.events], ["turn_yield"])
check("事件里带了停顿值", O.BUS.events[0][1].get("ms"), 60)

print("\n=== ③ 一轮**只让一次**（第 3/4 句不再让）===")
t1 = time.time()
o._maybe_yield_turn(t)
o._maybe_yield_turn(t)
check("后面两句没再停", time.time() - t1 < 0.03, True)
check("事件仍只有一条", len(O.BUS.events), 1)

print("\n=== ④ 后面没句子了就不让（让给谁？）===")
O.BUS = FakeBus()
o2 = make_orch(60, after=1, queued=0)       # 队列空
o2.session.set_state(State.SPEAKING)
t2 = o2.session.begin_turn()
t0 = time.time()
o2._maybe_yield_turn(t2)
check("队列空 → 不停", time.time() - t0 < 0.03, True)
check("也没有事件", O.BUS.events, [])

print("\n=== ⑤ 等待期间被打断 → 不用再让 ===")
O.BUS = FakeBus()
o3 = make_orch(60, after=1, queued=3, playing=True)
o3.session.set_state(State.SPEAKING)
t3 = o3.session.begin_turn()


def _interrupt_later():
    time.sleep(0.04)
    o3.session.interrupt()          # 模拟用户在停顿/等待期间插话
    o3.player.playing = False


threading.Thread(target=_interrupt_later, daemon=True).start()
t0 = time.time()
o3._maybe_yield_turn(t3)
check("被打断后没走完整个停顿", time.time() - t0 < 0.12, True)
check("没发 turn_yield", [k for k, _ in O.BUS.events], [])

print("\n=== ⑥ 新的一轮重新计数（上一轮让过不影响这一轮）===")
O.BUS = FakeBus()
o4 = make_orch(50, after=2, queued=4)
o4.session.set_state(State.SPEAKING)
ta = o4.session.begin_turn()
o4._maybe_yield_turn(ta)
o4._maybe_yield_turn(ta)             # 这一轮让过
check("第一轮让了一次", len(O.BUS.events), 1)
tb = o4.session.begin_turn()          # 新轮
o4.session.set_state(State.SPEAKING)
o4._maybe_yield_turn(tb)             # 新轮第 1 句
check("新轮第 1 句不让", len(O.BUS.events), 1)
o4._maybe_yield_turn(tb)             # 新轮第 2 句 → 该让
check("新轮第 2 句又让了一次", len(O.BUS.events), 2)

print()
if FAIL:
    print(f"❌ {len(FAIL)} 项失败：{FAIL}")
    sys.exit(1)
print("✅ 全部通过")
