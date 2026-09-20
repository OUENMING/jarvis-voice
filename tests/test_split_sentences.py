#!/usr/bin/env python3
"""切句器回归测试（2026-09-20）。

**背景**：`_split_sentences` 的 docstring 说修过两个缺陷。这是**第三个**，
从我自己的真机日志里挖出来的（`ocr` 没报、代码审查也没看出来）。

**机制**：消费掉一句后 `buf` 往往以 `\n` 开头（模型段落之间就是换行），
而 `\n` **在 `SENT_END` 里** → `buf.find('\n') == 0` → `min(idxs) == 0`
→ `0 < NEXT_SENT_MIN(4)` → **立刻 break**。`buf` 从此不再缩小，
后续每个 delta 都撞同一个 0 → **本轮剩下的内容永远切不开**，
最后在 `result` 处被 `if buf.strip(): yield …` **整块不切**地吐出去。

**真机证据**（`~/.jarvis/events.jsonl`，125 轮）：
**41 条 >120 字的 `sentence` 事件里，41 条全部是每轮的最后一句**，
且都含多个「。」与「\n」。位置统计是 `{'最后一句': 41}`，**零例外**。

**影响**：首句仍能提前出声（所以 `first_ms` 看起来正常），但**第二句往后要等
整段生成完才开始合成** —— 切句本来就是为了避免这件事。

⚠️ 用例 ②③④⑤ 在旧实现上**必须失败**（red-green）。纯字符串函数，无副作用。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude_bridge import ClaudeBridge          # noqa: E402

FAIL = []


def feed(chunks):
    """按 delta 逐块喂（真 bridge 就是这样调的）。返回 (切出的句子, 卡住的余量)。"""
    buf, out = "", []
    for c in chunks:
        buf += c
        sents, buf = ClaudeBridge._split_sentences(buf)
        out += sents
    return out, buf


def check(label, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {label}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(label)


print("=== ① 无换行：基线（旧实现也对）===")
out, rest = feed(["你好。", "这是第一句。", "这是第二句，长一点。", "第三句。"])
check("四句都切出来", out, ["你好。", "这是第一句。", "这是第二句，长一点。", "第三句。"])
check("没有卡住", rest, "")

print("\n=== ② 句号后紧跟换行 —— 真机最常见的输出形状（旧实现只切出 1 句）===")
out, rest = feed(["你好。", "\n这是第一段的内容。", "\n这是第二段的内容。", "\n第三段。"])
check("四句都切出来", out,
      ["你好。", "这是第一段的内容。", "这是第二段的内容。", "第三段。"])
check("没有卡住", rest, "")

print("\n=== ③ 换行落在块首 ===")
out, rest = feed(["嗯。", "\n我查了一下。", "\n结果是这样。"])
check("三句都切出来", out, ["嗯。", "我查了一下。", "结果是这样。"])

print("\n=== ④ 一次性喂整块（含换行）—— 修早期版本时这里还漏切 ===")
out, _ = feed(["问题不在季节，在纬度。银心在人马座那块。\n"
               "所以你想要的那种拱桥拍不到。\n但你换个目标就有了。"])
check("四句都切出来", out,
      ["问题不在季节，在纬度。", "银心在人马座那块。",
       "所以你想要的那种拱桥拍不到。", "但你换个目标就有了。"])

print("\n=== ⑤ 逐字符喂（真 delta 是逐字/逐块的）===")
out, rest = feed(list("你好。\n这是第一段。\n这是第二段。"))
check("三句都切出来", out, ["你好。", "这是第一段。", "这是第二段。"])
check("没有卡住", rest, "")

print("\n=== ⑥ 不丢内容：把所有切出的句子接起来 == 原文（去掉空白）===")
src = "你好。\n这是第一段的内容，有点长。\n\n第二段也有内容。最后一句没标点"
out, rest = feed(list(src))
joined = "".join(out) + rest
check("内容不多不少",
      "".join(joined.split()), "".join(src.split()))

print("\n=== ⑦ 门限的真实行为（记录一个既存怪癖，不是我这轮引进的）===")
print("   ⚠️ `_split_sentences` 是**每个 delta 调一次**，而 `sents` 是**每次调用内部的**")
print("      局部变量 —— 所以每次调用开始时 `sents` 都是空的 → `min_len` 恒取")
print("      `FIRST_SENT_MIN=1` → **`NEXT_SENT_MIN=4` 实际上从来没生效过**。")
print("      （后果：短句也会被当句发出去，比设计更碎一点。**不改** —— 改它要跨调用传状态，")
print("        而且会让短句的出声变晚，得先测过再动。）")
out, rest = feed(["嗯。", "好。"])
check("首句 ≥1 字即发", out[0], "嗯。")
check("后续句按**实际**行为也发了（NEXT_SENT_MIN 未生效）", out, ["嗯。", "好。"])

print("\n=== ⑧ 纯标点/空白块不该产出空句子 ===")
out, rest = feed(["。。。", "\n\n", "  "])
check("不产生空句", out, [])
check("也不留下垃圾", rest.strip(), "")

print("\n=== ⑨ 未收尾的半句要留在 rest 里等后续 delta ===")
out, rest = feed(["这是一句还没说完的话"])
check("不提前发", out, [])
check("留在 rest", rest, "这是一句还没说完的话")
out, rest = feed(["这是一个完整的句子。"])
check("补齐后才发", out, ["这是一个完整的句子。"])

print()
if FAIL:
    print(f"❌ {len(FAIL)} 项失败：{FAIL}")
    sys.exit(1)
print("✅ 全部通过")
