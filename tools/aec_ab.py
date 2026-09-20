#!/usr/bin/env python3
"""AEC 离线对比测量台 —— 判断「换掉 WebRTC AEC3 会不会让打断时识别更准」。

**为什么需要它**：`cozec/aec` 基准说 FDAF 类滤波器的近端 SDR 比 WebRTC 高 10 dB，
但那是**信号级**的，**从没测过 ASR**。而我们唯一的判据是「转写准不准」。
所以这个台子同时报信号指标**和**真 ASR 转写。

**两种模式**（真值来源不同，别混）：

  `--synth`   近端 = 一段**干净真人录音**，回声 = 另一段语音延迟+增益（可选过 RIR）。
              ⇒ **近端真值已知** ⇒ 能算真正的 near-end SDR（`cozec/aec` 那个指标）。
              用来快速筛选算法，**不能替代真机**（没有真实房间、没有真实失配）。

  `--pair`    真机落盘的三件套 `near_raw.wav` + `far.wav`（+ 可选 `post_aec.wav`）。
              ⇒ 近端真值**未知** ⇒ 只能算 ERLE / 谱统计 / ASR 转写。
              这是最终判据，但要先让 orchestrator 把 far 也落盘。

**RED-GREEN 自检**：`--selftest` 用合成数据跑一遍，要求
  ① 理想 AEC（直接减）的 near-SDR 达到解析上限
  ② WebRTC 在纯回声下 ERLE > 40 dB（已知它单讲很强，做不到就是台子坏了）
若 ② 不成立 → **先修台子，别看结果**。

用法：
    python3 tools/aec_ab.py --selftest
    python3 tools/aec_ab.py --synth --rir room.wav
    python3 tools/aec_ab.py --pair ~/.jarvis/bargein-audio/X-bargein-raw.wav \
                                   ~/.jarvis/bargein-audio/X-far.wav
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from math import gcd
from typing import Callable

import numpy as np
import soundfile as sf

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

SR = 16000
INT16 = 32768.0
WAVS = os.path.join(REPO, "models",
                    "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17", "test_wavs")


# ─────────────────────────── 基础 I/O ───────────────────────────

def to_i16(x: np.ndarray) -> np.ndarray:
    return np.clip(np.asarray(x, dtype=np.float64) * 32767, -32768, 32767).astype(np.int16)


def to_f32(x: np.ndarray) -> np.ndarray:
    return np.asarray(x, dtype=np.float32) / INT16


def load16(path: str) -> np.ndarray:
    """任意采样率/声道 → 单声道 16k float32。"""
    from scipy.signal import resample_poly
    x, sr = sf.read(path, dtype="float32", always_2d=True)
    x = x.mean(axis=1)
    if sr != SR:
        g = gcd(SR, int(sr))
        x = resample_poly(x, SR // g, int(sr) // g).astype(np.float32)
    return x


def rms_db(x: np.ndarray) -> float:
    x = np.asarray(x, dtype=np.float64)
    if x.size == 0:
        return -np.inf
    return 10 * np.log10(max(float(np.mean(x ** 2)), 1e-20))


# ─────────────────────────── AEC 适配器（唯一的接缝） ───────────────────────────

@dataclass
class Adapter:
    """一个可测的 AEC 实现。

    `run(rec, far)` 收等长的 int16 近端/远端，返回等长 int16 输出。
    `note` 是给人看的一条短说明，会打进报表。
    """
    name: str
    run: Callable[[np.ndarray, np.ndarray], np.ndarray]
    note: str = ""


def _webrtc(delay_ms: int = 0, ns: bool = True, hpf: bool = True) -> Adapter:
    from pywebrtc_audio import AudioProcessor

    def run(rec, far):
        ap = AudioProcessor(sample_rate=SR, echo_cancellation=True,
                            noise_suppression=ns, high_pass_filter=hpf,
                            auto_gain_control=False, stream_delay_ms=delay_ms)
        out = np.zeros(len(rec), np.int16)
        C = 1600                                  # 100ms，与生产一致
        for i in range(0, len(rec) - C + 1, C):
            out[i:i + C] = np.asarray(ap.process(rec[i:i + C], far[i:i + C]),
                                      dtype=np.int16)
        return out

    return Adapter(f"webrtc(d={delay_ms}ms)", run,
                   "生产用的 WebRTC AEC3（自适应滤波 + NLP 残余抑制）")


def _wetdry(alpha: float, delay_ms: int = 0) -> Adapter:
    """**wet/dry 混合**：`alpha·AEC输出 + (1-alpha)·原始麦克风`。

    为什么值得试：Deepgram 官方文档明说「激进的 AEC 抑制会让 barge-in 更难 ——
    系统会把客户的声音连同回声一起压掉」，推荐**别用 100% AEC 输出、做 wet/dry 混合**。
    我们的实测（双讲时近端被压 3.19×）正是这个症状，而**这个旋钮我们还没试过**：
    掺回一点原始音频能减轻近端损伤，代价是漏回一点回声 —— 净收益要实测。
    """
    base = _webrtc(delay_ms)

    def run(rec, far):
        w = base.run(rec, far).astype(np.float64)
        d = np.asarray(rec, np.float64)
        return np.clip(alpha * w + (1 - alpha) * d, -32768, 32767).astype(np.int16)

    return Adapter(f"wet{int(alpha * 100)}/dry{int((1 - alpha) * 100)}", run,
                   "AEC 输出掺回一部分原始音频（Deepgram 推荐的旋钮）")


def _pyaec(frame_size: int = 160, filter_length: int = 3200,
           preprocess: bool = False) -> Adapter:
    from pyaec import Aec

    def run(rec, far):
        # ⚠️ pyaec 的 Aec 是**有状态**的，每次 run 必须新建。
        # ⚠️ ⚠️ aec-rs 从不调 `SPEEX_ECHO_SET_SAMPLING_RATE`（dylib 只导出 3 个符号，
        #    无从设置）→ Speex 内部**始终认为采样率 8000**。我们喂 16k 时：
        #      beta0  = 2*frame_size/8000 → 比正确的 16k 值**大一倍**（泄漏翻倍）
        #    → 收敛上限被压低。这是 pyaec 在 16k 下的**固有缺陷**，不是用法错误。
        a = Aec(frame_size=frame_size, filter_length=filter_length,
                sample_rate=SR, enable_preprocess=preprocess)
        out = np.zeros(len(rec), np.int16)
        for i in range(0, len(rec) - frame_size + 1, frame_size):
            out[i:i + frame_size] = np.asarray(
                a.cancel_echo(rec[i:i + frame_size].tolist(),
                              far[i:i + frame_size].tolist()), dtype=np.int16)
        return out

    tag = "pre" if preprocess else "lin"
    return Adapter(f"pyaec-{tag}(fl={filter_length})", run,
                   "SpeexDSP：分块频域自适应滤波" +
                   ("＋残余抑制（压近端那级）" if preprocess
                    else "，**无残余抑制**（理论保近端）"))


def default_adapters() -> list[Adapter]:
    return [
        Adapter("raw(不过AEC)", lambda r, f: r,
                "原始麦克风：近端完好，但**混着回声**"),
        _webrtc(0),
        _wetdry(0.7), _wetdry(0.5), _wetdry(0.3),
        _pyaec(filter_length=3200, preprocess=False),
        _pyaec(filter_length=3200, preprocess=True),
        _pyaec(filter_length=6400, preprocess=False),
    ]


# ─────────────────────────── 指标 ───────────────────────────

def align(a: np.ndarray, b: np.ndarray, max_lag: int) -> tuple[np.ndarray, np.ndarray, int]:
    """在 ±max_lag 内对齐 a、b（用低频包络避免被高频噪声骗），返回 (a', b', lag)。"""
    n = min(len(a), len(b))
    a, b = np.asarray(a, np.float64)[:n], np.asarray(b, np.float64)[:n]
    w = 160                                        # 10ms 包络
    k = n // w
    ea = np.sqrt((a[:k * w] ** 2).reshape(k, w).mean(1) + 1e-20)
    eb = np.sqrt((b[:k * w] ** 2).reshape(k, w).mean(1) + 1e-20)
    ea, eb = ea - ea.mean(), eb - eb.mean()
    maxk = max(1, max_lag // w)
    c = np.correlate(ea, eb, "full")[len(ea) - 1 - maxk: len(ea) + maxk]
    lag = (int(np.argmax(c)) - maxk) * w
    if lag > 0:
        a, b = a[lag:], b[:n - lag]
    elif lag < 0:
        a, b = a[:n + lag], b[-lag:]
    return a, b, lag


def near_sdr(out: np.ndarray, near: np.ndarray, warmup_s: float = 1.0) -> float:
    """**近端 SDR**：输出相对干净近端的保真度（越高越好）。

    这是 `cozec/aec` 用来排序算法的那个指标。需要**近端真值** → 只在 `--synth` 下有意义。
    ⚠️ 先对齐再算：AEC 内部有帧延迟，不对齐会把延迟当失真。
    """
    o, nr, _ = align(to_f32(out), np.asarray(near, np.float32),
                     max_lag=int(0.1 * SR))
    s = int(warmup_s * SR)                          # 跳过收敛期
    o, nr = o[s:], nr[s:]
    m = min(len(o), len(nr))
    e = nr[:m] - o[:m]
    return 10 * np.log10(max(np.sum(nr[:m] ** 2), 1e-20)
                         / max(np.sum(e ** 2), 1e-20))


def erle_db(rec: np.ndarray, out: np.ndarray, warmup_s: float = 1.0) -> float:
    """经典 ERLE。⚠️ **双讲下不适用**（ICASSP AEC Challenge 明确）——
    这里只在「纯回声」对照里当**台子自检**用。"""
    s = int(warmup_s * SR)
    return (rms_db(to_f32(rec)[s:]) - rms_db(to_f32(out)[s:]))


def spectrum(x: np.ndarray, warmup_s: float = 1.0) -> tuple[float, float]:
    """返回 (谱质心 Hz, 4–8kHz 能量占比)。**不是转写判据**，只是「高频有没有被削」的快照。"""
    from scipy.signal import welch
    v = to_f32(x)[int(warmup_s * SR):]
    if len(v) < 1024:
        return float("nan"), float("nan")
    f, P = welch(v, fs=SR, nperseg=1024)
    band = (f >= 100) & (f <= 8000)
    centroid = float(np.sum(f[band] * P[band]) / max(np.sum(P[band]), 1e-20))
    hi = float(np.sum(P[(f >= 4000) & (f <= 8000)])
               / max(np.sum(P[band]), 1e-20))
    return centroid, hi * 100


# ─────────────────────────── ASR ───────────────────────────

def make_asr():
    """复用生产同一套 SenseVoice（int8）。返回 `transcribe(pcm16) -> str` 或 None。"""
    try:
        from jarvis_voice.asr import SenseVoiceASR
        from jarvis_voice.config import Config
        asr = SenseVoiceASR(Config.load())
    except Exception as e:                          # 没有模型/配置时降级
        print(f"[warn] ASR 不可用，跳过转写：{e}")
        return None

    def go(pcm16):
        try:
            return asr.transcribe(pcm16).text
        except Exception as e:
            return f"<ASR 失败: {e}>"

    return go


# ─────────────────────────── 合成回声 ───────────────────────────

def synth_rir(rt60_s: float = 0.25, delay_ms: int = 150) -> np.ndarray:
    """合成一个指数衰减的房间响应（自检用；真机应传 `--rir` 用实测 RIR）。"""
    rng = np.random.default_rng(1234)
    n = int(rt60_s * SR)
    h = rng.standard_normal(n) * np.exp(-6.9 * np.arange(n) / max(n - 1, 1))
    h /= np.sqrt(np.sum(h ** 2)) + 1e-20
    out = np.zeros(delay_ms * SR // 1000 + n, np.float32)
    out[delay_ms * SR // 1000:] = h
    return out


def make_rec(near: np.ndarray, far: np.ndarray, rir: np.ndarray | None,
             delay_ms: int, gain: float) -> np.ndarray:
    """把 far 变成回声叠到 near 上，返回 (rec, far_used)。"""
    n = len(near)
    far = np.asarray(far, np.float32)
    if rir is None:
        d = delay_ms * SR // 1000
        e = np.zeros(n, np.float32)
        k = min(n - d, len(far))
        e[d:d + k] = far[:k]
    else:
        e = np.convolve(far, rir.astype(np.float32))[:n]
        if len(e) < n:
            e = np.concatenate([e, np.zeros(n - len(e), np.float32)])
    return near + gain * e


# ─────────────────────────── 报表 ───────────────────────────

@dataclass
class Row:
    name: str
    near_sdr: float | None
    erle: float
    centroid: float
    hi_pct: float
    text: str | None
    note: str = ""


def report(rows: list[Row], title: str, ref_centroid: float, ref_hi: float,
           ref_text: str | None, has_truth: bool,
           ref_label: str = "干净近端") -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")
    print(f"参考（{ref_label}）：质心 {ref_centroid:.0f} Hz   4-8k {ref_hi:.2f}%"
          + (f"   转写 {ref_text!r}" if ref_text is not None else ""))
    print(f"{'变体':<24}{'nearSDR':>9}{'ERLE':>8}{'质心Hz':>9}{'4-8k%':>8}  转写")
    print("-" * 78)
    for r in rows:
        sdr = f"{r.near_sdr:8.1f}" if r.near_sdr is not None else "       —"
        print(f"{r.name:<24}{sdr}{r.erle:8.1f}{r.centroid:9.0f}{r.hi_pct:8.2f}  "
              f"{r.text if r.text is not None else '—'}")
    if not has_truth:
        print("\n⚠️ 无近端真值 → nearSDR 一列无意义（本模式只能看质心/4-8k/转写/ERLE）。")
    print("\n判读：**只看转写**。质心与 4-8k 只是「高频有没有被削」的旁证；"
          "ERLE 在双讲下不适用，别拿来排序。")
    for r in rows:
        if r.note:
            print(f"  · {r.name}: {r.note}")


def measure(adapters, rec, far, near_or_none, asr, warmup_s=1.0) -> list[Row]:
    rec_i, far_i = to_i16(rec), to_i16(far)
    rows = []
    for a in adapters:
        out = a.run(rec_i, far_i)
        c, h = spectrum(out, warmup_s)
        rows.append(Row(
            name=a.name,
            near_sdr=near_sdr(out, near_or_none, warmup_s) if near_or_none is not None else None,
            erle=erle_db(rec_i, out, warmup_s),
            centroid=c, hi_pct=h,
            text=asr(out) if asr else None,
            note=a.note,
        ))
    return rows


# ─────────────────────────── 自检（RED-GREEN） ───────────────────────────

def selftest() -> int:
    """验证**测量台本身**可信。台子坏了，结果全是错的。"""
    print("=" * 78)
    print("自检：验证测量台（先证明台子对，再看结果）")
    print("=" * 78)
    ok = True
    near = load16(os.path.join(WAVS, "zh.wav"))
    far = load16(os.path.join(WAVS, "en.wav"))
    n = min(len(near), len(far))
    near, far = near[:n], far[:n]
    rec = make_rec(near, far, None, delay_ms=150, gain=0.5)
    near_i = to_i16(near)

    # ① 理想抵消：直接从 rec 里减掉已知回声 → near-SDR 应为解析上限（很高）
    d = 150 * SR // 1000
    e = np.zeros(n, np.float32)
    e[d:d + min(n - d, n)] = far[:min(n - d, n)]
    ideal = to_i16(rec - 0.5 * e)
    sdr_ideal = near_sdr(ideal, near)
    print(f"\n① 理想抵消器的 near-SDR = {sdr_ideal:.1f} dB  (要求 > 60)")
    ok &= sdr_ideal > 60

    # ② WebRTC 在**纯回声**（单讲）下应该很强 —— 它单讲本来就强（生产真机 34 dB）
    echo_only = make_rec(np.zeros(n, np.float32), far, None, delay_ms=150, gain=0.5)
    out_w = _webrtc(0).run(to_i16(echo_only), to_i16(far))
    erle_w = erle_db(to_i16(echo_only), out_w)
    print(f"② WebRTC 纯回声 ERLE = {erle_w:.1f} dB  (要求 > 40，否则台子/调用有问题)")
    ok &= erle_w > 40

    # ③ 对齐器不能把「已经对齐的同一信号」报出偏移
    _, _, lag = align(near, near.copy(), max_lag=int(0.1 * SR))
    print(f"③ 自对齐 lag = {lag}  (要求 0)")
    ok &= (lag == 0)

    print("\n" + ("✅ 台子可信" if ok else "❌ 台子有问题 —— 先修台子"))
    return 0 if ok else 1


def sweep(asr, args) -> int:
    """多句 × 多延迟 × 多失配的聚合对比 —— 用**字符错误率**排序，不是用 SDR。

    N=1 的单句结果不算数（一次转写可能是巧合）。这里把错误**累积**起来：
    每个变体跑同一批场景，报「总错字数 / 总字数」。这才是能下结论的证据量。
    """
    import itertools

    names = [f for f in sorted(os.listdir(WAVS)) if f.endswith(".wav")]
    nears = [args.near] if args.near else [os.path.join(WAVS, f) for f in names]
    delays = [args.delay_ms] if args.jitter_ms else [args.delay_ms]
    jitters = [0, args.jitter_ms] if args.jitter_ms else [0]
    gains = [args.gain]

    cases = []
    for near_p, far_p in itertools.permutations(
            [args.near] if args.near else [os.path.join(WAVS, f) for f in names], 2):
        cases.append((near_p, far_p))
    if args.far:
        cases = [(p, args.far) for p in nears] or cases

    total = {a.name: [0, 0] for a in default_adapters()}   # 含 passthrough=不过 AEC
    per_case = []
    for near_p, far_p in cases:
        near, far = load16(near_p), load16(far_p)
        n = min(len(near), len(far))
        if n < int(2.5 * SR):                       # 太短，AEC 没收敛，跳过
            continue
        near, far = near[:n], far[:n]
        truth = asr(to_i16(near)) if asr else ""
        for dl, jt in itertools.product(delays, jitters):
            rir = load16(args.rir) if args.rir else None
            rec = make_rec(near, far, rir, dl, gains[0])
            j = jt * SR // 1000
            far_ref = (np.concatenate([np.zeros(j, np.float32), far])[:len(far)]
                       if j > 0 else far)
            rec_i, far_i = to_i16(rec), to_i16(far_ref)
            line = [f"{os.path.basename(near_p)}/{os.path.basename(far_p)}"
                    f" d={dl} j={jt}"]
            for a in default_adapters():
                txt = asr(a.run(rec_i, far_i)) if asr else ""
                err = edit_distance(truth, txt)
                total[a.name][0] += err
                total[a.name][1] += max(len(truth), 1)
                line.append(f"{a.name.split('(')[0]}={err}")
            line.append(f"| 真值 {len(truth)} 字")
            per_case.append("  ".join(line))

    print("=" * 78)
    print("聚合对比（**字符错误数**，越小越好；排序只看这个）")
    print("=" * 78)
    print(f"场景数：{len(per_case)}  （每场景 = 一段干净近端 + 一段无关远端语音）")
    for ln in per_case:
        print("  " + ln)
    print("-" * 78)
    print(f"{'变体':<26}{'总错字':>8}{'总字数':>8}{'错误率':>9}")
    rank = sorted(total.items(), key=lambda kv: kv[1][0])
    for name, (err, tot) in rank:
        print(f"{name:<26}{err:8d}{tot:8d}{err / max(tot, 1) * 100:8.1f}%")
    print("\n⚠️ 合成场景：纯延迟+增益回声（除非 --rir）。真实房间更狠，只能定**方向**。")
    return 0


def edit_distance(a: str, b: str) -> int:
    """字符级 Levenshtein —— 中文按字算，正好。"""
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


# ─────────────────────────── CLI ───────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true", help="验证测量台本身")
    ap.add_argument("--synth", action="store_true",
                    help="合成回声模式（近端真值已知 → 能算 near-SDR）")
    ap.add_argument("--sweep", action="store_true",
                    help="多句 × 多维度的聚合对比（按字符错误率排序）")
    ap.add_argument("--pair", nargs="+", metavar="WAV",
                    help="真机文件：NEAR_RAW [FAR]。只给近端时降级为 raw-vs-生产输出对比")
    ap.add_argument("--post-aec", metavar="WAV",
                    help="（可选）生产 AEC 的同期输出，一并列出")
    ap.add_argument("--rir", metavar="WAV", help="实测房间响应（不给则用纯延迟+增益）")
    ap.add_argument("--near", metavar="WAV", help="合成模式的干净近端（默认 zh.wav）")
    ap.add_argument("--far", metavar="WAV", help="合成模式的远端（默认 en.wav）")
    ap.add_argument("--delay-ms", type=int, default=150)
    ap.add_argument("--gain", type=float, default=0.5)
    ap.add_argument("--jitter-ms", type=int, default=0,
                    help="给 far 参考**故意**加这么久的偏移，模拟「延迟是估计值」的失配")
    ap.add_argument("--repeat", type=int, default=1, help="把近端循环 N 遍（凑长音频）")
    ap.add_argument("--warmup", type=float, default=1.0, help="跳过收敛期的秒数")
    ap.add_argument("--raw-lead-ms", type=int, default=500,
                    help="`--pair` 里未过 AEC 的近端比生产输出**多出的预滚**（默认 500 = "
                         "vad_min_silence）。不裁掉就是在拿两段起点不同的窗对比")
    ap.add_argument("--no-asr", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    asr = None if args.no_asr else make_asr()
    adapters = default_adapters()

    if args.synth:
        near = load16(args.near or os.path.join(WAVS, "zh.wav"))
        far = load16(args.far or os.path.join(WAVS, "en.wav"))
        n = min(len(near), len(far))
        near, far = near[:n], far[:n]
        if args.repeat > 1:
            near, far = (np.tile(near, args.repeat), np.tile(far, args.repeat))
        rir = load16(args.rir) if args.rir else None
        rec = make_rec(near, far, rir, args.delay_ms, args.gain)
        far_ref = far
        if args.jitter_ms:
            j = args.jitter_ms * SR // 1000
            far_ref = (np.concatenate([np.zeros(j, np.float32), far])[:len(far)]
                       if j > 0 else far[-j:])
        # 合成模式下「passthrough」就是 rec 本身，基线应是**干净近端**
        adapters = [Adapter("clean-near", lambda r, f: to_i16(near),
                            "真值：干净近端（上界）")] + default_adapters()[1:]
        rows = measure(adapters, rec, far_ref, near, asr, args.warmup)
        c, h = spectrum(to_i16(near), args.warmup)
        report(rows, f"合成回声：delay {args.delay_ms}ms  gain {args.gain}"
                     + (f"  RIR {os.path.basename(args.rir)}" if args.rir else "  （纯延迟+增益）")
                     + (f"  far 参考故意偏 {args.jitter_ms}ms" if args.jitter_ms else ""),
               c, h, asr(to_i16(near)) if asr else None, has_truth=True)
        return 0

    if args.sweep:
        return sweep(asr, args)


    if args.pair:
        near_raw = args.pair[0]
        near = load16(near_raw)
        post = load16(args.post_aec) if args.post_aec else None
        if post is not None:
            # ⚠️⚠️ **未过 AEC 的近端比生产输出长 `vad_min_silence`**：`raw_slice` 带
            # `end_offset` 就是为了把段前那截预滚一起取回来（VAD 等静音才吐段）。
            # 不裁掉就是在拿**两段起点不同的窗**对比 → 结论全错（§5 记过同型 bug）。
            # 对齐关系：post[k] ⇔ raw[k+off]（raw 的窗整体更早 off 个样本）。
            d = len(near) - len(post)
            want = int(args.raw_lead_ms * SR / 1000)
            print(f"    长度检查：raw {len(near)} vs post {len(post)}，差 {d} 样本"
                  f"（{d / SR * 1000:.0f}ms；期望 ≈{args.raw_lead_ms}ms）"
                  + ("  ✅ 吻合" if abs(d - want) <= 2 else "  ⚠️ 不吻合，对齐可能错"))
            if d > 0:
                near = near[d:]
            elif d < 0:
                post = post[-d:]
        if len(args.pair) > 1:
            far = load16(args.pair[1])
            n = min(len(near), len(far))
            near, far = near[:n], far[:n]
            rows = measure(adapters, near, far, None, asr, args.warmup)
            title = f"真机对：{os.path.basename(near_raw)} × {os.path.basename(args.pair[1])}"
        else:
            # 只有近端（老采集没有 far）：至少能比「原始麦克风 vs 生产 AEC 输出」。
            # ⚠️ 这不是干净的因果对照 —— 原始里**混着回声**，所以它也可能转错。
            #    真正干净的对照要等带 far 的采集（`--pair NEAR FAR`）。
            print("⚠️ 未提供 far → 只能比 raw vs 生产输出，不能换 AEC 重算。\n")
            c, h = spectrum(to_i16(near), args.warmup)
            rows = [Row("原始麦克风（未过 AEC）", None, 0.0,
                        c, h, asr(to_i16(near)) if asr else None,
                        "混着回声，不是干净真值 —— 只作对照")]
            title = f"真机对：{os.path.basename(near_raw)}（无 far，降级对比）"
        if post is not None:
            out = to_i16(post[:len(near)])
            c, h = spectrum(out, args.warmup)
            rows.insert(1, Row("★生产 AEC 实际输出", None,
                               erle_db(to_i16(near), out, args.warmup),
                               c, h, asr(out) if asr else None,
                               "这就是当时真机上被送去 ASR 的那份"))
        c, h = spectrum(to_i16(near), args.warmup)
        report(rows, title, c, h, None, has_truth=False,
               ref_label="原始麦克风（未过 AEC）")
        return 0

    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
