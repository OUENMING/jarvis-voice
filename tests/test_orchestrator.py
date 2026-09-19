#!/usr/bin/env python3
"""主循环验证（**不需要麦克风**）：把一段语音直接注入 utt_q，跑通
   ASR → CC → 切句 → 清洗 → TTS → 播放，并验证打断。

  .venv/bin/python test_orchestrator.py          # 正常一轮
  .venv/bin/python test_orchestrator.py --interrupt  # 中途打断
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import sys
import time
import wave

import numpy as np

from jarvis_voice.config import Config
from jarvis_voice.orchestrator import Orchestrator

WAV = "models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/test_wavs/zh.wav"


def load_utt() -> np.ndarray:
    with wave.open(WAV) as f:
        return np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16).copy()


def main():
    cfg = Config.load()
    orch = Orchestrator(cfg)
    orch.start()
    time.sleep(0.5)

    utt = load_utt()
    print(f"\n>>> 注入语音段 {len(utt)/cfg.sample_rate:.2f}s（内容：SenseVoice 测试音频）")
    orch.utt_q.put(utt)

    if "--survive" in sys.argv:
        # 复现真机 bug：打断一次后，BrainThread 是否还活着（旧代码 return 把线程干掉了）
        for _ in range(600):
            if orch.player.played_seconds() > 0.5:
                break
            time.sleep(0.05)
        orch._do_interrupt()
        t_after_first = orch.session.turn_id
        print(f"\n>>> 已打断，turn_id={t_after_first}")
        time.sleep(0.8)
        print(">>> 注入第二段语音，看线程是否还活着…")
        orch.utt_q.put(utt)
        for _ in range(800):
            if orch.session.turn_id > t_after_first and orch.session.state.value == "idle":
                break
            time.sleep(0.05)
        ok = orch.session.turn_id > t_after_first
        print(f"\n>>> turn_id={orch.session.turn_id}（打断时={t_after_first}）")
        print(">>> " + ("✅ BrainThread 存活，打断后仍能继续工作"
                        if ok else "❌ BrainThread 已死（bug 复现）"))
    elif "--interrupt" in sys.argv:
        # 等**真的播出声**（>0.5s）再打断 —— 否则只是在"思考期/刚入队"打断，测不到播放中打断
        for _ in range(600):
            if orch.player.played_seconds() > 0.5:
                break
            time.sleep(0.05)
        print(f"\n>>> 状态={orch.session.state.value}，已播 {orch.player.played_seconds():.2f}s → 触发打断")
        orch._do_interrupt()
        time.sleep(2.0)
        print(f">>> 打断后状态={orch.session.state.value}，缓冲 {orch.player.buffered_seconds():.3f}s")
    else:
        # ① 先等本轮**开始**（否则一开始就是 IDLE，会立刻误判为完成）
        for _ in range(400):
            if orch.session.state.value != "idle":
                break
            time.sleep(0.05)
        # ② 再等本轮**结束**
        for _ in range(1200):
            if orch.session.state.value == "idle" and not orch.player.is_playing():
                break
            time.sleep(0.05)
        time.sleep(0.3)
        print(f"\n>>> 完成，状态={orch.session.state.value}，共播 {orch.player.played_seconds():.2f}s")

    orch.stop()
    print(f"\n[汇总] 打断次数={orch.session.interrupts} | underrun={orch.player._underruns}")


if __name__ == "__main__":
    main()
