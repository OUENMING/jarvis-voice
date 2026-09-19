#!/usr/bin/env python3
"""
实验：stream-json control_request/interrupt 探针（P0 §1 路径 A 验证）

目的：一手确认——在 `--input-format stream-json` 常驻会话里，往 stdin 写
      {"type":"control_request","request_id":..,"request":{"subtype":"interrupt"}}
      能否中断在途 turn，且 (a) 进程存活 (b) 会话存活 (c) 中断后 result 事件长什么样。

不动 claude_bridge.py，纯探针。用法：.venv/bin/python test_interrupt_probe.py
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import json
import subprocess
import sys
import threading
import time
import uuid

MODEL = sys.argv[1] if len(sys.argv) > 1 else "haiku"

cmd = ["claude", "--input-format", "stream-json", "--output-format", "stream-json",
       "--include-partial-messages", "--verbose", "--model", MODEL,
       "--bare", "--dangerously-skip-permissions"]

print("[cmd]", " ".join(cmd), flush=True)
proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True, bufsize=1)

events: list[dict] = []
got_text = threading.Event()
t0 = time.time()


def stamp() -> str:
    return f"{time.time() - t0:6.2f}s"


def pump():
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            print(f"[stdout-raw] {line[:200]}", flush=True)
            continue
        events.append(ev)
        t = ev.get("type")
        sub = ev.get("subtype", "")
        if t == "assistant":
            for blk in ev.get("message", {}).get("content", []):
                if blk.get("type") == "tool_use":
                    print(f"[{stamp()}] << tool_use {blk.get('name')} "
                          f"{json.dumps(blk.get('input'), ensure_ascii=False)[:160]}", flush=True)
            continue
        if t == "stream_event":
            se = ev.get("event", {})
            if se.get("type") == "content_block_delta" and se["delta"].get("type") == "text_delta":
                txt = se["delta"].get("text", "")
                print(f"[{stamp()}] <delta> {txt!r}", flush=True)
                got_text.set()
            continue
        print(f"[{stamp()}] << {t}{'/'+sub if sub else ''}  {json.dumps(ev, ensure_ascii=False)[:300]}",
              flush=True)


threading.Thread(target=pump, daemon=True).start()


def send(obj: dict):
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()


def user_msg(text: str):
    send({"type": "user",
          "message": {"role": "user", "content": [{"type": "text", "text": text}]},
          "parent_tool_use_id": None, "session_id": None})


# ---- 1) 第一轮：造一个真·长在途 turn（Bash sleep），方便中途打断 ----
print(f"\n[{stamp()}] >>> 第 1 轮：请求一个耗时命令（Bash sleep 30）", flush=True)
user_msg("先用 Bash 运行命令 `sleep 30`。等它跑完，再只回两个字：完成。")

# 等 tool_use 出现（说明命令已在跑），再多等 1.5s 让 turn 稳定处于在途
for _ in range(200):
    if any(e.get("type") == "assistant" for e in events):
        break
    time.sleep(0.05)
time.sleep(1.5)
rid = str(uuid.uuid4())
frame = {"type": "control_request", "request_id": rid,
         "request": {"subtype": "interrupt"}}
print(f"\n[{stamp()}] >>> 发送 interrupt 帧 request_id={rid[:8]}", flush=True)
print(f"           {json.dumps(frame, ensure_ascii=False)}", flush=True)
send(frame)

# 观察 15 秒（看是否有 aborted 的 result / 被取消的 tool 结果）
time.sleep(15)

print(f"\n[{stamp()}] >>> 第 2 轮：验证会话是否存活", flush=True)
user_msg("刚才被打断了。现在只回四个字：我还活着")
time.sleep(20)

proc.stdin.close()
try:
    proc.wait(timeout=5)
except subprocess.TimeoutExpired:
    proc.kill()

# ---- 汇总 ----
print("\n" + "=" * 60)
types = {}
for e in events:
    k = e.get("type") + ("/" + e["subtype"] if e.get("subtype") else "")
    types[k] = types.get(k, 0) + 1
print("事件类型统计:", json.dumps(types, ensure_ascii=False, indent=1))

print("\n--- 所有 result 事件全文（全字段） ---")
for e in events:
    if e.get("type") == "result":
        print(json.dumps({k: v for k, v in e.items() if k != "usage"}, ensure_ascii=False, indent=1))
        print("  usage keys:", list(e.get("usage", {}).keys()))

print("\n--- 所有 control_response 事件全文 ---")
for e in events:
    if e.get("type") == "control_response":
        print(json.dumps(e, ensure_ascii=False))

print("\n--- system/init 全文（含 capabilities?） ---")
for e in events:
    if e.get("type") == "system" and e.get("subtype") == "init":
        print("capabilities:", json.dumps(e.get("capabilities"), ensure_ascii=False))
        print("tools:", json.dumps(e.get("tools"), ensure_ascii=False))
        print("model:", e.get("model"), "| version:", e.get("claude_code_version"))

err = proc.stderr.read() if proc.stderr else ""
if err.strip():
    print("\n--- stderr ---\n", err[:1000])
print(f"\n[退出码] {proc.returncode}")
