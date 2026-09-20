#!/usr/bin/env python3
"""编排器侧「非应答轮」回归测试（见 docs/WORKORDER-ASYNC-01.md §3.2）。

覆盖：句子攒着、忙时不抢话、分批续播用同一个 turn、结束哨兵、纯符号不进 TTS、
以及**看门狗**（CC 若在收尾前死掉，不能把状态卡在 SPEAKING）。

⚠️ 全程用假对象，**不碰音频设备、不启 claude 子进程、不写真实 events.jsonl**。
"""
import os
import queue
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis_voice import orchestrator as O          # noqa: E402
from jarvis_voice.echoguard import EchoGuard        # noqa: E402
from jarvis_voice.session import Session, State     # noqa: E402

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


def make_orch():
    o = object.__new__(O.Orchestrator)
    o.cfg = O.Config.load()
    o.session = Session()
    o.sent_q = queue.Queue()
    o.echo_guard = EchoGuard()
    o._async_pending = []
    o._async_turn = None
    o._async_spoke = False
    o._async_last_ev = 0.0
    o.log = lambda *a, **k: None
    o._set_state = lambda s: o.session.set_state(s)
    return o


def drain(q):
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


def main():
    saved = O.BUS
    O.BUS = FakeBus()

    print("=== ① 没有句子就收尾 → 不占 turn、不发哨兵 ===")
    o = make_orch()
    o._handle_async({"type": "async_done"})
    check("没分配 turn", o._async_turn, None)
    check("sent_q 为空", drain(o.sent_q), [])
    check("状态仍是 IDLE", o.session.state, State.IDLE)

    print("\n=== ② 空闲时：句子 → 哨兵，同一个 turn ===")
    o = make_orch()
    o._handle_async({"type": "sentence", "text": "查到了，明天阴天。"})
    o._handle_async({"type": "sentence", "text": "不下雨。"})
    o._handle_async({"type": "async_done"})
    items = drain(o.sent_q)
    turns = {t for t, _ in items if t is not None}
    check("说了两句", [x[1] for x in items], ["查到了，明天阴天。", "不下雨。", None])
    check("同一个 turn", len(turns), 1)
    check("状态变 SPEAKING", o.session.state, State.SPEAKING)
    check("turn 已归还", o._async_turn, None)

    print("\n=== ③ 忙时不抢话，空闲后补播 ===")
    o = make_orch()
    user_turn = o.session.begin_turn()
    o.session.set_state(State.THINKING)          # 用户刚问完，CC 在想
    o._handle_async({"type": "sentence", "text": "后台跑完了。"})
    check("忙时没播", drain(o.sent_q), [])
    check("攒着", len(o._async_pending), 1)
    o._handle_async({"type": "async_done"})
    check("忙时连哨兵也不发", drain(o.sent_q), [])
    check("用户的轮没被抢", o.session.is_current(user_turn), True)
    o.session.set_state(State.IDLE)               # 用户这一轮结束
    o._flush_async()
    check("空闲后补播 + 收尾", [x[1] for x in drain(o.sent_q)], ["后台跑完了。", None])

    print("\n=== ④ 分批续播：中间没有 async_done 也不能卡死 ===")
    o = make_orch()
    o._handle_async({"type": "sentence", "text": "第一句。"})
    t1 = o._async_turn
    check("第一批就开了 turn", t1 is not None, True)
    check("状态 SPEAKING（自己的轮）", o.session.state, State.SPEAKING)
    o._handle_async({"type": "sentence", "text": "第二句。"})   # 状态非 IDLE，但是这轮是我们自己的
    items = drain(o.sent_q)
    check("两批都播了", [x[1] for x in items], ["第一句。", "第二句。"])
    check("同一 turn", {x[0] for x in items}, {t1})
    o._handle_async({"type": "async_done"})
    check("最后补哨兵", [x[1] for x in drain(o.sent_q)], [None])

    print("\n=== ⑤ 纯符号/空的句子不进 TTS ===")
    o = make_orch()
    o._handle_async({"type": "sentence", "text": "。。。"})
    o._handle_async({"type": "sentence", "text": ""})
    check("都丢掉了", o._async_pending, [])
    check("没占 turn", o._async_turn, None)

    print("\n=== ⑥ 看门狗：轮开着但再无事件 → 强制收尾 ===")
    o = make_orch()
    o._ASYNC_IDLE_CLOSE_S = 0.05                 # 缩短阈值便于测试
    o._handle_async({"type": "sentence", "text": "说了一半。"})
    drain(o.sent_q)
    o._async_last_ev = time.time() - 10           # 假装很久没事件了
    o._async_watchdog()
    check("强制补了哨兵", [x[1] for x in drain(o.sent_q)], [None])
    check("turn 已归还", o._async_turn, None)

    print("\n=== ⑦ 任务生命周期帧只发事件、不出声 ===")
    bus = FakeBus()
    O.BUS = bus
    o = make_orch()
    o._handle_async({"type": "system", "subtype": "task_notification", "task_id": "t1",
                     "status": "completed", "output_file": "/tmp/x",
                     "summary": "done"})
    check("没出声", drain(o.sent_q), [])
    kinds = [k for k, _ in bus.events]
    check("发了 background_task 事件", kinds, ["background_task"])
    check("带上 output_file", bus.events[0][1].get("output_file"), "/tmp/x")

    O.BUS = saved
    print()
    if FAIL:
        print(f"❌ {len(FAIL)} 项失败：{FAIL}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
