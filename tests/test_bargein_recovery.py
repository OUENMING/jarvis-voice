#!/usr/bin/env python3
"""P1「打断误判恢复」回归测试（见 docs/PLAN-HUMANNESS-20260920.md）。

**要解决的问题**（🟢 真机核实）：打断由 **VAD 起音**驱动，而转写要等用户说完
（`min_silence_duration=0.5s`）再跑 ASR —— 所以 `filler.is_backchannel()` **永远来不及**
用。`filler.py` 的 docstring 写着用途是「不要因为一句『嗯』『对』就打断正在播报的助手」，
**这个声明的目的从来没实现过**。

真机数据（`~/.jarvis/events.jsonl`）：41 次「助手才播不到 3 秒就被打断」里，
大量是 `'嗯。'` `'うん。'` `'.'` `'那个。'` —— 用户只是在应答。
（`'那个。'` `'让我想想。'` 甚至是**我们自己填充音的回声** —— 助手在把自己打断。）

**做法**（照抄 LiveKit `false_interruption_timeout` 语义）：
VAD 起音 → 先**暂停**（保留缓冲）→ 窗口内等转写 → 误判则**恢复播放** / 真打断则提交。

⚠️ 离线，不开任何音频设备（`Player._cb` 用假的 outdata 直接调）。
"""
import os
import sys
import threading
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis_voice import orchestrator as O              # noqa: E402
from jarvis_voice.config import Config                  # noqa: E402
from jarvis_voice.filler import is_false_interruption   # noqa: E402
from jarvis_voice.player import Player                  # noqa: E402
from jarvis_voice.session import Session, State         # noqa: E402
from jarvis_voice.tts.base import AudioFormat           # noqa: E402

FAIL = []


