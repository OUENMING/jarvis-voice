"""入口：python -m jarvis_voice [--speaker|--speaker-aec] [--fresh] [--dashboard] [--quiet]

  --speaker     免提模式（内置麦 + 扬声器，不用耳机）。**没有 AEC**，所以自动
                关闭打断并改为**半双工**（播出时不听麦克风）。仍可用仪表盘
                上的「打断」按钮手动打断。
  --speaker-aec 免提 + **软件 AEC**：能从麦克风里消掉我们自己播的声音，
                所以**免提也能插话打断**。实测本机稳态 ERLE 34–36 dB
                （docs/PROBE-AEC-RESULTS-20260919.md）。
                ⚠️ AEC 只消**本项目自己播的**回声；环境里别人的声音（网课、
                视频）它消不掉——那需要说话人分离，是另一条路。
  --fresh       从空白开始（**不恢复**上次对话）。要清掉被污染的链条时用它。
  --resume      （这是**默认行为**；保留开关仅为可读性）恢复上次会话。
                ⚠️ resume 是**链式**的——每次恢复都继承上一次全部上下文，
                一旦链条被污染会一直传下去（真机踩过），所以清理必须从 --fresh 起。
  --dashboard   同时启动本地可视化仪表盘 http://127.0.0.1:8848
                （只绑回环，不对局域网/公网暴露）
  --quiet       少打日志（仪表盘照常收事件）
"""
import os
import sys
import threading

from .config import Config
from .orchestrator import Orchestrator

DASH_PORT = 8848

# 认得的开关。⚠️ 显式列出来是为了**拼错时报错**而不是静默忽略
# （ocr 2026-09-20 报的：原来全是 `in argv` 判断，`--dashbord` / `--speaker-aecd`
#  这类拼错会被无声吃掉 —— 最坏的情况是 `--fressh` 拼错后**恢复了被污染的会话链**，
#  正是本文件 docstring 里警告的那件事）。
_KNOWN_FLAGS = {"-h", "--help", "--speaker", "--speaker-aec",
                "--fresh", "--resume", "--dashboard", "--quiet"}


def main():
    argv = sys.argv[1:]
    unknown = [a for a in argv if a.startswith("-") and a not in _KNOWN_FLAGS]
    if unknown:
        print(f"❌ 认不得的参数: {' '.join(unknown)}")
        print(f"   能用的是: {' '.join(sorted(_KNOWN_FLAGS))}")
        print("   （拼错会被静默忽略，所以这里直接报错 —— 用 --help 看说明）")
        return 2
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        print("环境变量: JARVIS_AUDIO_MODE=headphones|speaker|speaker_aec  JARVIS_TTS=fish|say\n"
              "          FISH_VOICE=...  JARVIS_VAD=silero|ten-vad  JARVIS_ASR=sensevoice|fish\n"
              "          JARVIS_OUTPUT_DEVICE=MacBook   JARVIS_INPUT_DEVICE=MacBook\n"
              "            （真免提记得把输出也切到内置扬声器，否则麦克风听不到自己的声音）\n"
              "          JARVIS_BARE=0   非 bare（拿到 skills + 懒加载工具，每轮 +156ms）\n"
              "          JARVIS_BRAIN_COMPACT_WINDOW=200000\n"
              "            脑的上下文窗口。⚠️ 不设 = 自动压缩**永不触发**（走代理时拿不到\n"
              "            服务端窗口表 → 判定函数直接放弃）。见 config.py 的注释。")
        return

    if "--speaker" in argv:
        os.environ["JARVIS_AUDIO_MODE"] = "speaker"   # 必须在 Config.load() 之前
    if "--speaker-aec" in argv:
        os.environ["JARVIS_AUDIO_MODE"] = "speaker_aec"
        # 免提的语义就是「内置扬声器 + 内置麦」。不锁输出的话，插着耳机启动时声音
        # 仍从耳机出 → 麦克风听不到自己 → AEC 没东西可消，等于白开。
        # （仪表盘切模式走 `set_mode()`，它本来就会强制内置设备，这里对齐。
        #   用 setdefault：用户显式设了 JARVIS_OUTPUT_DEVICE 就听他的。）
        os.environ.setdefault("JARVIS_OUTPUT_DEVICE", "MacBook")
    # 记忆/会话连续性：**默认恢复**上次会话（个人助手要跨天记住）。session.json 已隔离在
    # ~/.jarvis，且"清理上下文"会走 forget_saved_session 抹掉落盘 id（元命令已接）。
    # ⚠️ resume 是**链式**的：一旦链条被污染会一直传下去（真机踩过）。要从空白开始用 --fresh。
    if "--fresh" in argv:
        os.environ["JARVIS_RESUME"] = "0"
    else:
        os.environ.setdefault("JARVIS_RESUME", "1")

    cfg = Config.load()

    orch = Orchestrator(cfg, verbose="--quiet" not in argv)

    if "--dashboard" in argv:
        from . import dashboard
        dashboard.bind(orch)          # 控制按钮需要它

        def _dash():
            # ⚠️ 线程里的异常**必须自己报出来**（ocr 2026-09-20 报的）：
            # 原来这里直接 `target=dashboard.serve`，端口被占时 uvicorn 在线程里抛，
            # 主流程照常跑、用户以为仪表盘正常。
            try:
                dashboard.serve(host="127.0.0.1", port=DASH_PORT)
            except Exception as e:                       # noqa: BLE001
                print(f"[仪表盘] ⚠️ 起不来: {type(e).__name__}: {e}", flush=True)

        threading.Thread(target=_dash, daemon=True).start()
        if dashboard.wait_ready():
            print(f"[仪表盘] http://127.0.0.1:{DASH_PORT}  （仅本机可访问）", flush=True)
        else:
            print(f"[仪表盘] ⚠️ {DASH_PORT} 没能起来（端口被占？）—— 其余功能不受影响",
                  flush=True)

    try:
        orch.run()
    finally:
        # 收尾顺序：先请仪表盘退出（否则它的线程会继续往已关闭的 BUS 里 emit），
        # 再关事件总线。
        try:
            from . import dashboard
            dashboard.shutdown()
        except Exception:                                # noqa: BLE001
            pass
        from .events import BUS
        BUS.close()


if __name__ == "__main__":
    # ⚠️ 必须 `sys.exit(...)`：`main()` 认不得参数时 return 2，
    # 而入口写成裸 `main()` 会把退出码丢掉 —— 脚本没法靠退出码判断成败。
    sys.exit(main())
