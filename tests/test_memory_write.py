#!/usr/bin/env python3
"""「记住 X」→ 写进 `memory.md` 的回归测试（2026-09-20）。

背景：`memory.md` 的**读**链路一直是通的（`_compose_system_prompt` 注进系统提示），
但全库**没有任何写入路径** —— 上线至今 0 条内容，是个只读的死文件。
而 `--resume` 默认开的会话被 `/clear` 一清就全没了，它是**唯一**能留下来的地方。

⚠️ 全程用临时目录 + 假对象，**不碰真实 `~/.jarvis/memory.md`**。
"""
import dataclasses
import os
import queue
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis_voice import orchestrator as O            # noqa: E402
from jarvis_voice.commands import match               # noqa: E402
from jarvis_voice.config import Config                # noqa: E402

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


def make_orch(mem_path):
    o = object.__new__(O.Orchestrator)
    o.cfg = dataclasses.replace(Config.load(), memory_file=mem_path)
    o.log = lambda *a, **k: None
    return o


def main():
    saved = O.BUS
    bus = FakeBus()
    O.BUS = bus

    print("=== ① 识别「记住 X」===")
    for text, want in (
        ("记住我下周三要交计量作业。", "我下周三要交计量作业"),
        ("你帮我记一下明天买牛奶。", "明天买牛奶"),
        ("记下来，我的学号是 24201234。", "我的学号是24201234"),
        ("记住：我妈生日是十月八号。", "我妈生日是十月八号"),
    ):
        m = match(text)
        check(f"{text!r}", (m.name, m.payload) if m else None, ("remember", want))

    print("\n=== ①b 触发词要取「消耗前缀最远」的，不能取最长的 ===")
    print("   （ocr 2026-09-20 报的：按长度排序会让「帮我记」抢在「记下」前面，")
    print("     payload 被截成『下明天买牛奶』—— 多一个残字）")
    for text, want in (
        ("帮我记下明天买牛奶。", "明天买牛奶"),
        ("麻烦帮我记住我妈生日是十月八号。", "我妈生日是十月八号"),
        ("给我记一下学号是 24201234。", "学号是24201234"),
    ):
        m = match(text)
        check(f"{text!r}", (m.name, m.payload) if m else None, ("remember", want))

    print("\n=== ② 疑问句/回忆**不是**指令（别把 CC 的问话吃掉）===")
    for text in ("你记住我说的话了吗？", "我不记得了。", "你还记得吗？",
                 "记住了吗？", "我记得好像是这样。"):
        m = match(text)
        check(f"{text!r}", m.name if m else None, None)

    print("\n=== ③ 没有内容不算指令 ===")
    check("「记住」光杆", match("记住。"), None)

    print("\n=== ④ 真的写得进去（临时文件）===")
    tmp = tempfile.mkdtemp()
    mem = os.path.join(tmp, "memory.md")
    with open(mem, "w", encoding="utf-8") as f:
        f.write("# 可写记忆\n\n<!-- 一行一条 -->\n")
    o = make_orch(mem)
    ok, why = o._remember("下周三要交计量作业")
    check("首次写入成功", (ok, why), (True, ""))
    body = open(mem, encoding="utf-8").read()
    check("内容落盘", "下周三要交计量作业" in body, True)
    check("格式是 `- YYYY-MM-DD 内容`",
          any(ln.startswith("- ") and "下周三要交计量作业" in ln for ln in body.splitlines()),
          True)
    check("原有内容没被动过", "# 可写记忆" in body and "<!-- 一行一条 -->" in body, True)
    check("发了 memory_write 事件",
          [k for k, _ in bus.events].count("memory_write"), 1)

    print("\n=== ⑤ 去重：同一件事说两遍不写两行 ===")
    before = len([ln for ln in open(mem, encoding="utf-8").read().splitlines()
                  if ln.startswith("- ")])
    ok2, _ = o._remember("下周三要交计量作业")
    after = len([ln for ln in open(mem, encoding="utf-8").read().splitlines()
                 if ln.startswith("- ")])
    check("重复仍算成功（用户目标已达成）", ok2, True)
    check("但没多写一行", after, before)
    ok3, _ = o._remember("下周三要交计量作业!")     # 差一个标点也算重复
    check("轻微差异也算重复", ok3, True)
    check("仍然只有一条",
          len([ln for ln in open(mem, encoding="utf-8").read().splitlines()
               if ln.startswith("- ")]), before)

    print("\n=== ⑥ 不同的事要各写一行 ===")
    o._remember("我妈生日是十月八号")
    check("两条都在",
          sum(1 for ln in open(mem, encoding="utf-8").read().splitlines()
              if ln.startswith("- ")), 2)

    print("\n=== ⑦ 文件不存在时优雅处理（不崩、也不建垃圾）===")
    o2 = make_orch(os.path.join(tmp, "nope", "memory.md"))
    ok4, why4 = o2._remember("随便一件事")
    check("写不进去要报出来，不静默", ok4, False)
    check("给了原因", bool(why4), True)

    print("\n=== ⑧ `_run_meta` 的返回契约：只有「记住」成功才继续送 CC ===")
    print("   （写成功必须继续送 CC，否则 CC 在本会话内不知道这件事；")
    print("     写失败/其它元命令必须吃掉这一轮，否则一句会被念两遍）")
    tmp2 = tempfile.mkdtemp()
    mem2 = os.path.join(tmp2, "memory.md")
    open(mem2, "w", encoding="utf-8").write("# 可写记忆\n")
    o3 = make_orch(mem2)
    spoken = []
    o3._speak_local = lambda t: spoken.append(t)
    o3._do_interrupt = lambda: None
    o3.pause = lambda: None
    o3.resume = lambda: None
    o3.reconnect_memory = lambda: True
    o3.brain = type("B", (), {"clear_context": staticmethod(lambda: True)})()
    from jarvis_voice.commands import match as m2
    check("remember 写成功 → False（继续送 CC）",
          o3._run_meta(m2("记住明天买牛奶"), "记住明天买牛奶"), False)
    check("而且本地没出声（交给 CC 应一声）", spoken, [])
    o4 = make_orch(os.path.join(tmp2, "nope", "memory.md"))   # 写不进去
    spoken4 = []
    o4._speak_local = lambda t: spoken4.append(t)
    check("remember 写失败 → True（就地吃掉）",
          o4._run_meta(m2("记住明天买牛奶"), "记住明天买牛奶"), True)
    check("且明确报了失败", bool(spoken4), True)
    check("clear 仍然 → True", o3._run_meta(m2("清空上下文"), "清空上下文"), True)

    O.BUS = saved
    print()
    if FAIL:
        print(f"❌ {len(FAIL)} 项失败：{FAIL}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
