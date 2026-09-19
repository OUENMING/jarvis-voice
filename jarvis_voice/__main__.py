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


def main():
    argv = sys.argv[1:]
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        print("环境变量: JARVIS_AUDIO_MODE=headphones|speaker|speaker_aec  JARVIS_TTS=fish|say\n"
              "          FISH_VOICE=...  JARVIS_VAD=silero|ten-vad  JARVIS_ASR=sensevoice|fish\n"
              "          JARVIS_OUTPUT_DEVICE=MacBook   JARVIS_INPUT_DEVICE=MacBook\n"
              "            （真免提记得把输出也切到内置扬声器，否则麦克风听不到自己）")
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
        threading.Thread(target=dashboard.serve,
                         kwargs={"host": "127.0.0.1", "port": DASH_PORT},
                         daemon=True).start()

    try:
        orch.run()
    finally:
        from .events import BUS
        BUS.close()


if __name__ == "__main__":
    main()
