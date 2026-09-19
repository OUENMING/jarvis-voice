#!/usr/bin/env python3
"""AEC 可行性探针 —— 一次跑完，输出决定路线的全部数字。

设计对齐 docs/RESEARCH-AEC-20260919.md §3 的 P0，但补了三处：
  1. **用真实语音（zh.wav）当 far 激励**，不用 440Hz 纯音 ——
     AEC3 为语音设计，纯音是窄带 + 非典型，ERLE 会失真
  2. **先测噪声底**（不播不说）→ 因为真正的判据不是"残余多少 dB"，
     而是**"残余是否高于噪声底的 3 倍"**（= 项目 vad_min_snr 的门限口径）
  3. **把残余直接喂 SenseVoice** → 幻觉判定（三条分叉）

三个阶段：
  A. 静默基线（不播不说）→ 噪声底
  B. 只播不说            → near_raw（纯回声）/ AEC 后 / AEC+NS 后
  C. 双讲（播 + 你说）    → 保真（默认关，需 --double-talk）

用法：
    .venv/bin/python aec_probe.py                 # 跑 A+B（自动，约 10 秒）
    .venv/bin/python aec_probe.py --double-talk   # 加跑 C（提示后你念一句话）

会出声。跑之前确认音量。
"""
import argparse
import os
import sys
import wave

import numpy as np
import sounddevice as sd
from scipy.signal import resample_poly

from pywebrtc_audio import AudioProcessor, EchoCanceller

RATE = 16000
FRAME = 160                      # 10ms —— pywebrtc / WebRTC APM 的原生帧长
SENSE_DIR = os.path.expanduser(
    "~/jarvis-voice/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17")
# 项目口径：vad.py 的 _passes_gate() 用 need = max(floor * vad_min_snr, vad_min_rms)
VAD_MIN_SNR = 3.0
VAD_MIN_RMS = 0.012


def rms_i16(x: np.ndarray) -> float:
    x = x.astype(np.float64)
    return float(np.sqrt(np.mean(x * x))) if x.size else 0.0


def rms_f32_from_i16(x: np.ndarray) -> float:
    """把 int16 换算成 [-1,1] 域的 RMS —— 与项目 vad.py 的口径可比。"""
    return rms_i16(x) / 32768.0


def load_far(seconds: float = 6.0) -> np.ndarray:
    """取 zh.wav 当 far 激励，重采样到 16k，循环/裁剪到指定长度。"""
    p = os.path.join(SENSE_DIR, "test_wavs", "zh.wav")
    with wave.open(p, "rb") as w:
        sr, ch, n = w.getframerate(), w.getnchannels(), w.getnframes()
        a = np.frombuffer(w.readframes(n), dtype=np.int16)
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1).astype(np.int16)
    if sr != RATE:
        a = resample_poly(a.astype(np.float32), RATE, sr)
        a = np.clip(a, -32768, 32767).astype(np.int16)
    want = int(RATE * seconds)
    if a.size < want:
        reps = int(np.ceil(want / max(a.size, 1)))
        a = np.tile(a, reps)
    return a[:want]


def transcribe(pcm16: np.ndarray) -> str:
    """用项目同款 SenseVoice 转写（use_itn=True，与 asr.py 一致）。"""
    import sherpa_onnx
    rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=os.path.join(SENSE_DIR, "model.int8.onnx"),
        tokens=os.path.join(SENSE_DIR, "tokens.txt"),
        use_itn=True, num_threads=4)
    s = rec.create_stream()
    s.accept_waveform(RATE, pcm16)
    rec.decode_stream(s)
    return (s.result.text or "").strip()


def run_session(far: np.ndarray, *, ec: EchoCanceller | None,
                ap: AudioProcessor | None):
    """逐帧同步配对 far/near —— 这是 pywebrtc 示例用的正确做法。

    顺序必须是「先写 far（render）再读 near（capture）」，对齐 WebRTC
    AudioProcessing 的官方用法。
    """
    n_frames = len(far) // FRAME
    far_buf, raw_buf, ec_buf, ap_buf = [], [], [], []
    with sd.Stream(samplerate=RATE, blocksize=FRAME, channels=1,
                   dtype="int16") as st:
        for i in range(n_frames):
            s = i * FRAME
            ff = far[s:s + FRAME]
            st.write(ff.reshape(-1, 1))
            nf, _ = st.read(FRAME)
            nf = nf.flatten()
            far_buf.append(ff.copy())
            raw_buf.append(nf.copy())
            if ec is not None:
                ec_buf.append(ec.process(nf, ff))
            if ap is not None:
                ap_buf.append(ap.process(nf, ff))
    cat = lambda b: np.concatenate(b) if b else np.zeros(0, dtype=np.int16)
    return cat(far_buf), cat(raw_buf), cat(ec_buf), cat(ap_buf)


