#!/usr/bin/env python3
"""桥接的「非应答轮」分流回归测试（见 docs/WORKORDER-ASYNC-01.md §3.1）。

背景：CC 在后台任务完成后会**自己起一轮**汇报结果。那一轮的帧若落进 `_pending`，
会被下一个 `ask()` 当成它自己的回答吐出去，且那段的 `result` 会把真正的这一轮
**提前结束**。

⚠️ 用例①②③ 在旧代码（`_pump` 把所有非 init/control 帧都塞 `_pending`）上**必须失败**（red-green）。
⚠️ 全程用假 stdout，**不启动真 claude 子进程**。
"""
import json
import os
import queue
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude_bridge import ClaudeBridge          # noqa: E402

FAIL = []


def check(label, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {label}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(label)


class FakeProc:
    def __init__(self, lines):
        self.stdout = iter(lines)


def make_bridge(awaiting=False, *frames):
    b = object.__new__(ClaudeBridge)
    b._pending_lock = threading.Lock()
    b._pending = []
    b._control_lock = threading.Lock()
    b._control = {}
    b._interrupt_rids = set()
    b._interrupted = False
    b._ready = threading.Event()
    b._save_session = lambda: None          # 不写真实 ~/.jarvis/session.json
    b.session_id = None
    b.capabilities = []
    b.total_cost = 0.0
    b._awaiting = awaiting
    b._tasks = queue.Queue()
    b._async_q = queue.Queue()
    b._async_buf = ""
    b.proc = FakeProc([json.dumps(f) + "\n" for f in frames])
    b._pump()                                # 同步跑完（FakeProc 是有限迭代器）
    return b


def drain(q):
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


def text_delta(s):
    return {"type": "stream_event",
            "event": {"type": "content_block_delta",
                      "delta": {"type": "text_delta", "text": s}}}


NOTIF = {"type": "system", "subtype": "task_notification", "task_id": "t1",
         "tool_use_id": "c1", "status": "completed",
         "output_file": "/tmp/x.output", "summary": "done"}
STARTED = {"type": "system", "subtype": "task_started", "task_id": "t1",
           "tool_use_id": "c1", "is_backgrounded": True}
STATUS = {"type": "system", "subtype": "status", "status": "requesting"}
RESULT = {"type": "result", "num_turns": 2, "terminal_reason": "completed",
          "session_id": "s1", "total_cost_usd": 0.01}

print("=== ① 没有 ask() 在等时，CC 续跑的文本**不得**进 _pending ===")
b = make_bridge(False, text_delta("后台任务已完成。"), RESULT)
check("_pending 里没有续跑帧", b._pending, [])
evs = drain(b._async_q)                       # ⚠️ 只能排空一次（get_nowait 是消费）
sents = [e for e in evs if e["type"] == "sentence"]
check("续跑的句子进了 async 通道", [s["text"] for s in sents], ["后台任务已完成。"])
check("续跑的 result 变成 async_done",
      [e for e in evs if e["type"] == "async_done"],
      [{"type": "async_done", "terminal_reason": "completed", "is_error": False}])

print("\n=== ② 任务生命周期帧进 _tasks，不进 _pending ===")
b = make_bridge(False, STARTED, NOTIF)
check("_pending 为空", b._pending, [])
check("_tasks 收到两条", [e.get("subtype") for e in drain(b._tasks)],
      ["task_started", "task_notification"])

print("\n=== ③ system/status 直接丢弃（两边都不进）===")
b = make_bridge(True, STATUS)
check("_pending 为空", b._pending, [])
check("_tasks 为空", drain(b._tasks), [])

print("\n=== ③b 但 compact_boundary / api_error 必须透出（不能跟 status 一起丢）===")
print("   （`compact_boundary` 是「窗口配对了没有」唯一的现场证据）")
CB = {"type": "system", "subtype": "compact_boundary"}
AE = {"type": "system", "subtype": "api_error", "error": "boom"}
b = make_bridge(False, CB, AE)
check("_pending 为空", b._pending, [])
check("_tasks 收到两条", [e.get("subtype") for e in drain(b._tasks)],
      ["compact_boundary", "api_error"])

print("\n=== ④ 有 ask() 在等时，行为**不变**（帧仍进 _pending）===")
b = make_bridge(True, text_delta("你好啊。"))
check("_pending 收到该帧", len(b._pending), 1)
check("_async_q 为空", drain(b._async_q), [])

print("\n=== ⑤ next_async() 能取到东西，超时返回 None ===")
print("   （`_tasks` 优先于 `_async_q` —— 与真实时序一致：task_notification 在续跑句子之前）")
b = make_bridge(False, text_delta("查到了。"), NOTIF)
got = []
for _ in range(3):
    e = b.next_async(timeout=0.2)
    if e is None:
        break
    got.append(e.get("type") if e.get("type") != "system" else e.get("subtype"))
check("两条都取到（顺序不限）", sorted(got), ["sentence", "task_notification"])
check("空了以后超时返回 None", b.next_async(timeout=0.05), None)

print("\n=== ⑥ assistant 帧里的 tool_use 也要透出（续跑轮会自己 Read 输出文件）===")
b = make_bridge(False, {"type": "assistant", "message": {"content": [
    {"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/x.output"}}]}})
tools = [e for e in drain(b._async_q) if e["type"] == "tool"]
check("tool 事件透出", [(t["name"], t["input"]["file_path"]) for t in tools],
      [("Read", "/tmp/x.output")])

print("\n=== ⑦ 排空残留帧时 `_awaiting` 必须已经置位（否则残句会被念出来）===")
print("   （ocr 2026-09-20 报的，是引入非应答轮通道时的回归）")
import io                                            # noqa: E402


class _Var:
    """假的 Popen：stdin 可写、stdout 立刻 EOF。"""
    pid = 1

    def __init__(self):
        self.stdin = io.StringIO()
        self.stdout = iter(())


def spy_on_drain():
    b = object.__new__(ClaudeBridge)
    b._write_lock = threading.Lock()
    b._pending_lock = threading.Lock()
    b._pending = []
    b._control_lock = threading.Lock()
    b._control = {}
    b._interrupt_rids = set()
    b._interrupted = False
    b._ready = threading.Event()
    b._ready.set()
    b._save_session = lambda: None
    b.session_id = "s"
    b.total_cost = 0.0
    b._awaiting = False
    b._tasks = queue.Queue()
    b._async_q = queue.Queue()
    b._async_buf = ""
    b._turn_lock = threading.Lock()
    b._turn_open = True                     # 上一轮没收尾 → 会走排空分支
    b.proc = _Var()
    b.alive = lambda: True
    b._load_session = lambda: None
    seen = []
    b._drain_until_result = lambda timeout=5.0: seen.append(b._awaiting) or True
    b._read_turn = lambda timeout: iter(())  # 排空之后立刻结束本轮
    for _ in b.ask("测试"):
        pass
    return seen


seen = spy_on_drain()
check("排空被调用过", len(seen), 1)
check("调用时 _awaiting 已是 True", seen, [True])

print()
if FAIL:
    print(f"❌ {len(FAIL)} 项失败：{FAIL}")
    sys.exit(1)
print("✅ 全部通过")
