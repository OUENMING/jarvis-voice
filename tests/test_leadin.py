#!/usr/bin/env python3
"""A 档「按工具类别播承接句」回归测试（见 docs/WORKORDER-LEADIN-01.md）。

覆盖三层：
  ① `tool_tag()` 纯映射（含认不出 / 空串）
  ② `FillerClips.pick(tag)` 分池 + 退回通用池 + 不连着重复
  ③ `Orchestrator._play_filler()` 的「同一轮只播一次」守卫

⚠️ 用例 ③ 在守卫加上之前**必须失败**（red-green）。
⚠️ 全程用假 TTS / 假播放器 + 把 `BUS` 换掉 —— **绝不碰音频设备，也不写真实 events.jsonl**。
   （音频设备那条见全局记忆 `audio-probe-device-contention`。）
"""
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis_voice import orchestrator as O          # noqa: E402
from jarvis_voice.config import Config              # noqa: E402
from jarvis_voice.echoguard import EchoGuard        # noqa: E402
from jarvis_voice.fillers import (TEXTS, TOOL_TEXTS, FillerClips,  # noqa: E402
                                  tool_tag)
from jarvis_voice.session import Session, State     # noqa: E402

FAIL = []


def check(label, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {label}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(label)


class FakeFmt:
    sample_rate = 16000


class FakeTTS:
    """够 `FillerClips` 用：算缓存键 + 吐 10ms 静音 PCM。"""
    name = "fake"
    voice_id = "v"
    temperature = 0.3
    audio_format = FakeFmt()

    def synthesize(self, text):
        yield b"\x00\x00" * 160


class FakePlayer:
    def __init__(self):
        self.written = []

    def write_filler(self, pcm, tag="filler"):
        self.written.append(pcm)


class FakeBus:
    def __init__(self):
        self.events = []

    def emit(self, kind, **kw):
        self.events.append((kind, kw))


def make_clips(tmp):
    fc = FillerClips(FakeTTS(), texts={FillerClips.GENERIC: list(TEXTS), **TOOL_TEXTS},
                     cache_dir=tmp)
    fc.ensure(log=lambda *a: None)
    return fc


def make_orch(fc, bus):
    """绕过 `__init__`（它会开音频设备）只搭出 `_play_filler` 需要的那几个属性。"""
    o = object.__new__(O.Orchestrator)
    o.cfg = Config.load()
    o.fillers = fc
    o.player = FakePlayer()
    o.echo_guard = EchoGuard()
    o.session = Session()
    o._filler_lock = threading.Lock()
    o._filler_played_turn = None
    o._filler_last_at = 0.0
    o._filler_timer = None
    o.log = lambda *a, **k: None
    return o


def main():
    saved_bus = O.BUS
    bus = FakeBus()
    O.BUS = bus

    print("=== ① tool_tag() 映射 ===")
    for name, want in (("Bash", "run"),
                       ("mcp__browser__computer", "browse"),
                       ("mcp__anysearch__batch_search", "search"),
                       ("mcp__parallel-search__web_search", "search"),
                       ("mcp__tavily__tavily_search", "search"),
                       ("mcp__obsidian-vault__vault_read", "notes"),
                       ("ToolSearch", ""),
                       ("Skill", ""),
                       ("", "")):
        check(f"{name or '(空串)'}", tool_tag(name), want)

    tmp = os.path.join(tempfile.gettempdir(), "jarvis_leadin_test")
    os.makedirs(tmp, exist_ok=True)
    fc = make_clips(tmp)
    print(f"\n  预渲染 {len(fc.clips)} 条（{len(TEXTS)}+{sum(len(v) for v in TOOL_TEXTS.values())}）")
    check("全部渲染成功", len(fc.clips),
          len(TEXTS) + sum(len(v) for v in TOOL_TEXTS.values()))

    print("\n=== ② pick(tag) 分池 ===")
    for tag in ("search", "browse", "notes", "run"):
        _p, text = fc.pick(tag)
        check(f"pick({tag!r}) 落在本池", text in TOOL_TEXTS[tag], True)
    _p, text = fc.pick("不存在的类别")
    check("认不出的 tag 退回通用池", text in TEXTS, True)
    _p, text = fc.pick("")
    check("空 tag 走通用池", text in TEXTS, True)

    print("\n=== ② 池内轮转不连着重复 ===")
    fc2 = make_clips(tmp)
    a = fc2.pick("search")[1]
    b = fc2.pick("search")[1]
    check("search 池两条不同", a != b, True)
    # 只有 1 条的池不能因为"防重复"就返回空
    c = fc2.pick("notes")[1]
    d = fc2.pick("notes")[1]
    check("单条池仍能返回", (c, d), ("我翻下笔记。", "我翻下笔记。"))

    print("\n=== ③ 同一轮内不能连着播（时间门，不是「一轮一次」）===")
    fc3 = make_clips(tmp)
    o = make_orch(fc3, bus)
    turn = o.session.begin_turn()
    o.session.set_state(State.THINKING)
    check("首次 _play_filler 播了", o._play_filler(turn, "search", "tool"), True)
    check("紧接着再播是 no-op", o._play_filler(turn, "search", "tool"), False)
    check("同一轮换个 trigger 也是 no-op",
          o._play_filler(turn, "", "latency"), False)
    check("播放器只收到 1 段", len(o.player.written), 1)
    check("发了 filler 事件", len([e for e in bus.events if e[0] == "filler"]), 1)

    print("\n=== ③b 🆕 隔够久之后**允许**同一轮再播一条（真机修的正是这个）===")
    print("   （真机：兜底 2.0s 播了「嗯……」，而第一个工具事件中位 3.4s 才到 ——")
    print("     「一轮一条」会让按工具类别选的承接句**永远没机会**）")
    o2 = make_orch(make_clips(tmp), FakeBus())
    t2 = o2.session.begin_turn()
    o2.session.set_state(State.THINKING)
    check("先播兜底（通用池）", o2._play_filler(t2, "", "latency"), True)
    o2._filler_last_at -= (o2.cfg.filler_min_gap_ms / 1000.0 + 0.1)   # 把时钟往前拨 1.6s
    check("隔够久 → 承接句能播", o2._play_filler(t2, "search", "tool"), True)
    check("两段都真播出去了", len(o2.player.written), 2)
    _sp = o2.echo_guard._spoken[-1]
    # ⚠️ `_spoken` 里存的是**归一化后**的文本（`echoguard.normalize` 去标点），
    # 所以是「我搜一下」不是「我搜一下。」
    check("第二条来自 search 池（不是通用池）",
          _sp[1] in ("我搜一下", "我查一下"), True)

    print("\n=== ③ 新的一轮可以再播 ===")
    turn2 = o.session.begin_turn()
    o.session.set_state(State.THINKING)
    check("新一轮能播", o._play_filler(turn2, "browse", "tool"), True)

    print("\n=== ③ 不是 THINKING 时不播（CC 已经开口了）===")
    turn3 = o.session.begin_turn()
    o.session.set_state(State.SPEAKING)
    check("SPEAKING 时不播", o._play_filler(turn3, "run", "tool"), False)

    print("\n=== ③ 过期的 turn 不播 ===")
    turn4 = o.session.begin_turn()
    o.session.set_state(State.THINKING)
    o.session.interrupt()                     # turn4 作废
    check("作废轮不播", o._play_filler(turn4, "run", "tool"), False)

    print("\n=== ③ 填充音文本进回声护栏 ===")
    fc4 = make_clips(tmp)
    o4 = make_orch(fc4, FakeBus())
    t = o4.session.begin_turn()
    o4.session.set_state(State.THINKING)
    o4._play_filler(t, "notes", "tool")
    hit, score, _ = o4.echo_guard.check("我翻下笔记。")
    check("护栏认得自己的承接句", hit, True)

    O.BUS = saved_bus
    print()
    if FAIL:
        print(f"❌ {len(FAIL)} 项失败：{FAIL}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
