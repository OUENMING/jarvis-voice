#!/usr/bin/env python3
"""`EventBus` 的失败安全回归测试（2026-09-20）。

背景（ocr 报的 critical）：`BUS` 是**模块级单例**，有 **28 个调用点**，
其中 `BUS.emit("level")` 就在**实时麦克风主循环**里。原实现在三种情况下会把异常
抛出 `emit()`：

  ① `_rotate()` 里 `close() → replace() → open()` 任何一步失败 →
     `_fh` 停在**已关闭**的句柄上；下次 write 抛 **ValueError**（不是 OSError，旧 except 拦不住）
  ② `close()` 之后还在 emit（仪表盘/看门狗线程晚于主流程退出）
  ③ `json.dumps` 遇到不可序列化的 `**data`（bytes/set）→ **TypeError**

异常一旦逃进实时主循环，麦克风循环会静默终止 —— 其余线程还活着，
表现为「应用看着正常但不再响应语音」，是最难查的一类故障。

⚠️ 全程用临时文件，**不碰真实的 `~/.jarvis/events.jsonl`**。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jarvis_voice.events as E          # noqa: E402
from jarvis_voice.events import EventBus  # noqa: E402

FAIL = []


def check(label, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {label}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(label)


def new_bus():
    d = tempfile.mkdtemp()
    return EventBus(path=os.path.join(d, "events.jsonl"))


def no_raise(fn) -> str:
    """跑 fn，返回 'ok' 或异常类名。"""
    try:
        fn()
        return "ok"
    except Exception as e:                      # noqa: BLE001 —— 就是要抓全部
        return type(e).__name__


print("=== ① 轮转失败后句柄必须仍然可用（原实现会留一个已关闭的句柄）===")
b = new_bus()
b.emit("probe", n=1)                            # 先写一条，确认可用
saved = E.os.replace
E.os.replace = lambda *a, **k: (_ for _ in ()).throw(OSError("模拟 replace 失败"))
try:
    b._rotate()                                 # 模拟轮转中途失败
finally:
    E.os.replace = saved
check("轮转失败后 _fh 不是 None", b._fh is not None, True)
check("轮转失败后仍能 emit（不抛）", no_raise(lambda: b.emit("probe", n=2)), "ok")
b.emit("probe", n=3)
lines = [json.loads(x) for x in open(b.path) if x.strip()]
check("后两条都真的落盘了", [x["n"] for x in lines], [1, 2, 3])
b.close()

print("\n=== ② 不可序列化的 data 不能把异常抛出去（TypeError）===")
b = new_bus()
check("bytes 参数", no_raise(lambda: b.emit("probe", blob=b"\x00\x01")), "ok")
check("set 参数", no_raise(lambda: b.emit("probe", s={1, 2})), "ok")
check("自定义对象", no_raise(lambda: b.emit("probe", o=object())), "ok")
b.close()

print("\n=== ③ close() 之后还能安全 emit（真实场景：仪表盘线程晚于主流程退出）===")
b = new_bus()
b.close()
check("close 后 _fh 被置 None", b._fh, None)
check("close 后 emit 不抛", no_raise(lambda: b.emit("probe", n=1)), "ok")

print("\n=== ④ 重复 close 不崩 ===")
b = new_bus()
b.close()
check("再 close 一次", no_raise(b.close), "ok")

print("\n=== ⑤ 正常路径没被改坏（回归）===")
b = new_bus()
q = b.subscribe()
b.emit("user", text="你好")
b.emit("level", v=0.1)                          # 瞬时事件：只给订阅者，不落盘
import queue as _q
got = []
while True:
    try:
        got.append(q.get_nowait()["kind"])
    except _q.Empty:
        break
check("订阅者收到两条", got, ["user", "level"])
lines = [json.loads(x) for x in open(b.path) if x.strip()]
check("落盘只有非瞬时那条", [x["kind"] for x in lines], ["user"])
b.close()

print()
if FAIL:
    print(f"❌ {len(FAIL)} 项失败：{FAIL}")
    sys.exit(1)
print("✅ 全部通过")
