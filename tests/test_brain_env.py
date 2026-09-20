#!/usr/bin/env python3
"""脑进程的环境变量回归测试（2026-09-20）。

锁住的是一件**静默失效**的事：不显式告诉 Claude Code「窗口有多大」，
**自动压缩根本不会武装** —— 走 cc-switch 代理拿不到服务端窗口表，
窗口来源落到 `auto`，判定函数第一道闸直接 return。

真机证据（2026-09-20）：
  · 项目历史上 **250+ 个脑会话，`compact_boundary` 与 `isCompactSummary` 统统为 0**；
    其中一个会话跨 73.2 小时 / 215 轮 / 累计 370,618 token，依然零压缩
  · 本机 debug 日志双向验证：生产配置 **0 条** `autocompact` 行；
    加上 `CLAUDE_CODE_AUTO_COMPACT_WINDOW` 后立刻出现
    `autocompact: level=ok effectiveWindow=180000`

⚠️ 不启动真进程：把 `subprocess.Popen` 换掉，只检查传进去的 env。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude_bridge import ClaudeBridge          # noqa: E402
import claude_bridge                            # noqa: E402
from jarvis_voice.config import Config          # noqa: E402

FAIL = []


def check(label, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {label}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(label)


class FakePopen:
    """只截住 env / cmd，不起进程。"""
    last = None

    def __init__(self, cmd, **kw):
        FakePopen.last = {"cmd": cmd, "env": kw.get("env") or {}}
        self.pid = 12345
        self.stdin = None
        self.stdout = iter(())          # 立刻 EOF → _pump 线程干净退出


def run_start(compact_window):
    saved = claude_bridge.subprocess.Popen
    claude_bridge.subprocess.Popen = FakePopen
    try:
        b = ClaudeBridge(model="haiku", bare=True, resume=False,
                         system_prompt="x", compact_window=compact_window)
        b.start()
        return dict(FakePopen.last["env"]), list(FakePopen.last["cmd"])
    finally:
        claude_bridge.subprocess.Popen = saved


print("=== ① 配了窗口 → 变量必须传进子进程 ===")
env, cmd = run_start(200000)
check("CLAUDE_CODE_AUTO_COMPACT_WINDOW 已设置",
      env.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW"), "200000")

print("\n=== ② 生产配置的默认值来自 config.py ===")
cfg = Config.load()
check("cfg.brain_compact_window 非 0（不设 = 永不压缩）",
      cfg.brain_compact_window > 0, True)
check("默认值", cfg.brain_compact_window, 200000)
env2, _ = run_start(cfg.brain_compact_window)
check("用生产配置也能武装",
      env2.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW"), str(cfg.brain_compact_window))

print("\n=== ③ 0 = 显式不设（留给「我就不要压缩」的逃生门）===")
env3, _ = run_start(0)
check("0 时不设该变量", "CLAUDE_CODE_AUTO_COMPACT_WINDOW" in env3, False)

print("\n=== ④ 别的环境变量不受影响（回归）===")
check("MAX_THINKING_TOKENS 仍在", env.get("MAX_THINKING_TOKENS"), "0")
check("--bare 仍在命令行里", "--bare" in cmd, True)

print()
if FAIL:
    print(f"❌ {len(FAIL)} 项失败：{FAIL}")
    sys.exit(1)
print("✅ 全部通过")
