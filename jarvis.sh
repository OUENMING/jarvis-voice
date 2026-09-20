#!/usr/bin/env bash
# jarvis-voice 启停脚本
#
# 为什么需要它：手动 `pkill -f "python -m jarvis_voice"` **只杀当次启动的进程**——
# 更早通过后台启动的实例会变成孤儿继续运行，而且**还在抢麦克风**。
# 真机踩过：同时跑着 3 个实例，用户对着旧代码那个说话，以为新功能没生效。
#
# 精确匹配（不会误杀你自己在用的 Claude Code）：
#   · 应用进程   → `python -m jarvis_voice`
#   · 脑子进程   → 命令行含 `mcp-jarvis.local.json`（只有我们的 bridge 会带这个）
set -uo pipefail
cd "$(dirname "$0")"

# ⚠️ 匹配模式用 `-m jarvis_voice`，**不能**用 `python -m jarvis_voice`：
# macOS 上实际进程名是 `.../MacOS/Python`（**大写 P**），小写模式永远匹配不到，
# 结果只杀掉了 zsh 包装进程，真正的应用变成**孤儿继续跑**。
# 真机踩过：同时跑着 3 个实例，用户对着旧代码那个说话，以为新功能没生效。
APP_PAT='-m jarvis_voice'
BRAIN_PAT='mcp-jarvis.local.json'

pids() { pgrep -f -- "$1" 2>/dev/null || true; }

stop_all() {
  local n=0
  for p in $(pids "$APP_PAT"); do kill "$p" 2>/dev/null && n=$((n+1)); done
  [ "$n" -gt 0 ] && { echo "  已发 SIGTERM 给 $n 个应用进程…"; sleep 2; }
  for p in $(pids "$APP_PAT") $(pids "$BRAIN_PAT"); do kill -9 "$p" 2>/dev/null && echo "  强杀 $p"; done
  # 兜底：释放仪表盘端口。
  # ⚠️ 必须 `-sTCP:LISTEN` 只取**监听者**（ocr 2026-09-20 报的）：
  #    裸 `lsof -ti:8848` 会连**已建立连接的客户端**一起列出来 —— 也就是**浏览器**。
  #    原来的无差别 `kill -9` 会在 `stop` 时把用户正开着的浏览器标签页（或浏览器本身）杀掉。
  for p in $(lsof -ti:8848 -sTCP:LISTEN 2>/dev/null || true); do
    kill -9 "$p" 2>/dev/null && echo "  强杀占用 8848 的监听进程 $p"
  done
  sleep 1
}

status() {
  local a b
  a=$(pids "$APP_PAT" | wc -l | tr -d ' ')
  b=$(pids "$BRAIN_PAT" | wc -l | tr -d ' ')
  echo "  应用进程: $a   脑子进程: $b"
  if [ "$a" -gt 1 ]; then
    echo '  ⚠️ 有多个实例！这正是「旧模型还在说话」的成因，建议 stop 后重启'
  fi
  pids "$APP_PAT" | while read -r p; do echo "    PID $p"; done
  lsof -ti:8848 >/dev/null 2>&1 && echo "  仪表盘: 8848 已监听" || echo "  仪表盘: 未启动"
}

start() {
  status | head -1
  echo "  清理旧实例…"
  stop_all
  echo "  启动：$*"
  PYTHONUNBUFFERED=1 exec .venv/bin/python -m jarvis_voice "$@"
}

case "${1:-start}" in
  start)   shift || true; start "$@" ;;
  stop)    stop_all; echo "已停止" ;;
  restart) shift || true; stop_all; start "$@" ;;
  status)  status ;;
  *) echo "用法: $0 {start|stop|restart|status} [jarvis 参数...]"
     echo "例:   $0 start --speaker-aec --dashboard   # 免提+软件AEC（可插话打断）"
     echo "      $0 start --speaker --dashboard       # 免提无AEC（对照）"
     echo "      $0 start --dashboard                 # 耳机" ;;
esac
