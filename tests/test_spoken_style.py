#!/usr/bin/env python3
"""实验：CC 的"口语化"是 prompt 能解决，还是必须靠事后改写？

同一批问题，跑两套 system prompt：
  A = 宽松版（接近现状：简短、禁 markdown）
  B = 口语化强约束版（首句短、禁书面连接词、禁列表、允许语气词、数字口语读法）
对比输出的"书面语特征"和"保真度"。

用法：.venv/bin/python test_spoken_style.py
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import re
import time

from claude_bridge import ClaudeBridge

PROMPT_A = "你是语音助手。回复简短，禁 markdown。"

PROMPT_B = (
    "你是语音助手 Omen，用**口语**回答，因为你说的每句话都会被逐字朗读出来。规则：\n"
    "1. 第一句必须极短（≤12字），先给结论或应答，再展开。\n"
    "2. 禁止任何书面语结构：不要'首先/其次/最后/综上所述/值得注意的是/总的来说'。\n"
    "3. 禁止列表、编号、markdown、括号注释、emoji。\n"
    "4. 用说话的方式组织：短句、可停顿、必要时用'那个''就是''你懂的'这类语气词。\n"
    "5. 数字要按口语读法写：'三件'而不是'3 件'；'大概下午五点'而不是'17:00'。\n"
    "6. 需要列举时，用'第一…然后…还有个…'这种口头顺序词，不要用 1. 2. 3.。\n"
    "7. 有不确定就直说'我不太确定'，不要编。\n"
)

QUESTIONS = [
    ("列举型", "我这周要做的三件事是什么？你随便编三个合理的例子。"),
    ("解释型", "用一两句话解释一下复利是什么。"),
    ("任务汇报型", "假设你刚改完一个文件：路径是 /Users/owen/proj/config.py，把超时从 30 改成 60，测试 3 个通过了。请汇报。"),
]

WRITTEN_MARKERS = ["首先", "其次", "最后", "综上所述", "总的来说", "值得注意", "此外",
                   "另外", "因此", "然而", "总之", "如下", "以下", "第一，", "第二，",
                   "1.", "2.", "3.", "- ", "* ", "#", "**", "`"]


def analyze(text: str, first_sentence: str) -> dict:
    hits = [m for m in WRITTEN_MARKERS if m in text]
    return {
        "首句字数": len(first_sentence),
        "书面语标记": hits or "无",
        "换行数": text.count("\n"),
        "总字数": len(text),
    }


def run(prompt: str, label: str):
    print(f"\n{'='*70}\n### {label}\n{'='*70}")
    b = ClaudeBridge(system_prompt=prompt, model="haiku")
    b.start()
    for qlabel, q in QUESTIONS:
        print(f"\n--- [{qlabel}] 问：{q}")
        sentences, t0 = [], time.time()
        for ev in b.ask(q):
            if ev["type"] == "sentence":
                sentences.append(ev["text"])
        full = "".join(sentences)
        first = sentences[0] if sentences else ""
        ms = (time.time() - t0) * 1000
        print(f"  首句({ms:.0f}ms): {first}")
        print(f"  全文: {full}")
        print(f"  诊断: {analyze(full, first)}")
    b.stop()


if __name__ == "__main__":
    run(PROMPT_A, "A 宽松版（现状）")
    run(PROMPT_B, "B 口语化强约束版")
