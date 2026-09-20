#!/usr/bin/env python3
"""**测出真实的回声延迟** —— 决定 `aec_stream_delay_ms` 该设多少。

**为什么需要专门的工具**：从对话录音里抠不出这个数。试过三个判据（包络互相关 /
样本级互相关 / GCC-PHAT），**全部没通过零假设对照**（和另一段无关音频打平）——
双讲 + 短样本 + 语音着色，锁不住峰。**必须用已知的宽带信号**。

**做法**：把一段**啁啾（chirp）**通过**生产同一个 `Player`** 放出去，同时用
**生产同一个 `MicStream`** 录。用户不出声 → 麦克风里**只有回声** →
「消得干不干净」就是一个无歧义的数（残差能量），**不需要任何相关性判据**。

然后扫 `aec_stream_delay_ms` 的候选值，谁把残差压得最低就是它。

⚠️ 设备与采样率**完全照抄生产**（Player 44.1k + Mic 16k，同一对内置设备）——
不自己造 `sd.Stream`。历史上自造流把设备切了采样率 → 音频慢放变男声。

用法：
    .venv/bin/python tools/measure_echo_delay.py            # 会**放声音**，约 4 秒
    .venv/bin/python tools/measure_echo_delay.py --dry-run   # 不放声音，只打印计划
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# 双讲标定要念的文本。选它是因为：**辅音密度高**（辅音全在高频，正是 AEC3 双讲时
# 先丢的东西）、没有专名、念两遍不会记混。
DEFAULT_SAY = ("今天天气不错，我早上七点起床，先看了会儿书，然后煮了一杯咖啡。"
               "窗外有几只鸟在叫，路上的人渐渐多了起来。下午我打算去图书馆，"
               "把上周借的两本书还掉。")


def build_chirp(rate: int = 44100, n_pulse: int = 3,
                pulse_s: float = 0.4, gap_s: float = 0.35,
                levels: list[float] | None = None):
    """线性啁啾串：宽带 → 时延估计的分辨率高。每段之间留静音，便于分辨多次回声。

    `levels`：每段的**相对幅度**（默认全 1.0）。给一串递减/递增的值，就得到一个
    「音量梯度」——用来量「播放电平 → 残余回声」的关系（等价于调系统音量，
    但数字增益可控且可复现）。
    返回 `(信号, [(level, 起点样本, 终点样本), ...])`。
    """
    from scipy.signal import chirp
    t = np.arange(int(pulse_s * rate)) / rate
    base = chirp(t, f0=200.0, f1=7000.0, t1=pulse_s, method="linear")
    base *= np.hanning(base.size) ** 0.5        # 首尾淡入淡出，避免咔嗒
    gap = np.zeros(int(gap_s * rate), np.float32)
    lv = list(levels) if levels else [1.0] * n_pulse
    parts, segs, pos = [], [], 0
    for a in lv:
        p = (base * a).astype(np.float32)
        parts += [p, gap]
        segs.append((a, pos, pos + len(p)))
        pos += len(p) + len(gap)
    return np.concatenate(parts).astype(np.float32), segs


def segment_by_envelope(far16: np.ndarray, n_expect: int,
                        win_s: float = 0.05) -> list[tuple[int, int]] | None:
    """在 far 上按包络把 `n_expect` 段找出来（返回 16k 样本区间）。

    为什么要这样切：麦克风与 far 是**同一时间轴**（far 就是按生产那个 150ms 偏移
    取的），所以在 far 上定位到的段，直接搬到近端/输出上就是同一段时间。
    """
    w = int(win_s * 16000)
    k = len(far16) // w
    env = np.sqrt((far16[:k * w].astype(np.float64) ** 2).reshape(k, w).mean(1))
    thr = max(env.max() * 0.05, 1e-6)
    on = env > thr
    segs, i = [], 0
    while i < k:
        if on[i]:
            j = i
            while j < k and on[j]:
                j += 1
            segs.append((i * w, j * w))
            i = j
        else:
            i += 1
    return segs if len(segs) == n_expect else None


def xcorr(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """互相关（`a = b 延迟 D` → 峰值索引 − (len(b)−1) = D）。

    ⚠️ **必须走 FFT**：真机那段是 20 秒 = 32 万样本，`np.correlate(mode="full")`
    是 O(N²) ≈ 1e11 —— 会挂死（本地测试只有 4.8 万样本，看不出问题）。
    """
    from scipy.signal import fftconvolve
    a = np.asarray(a, np.float64)
    b = np.asarray(b, np.float64)
    return fftconvolve(a, b[::-1], mode="full")


def run_double_talk(args, cfg, player, mic, fmt) -> dict:
    """**双讲标定**：同一段话念两遍（一遍安静、一遍扬声器同时放带限噪声），比 ASR 字符错误率。

    两组对照回答两件事：

      ① **近端保真**：两遍念同一段文本 → 各字符错误率。第二遍明显更差
         ⇒ AEC 的 NLP 在双讲时**压了你的语音**。
      ② **残余回声**：噪声与语音不相关 → 输出与噪声做**互相关**，峰的幅度就是残余回声。

    `--repeat N` 跑 N 轮（**配对**：每轮都有安静组和双讲组），
    最后给聚合表。⚠️ 单轮 N=1 不能下结论 —— 两遍是两次不同的朗读，ASR 也有随机性。
    """
    from scipy.signal import resample_poly
    SAY = args.say or DEFAULT_SAY
    A_SEC, GAP, B_SEC, LEAD = args.dt_secs
    N = max(1, int(args.repeat))

    ACOUSTIC_ATTEN_DB = 14.0    # 实测：数字 far → 麦克风里的回声，约衰减这么多
                                # （见 `--levels` 那次扫描：far −18.2 → 麦克风 −32.2）

    def beam(sec, rms_db=None):
        """**带限噪声**（200–7000 Hz）—— AEC 测试的标准信号。

        ⚠️ 一开始用的是**重复啁啾**，结果互相关是**梳状峰**（间隔=重复周期），
        会选错齿 → far 整体错位整数个周期 → 测出来的东西全是假的。
        噪声的自相关近似 delta：**对齐唯一**，而且给自适应滤波器**满带激励**。

        ⚠️ 人声与噪声不相关 → 「残余回声」那步互相关的**零假设天然干净**。
        """
        rng = np.random.default_rng(20260920)
        n = int(sec * fmt.sample_rate)
        x = rng.standard_normal(n).astype(np.float32)
        X = np.fft.rfft(x)
        f = np.fft.rfftfreq(n, 1.0 / fmt.sample_rate)
        X[(f < 200) | (f > 7000)] = 0
        y = np.fft.irfft(X, n)
        y /= (np.sqrt(np.mean(y ** 2)) + 1e-20)
        lv = -14.0 if rms_db is None else float(rms_db)
        return (y * (10 ** (lv / 20)) * 32767).astype(np.int16)

    def countdown(n=3):
        for i in range(n, 0, -1):
            print(f"  {i} …", flush=True)
            time.sleep(1.0)

    def assert_playing():
        """⚠️ **播放流死了要立刻吵**。真人实测踩过：轮次之间误调 `stop()` →
        下一轮的 `叮` 和噪声都写进死掉的 Player → 人只看到"没听到叮"，
        而脚本这边**一声不响**地继续跑完，产出一份没人知道是废的数据。"""
        st = getattr(player, "_stream", None)
        if st is not None and not getattr(st, "active", True):
            print("  ⚠️⚠️ 播放流已关闭 —— 提示音/噪声放不出来，这一轮作废！", flush=True)

    def play_and_drain(sig: np.ndarray, timeout: float = 4.0) -> None:
        """写进播放队列，并**等到它真的播完**（缓冲清空）才返回。

        ⚠️ 为什么不能只 `write` 完就 sleep 一个估的时长：那样是**假设**队列会顺序播完。
        实测踩过"提示音被后面排的噪声盖掉/顺序看起来不对"的情况 ——
        提示音本身就没起到提示作用。直接问播放器"还欠多少秒没播"才是确定的。
        """
        assert_playing()
        player.write((sig * 32767).astype(np.int16).tobytes())
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                if player.buffered_seconds() <= 0.01:
                    break
            except Exception:
                break
            time.sleep(0.02)
        time.sleep(0.15)            # 留一点声学/设备余量

    def beep_sig(n=1) -> np.ndarray:
        t = np.arange(int(0.15 * fmt.sample_rate)) / fmt.sample_rate
        one = np.sin(2 * np.pi * 880 * t) * np.hanning(t.size) * 0.25
        gap = np.zeros(int(0.12 * fmt.sample_rate))
        return np.concatenate([one, gap] * n)[:-len(gap)]

    def cue():
        """**一声叮 = 现在开始念**。放在**测量窗之外**（播完再开录），不污染任何一段。"""
        play_and_drain(beep_sig(1))
        time.sleep(0.4)

    def record(sec):
        """录 `sec` 秒。⚠️ **按样本数封顶**：若 `mic.read()` 瞬时返回（假设备/队列被排空），
        只按墙上时间判会在一秒内堆几十万个块 → 内存爆掉（冒烟测试踩到）。"""
        blocks, got = [], 0
        want = int(sec * cfg.sample_rate)
        t0 = time.time()
        while got < want and time.time() - t0 < sec * 3:
            b = mic.read(timeout=0.5)
            if b is None:
                continue
            blocks.append(np.asarray(b, dtype=np.float32))
            got += blocks[-1].shape[0]
        return np.concatenate(blocks)[:want] if blocks else np.zeros(0, np.float32)

    def flush_mic():
        """开录前排空麦克风队列。⚠️ 不排的话倒计时/提示音那几秒的**陈旧块**会被算进
        测量窗 —— 真人实测就是这么把第一段电平压到 −47 dBFS、ASR 只出「星.」的。"""
        d = getattr(mic, "drain", None)
        if d:
            d()

    def rms_db(x):
        return 20 * np.log10(max(float(np.sqrt(np.mean(np.asarray(x, np.float64) ** 2))), 1e-9))

    def aec_of(x, far16=None):
        xi = np.clip(x * 32767, -32768, 32767).astype(np.int16)
        if far16 is None:
            far16 = np.zeros(len(xi), np.int16)
        if getattr(args, "aec", "prod") == "v21":
            sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            "webrtc-aec"))
            import shim                       # noqa: E402
            return shim.process(xi, far16, 0), xi      # 0 = 全链（抑制器之后）
        from pywebrtc_audio import AudioProcessor
        apz = AudioProcessor(sample_rate=cfg.sample_rate, echo_cancellation=True,
                             noise_suppression=True, high_pass_filter=True,
                             auto_gain_control=False, stream_delay_ms=0)
        n = min(len(xi), len(far16))
        out = np.zeros(n, np.int16)
        for i in range(0, n - 1600 + 1, 1600):
            out[i:i + 1600] = np.asarray(
                apz.process(xi[i:i + 1600], far16[i:i + 1600]), dtype=np.int16)
        return out, xi

    def ed(a_, b_):
        prev = list(range(len(b_) + 1))
        for i, ca in enumerate(a_, 1):
            cur = [i]
            for j, cb in enumerate(b_, 1):
                cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
            prev = cur
        return prev[-1]

    print("\n" + "=" * 72)
    print("请念这段（每轮念**两遍**，正常语速，不用赶）：\n")
    print("    " + SAY + "\n")
    print(f"每轮：叮 → 念 {A_SEC:.0f}s（**安静**）→ 停 {GAP:.0f}s → 叮 → 再念 {B_SEC:.0f}s"
          f"（**扬声器同时放噪声**）")
    print(f"共 {N} 轮。⚠️ 两遍念**完全一样**的文本；噪声像收音机雪花声，是故意的。")
    print(f"噪声电平**自动标定**：按你安静朗读的电平定，让**麦克风里的回声**≈你的语音"
          f"（声学衰减按实测 {ACOUSTIC_ATTEN_DB:.0f} dB 计）。")
    print("=" * 72)

    asr = None
    try:
        from jarvis_voice.asr import SenseVoiceASR
        asr = SenseVoiceASR(cfg)
    except Exception as e:
        print(f"[warn] ASR 不可用（只报回声，不报转写）：{e}")

    truth = SAY.strip()
    trials = []
    far_db = None          # 标定值，只在第 1 轮设定（见下面）
    for trial in range(N):
        if N > 1:
            print(f"\n{'─' * 72}\n【第 {trial + 1}/{N} 轮】\n{'─' * 72}")
        countdown(int(LEAD) if trial == 0 else max(3, int(LEAD) - 3))
        cue()
        flush_mic()
        print("[安静] 开始念 ……", flush=True)
        a = record(A_SEC)
        print("[安静] 停。", flush=True)
        voice_db = rms_db(a)
        # 闭环标定：放一小段**已知电平**的噪声、用户保持安静 → 直接量出声学衰减。
        # ⚠️ 不能拿"典型衰减"当常数 —— 实测同一台机器上它从 7.6 到 14 dB 都出现过，
        #    用错了会让回声电平偏掉 6 dB 以上（真人实测踩到）。
        if far_db is None:
            play_and_drain(beep_sig(2))   # 两声 = 别说话
            print("  [标定] 两声叮＝**请别说话**，我在量房间衰减（约 3 秒）", flush=True)
            flush_mic()
            CAL_DB = -18.0
            cch = beam(2.5, CAL_DB)
            player.write(cch.tobytes())
            calm = record(2.0)
            player.flush()
            c16 = resample_poly(cch.astype(np.float32) / 32768.0, 160, 441)
            cl = min(len(calm), len(c16))
            atten = CAL_DB - rms_db(calm[:cl])          # 数字 far → 麦克风，掉多少 dB
            far_db = max(-30.0, min(-6.0, voice_db + atten))
            print(f"  [标定] 房间衰减实测 {atten:.1f} dB（不是常数！）｜"
                  f"语音 {voice_db:.1f} → 噪声锁定 {far_db:.1f} dBFS"
                  f"（预期回声到麦 ≈ {far_db - atten:.1f}）")
        else:
            print(f"  [标定] 沿用量好的衰减，噪声仍为 {far_db:.1f} dBFS（本轮语音 {voice_db:.1f}）")
        time.sleep(GAP)
        # ⚠️ 顺序要紧：**先放提示音，再排噪声**。反过来的话噪声已在播放队列里，
        #    提示音会排在它**后面**（20 秒后才响）—— 等于没提示。
        cue()
        ch = beam(B_SEC + LEAD + 2, far_db)
        assert_playing()
        player.write(ch.tobytes())
        flush_mic()
        print("[双讲] 开始念（扬声器在响）……", flush=True)
        b = record(B_SEC)
        print("[双讲] 停。", flush=True)
        # ⚠️⚠️ **轮次之间只能 `flush()`，绝不能 `stop()`。**
        # `stop()` 会关掉播放流 → **下一轮的 `叮` 和噪声都写进一个死掉的 Player**，
        # 等于没放（真人实测："第二次没听到叮"）。`flush()` 只清缓冲、保持流开着。
        # 清缓冲是必须的 —— 否则上一轮的噪声会一直播到下一轮的安静段里。
        # 单轮版本没暴露这个 bug（stop 落在最后），是改成多轮循环时引进的。
        player.flush()

        # 第 2 段的 far：**不用 emit 记账**（麦克风队列会滞后），改用**已知的噪声**
        # 与录到的近端互相关自对齐 —— 噪声自相关近似 delta，对齐唯一。
        ch16 = resample_poly(ch.astype(np.float32) / 32768.0, 160, 441)
        nb = np.asarray(b[:min(len(b), len(ch16))], np.float64)
        v = ch16[:len(nb)].astype(np.float64)
        cc = xcorr(nb - nb.mean(), v - v.mean())
        mid = len(nb) - 1
        m = int(0.5 * cfg.sample_rate)
        seg = cc[max(0, mid - m):mid + m]
        k = int(np.argmax(np.abs(seg))) - min(m, mid)
        prom = float(np.abs(seg).max() / (np.mean(np.abs(cc)) + 1e-12))
        if k >= 0:
            far16 = np.concatenate([np.zeros(k, np.float32), ch16])[:len(b)]
        else:
            far16 = ch16[-k:len(b) - k]
            far16 = np.concatenate([far16, np.zeros(len(b) - len(far16), np.float32)])
        far_i16 = np.clip(far16 * 32767, -32768, 32767).astype(np.int16)

        # 实测**未过 AEC** 的麦克风里的回声电平（同一套互相关反解）。
        # 这才是 AEC 面对的 far/near 比，比"数字 far 电平"有意义得多。
        ref0 = far16.astype(np.float64)
        rb = np.asarray(b[:min(len(b), len(ref0))], np.float64)
        r0 = ref0[:len(rb)]
        c0 = xcorr(rb - rb.mean(), r0 - r0.mean())
        m0 = len(r0) - 1
        s0 = c0[m0 - 200:m0 + 200]
        alpha0 = float(np.abs(s0).max() / (np.linalg.norm(r0 - r0.mean()) ** 2 + 1e-12))
        echo_mic = alpha0 * float(np.sqrt(np.mean((r0 - r0.mean()) ** 2)))

        outA, _ = aec_of(a)
        outB, _ = aec_of(b, far_i16)

        # 残余回声：**报绝对电平**，不报归一化峰 —— 归一化要除以 `||ob||`，
        # 而 `ob` 含你的语音 → 峰被稀释 → 门槛没法跟单讲那次（纯回声）比。
        # `ob = α·ref + 语音` ⇒ 峰 ≈ α·||ref||² ⇒ 反解 α 再乘 rms(ref)。
        ref = far16.astype(np.float64)
        ob = outB.astype(np.float64) / 32768.0
        mm = min(len(ref), len(ob))
        ref, ob = ref[:mm] - ref[:mm].mean(), ob[:mm] - ob[:mm].mean()
        c = xcorr(ob, ref)
        mid2 = len(ref) - 1
        s2 = c[mid2 - 200:mid2 + 200]
        alpha = float(np.abs(s2).max() / (np.linalg.norm(ref) ** 2 + 1e-12))
        echo_rms = alpha * float(np.sqrt(np.mean(ref ** 2)))

        if getattr(args, "save_dt", None):
            _sa = {f"A{trial}": a, f"B{trial}": b, f"far{trial}": far_i16}
            if os.path.exists(args.save_dt):
                _old = dict(np.load(args.save_dt))
                _old.update(_sa); _sa = _old
            np.savez_compressed(args.save_dt, **_sa)

        tA = tB = ""
        if asr is not None:
            try:
                tA = asr.transcribe(outA).text or ""
                tB = asr.transcribe(outB).text or ""
            except Exception as e:
                print(f"  [warn] 转写失败：{e}")
        ea = ed(truth, tA) / max(len(truth), 1)
        eb = ed(truth, tB) / max(len(truth), 1)
        trials.append({"align_ms": k / 16.0, "prom": prom, "echo_rms": echo_rms,
                       "voice_db": voice_db, "echo_mic_db": 20 * np.log10(max(echo_mic, 1e-12)),
                       "far_db": far_db,
                       "cer_quiet": ea, "cer_double": eb, "tA": tA, "tB": tB,
                       "lvl_a": rms_db(a), "lvl_b": rms_db(b)})
        print(f"  对齐 {k / 16:.0f}ms（突出度 {prom:.0f}）| 电平 安静 {rms_db(a):.1f} / "
              f"双讲 {rms_db(b):.1f} dBFS")
        print(f"  安静念：{tA}")
        print(f"  边响念：{tB}")
        print(f"  字符错误率：安静 **{ea * 100:.1f}%** | 双讲 **{eb * 100:.1f}%**")
        print(f"  [near/far 核实] 你的语音 {voice_db:.1f} dBFS ｜ 麦克风里的回声 "
              f"{20 * np.log10(max(echo_mic, 1e-12)):.1f} dBFS ⇒ 差 "
              f"{voice_db - 20 * np.log10(max(echo_mic, 1e-12)):+.1f} dB"
              f"{'（≈同级 ✅）' if abs(voice_db - 20 * np.log10(max(echo_mic, 1e-12))) < 4 else '（未对齐 ⚠️）'}")
        if trial < N - 1:
            time.sleep(3)

    player.stop()          # 全部跑完才关流

    # ---- 聚合 ----
    print("\n" + "=" * 72)
    print(f"聚合（{N} 轮**配对**对照）")
    print("=" * 72)
    print("⚠️ 有效性：安静段 CER > 25% 视为**这遍没念完整/转写失败**，该轮不计入统计")
    print(f"{'轮':>3}{'安静CER':>10}{'双讲CER':>10}{'倍数':>8}"
          f"{'语音dBFS':>10}{'回声到麦dBFS':>14}{'残余回声dBFS':>14}")
    print("-" * 70)
    for i, t in enumerate(trials, 1):
        ratio = t["cer_double"] / max(t["cer_quiet"], 1e-4)
        bad = "  ← 无效（安静段残缺）" if t["cer_quiet"] > 0.25 else ""
        print(f"{i:>3}{t['cer_quiet'] * 100:9.1f}%{t['cer_double'] * 100:9.1f}%"
              f"{ratio:6.2f}×{'':>1}{t['voice_db']:10.1f}{t['echo_mic_db']:14.1f}"
              f"{20 * np.log10(max(t['echo_rms'], 1e-12)):14.1f}{bad}")
    valid = [t for t in trials if t["cer_quiet"] <= 0.25]
    n_bad = len(trials) - len(valid)
    print(f"（有效轮数 {len(valid)}/{len(trials)}"
          + (f"，剔除 {n_bad} 轮" if n_bad else "") + "）")
    mq = float(np.mean([t["cer_quiet"] for t in valid])) if valid else float("nan")
    md = float(np.mean([t["cer_double"] for t in valid])) if valid else float("nan")
    worse = sum(1 for t in valid if t["cer_double"] > t["cer_quiet"])
    Nv = len(valid)
    gate = getattr(cfg, "vad_min_rms", 0.012)
    print("-" * 70)
    print(f"均值：安静 {mq * 100:.1f}% ｜ 双讲 {md * 100:.1f}% ｜ "
          f"{md / max(mq, 1e-4):.2f} 倍")
    print(f"双讲更差的轮数：**{worse}/{N}**")
    max_echo = max(t["echo_rms"] for t in trials) if trials else 1e-6
    print(f"残余回声最高一轮 = {20 * np.log10(max(max_echo, 1e-12)):.1f} dBFS "
          f"（门限 {20 * np.log10(gate):.1f} dBFS）")
    print()
    if Nv == 0:
        print("❌ 没有一轮有效 —— 重念。")
    elif Nv == 1:
        print("⚠️ 只剩 1 轮有效 —— 别下结论。")
    elif worse == Nv:
        print(f"✅ **{Nv}/{Nv} 轮（有效）双讲都更差** → 结论稳了。")
    elif worse >= Nv - 1:
        print(f"🟡 {Nv} 轮里 {worse} 轮更差 → 方向成立，但不如全中那么硬。")
    else:
        print(f"❌ 只有 {worse}/{Nv} 轮更差 → **证据不支持**。")
    snrs = [t["voice_db"] - t["echo_mic_db"] for t in valid] or [0.0]
    print(f"near/far 比（语音 − 回声到麦）：中位 {float(np.median(snrs)):+.1f} dB "
          f"→ {'≈同级，可比生产 ✅' if abs(float(np.median(snrs))) < 4 else '⚠️ 未对齐，倍数仍不可外推'}")
    if max_echo >= gate:
        print("⚠️ 有轮次残余回声越过门限 → 那一轮可能混了「回声误触发」。")
    return {"trials": trials, "mean_quiet": mq, "mean_double": md, "worse": worse}



def _aec_process(args, cfg, x_f32, far_i16=None):
    """按 `--aec` 选后端过一遍。返回 int16。`x_f32` 是 float32 [-1,1]。"""
    xi = np.clip(np.asarray(x_f32) * 32767, -32768, 32767).astype(np.int16)
    if far_i16 is None:
        far_i16 = np.zeros(len(xi), np.int16)
    n = min(len(xi), len(far_i16))
    xi, far_i16 = xi[:n], far_i16[:n]
    if args.aec == "v21":
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "webrtc-aec"))
        import shim                                    # noqa: E402
        return shim.process(xi, far_i16, 0)            # 0 = 全链
    from pywebrtc_audio import AudioProcessor
    apz = AudioProcessor(sample_rate=cfg.sample_rate, echo_cancellation=True,
                         noise_suppression=True, high_pass_filter=True,
                         auto_gain_control=False, stream_delay_ms=0)
    out = np.zeros(n, np.int16)
    for i in range(0, n - 1600 + 1, 1600):
        out[i:i + 1600] = np.asarray(apz.process(xi[i:i + 1600], far_i16[i:i + 1600]),
                                     dtype=np.int16)
    return out


def eval_dt(args, cfg, fmt) -> int:
    """离线评估已存的 `--save-dt` 录制。**不碰任何音频设备**（可反复换后端跑）。"""
    d = dict(np.load(args.eval_dt))
    idx = sorted(k[1:] for k in d if k.startswith("A"))
    if not idx:
        print("❌ 这个 npz 里没有 A*/B*/far* 键")
        return 1
    try:
        from jarvis_voice.asr import SenseVoiceASR
        asr = SenseVoiceASR(cfg)
    except Exception as e:
        print(f"❌ ASR 不可用：{e}")
        return 1

    def tr(x):
        return asr.transcribe(x).text or ""

    def ed(a_, b_):
        prev = list(range(len(b_) + 1))
        for i, ca in enumerate(a_, 1):
            cur = [i]
            for j, cb in enumerate(b_, 1):
                cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
            prev = cur
        return prev[-1]

    truth = (args.say or DEFAULT_SAY).strip()
    print(f"\n后端 = **{args.aec}**（{'自编 webrtc-audio-processing' if args.aec == 'v21' else '现用 pywebrtc-audio'}）")
    print(f"{'轮':>3}{'安静CER':>10}{'双讲CER':>10}{'倍数':>8}   安静转写 / 双讲转写")
    print("-" * 78)
    res = []
    for i in idx:
        a = d[f"A{i}"].astype(np.float32)
        b = d[f"B{i}"].astype(np.float32)
        far = d[f"far{i}"]
        oa = _aec_process(args, cfg, a)
        ob = _aec_process(args, cfg, b, far)
        tA, tB = tr(oa), tr(ob)
        ea = ed(truth, tA) / max(len(truth), 1)
        eb = ed(truth, tB) / max(len(truth), 1)
        res.append((ea, eb))
        print(f"{i:>3}{ea * 100:9.1f}%{eb * 100:9.1f}%{eb / max(ea, 1e-4):7.2f}×   "
              f"{tA[:26]} / {tB[:26]}")
    if res:
        mq = sum(r[0] for r in res) / len(res)
        md = sum(r[1] for r in res) / len(res)
        worse = sum(1 for r in res if r[1] > r[0])
        print("-" * 78)
        print(f"均值：安静 {mq * 100:.1f}% ｜ 双讲 {md * 100:.1f}% ｜ {md / max(mq, 1e-4):.2f} 倍"
              f"｜ 双讲更差 {worse}/{len(res)} 轮")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="不放声音，只打印计划")
    ap.add_argument("--record-s", type=float, default=5.0)
    ap.add_argument("--sweep-ms", type=int, nargs="+",
                    default=list(range(0, 651, 25)))
    ap.add_argument("--levels", type=float, nargs="+", default=None,
                    help="多电平模式：每段啁啾的相对幅度。量「播放电平 → 残余回声」")
    ap.add_argument("--double-talk", action="store_true",
                    help="双讲标定：同一段话念两遍（一遍安静、一遍扬声器同时响）")
    ap.add_argument("--say", default=None, help="双讲模式要念的文本（默认内置一段）")
    ap.add_argument("--aec", choices=["prod", "v21"], default="prod",
                    help="用哪个 AEC 后端评双讲：prod=现用的 pywebrtc-audio；"
                         "v21=自编的 webrtc-audio-processing（见 tools/webrtc-aec/）")
    ap.add_argument("--save-dt", metavar="NPZ",
                    help="把两遍录制存下来，之后用 --eval-dt 离线换后端重算（**不用重念**）")
    ap.add_argument("--eval-dt", metavar="NPZ",
                    help="离线评估已存的双讲录制（不碰音频设备）")
    ap.add_argument("--repeat", type=int, default=1,
                    help="双讲模式跑几轮（**配对**对照）。⚠️ 单轮 N=1 别下结论，用 3")
    ap.add_argument("--dt-secs", type=float, nargs=4, default=[20.0, 5.0, 20.0, 2.0],
                    metavar=("安静段", "间隔", "双讲段", "倒数"),
                    help="双讲模式四段时长（秒）。调小可用于离线自测")
    ap.add_argument("--out", default=None, help="把这次录到的 near/far 存成 npz 供复查")
    args = ap.parse_args()

    # 设备解析完全照抄 `__main__`（--speaker-aec 的语义）
    os.environ["JARVIS_AUDIO_MODE"] = "speaker_aec"
    os.environ.setdefault("JARVIS_OUTPUT_DEVICE", "MacBook")
    os.environ.setdefault("JARVIS_INPUT_DEVICE", "MacBook")

    from jarvis_voice.audio_io import MicStream, resolve_device
    from jarvis_voice.config import Config
    from jarvis_voice.player import Player
    from jarvis_voice.tts.base import AudioFormat

    cfg = Config.load()
    out_dev = resolve_device(getattr(cfg, "output_device", ""), True)
    in_dev = resolve_device(getattr(cfg, "input_device", ""), False)
    # ⚠️ 44.1k 与生产一致 —— `AecGate._delay_frames` 就是按 44100 算的。
    fmt = AudioFormat(sample_rate=44100)
    print(f"配置：输出设备 idx={out_dev} | 输入设备 idx={in_dev} | 近端 {cfg.sample_rate} Hz")
    plan = (f"放 {len(args.levels)}×0.4s 啁啾，幅度 {args.levels}（多电平模式）"
            if args.levels else
            f"放 3×0.4s 啁啾（约 2.2s），录 {args.record_s}s，"
            f"扫 {len(args.sweep_ms)} 个延迟值 0–{max(args.sweep_ms)}ms")
    print("计划：" + plan)
    if args.dry_run:
        print("[dry-run] 不放声音，退出")
        return 0

    # ⚠️ 离线评估必须在**开设备之前**分流 —— `--eval-dt` 不该碰任何音频设备
    #（既是纪律（探针别抢设备），也让它可以随便重跑）。
    if args.double_talk and args.eval_dt:
        return eval_dt(args, cfg, fmt)

    player = Player(fmt, device=out_dev)
    mic = MicStream(cfg, device=in_dev)
    if not player.start():
        print("❌ 播放流打不开")
        return 1
    mic.start()

    if args.double_talk:
        try:
            run_double_talk(args, cfg, player, mic, fmt)
            return 0
        finally:
            try:
                player.stop()
                mic.stop()
            except Exception:
                pass

    ch44, segs44 = build_chirp(player.fmt.sample_rate, levels=args.levels)
    need44 = 1600 * 441 // 160                    # accept() 每块要的 far 帧数
    delays = sorted(set(args.sweep_ms))
    near_blocks, emit_log, far_by_delay = [], [], {d: [] for d in delays}
    lead_s = 1.4
    need_s = lead_s + len(ch44) / player.fmt.sample_rate + 1.0
    rec_s = max(args.record_s, need_s)
    print(f"实际录制 {rec_s:.1f}s（要覆盖 {len(ch44) / player.fmt.sample_rate:.1f}s 信号 + {lead_s}s 前置）")

    try:
        time.sleep(lead_s)   # ⚠️ 够长：让 emit 超过最大扫描偏移（650ms=28665 帧）
                          #    → 每块 far_slice 都返回满 4410 帧，不会前段被 clamp
                          #    → 各偏移的分析长度一致，比较才公平
        player.write((ch44 * 0.3 * 32767).astype(np.int16).tobytes())
        t0 = time.time()
        while time.time() - t0 < rec_s:
            blk = mic.read(timeout=0.5)
            if blk is None:
                continue
            near_blocks.append(np.asarray(blk, dtype=np.float32))
            # ⚠️ 与 `AecGate.accept()` 完全同型：先读 emit，再按偏移取 far
            emit = player.emit_frames()
            emit_log.append(emit)
            for d in delays:
                df = int(d / 1000.0 * player.fmt.sample_rate)
                far_by_delay[d].append(player.far_slice(max(0, emit - df), need44))
    finally:
        player.stop()
        mic.stop()

    if not near_blocks:
        print("❌ 一个近端块都没录到（设备/权限？）")
        return 1

    near = np.concatenate(near_blocks)
    print(f"录到 {len(near)} 样本 = {len(near) / cfg.sample_rate:.2f}s，"
          f"共 {len(near_blocks)} 块")
    print(f"麦克风 RMS = {20 * np.log10(max(float(np.sqrt(np.mean(near.astype(np.float64) ** 2))), 1e-9)):.1f} dBFS")

    if args.out:
        # ⚠️ 把**每个候选偏移的 far** 也存下来 —— 否则调一次分析就要再放一次声音。
        np.savez_compressed(
            args.out, near=near, emit=np.array(emit_log), delays=np.array(delays),
            **{f"far_{d}": np.concatenate(far_by_delay[d]) if far_by_delay[d]
               else np.zeros(0, np.int16) for d in delays})
        print(f"已存 {args.out}（含 {len(delays)} 个偏移的 far）")

    # ---- 扫延迟：谁把残差压得最低 ----
    from pywebrtc_audio import AudioProcessor
    from scipy.signal import resample_poly
    near_i16 = np.clip(near * 32767, -32768, 32767).astype(np.int16)

    def far_i16_for(d):
        blocks = far_by_delay[d]
        if not blocks:
            return None
        far44 = np.concatenate(blocks)
        # 与 aec.py 一致：**必须 /32768 归一化**（漏了会饱和成方波 —— 2026-09-20 的 bug）
        f = resample_poly(far44.astype(np.float32) / 32768.0, 160, 441)
        n = min(len(f), len(near_i16))
        return np.clip(f[:n] * 32767, -32768, 32767).astype(np.int16)

    def run_aec(far_i16, delay_hint=0):
        # ⚠️ 前几块的 far 可能短于 4410（镜像还没填满）→ 拼接后长度对不上近端。
        # 取公共长度，别让它报 "near and far must have the same length"。
        n = min(len(near_i16), len(far_i16))
        near_c, far_c = near_i16[:n], far_i16[:n]
        apz = AudioProcessor(sample_rate=cfg.sample_rate, echo_cancellation=True,
                             noise_suppression=True, high_pass_filter=True,
                             auto_gain_control=False, stream_delay_ms=delay_hint)
        out = np.zeros(n, np.int16)
        C = 1600
        for i in range(0, n - C + 1, C):
            out[i:i + C] = np.asarray(apz.process(near_c[i:i + C], far_c[i:i + C]),
                                      dtype=np.int16)
        return out.astype(np.float64) / 32768.0

    def rms_db(x):
        return 20 * np.log10(max(float(np.sqrt(np.mean(np.asarray(x, np.float64) ** 2))), 1e-9))

    base = rms_db(near)

    # ---- 多电平模式：播放电平 → 残余回声（回答「音量越大越容易误打断吗」）----
    if args.levels:
        f150 = far_i16_for(150)
        if f150 is None or len(f150) < 3200:
            print("❌ 拿不到 150ms 偏移的 far，无法分析")
            return 1
        out = run_aec(f150)
        far16 = f150.astype(np.float64) / 32768.0
        segs = segment_by_envelope(far16, len(args.levels))
        if segs is None:
            print("❌ 在 far 上切不出预期段数（信号没放全？）—— 打印原始电平供排查")
            print(f"   far RMS={rms_db(far16):.1f} dBFS  near RMS={base:.1f} dBFS")
            return 1
        gate = cfg.vad_min_rms
        print(f"\nVAD 绝对门限 `vad_min_rms` = {gate}")
        print(f"{'段':>3}{'播放幅度':>9}{'far dBFS':>10}{'麦克风dBFS':>11}"
              f"{'过AEC dBFS':>11}{'消掉dB':>8}{'越门限?':>9}")
        print("-" * 62)
        for i, (lvl, (a, b)) in enumerate(zip(args.levels, segs)):
            fr, nr, orr = rms_db(far16[a:b]), rms_db(near_i16[a:b].astype(np.float64) / 32768.0), rms_db(out[a:b])
            sup = fr - orr
            over = "⚠️ 是" if 10 ** (orr / 20) >= gate else "否"
            print(f"{i:>3}{lvl:>9.2f}{fr:>10.1f}{nr:>11.1f}{orr:>11.1f}{sup:>8.1f}{over:>9}")
        print(f"\n判读：`过AEC dBFS` 换算成线性后与门限 {gate} 比。")
        print("  · 全部「否」→ **残余回声越不过 VAD 门限** ⇒ 误打断不是回声造成的")
        print("  · 高电平那几段「是」→ 音量越大越容易误触发 ⇒ **你的假设成立**")
        print(f"  · 注意：这里是**纯回声**（你不出声）。双讲时 AEC 表现可能不同。")
        return 0

    print(f"\n{'总偏移':>8}{'输出 RMS dBFS':>15}{'消掉多少 dB':>13}")
    print("-" * 38)
    best = None
    for d in delays:
        fi = far_i16_for(d)
        if fi is None or len(fi) < 3200:
            continue
        out = run_aec(fi)
        r = rms_db(out)
        drop = base - r
        print(f"{d:6d}ms{r:15.1f}{drop:13.1f}")
        if best is None or r < best[1]:
            best = (d, r)
    if best:
        print(f"\n★ 残差最低的偏移 = **{best[0]}ms**（输出 {best[1]:.1f} dBFS，"
              f"比不消低 {base - best[1]:.1f} dB）")
        print(f"  现行配置 `aec_stream_delay_ms` = 150ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
