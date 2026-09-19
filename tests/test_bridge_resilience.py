#!/usr/bin/env python3
"""bridge 韧性回归：这些场景曾让助手"突然不回应"。

  1. 中途 `break` 放弃 ask() 生成器 → 下一轮还能用吗？
     （旧版：生成器 finally 没跑 → _turn_lock 被永久持有 → 下次 ask 返回 "busy"）
  2. interrupt() 打断在途 turn → 下一轮还能用吗？
  3. 连续多轮不被打断 → 会话是否稳定复用

用法：.venv/bin/python test_bridge_resilience.py
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import time

from claude_bridge import ClaudeBridge

b = ClaudeBridge(system_prompt="你是语音助手，回复纯口语短句，禁 markdown。", model="haiku")
b.start()
fails = []


def drain(gen, limit=None):
    out = []
    for ev in gen:
        out.append(ev)
        if limit and len(out) >= limit:
            break
    return out


print("=== 1) 中途 break 放弃生成器，看看锁有没有释放 ===")
gen = b.ask("从 1 数到 40，每行一个数字。")
got = drain(gen, limit=3)
print(f"  消费了 {len(got)} 个事件后 break")
gen.close()                      # ← 模拟 orchestrator 的 break + finally: gen.close()
time.sleep(0.3)
evs = drain(b.ask("只回两个字：在的"))
text = "".join(e.get("text", "") for e in evs if e["type"] == "sentence")
busy = any(e.get("error", "").startswith("busy") for e in evs)
print(f"  下一轮结果: {text!r}  busy={busy}")
if busy or not text:
    fails.append("break 后锁未释放（下一轮 busy/无输出）")

print("\n=== 2) interrupt() 打断在途 turn，下一轮是否可用 ===")
import threading
t = threading.Thread(target=lambda: (time.sleep(2.5), b.interrupt()), daemon=True)
t.start()
evs = drain(b.ask("请用 Bash 运行 sleep 30，然后说完成"))
kinds = [e["type"] for e in evs]
print(f"  本轮事件类型: {kinds}")
t.join(timeout=3)
evs2 = drain(b.ask("只回四个字：我还活着"))
text2 = "".join(e.get("text", "") for e in evs2 if e["type"] == "sentence")
busy2 = any(e.get("error", "").startswith("busy") for e in evs2)
print(f"  下一轮结果: {text2!r}  busy={busy2}")
if busy2 or not text2:
    fails.append("interrupt 后锁未释放")

print("\n=== 3) 连续 3 轮 ===")
for i, q in enumerate(["回一个字：一", "回一个字：二", "回一个字：三"], 1):
    t0 = time.perf_counter()
    evs = drain(b.ask(q))
    txt = "".join(e.get("text", "") for e in evs if e["type"] == "sentence")
    print(f"  第{i}轮 {(time.perf_counter()-t0)*1000:.0f}ms -> {txt!r}")
    if not txt:
        fails.append(f"第{i}轮无输出")

b.stop()
print("\n" + "=" * 50)
print("❌ 失败: " + "; ".join(fails) if fails else "✅ 全部通过")
