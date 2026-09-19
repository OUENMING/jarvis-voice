"""SenseVoice ASR+情感 识别测试：用模型自带 wav 验证输出格式"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import re
import time
import wave

import numpy as np
import sherpa_onnx

MODEL_DIR = "models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"

def load_model():
    t0 = time.perf_counter()
    asr = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=f"{MODEL_DIR}/model.int8.onnx",
        tokens=f"{MODEL_DIR}/tokens.txt",
        use_itn=True,
        num_threads=4,
    )
    print(f"模型加载: {time.perf_counter()-t0:.2f}s")
    return asr

EMOTIONS = {"HAPPY", "SAD", "ANGRY", "NEUTRAL", "FEARFUL", "DISGUSTED", "SURPRISED"}

def parse(raw: str):
    tags = re.findall(r"<\|([^|]+)\|>", raw)
    emotion = next((t for t in tags if t in EMOTIONS), "NEUTRAL")
    text = re.sub(r"<\|[^|]+\|>", "", raw).strip()
    return text, emotion

def read_wav(path):
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return data, sr

def main():
    asr = load_model()
    import glob
    wavs = sorted(glob.glob(f"{MODEL_DIR}/test_wavs/*.wav"))
    for w in wavs[:3]:
        data, sr = read_wav(w)
        t0 = time.perf_counter()
        s = asr.create_stream()
        s.accept_waveform(sr, data)
        asr.decode_stream(s)
        raw = s.result.text
        dt = time.perf_counter() - t0
        text, emo = parse(raw)
        print(f"\n[{w.split('/')[-1]}] {len(data)/sr:.1f}s 音频 → 识别耗时 {dt*1000:.0f}ms (RTF {dt/(len(data)/sr):.2f})")
        print(f"  原始: {raw}")
        print(f"  文本: {text}")
        print(f"  情感: {emo}")

if __name__ == "__main__":
    main()