def main():
    ap_ = argparse.ArgumentParser()
    ap_.add_argument("--double-talk", action="store_true",
                     help="加跑阶段 C（播放时你念一句，测保真）")
    ap_.add_argument("--out", default="/tmp/aec-probe")
    args = ap_.parse_args()
    os.makedirs(args.out, exist_ok=True)

    def save(name, sig):
        with wave.open(os.path.join(args.out, name), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
            w.writeframes(sig.tobytes())

    print("=" * 66)
    print("AEC 可行性探针")
    print("=" * 66)
    di, do = sd.default.device
    print(f"输入 #{di} {sd.query_devices(di)['name']}")
    print(f"输出 #{do} {sd.query_devices(do)['name']}")
    print()

    # ---------- A. 静默基线 ----------
    print("[A] 静默基线（2s，不播不说）… 请保持安静")
    silence = np.zeros(RATE * 2, dtype=np.int16)
    _, raw_sil, _, _ = run_session(silence, ec=None, ap=None)
    floor = rms_f32_from_i16(raw_sil)
    print(f"    噪声底 RMS = {floor:.5f}")
    print()

    # ---------- B. 只播不说 ----------
    far = load_far(6.0)
    print(f"[B] 只播不说（6s，far = zh.wav，循环 {far.size/RATE:.1f}s）… 请不要说话")
    ec = EchoCanceller(sample_rate=RATE)
    ap = AudioProcessor(sample_rate=RATE, echo_cancellation=True,
                        noise_suppression=True)
    far_rec, raw, ec_out, ap_out = run_session(far, ec=ec, ap=ap)

    save("far.wav", far_rec)
    save("near_raw.wav", raw)
    save("aec_only.wav", ec_out)
    save("aec_ns.wav", ap_out)

    e_far, e_raw = rms_f32_from_i16(far_rec), rms_f32_from_i16(raw)
    e_ec, e_ap = rms_f32_from_i16(ec_out), rms_f32_from_i16(ap_out)

    def db(a, b):
        return 20 * np.log10(b / a) if a > 0 and b > 0 else float("nan")

    print()
    print("─" * 66)
    print("【物理上限】ERL —— 播出去的东西有多少从麦克风回来")
    print(f"    far  RMS = {e_far:.5f}")
    print(f"    near RMS = {e_raw:.5f}  (纯回声，你没说话)")
    print(f"    → 系统 ERL ≈ {-db(e_far, e_raw):+.1f} dB "
          f"（含播放增益与麦增益；数值越大＝回声越弱）")
    print()
    print("【AEC 抑制】ERLE —— 相对原始回声消掉多少")
    print(f"    AEC 单独       : {-db(e_raw, e_ec):+6.1f} dB"
          f"   (残余 {e_ec:.5f})")
    print(f"    AEC + 降噪     : {-db(e_raw, e_ap):+6.1f} dB"
          f"   (残余 {e_ap:.5f})")
    print()
    print("【★ 真正的判据】残余 vs 你的 VAD 门限")
    need = max(floor * VAD_MIN_SNR, VAD_MIN_RMS)
    print(f"    门限 need = max(噪声底×{VAD_MIN_SNR}, {VAD_MIN_RMS}) = {need:.5f}")
    for label, e in (("原始回声", e_raw), ("AEC 后", e_ec), ("AEC+NS 后", e_ap)):
        verdict = "❌ 越限（会触发假打断）" if e >= need else "✅ 不越限"
        print(f"    {label:10} {e:.5f}  {verdict}")
    print("─" * 66)

    # ---------- SenseVoice 幻觉判定 ----------
    print()
    print("【幻觉判定】把三种信号分别喂 SenseVoice（与项目同款、use_itn=True）")
    res = {}
    for label, sig in (("原始回声", raw), ("AEC 后", ec_out), ("AEC+NS 后", ap_out)):
        txt = transcribe(sig)
        res[label] = txt
        shown = repr(txt) if txt else "（空）"
        print(f"    {label:10} → {shown[:70]}")
    print()
    print("  三条分叉：")
    nonempty = [k for k, v in res.items() if v]
    if not nonempty:
        print("    ✅ 三种输入都转不出文本 → 残余过不了 ASR，AEC 边际价值小")
    elif len(nonempty) < 3:
        print(f"    ✅ AEC 有效（{nonempty}还能转出，其余已干净）→ 值得接 AecGate")
    else:
        print("    ⚠️ 三种都能转出 → AEC 不够，需要考虑 VPIO / 硬件麦")

    # ---------- C. 双讲保真（可选）----------
    if args.double_talk:
        print()
        print("[C] 双讲保真 —— 播放时请你念一句（如「帮我把那个文件移到备份目录」）")
        input("    准备好后按回车开始（6s）… ")
        _, raw2, ec2, ap2 = run_session(far, ec=EchoCanceller(sample_rate=RATE),
                                        ap=AudioProcessor(sample_rate=RATE,
                                                          echo_cancellation=True,
                                                          noise_suppression=True))
        save("dt_raw.wav", raw2); save("dt_aec.wav", ec2); save("dt_aec_ns.wav", ap2)
        print("    保真（三者应都能转出你刚说的话，且 AEC+NS 后不该缺字）：")
        for label, sig in (("原始(含回声)", raw2), ("AEC 后", ec2), ("AEC+NS 后", ap2)):
            print(f"      {label:12} → {transcribe(sig)[:70]!r}")

    print()
    print(f"wav 已存到 {args.out}/  —— 可直接听 near_raw vs aec_ns 的差别")
    return 0


if __name__ == "__main__":
    sys.exit(main())
