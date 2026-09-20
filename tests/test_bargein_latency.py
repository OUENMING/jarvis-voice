#!/usr/bin/env python3
"""打断延迟回归测试（2026-09-20）。

背景：真机反馈「打断的灵敏度还是有点差」。已有日志显示打断**每次都在语音起点
触发了**（`interrupt` 事件总早于对应的 `user` 转写），所以问题不在触发条件、
而在触发**延迟**。

离线量化（本文件就是在锁这个数）：
  感知延迟 = 语音起点 → `vad.speaking` 翻 True → `interrupt_confirm_ms` 确认窗
  旧配置（confirm=300）→ **600ms**
  新配置（confirm=100）→ **400ms**

判据来源（全部离线，不开任何音频设备 —— 见全局记忆
`audio-probe-device-contention`）：
  1. `is_speech_detected()` **本身就要等 `vad_min_speech`(0.25s) 连续语音**
     （实测：min_speech 0.25→+300ms / 0.15→+200ms / 0.05→+100ms）
     → 那一层确认**已经存在**，`interrupt_confirm_ms` 是多余的第二次确认。
  2. 它本来要防的瞬态噪声，Silero 自己就拦得住：60–600ms 的宽带噪声爆发
     （30× 噪声底）在任何 min_speech 下**都不会**让 `is_speech_detected()` 翻 True。

⚠️ 用例 1 在旧配置（`interrupt_confirm_ms=300`）下**必须失败**（red-green）。
"""
import dataclasses
import os
import subprocess
import sys
import tempfile
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis_voice.config import Config          # noqa: E402
from jarvis_voice.vad import VadGate            # noqa: E402

SR = 16000
BLOCK = 1600            # = cfg.mic_blocksize，100ms
ONSET_S = 2.0           # 合成流里语音的真实起点
LATENCY_BUDGET_MS = 450  # 旧 600 会挂，新 400 会过

FAIL = []


def check(label, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {label}: got={got} want={want}")
    if not ok:
        FAIL.append(label)
    return ok


def speech_wav(path: str) -> str:
    """用 macOS `say` 现场合成一段中文语音（不播放，纯写文件 → 不碰音频设备）。"""
    if os.path.exists(path):
        return path
    aiff = path + ".aiff"
    subprocess.run(["say", "-v", "Tingting", "-o", aiff,
                    "喂，等一下，我想问你一个问题"], check=True)
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000",
                    "-c", "1", aiff, path], check=True)
    os.unlink(aiff)
    return path


def load_wav(path: str) -> np.ndarray:
    with wave.open(path) as w:
        assert w.getframerate() == SR, w.getframerate()
        return np.frombuffer(w.readframes(w.getnframes()),
                             dtype=np.int16).astype(np.float32) / 32768.0


def noise(n: int, rng) -> np.ndarray:
    return rng.normal(0, 0.002, n).astype(np.float32)      # 安静房间底噪


def bargein_latency(cfg: Config, sig: np.ndarray, onset_s: float):
    """复刻 `orchestrator.py` 主循环的打断状态机，返回「触发时刻 − 语音起点」。

    ⚠️ 必须与 orchestrator.py:352-376 保持一致；那边改了这里也要改。
    """
    gate = VadGate(cfg)
    t = 0.0
    silence_since = None
    prev_silence = None
    burst_started_at = None
    fired_for_this_speech = False
    turn_started_at = 0.0        # 假装正在播报：本轮起点在流开头
    for i in range(0, len(sig) - BLOCK + 1, BLOCK):
        gate.accept(sig[i:i + BLOCK])
        speaking = gate.speaking
        now = t
        if not speaking:
            if silence_since is None:
                silence_since = now
            burst_started_at = None
            fired_for_this_speech = False
        else:
            silence_since = None
            if burst_started_at is None:
                if (prev_silence is None
                        or (now - prev_silence) * 1000 >= cfg.interrupt_min_gap_ms):
                    burst_started_at = now
            elif (cfg.barge_in
                  and burst_started_at >= turn_started_at
                  and not fired_for_this_speech
                  and (now - burst_started_at) * 1000 >= cfg.interrupt_confirm_ms):
                return (now - onset_s) * 1000
        prev_silence = silence_since if silence_since is not None else prev_silence
        t += BLOCK / SR
    return None


def main():
    cfg = Config.load()
    print(f"配置： min_speech={cfg.vad_min_speech}s  confirm={cfg.interrupt_confirm_ms}ms  "
          f"gap={cfg.interrupt_min_gap_ms}ms  barge_in={cfg.barge_in}")

    tmp = os.path.join(tempfile.gettempdir(), "jarvis_bargein_test")
    os.makedirs(tmp, exist_ok=True)
    sp = load_wav(speech_wav(os.path.join(tmp, "speech.wav")))

    print("\n[1] 打断感知延迟（静音 2s → 真人语音）")
    rng = np.random.default_rng(12345)
    sig = np.concatenate([noise(int(ONSET_S * SR), rng), sp, noise(int(2 * SR), rng)])
    lat = bargein_latency(cfg, sig, ONSET_S)
    print(f"      实测：{'未触发' if lat is None else f'+{lat:.0f}ms'}"
          f"（预算 {LATENCY_BUDGET_MS}ms）")
    check("打断延迟 ≤ 预算", lat is not None and lat <= LATENCY_BUDGET_MS, True)

    print("\n[2] 瞬态噪声不得误触发打断（这是能把 confirm 压低的依据）")
    for dur_ms in (100, 250, 400, 600):
        n = int(dur_ms / 1000 * SR)
        burst = rng.normal(0, 0.06, n).astype(np.float32)   # 30× 底噪
        s = np.concatenate([noise(int(ONSET_S * SR), rng), burst, noise(int(2 * SR), rng)])
        got = bargein_latency(cfg, s, ONSET_S)
        check(f"{dur_ms}ms 噪声爆发不触发", got is None, True)

    print()
    if FAIL:
        print(f"❌ {len(FAIL)} 项失败：{FAIL}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
