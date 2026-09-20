#!/usr/bin/env python3
"""打断诊断轨迹回归测试（见 jarvis_voice/bargein_trace.py）。

**为什么要有这个模块**：用户报「要喊几遍才打断」时，`events.jsonl` 里**什么都看不到**
—— `level` 被刻意标成瞬时事件不落盘（12Hz 全量写盘会让 events.jsonl 涨到 2MB/小时，
实测过）。于是最该看的那个数（**那一刻麦克风多响、VAD 有没有翻**）恰恰没有留痕。

**做法**：环形缓冲 + 事件触发落盘。平时只进内存（10 次/秒，零盘 IO），
只有「播放中检测到语音 / 武装 / 待定 / 撤回 / 提交 / 超时」才把**前 15 秒**倒出来。
**失败的尝试也会留记录** —— 这正是它存在的意义。

⚠️ 全程临时文件，**不碰真实的 `~/.jarvis/bargein-trace.jsonl`**。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis_voice.bargein_trace import BargeinTrace, _PRE_SEC, _SAMPLE_HZ   # noqa: E402

FAIL = []


def check(label, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {label}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(label)


def new(path_name="t.jsonl"):
    d = tempfile.mkdtemp()
    return BargeinTrace(path=os.path.join(d, path_name)), os.path.join(d, path_name)


print("=== ① `note()` 只进内存，**不碰盘** ===")
t, p = new()
for i in range(30):
    t.note(0.01 * i, speaking=(i > 20), playing=True)
check("还没落盘（文件不存在）", os.path.exists(p), False)
check("dumps 计数为 0", t.dumps, 0)

print("\n=== ② `dump()` 才落盘，一行 JSON ===")
t.dump("speech_during_playback", state="speaking", armed=False, gap_ms=120)
check("文件出现了", os.path.exists(p), True)
lines = [json.loads(x) for x in open(p) if x.strip()]
check("只写了一行", len(lines), 1)
r = lines[0]
check("reason 对", r["reason"], "speech_during_playback")
check("extra 字段带上了", (r["state"], r["armed"], r["gap_ms"]), ("speaking", False, 120))
check("dumps 计数 +1", t.dumps, 1)

print("\n=== ③ 样本：时间相对、最后一条是 0、字段是 [dt, rms, speaking, playing] ===")
s = r["samples"]
check("样本数 = 喂进去的条数", len(s), 30)
check("最后一条 dt=0（= 落盘那一刻）", s[-1][0], 0)
check("时间**非递减**（连调会落在同一毫秒，所以不能要求严格递增）",
      all(s[i][0] <= s[i + 1][0] for i in range(len(s) - 1)), True)
check("每条 4 个字段", all(len(x) == 4 for x in s), True)
check("speaking 是 0/1", set(x[2] for x in s), {0, 1})
check("rms 放大成整数（省空间）", all(isinstance(x[1], int) for x in s), True)
check("最后一条的 speaking=1（i>20 都算说话）", s[-1][2], 1)

print("\n=== ④ 环形缓冲有界（不能无限吃内存）===")
t2, _ = new()
for i in range(int(_PRE_SEC * _SAMPLE_HZ) + 500):
    t2.note(0.001, False, False)
check("被 maxlen 截住", len(t2._buf), int(_PRE_SEC * _SAMPLE_HZ))

print("\n=== ⑤ 关掉时**完全不工作**（不记、不写） ===")
t3, p3 = new("off.jsonl")
t3.enabled = False
t3.note(0.5, True, True)
check("没记", len(t3._buf), 0)
check("dump 返回 None", t3.dump("x"), None)
check("没建文件", os.path.exists(p3), False)

print("\n=== ⑥ 写盘失败**绝不抛**（诊断工具不能把主链路带崩）===")
t4 = BargeinTrace(path="/proc/不存在的目录/nope.jsonl")   # 必定写不进去
t4.note(0.1, True, True)
try:
    t4.dump("x")
    ok = True
except Exception as e:                                    # noqa: BLE001
    ok = False
    print(f"     抛了 {type(e).__name__}: {e}")
check("没抛异常", ok, True)

print("\n=== ⑦ 没有任何样本时 dump 是 no-op（不写空记录） ===")
t5, p5 = new("empty.jsonl")
check("返回 None", t5.dump("x"), None)
check("没建文件", os.path.exists(p5), False)

print("\n=== ⑧ 超过上限会轮转（保留 .1） ===")
import jarvis_voice.bargein_trace as B          # noqa: E402
t6, p6 = new("big.jsonl")
t6.note(0.1, False, False)
old = B.MAX_BYTES
B.MAX_BYTES = 10                                # 弄小，方便触发
try:
    t6.dump("first")
    t6.dump("second")
finally:
    B.MAX_BYTES = old
check("主文件仍在", os.path.exists(p6), True)
check("轮转出了 .1", os.path.exists(p6 + ".1"), True)
# ⚠️ 轮转是「先把旧文件挪成 .1」，所以**第一条**应该在 .1 里、**第二条**在主文件里
_first = [json.loads(x)["reason"] for x in open(p6 + ".1") if x.strip()]
_now = [json.loads(x)["reason"] for x in open(p6) if x.strip()]
check("旧的挪进了 .1", _first, ["first"])
check("新的留在主文件", _now, ["second"])

print()
if FAIL:
    print(f"❌ {len(FAIL)} 项失败：{FAIL}")
    sys.exit(1)
print("✅ 全部通过")
