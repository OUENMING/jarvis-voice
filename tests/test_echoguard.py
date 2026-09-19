#!/usr/bin/env python3
"""EchoGuard 的单元测试 —— 重点是**不误吃用户**：宁可漏判回声，不可吃掉真话。

用法：.venv/bin/python tests/test_echoguard.py
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import sys
import time

from jarvis_voice.echoguard import EchoGuard, lcs_len, normalize, similarity

# 助手刚刚念出去的一句（真实场景样本）
SPOKEN = "我把那个文件移到备份目录了，原目录里已经没有它。"

FAILS = 0


def check(name, heard, expect_hit, spoken=SPOKEN, guard=None):
    global FAILS
    g = guard or EchoGuard(threshold=0.75, min_len=6, window_s=12.0)
    if not guard:
        g.note_spoken(spoken)
    hit, score, match = g.check(heard)
    ok = hit == expect_hit
    if not ok:
        FAILS += 1
    print(f"{'✅' if ok else '❌'} {name}")
    print(f"   听到: {heard!r}")
    print(f"   判定: {'回声' if hit else '放行'} (期望 {'回声' if expect_hit else '放行'}) "
          f"| 相似度 {score:.2f} | 最像的已说文本 <<{match[:24]}>>")
    return g


print("=== 应当判为回声（True Positive）===")
check("逐字相同的完整回声", "我把那个文件移到备份目录了，原目录里已经没有它。", True)
check("回声 + ASR 错字（2 字）", "我把那个文件移道备份目录了，原目录里已经没由它。", True)
check("回声被 VAD 截断（只到中段）", "我把那个文件移到备份目录了", True)
check("回声只剩后半段（片段包含）", "原目录里已经没有它", True)
check("回声 + 标点丢失", "我把那个文件移到备份目录了原目录里已经没有它", True)

print()
print("=== 必须放行（True Negative）===")
check("用户真说的话（内容不同）", "帮我看一下今天的天气怎么样", False)
check("用户的话题相近但不是回声", "那个文件你挪到哪去了", False)
check("用户的反问", "你为什么把文件移走了", False)
check("空串", "", False)
check("纯标点", "。。。", False)

print()
print("=== 边界：短文本一律放行（min_len 保护）===")
check("用户说「好的」", "好的", False)
check("用户说「清空上下文」（5 字，可能撞上元命令）", "清空上下文", False)
check("用户说「停」", "停", False)
check("短回声「没有它」也不判", "没有它", False)

print()
print("=== 边界：时间窗 ===")
g = EchoGuard(threshold=0.75, min_len=6, window_s=0.2)
g.note_spoken("我把那个文件移到备份目录了，原目录里已经没有它。")
time.sleep(0.35)
hit, score, _ = g.check("我把那个文件移到备份目录了，原目录里已经没有它。")
ok = not hit
if not ok:
    FAILS += 1
print(f"{'✅' if ok else '❌'} 超出时间窗后不再判为回声（相似度 {score:.2f}）")

print()
print("=== 相似度函数的性质 ===")
PROPS = [
    ("自身相似度为 1", similarity("abcde", "abcde") == 1.0),
    ("完全不同为 0", similarity("abcde", "xyzuv") == 0.0),
    ("归一化去标点", normalize("好的，。") == "好的"),
    ("LCS 基本正确", lcs_len("abcd", "acbd") == 3),
    ("短的一侧决定分母（截断 → 1.0）", similarity("原目录里已经没有它", SPOKEN) == 1.0),
]
for name, ok in PROPS:
    if not ok:
        FAILS += 1
    print(f"{'✅' if ok else '❌'} {name}")

print()
print(f"{'全部通过' if FAILS == 0 else f'{FAILS} 个用例失败'}")
sys.exit(1 if FAILS else 0)
