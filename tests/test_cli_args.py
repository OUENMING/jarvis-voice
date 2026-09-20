#!/usr/bin/env python3
"""入口参数校验 + 仪表盘生命周期回归测试（2026-09-20）。

两处都是 **`ocr` 报的 high**：

**① 参数拼错会被静默忽略。** 原来全是 `in argv` 判断，`--dashbord` / `--speaker-aecd`
这类拼错无声无息 —— 最坏的情况是 `--fressh` 拼错后**恢复了被污染的会话链**
（正是 `__main__.py` docstring 里警告的那件事）。现在认不得的参数直接报错退出。

**② 仪表盘起不来时用户看不见。** 原来 `serve()` **先 print**「[仪表盘] http://…」
**再** `uvicorn.run()` —— 端口被占时 uvicorn 在线程里抛、异常没人接，
而用户已经看到那句"已启动"。现在改成 `wait_ready()` 确认真起来了（且端口真能连上）
才宣告；`__main__` 收尾还会 `shutdown()`（否则它的线程会继续往已关闭的 BUS emit）。

⚠️ 仪表盘用**随机空闲端口**，不碰生产用的 8848。
"""
import os
import socket
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAIL = []


def check(label, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {label}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(label)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

print("=== ① 认不得的参数要报错退出（而不是静默忽略）===")
for bad in ("--dashbord", "--fressh", "--speaker-aecd", "--nope"):
    r = subprocess.run([sys.executable, "-m", "jarvis_voice", bad],
                       cwd=ROOT, capture_output=True, text=True, timeout=60)
    ok = r.returncode == 2 and "认不得" in r.stdout
    print(f"  {'✅' if ok else '❌'} {bad} → 退出码 {r.returncode}，"
          f"提示 {'有' if '认不得' in r.stdout else '没有'}")
    if not ok:
        FAIL.append(f"argc {bad}")

print("\n=== ② `--help` 仍要正常（退出码 0）===")
r = subprocess.run([sys.executable, "-m", "jarvis_voice", "--help"],
                   cwd=ROOT, capture_output=True, text=True, timeout=60)
check("--help 退出码", r.returncode, 0)
check("--help 有内容", "环境变量" in r.stdout, True)

print("\n=== ③ 仪表盘：起来之后 `wait_ready` 为真，且端口真能连上 ===")
from jarvis_voice import dashboard          # noqa: E402

port = free_port()
th = threading.Thread(target=dashboard.serve,
                      kwargs={"host": "127.0.0.1", "port": port}, daemon=True)
th.start()
check("wait_ready", dashboard.wait_ready(timeout=8), True)

connected = False
try:
    s = socket.create_connection(("127.0.0.1", port), timeout=2)
    s.close()
    connected = True
except OSError:
    pass
check("端口可连（不能只看 started 标志）", connected, True)

print("\n=== ④ shutdown 之后必须真的关闭 ===")
dashboard.shutdown()
time.sleep(1.5)
still_open = True
try:
    s = socket.create_connection(("127.0.0.1", port), timeout=1)
    s.close()
except OSError:
    still_open = False
check("已关闭", still_open, False)

print("\n=== ⑤ 端口被占时 `wait_ready` 必须返回 False（不能说已启动）===")


def _safe(fn, *a, **k):
    try:
        fn(*a, **k)
    except Exception:                        # noqa: BLE001
        pass


holder = socket.socket()
holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
holder.bind(("127.0.0.1", 0))
holder.listen(1)
p2 = holder.getsockname()[1]
import jarvis_voice.dashboard as D           # noqa: E402
D._server = None
threading.Thread(target=_safe, args=(D.serve, "127.0.0.1", p2), daemon=True).start()
check("被占时 wait_ready", D.wait_ready(timeout=8), False)
holder.close()

print()
if FAIL:
    print(f"❌ {len(FAIL)} 项失败：{FAIL}")
    sys.exit(1)
print("✅ 全部通过")
