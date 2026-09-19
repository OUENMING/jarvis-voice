#!/usr/bin/env python3
"""
验证 claude_bridge 的中断能力（P0 §1 落到实现后）。

三问：
  1. 另一线程调 interrupt() 能否**不被 turn 锁挡住**地打断在途 turn？
  2. 被打断时 ask() 是否 yield {"type":"interrupted", terminal_reason:"aborted_*"}？
  3. 会话是否存活（下一轮正常 done）？
用法：.venv/bin/python test_bridge_interrupt.py
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import threading
import time

from claude_bridge import ClaudeBridge

b = ClaudeBridge(system_prompt="你是语音助手，回复纯口语短句，禁 markdown。", model="haiku")
pid = b.start()
print(f"[start] pid={pid}")

sent_flag = {}


def interrupt_later(delay: float = 4.0):
    time.sleep(delay)
    ok = b.interrupt()
    sent_flag["ok"] = ok
    print(f"\n{'='*50}\n[interrupt] 发出={ok}\n{'='*50}")


th = threading.Thread(target=interrupt_later, daemon=True)
th.start()

print("\n>>> 第 1 轮（应被打断）: 让它跑 sleep 30")
t0 = time.time()
for ev in b.ask("先用 Bash 运行命令 `sleep 30`，命令跑完后再只回两个字：完成"):
    dt = (time.time() - t0) * 1000
    if ev["type"] == "sentence":
        print(f"  [{dt:6.0f}ms] sentence: {ev['text'][:60]!r}")
    elif ev["type"] == "tool":
        print(f"  [{dt:6.0f}ms] tool: {ev['name']} {str(ev.get('input'))[:80]}")
    elif ev["type"] == "interrupted":
        print(f"  [{dt:6.0f}ms] ★ interrupted  terminal_reason={ev.get('terminal_reason')}")
    elif ev["type"] == "done":
        print(f"  [{dt:6.0f}ms] done (未被打断?) ttft={ev.get('ttft')}")
    elif ev["type"] == "error":
        print(f"  [{dt:6.0f}ms] error: {ev.get('error')} {ev.get('detail')}")
th.join(timeout=3)

print("\n>>> 第 2 轮（验证会话存活）")
t0 = time.time()
got = []
for ev in b.ask("刚才被打断了。现在只回四个字：我还活着"):
    dt = (time.time() - t0) * 1000
    if ev["type"] == "sentence":
        got.append(ev["text"])
        print(f"  [{dt:6.0f}ms] sentence: {ev['text']!r}")
    elif ev["type"] == "done":
        print(f"  [{dt:6.0f}ms] done ttft={ev.get('ttft')}ms cost=${ev.get('cost')}")
    elif ev["type"] == "interrupted":
        print(f"  [{dt:6.0f}ms] ★ interrupted (不该出现)")
    elif ev["type"] == "error":
        print(f"  [{dt:6.0f}ms] error: {ev.get('error')}")

b.stop()
print("\n[汇总]")
print("  capabilities:", b.capabilities)
print("  interrupt 发出成功:", sent_flag.get("ok"))
print("  第 2 轮是否拿到回复:", bool("".join(got).strip()), "->", "".join(got).strip()[:40])