def check(label, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {label}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(label)


# ---------------- 播放器（不建流，直接喂回调）----------------
def make_player():
    p = object.__new__(Player)
    p.fmt = AudioFormat(sample_rate=16000)
    p.block_frames = 1024
    p._buf = __import__("collections").deque()
    p._lock = threading.Lock()
    p._uptime = p._played_audio = p._buffered = p._underruns = 0
    p._had_audio = False
    p._paused = False
    p._far_cap = 16000 * 3
    p._far = np.zeros(p._far_cap, dtype=np.int16)
    p._far_w = 0
    p._far_lock = threading.Lock()
    return p


def run_cb(p, frames=512):
    """跑一次回调，返回实际输出（是否全静音）。"""
    out = np.zeros((frames, 1), dtype=np.int16)
    p._cb(out, frames, None, None)
    return out[:, 0]


print("=== ① `pause()` 必须**保留**缓冲（这是它与 `flush()` 的唯一区别）===")
p = make_player()
p.write(np.full(4096, 5000, dtype=np.int16).tobytes())      # 4096 帧音频
before = p.buffered_seconds()
out = run_cb(p, 512)
check("未暂停时回调输出的是音频（非全零）", bool(np.any(out != 0)), True)
p.pause()
mid = p.buffered_seconds()
out2 = run_cb(p, 512)
check("暂停时输出全静音", bool(np.all(out2 == 0)), True)
check("暂停时**缓冲没被清掉**", p.buffered_seconds(), mid)
check("而且比一开始只少了前面正常播掉的那点", mid < before, True)
check("is_paused", p.is_paused, True)

print("\n=== ② `resume()` 从暂停处接着播（不是从头）===")
p.resume()
out3 = run_cb(p, 512)
check("恢复后又出声了", bool(np.any(out3 != 0)), True)
check("is_paused 复位", p.is_paused, False)
check("暂停不算卡顿（_underruns 不涨）", p._underruns, 0)

print("\n=== ③ `flush()` 与 `pause()` 的区别：flush 是真销毁 ===")
p2 = make_player()
p2.write(np.full(4096, 5000, dtype=np.int16).tobytes())
p2.pause()
p2.flush()
check("flush 后缓冲为空", p2.buffered_seconds(), 0)
check("flush 顺带解除暂停", p2.is_paused, False)

print("\n=== ④ `is_false_interruption`：全部用真机原话 ===")
for t, want in (("嗯。", True), ("うん。", True), ("うう？", True), (".", True),
                ("那个。", True), ("嗯ん。", True), ("", True), ("。。。", True),
                ("等一下你先暂停。", False), ("这个游戏有中文吗？", False),
                ("有一个问题就是。", False), ("明天天气说错了。", False)):
    check(f"{t!r}", is_false_interruption(t), want)


# ---------------- 编排器（绕过 __init__，不开设备）----------------
class FakeBus:
    def __init__(self):
        self.events = []

    def emit(self, kind, **kw):
        self.events.append((kind, kw))


class FakeTrace:
    """诊断轨迹的替身 —— P1 会在关键点落盘，测试里不碰真实文件。"""
    def __init__(self):
        self.dumps = []

    def note(self, *a, **k):
        pass

    def dump(self, reason, **extra):
        self.dumps.append(reason)


def make_orch(player=None):
    o = object.__new__(O.Orchestrator)
    o.cfg = Config.load()
    o.session = Session()
    o.player = player or make_player()
    o.trace = FakeTrace()
    o.log = lambda *a, **k: None
    o._pending_bargein_at = None
    o._filler_timer = None
    o._cancel_filler = lambda: None
    o._drain = lambda q: None
    o.sent_q = __import__("queue").Queue()
    o.brain = type("B", (), {"interrupt": staticmethod(lambda: None)})()
    return o


print("\n=== ⑤ 起音 → 暂停 + 开窗口；转写是应答词 → **撤回** ===")
bus = FakeBus()
O.BUS = bus
o = make_orch()
turn = o.session.begin_turn()
o.session.set_state(State.SPEAKING)
o.player.write(np.full(8000, 5000, dtype=np.int16).tobytes())
o._begin_bargein()
check("已暂停", o.player.is_paused, True)
check("窗口已开", o._pending_bargein_at is not None, True)
check("会话**没有**被作废（turn 还在）", o.session.is_current(turn), True)
o._resolve_bargein("嗯。")
check("撤回后恢复播放", o.player.is_paused, False)
check("窗口关掉", o._pending_bargein_at, None)
check("turn 仍然有效（没作废）", o.session.is_current(turn), True)
check("缓冲还在（能接着播）", o.player.buffered_seconds() > 0, True)
check("发了 bargein_reverted 事件",
      [k for k, _ in bus.events], ["bargein_pending", "bargein_reverted"])

print("\n=== ⑥ 转写是真话 → **提交**打断 ===")
bus2 = FakeBus()
O.BUS = bus2
o2 = make_orch()
t2 = o2.session.begin_turn()
o2.session.set_state(State.SPEAKING)
o2.player.write(np.full(8000, 5000, dtype=np.int16).tobytes())
o2._begin_bargein()
o2._resolve_bargein("等一下你先暂停。")
check("turn 被作废", o2.session.is_current(t2), False)
check("缓冲被清空", o2.player.buffered_seconds(), 0)
check("不是暂停状态", o2.player.is_paused, False)
check("累计打断 +1", o2.session.interrupts, 1)

print("\n=== ⑦ 回声（我们自己填充音的回声）也算误判 ===")
o3 = make_orch()
t3 = o3.session.begin_turn()
o3.session.set_state(State.SPEAKING)
o3.player.write(np.full(8000, 5000, dtype=np.int16).tobytes())
o3._begin_bargein()
o3._resolve_bargein("让我想想。", is_echo=True)     # 「让我想想」不在应答词表里
check("靠 is_echo 撤回（否则会误提交）", o3.player.is_paused, False)
check("turn 保住了", o3.session.is_current(t3), True)

print("\n=== ⑧ 没有待决窗口时 `_resolve_bargein` 是 no-op（不能误伤正常轮次）===")
o4 = make_orch()
t4 = o4.session.begin_turn()
o4.session.set_state(State.SPEAKING)
o4._resolve_bargein("嗯。")
check("没开窗口就不动", o4.session.is_current(t4), True)
check("也没碰播放器", o4.player.is_paused, False)

print("\n=== ⑨ 明说的打断路径仍是**立即**提交（不走宽限）===")
o5 = make_orch()
t5 = o5.session.begin_turn()
o5.session.set_state(State.SPEAKING)
o5.player.write(np.full(8000, 5000, dtype=np.int16).tobytes())
o5._do_interrupt()
check("立刻作废", o5.session.is_current(t5), False)
check("立刻清缓冲", o5.player.buffered_seconds(), 0)
check("不会留下待决窗口", o5._pending_bargein_at, None)

O.BUS = O.__dict__.get("_REAL_BUS", None) or __import__("jarvis_voice.events", fromlist=["BUS"]).BUS

print()
if FAIL:
    print(f"❌ {len(FAIL)} 项失败：{FAIL}")
    sys.exit(1)
print("✅ 全部通过")
