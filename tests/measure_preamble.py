#!/usr/bin/env python3
"""P0 验收：数「工具回合里，CC 在首个工具调用之前说过话」的比例。

背景（docs/PLAN-LATENCY-20260919.md §1.4）：
  工具回合中位首句 15.9s —— 因为 CC 的默认人格是「别废话，直接干」
  （claude-code#9128 被 CLOSED/NOT_PLANNED）。但实测 26 个回合里有 2 次
  它**主动说了承接语**（「好，我上去看看。」）→ 机制是通的，只是不可靠。

  基线（加规则 14 之前）：**2/26 = 8%**
  目标：**>50%**

用法：跑一场带工具调用的对话，然后
    .venv/bin/python tests/measure_preamble.py
"""
import json
import os
import sys

LOG = os.path.expanduser("~/.jarvis/events.jsonl")


def load_sessions(path: str) -> list[list[dict]]:
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    sess, cur = [], []
    for r in rows:
        if r.get("kind") == "session_start":
            if cur:
                sess.append(cur)
            cur = [r]
        else:
            cur.append(r)
    if cur:
        sess.append(cur)
    return sess


def main() -> int:
    if not os.path.exists(LOG):
        print(f"找不到 {LOG}")
        return 1
    sess = load_sessions(LOG)

    total = 0
    with_pre = 0
    examples: list[tuple[str, str, int]] = []

    for s in sess:
        i = 0
        while i < len(s):
            if s[i].get("kind") != "user":
                i += 1
                continue
            user_text = s[i].get("text", "")
            j = i + 1
            n_tools = 0
            pre: list[str] = []
            while j < len(s) and s[j].get("kind") != "user":
                k = s[j].get("kind")
                if k == "tool":
                    n_tools += 1
                elif k == "sentence" and n_tools == 0:
                    pre.append(s[j].get("text", ""))
                j += 1
            if n_tools:
                total += 1
                if pre:
                    with_pre += 1
                    examples.append((user_text[:28], pre[0][:40], n_tools))
            i = j

    print("=" * 74)
    print(f"工具回合总数: {total}")
    # ⚠️ 除法**必须放在条件分支里面**（ocr 2026-09-20 报的）：f-string 是**先求值整个
    #    表达式**再选择分支的，`... if total else ...` 保护不了 `with_pre / total` ——
    #    没有工具回合时照样 `ZeroDivisionError`。而这是 P0 的验收脚本。
    if total:
        print(f"其中「首个工具之前说过话」: {with_pre}  ({with_pre / total * 100:.0f}%)")
    else:
        print("  没有工具回合")
    print()
    print("基线（规则 14 之前，2026-09-19）: 2/26 = 8%      目标: >50%")
    print("=" * 74)
    if examples:
        print("\n承接语样例：")
        for u, p, n in examples[:12]:
            print(f"  [{n:2d} 步] 用户「{u}」→ CC 先说「{p}」")
    else:
        print("\n⚠️ 一个都没有 —— 规则 14 没起作用，考虑走 P1（填充音改 tool 事件触发）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
