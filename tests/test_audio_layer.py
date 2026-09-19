#!/usr/bin/env python3
"""音频层验证：VAD → ASR。

  .venv/bin/python test_audio_layer.py            # 文件模式（用 SenseVoice 自带测试音频）
  .venv/bin/python test_audio_layer.py --mic      # 实时麦克风（对着耳机麦说话，按 Ctrl+C 退出）
  .venv/bin/python test_audio_layer.py --devices  # 列出音频设备

文件模式额外构造一个"两句 + 中间静音"的合成样本，专门验证**停顿切分**。
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import glob
import sys
import time
import wave

import numpy as np

from jarvis_voice.asr import SenseVoiceASR
from jarvis_voice.audio_io import MicStream, input_devices, output_devices
from jarvis_voice.config import Config
from jarvis_voice.vad import VadGate

WAV_DIR = "models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/test_wavs"


def read_wav(path: str) -> np.ndarray:
    with wave.open(path) as f:
        assert f.getframerate() == 16000, f"非 16k: {f.getframerate()}"
        return np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16).copy()


def run_vad_asr(cfg, pcm16: np.ndarray, label: str, vad: VadGate, asr: SenseVoiceASR):
    """喂 int16 音频 → VAD 切段 → 逐段 ASR。"""
    f32 = pcm16.astype(np.float32) / 32768.0
    t0 = time.perf_counter()
    utts = vad.accept(f32)
    utts += vad.flush()
    vad_ms = (time.perf_counter() - t0) * 1000
    print(f"\n[{label}] {len(pcm16)/16000:.2f}s 音频 → VAD {len(utts)} 段 ({vad_ms:.0f}ms)")
    for i, u in enumerate(utts):
        if len(u) / 16000 < cfg.asr_min_utt_sec:
            print(f"  段{i}: {len(u)/16000:.2f}s 太短, 跳过")
            continue
        r = asr.transcribe(u)
        print(f"  段{i} [{len(u)/16000:5.2f}s] ({r.latency_ms:4.0f}ms) "
              f"情感={r.emotion:8s} 文本: {r.text!r}")
    vad.reset()


def file_mode(cfg):
    vad, asr = VadGate(cfg), SenseVoiceASR(cfg)
    wavs = sorted(glob.glob(f"{WAV_DIR}/*.wav"))
    print(f"加载完成 | VAD={cfg.vad_backend} 窗={cfg.vad_window} | 测试音频 {len(wavs)} 个")

    for w in wavs:
        run_vad_asr(cfg, read_wav(w), w.split("/")[-1], vad, asr)

    # 合成：中文 + 1.2s 静音 + 日语 —— 验证停顿切分
    sil = np.zeros(int(1.2 * 16000), dtype=np.int16)
    combo = np.concatenate([read_wav(f"{WAV_DIR}/zh.wav"), sil, read_wav(f"{WAV_DIR}/ja.wav")])
    run_vad_asr(cfg, combo, "合成: zh + 1.2s静音 + ja", vad, asr)


def mic_mode(cfg):
    print("输入设备:")
    for i, name, ch in input_devices():
        print(f"  [{i}] {name} ({ch}ch)")
    vad, asr = VadGate(cfg), SenseVoiceASR(cfg)
    print(f"\n开始监听 (VAD={cfg.vad_backend})。说话→停顿会自动切段识别。Ctrl+C 退出。\n")
    with MicStream(cfg) as mic:
        t_last = time.time()
        try:
            while True:
                chunk = mic.read(timeout=0.5)
                if chunk is None:
                    continue
                for u in vad.accept(chunk):
                    if len(u) / 16000 < cfg.asr_min_utt_sec:
                        continue
                    r = asr.transcribe(u)
                    if r.text:
                        print(f"[{time.time()-t_last:5.1f}s] 情感={r.emotion:8s} "
                              f"({r.latency_ms:4.0f}ms) 你: {r.text}")
                    t_last = time.time()
        except KeyboardInterrupt:
            print("\n[退出]")
        if mic.dropped:
            print(f"[注意] 队列丢帧 {mic.dropped} 次（处理跟不上采集）")


def main():
    cfg = Config.load()
    if "--devices" in sys.argv:
        print("输入设备:")
        for i, n, c in input_devices():
            print(f"  [{i}] {n} ({c}ch)")
        print("输出设备:")
        for i, n, c in output_devices():
            print(f"  [{i}] {n} ({c}ch)")
        return
    if "--mic" in sys.argv:
        mic_mode(cfg)
    else:
        file_mode(cfg)


if __name__ == "__main__":
    main()
