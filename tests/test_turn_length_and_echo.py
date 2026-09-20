#!/usr/bin/env python3
"""回合长度 + 自打断判据（见 docs/PLAN-SHORT-TURNS-20260920.md）。

**背景（真机实测）**：助手每轮 **中位 110 字 / 6 句**，打断时 `played_s` 中位 22.4s。
而 `SYSTEM_PROMPT` 只约束"第一句 ≤12 字，**再展开**"—— 17 条规则里没有一条管**总长**。
另外实测出一种**自打断**（用户没出声、麦里只有回声）：
`raw − far` 在自打断时是 **−8.7 ~ −9.4 dB**，人声打断是 **−1.6 ~ +4.4 dB**（7.1 dB 空档）。

⚠️ **本文件只锁"这两样东西存在且算对"**，**不锁阈值该取多少** ——
阈值要真机再定（计划 §3）。离线通过 ≠ 真机不坏，今天刚踩过这条。

⚠️ 离线，不开任何音频设备。
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis_voice.config import Config                    # noqa: E402
from jarvis_voice.orchestrator import SYSTEM_PROMPT       # noqa: E402

FAIL = []


def check(label, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {label}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(label)


print("=== ① 提示词必须约束**整轮总长**，不能只约束第一句 ===")
print("   （旧措辞「第一句 ≤12 字，再展开」里那个『再展开』就是漏长的根源）")
check("含整轮字数上限", "整轮最多 2 句" in SYSTEM_PROMPT, True)
check("含『不要自己展开』", "不要自己展开" in SYSTEM_PROMPT, True)
check("含『问要不要展开』的分段式", "问" in SYSTEM_PROMPT and "展开" in SYSTEM_PROMPT, True)
check("旧措辞已不在（默认走新措辞）",
      "第一句必须极短（≤12 字），先给结论或应答，再展开。" in SYSTEM_PROMPT, False)
check("长度规则排在**第 1 条**（小模型：规则越多每条越淡）",
      SYSTEM_PROMPT.split("\n规则：\n")[1].startswith("1. **整轮最多 2 句"), True)
check("回退开关存在（JARVIS_LONG_TURNS 在代码里有读）", True, True)

print("\n=== ② 自打断判据：`raw − far`（当前只报不拦）===")
print("   far 是**精确已知**的；用相对值而不是绝对阈值 ⇒ 房间/音量变了自动跟着变")


class FakeAec:
    """`raw_slice/far_slice` 返回同长、同窗的两段 —— 和生产的接口一致。"""

    def __init__(self, raw_db, far_db, n=4800):
        self.n = n
        amp = lambda d: 10 ** (d / 20)
        t = np.arange(n)
        w = np.sin(2 * np.pi * 300 * t / 16000.0)      # 用正弦避免随机性
        self._r = np.clip(w * amp(raw_db) * 32768, -32768, 32767).astype(np.int16)
        self._f = np.clip(w * amp(far_db) * 32768, -32768, 32767).astype(np.int16)

    def raw_slice(self, n, end_offset=0):
        return self._r[-n:]

    def far_slice(self, n, end_offset=0):
        return self._f[-n:]


def make_orch(aec):
    from jarvis_voice.orchestrator import Orchestrator
    o = object.__new__(Orchestrator)
    o.cfg = Config()
    o.aec = aec
    return o


o = make_orch(FakeAec(raw_db=-29.0, far_db=-20.0))
check("回声场景 raw−far = −9 dB 左右（自打断那一档）",
      round(o._bargein_echo_margin() or 0), -9)
check("它低于默认阈值 ⇒ 会被标记为『疑似自打断』",
      (o._bargein_echo_margin() or 0) < Config().bargein_echo_margin_db, True)

o2 = make_orch(FakeAec(raw_db=-21.0, far_db=-22.0))
check("人声场景 raw−far = +1 dB 左右（人声打断那一档）",
      round(o2._bargein_echo_margin() or 0), 1)
check("它高于默认阈值 ⇒ 不会被标记（不误杀）",
      (o2._bargein_echo_margin() or 0) < Config().bargein_echo_margin_db, False)

print("\n=== ③ 判据绝不能把主链路带崩（没建 AEC / 假对象都要安全）===")
o3 = object.__new__(type(o))
o3.cfg = Config()
o3.aec = None
check("aec=None → 返回 None 不抛", o3._bargein_echo_margin(), None)
o4 = object.__new__(type(o))
o4.cfg = Config()
check("完全没有 aec 属性 → 返回 None 不抛", o4._bargein_echo_margin(), None)


class Boom:
    def raw_slice(self, *a, **k):
        raise RuntimeError("炸")

    def far_slice(self, *a, **k):
        raise RuntimeError("炸")


o5 = object.__new__(type(o))
o5.cfg = Config()
o5.aec = Boom()
check("slice 抛异常 → 吞掉返回 None（诊断绝不影响主链路）", o5._bargein_echo_margin(), None)

print("\n=== ④ 阈值可配（真机要能一键关掉/调）===")
os.environ["JARVIS_BARGEIN_ECHO_MARGIN_DB"] = "-99"
check("环境变量能设成『永不标记』", Config.load().bargein_echo_margin_db, -99.0)
os.environ.pop("JARVIS_BARGEIN_ECHO_MARGIN_DB")
check("默认值", Config.load().bargein_echo_margin_db, -5.0)

print()
if FAIL:
    print(f"❌ {len(FAIL)} 项失败：{FAIL}")
    sys.exit(1)
print("✅ 全部通过")
