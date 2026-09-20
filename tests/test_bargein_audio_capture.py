#!/usr/bin/env python3
"""回归：**超时提交的打断，音频也必须落盘**。

**真机 bug（2026-09-20 15:5x，本次抓到）**：整轮 8 次打断，`~/.jarvis/bargein-audio/`
**一份文件都没多**。日志里没有 traceback、也**没有**那行"没存下来" —— 纯沉默失败。

**根因**：保存的判据原来是

    if self.session.state is not State.IDLE or self._pending_bargein_at is not None:

而 `_commit_interrupt()` → `session.interrupt()` 会把 state **置成 IDLE**；
超时分支又会把 `_pending_bargein_at` **清掉**。时间线：

    T+0      VAD 起音 → `_begin_bargein()`（暂停，开 800ms 窗口）
    T+800ms  `[打断-超时]` → 清窗口 + state→IDLE
    T+1.5~2s 转写才到（VAD 要等 `min_silence_duration` 才吐段）→ **两个条件都已是假**

⇒ 这个诊断**只在转写快过 800ms 时才工作**。老那轮存下来了是因为
「看到了看到了」382ms 就到了；这轮句子长（「呃，等一下，你知道那个明天天气怎么样吗？」）
全部超时 → 全部没存。而**超时恰恰是最常见的那条路**（真机 22/25）。

**修法**：诊断用自己的时间戳 `_bargein_utt_at`，**跨状态重置存活**；
在 `_brain_once` 里**读一次就清**，且在所有提前 return 之前取。

⚠️ 离线，不开任何音频设备。
"""
import dataclasses
import os
import queue
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis_voice import orchestrator as O                    # noqa: E402
from jarvis_voice.config import Config                        # noqa: E402
from jarvis_voice.session import Session, State               # noqa: E402

FAIL = []


def check(label, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {label}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(label)


class FakeTrace:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.saved = []

    def new_stem(self):
        return "STEM"

    def save_pcm(self, pcm, tag, sample_rate=16000, stem=None):
        if not self.enabled or pcm is None:
            return None
        self.saved.append((tag, stem, len(pcm)))
        return f"/tmp/fake-{stem}-{tag}.wav"

    def dump(self, *a, **k):
        return None

    def note(self, *a, **k):
        return None


class FakeAec:
    def raw_slice(self, n, end_offset=0):
        return np.zeros(max(0, n), np.int16)

    def far_slice(self, n, end_offset=0):
        return np.zeros(max(0, n), np.int16)


class FakeASR:
    """返回**空文本** → `_brain_once` 在保存块之后就早退，不碰后面的重逻辑。"""

    def __init__(self):
        self.calls = 0

    def transcribe(self, pcm):
        self.calls += 1
        return type("R", (), {"text": "", "latency_ms": 1.0,
                              "emotion": "NEUTRAL", "events": []})()


class FakePaused:
    def __init__(self, v=False):
        self.v = v

    def is_set(self):
        return self.v


class FakePlayer:
    """只提供 `_begin_bargein` / `_commit_interrupt` 会碰的那几个方法。"""

    def __init__(self):
        self.paused = False

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def flush(self):
        self.paused = False
        return 0.0


def make_orch(trace=None):
    o = object.__new__(O.Orchestrator)
    cfg = dataclasses.replace(Config.load(),
                              echo_guard_enabled=False)   # 跳过回声护栏（与本测试无关）
    o.cfg = cfg
    o.session = Session()
    o.trace = trace if trace is not None else FakeTrace()
    o.aec = FakeAec()
    o.asr = FakeASR()
    o.log = lambda *a, **k: None
    o.utt_q = queue.Queue()
    o._paused = FakePaused(False)
    o._pending_bargein_at = None
    o._bargein_utt_at = None
    o._trace_utt_path = None
    o.echo_guard = None
    o.player = FakePlayer()
    o.sent_q = queue.Queue()
    o._cancel_filler = lambda: None
    o._drain = lambda q: None
    o.brain = type("B", (), {"interrupt": staticmethod(lambda: None)})()
    return o


def feed(o, n=16000):
    """把一段够长的语音塞进 utt_q 并跑一次 `_brain_once`。"""
    o.utt_q.put(np.zeros(n, np.int16))
    o._brain_once()


print("=== ① 超时提交后再到转写：**必须仍然落盘**（这就是真机漏掉的那条路）===")
o = make_orch()
o.session.begin_turn()
o.session.set_state(State.SPEAKING)
o._begin_bargein()
check("起音后有诊断时间戳", o._bargein_utt_at is not None, True)
# 模拟主循环里那条超时分支：清窗口 + 提交（state → IDLE）
o._pending_bargein_at = None
o._commit_interrupt()
check("提交后 state 已回 IDLE（正是原来判据失效的原因）",
      o.session.state is State.IDLE, True)
check("提交后 `_pending_bargein_at` 已清", o._pending_bargein_at, None)
check("但诊断时间戳**存活**", o._bargein_utt_at is not None, True)
feed(o)
tags = [t for t, _, _ in o.trace.saved]
check("三份都存了（过 AEC / 未过 AEC / far）", tags, ["bargein", "bargein-raw", "far"])
check("三份共用同一个 stem", {s for _, s, _ in o.trace.saved}, {"STEM"})
check("far 的长度 = 段长 + 预滚（vad_min_silence）",
      [n for t, _, n in o.trace.saved if t == "far"],
      [16000 + int(o.cfg.vad_min_silence * o.cfg.sample_rate)])

print("\n=== ② 读一次就清：**下一句无关的话不会被标成打断** ===")
check("用完后时间戳已清", o._bargein_utt_at, None)
n_before = len(o.trace.saved)
feed(o)
check("第二句没有新增落盘", len(o.trace.saved), n_before)

print("\n=== ③ 过期的时间戳不该触发落盘（避免把很久以后的句子标错）===")
o2 = make_orch()
o2._bargein_utt_at = time.time() - (O.BARGEIN_UTT_WINDOW_S + 10)
feed(o2)
check("超出窗口 → 不落盘", len(o2.trace.saved), 0)
check("过期值同样被清掉", o2._bargein_utt_at, None)

print("\n=== ④ 段太短被丢时，**标志也要清掉**（否则会标到下一句）===")
o3 = make_orch()
o3._begin_bargein()
check("先确认时间戳在", o3._bargein_utt_at is not None, True)
o3.utt_q.put(np.zeros(100, np.int16))          # 远短于 asr_min_utt_sec
o3._brain_once()
check("太短被丢后时间戳已清", o3._bargein_utt_at, None)
check("太短不落盘", len(o3.trace.saved), 0)

print("\n=== ⑤ 非打断句子**不该**落盘（别把诊断变成全量录音）===")
o4 = make_orch()
o4.session.begin_turn()
o4.session.set_state(State.IDLE)
feed(o4)
check("干净路径无落盘", len(o4.trace.saved), 0)
check("ASR 确实跑了（证明是判据生效，不是提前 return）", o4.asr.calls, 1)

print("\n=== ⑥ trace 关闭时：**要出声**，不能沉默失败 ===")
logs = []
o5 = make_orch(trace=FakeTrace(enabled=False))
o5.log = lambda *a, **k: logs.append(" ".join(str(x) for x in a))
o5._begin_bargein()
feed(o5)
check("没落盘", len(o5.trace.saved), 0)
check("但日志里有明确提示", any("没存下来" in s for s in logs), True)

print()
if FAIL:
    print(f"❌ {len(FAIL)} 项失败：{FAIL}")
    sys.exit(1)
print("✅ 全部通过")
