#!/usr/bin/env python3
"""VAD 预滚快照的生命周期回归测试（2026-09-20）。

（ocr 报的，两处都在我 09-20 新加的预滚代码里。）

**问题一：被门限拒绝的段不消耗快照 → 陈旧快照拼到下一段**
`_pre_snap` 只在「段刚起」（`is_speech_detected` False→True）那一刻刷新。
而 sherpa 还会因为 `max_speech_duration` **从中间切段**、`is_speech_detected` 也会抖动
—— 这些段**没有跳变**，不会刷新快照。原实现只在「门限通过」的分支里取走并清空快照，
被拒绝的段就把它留给了下一段 → `_pre_snap_fed` 落在本段 `start` 之后 → 把**无关音频**
拼到段首，正是注释里警告的「首字吐两遍」。

**问题二：切片边界不钳 → Python 负索引静默绕回**
`snap[lo:hi]` 里 `hi > len(snap)` 时 Python **不报错、只截断**，于是拼进去的是尾巴而不是
「段起点之前那一段」。

⚠️ 离线，不开任何音频设备。
"""
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
BLOCK = 1600
FAIL = []


def check(label, got, want):
    """⚠️ `got` 可能是 ndarray（陈旧快照），`==` 会返回数组而不是布尔、比较会抛
    `ValueError: truth value of an array is ambiguous` —— 那样红绿测试会以 traceback
    收场、看不出"哪条断言失败"。所以先按类型归一成可比较的标量。"""
    if isinstance(got, np.ndarray):
        got = f"<ndarray shape={got.shape}>"      # 非 None 就是"没清掉"
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {label}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(label)


def speech() -> np.ndarray:
    """真语音（`say` 现合成，不播放 → 不碰音频设备）。

    ⚠️ 不要用「调幅噪声」之类的合成信号：Silero 是**语音**检测器，
    白噪声/调幅噪声它一概不认（写这版时实测 0 段）。
    """
    d = os.path.join(tempfile.gettempdir(), "jarvis_vad_preroll_test")
    os.makedirs(d, exist_ok=True)
    wav = os.path.join(d, "s.wav")
    if not os.path.exists(wav):
        aiff = wav + ".aiff"
        subprocess.run(["say", "-v", "Tingting", "-o", aiff,
                        "喂，等一下，我想问你一个问题"], check=True)
        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000",
                        "-c", "1", aiff, wav], check=True)
        os.unlink(aiff)
    with wave.open(wav) as w:
        assert w.getframerate() == SR
        return np.frombuffer(w.readframes(w.getnframes()),
                             dtype=np.int16).astype(np.float32) / 32768.0


def run(monkey_gate_false):
    """喂 1s 底噪 + 真语音 + 1s 底噪，返回 (段数, gate, 门限被调用次数)。

    ⚠️ 被门限拒的段**不会进返回值**（`_drain` 只在通过时 append），
    所以要靠「门限被调用过几次」来证明「确实有段被弹出来过」。
    """
    cfg = Config.load()
    g = VadGate(cfg)
    calls = []
    if monkey_gate_false:
        def _reject(pcm):
            calls.append(len(pcm))
            return False
        g._passes_gate = _reject
    else:
        orig = g._passes_gate
        def _count(pcm):
            calls.append(len(pcm))
            return orig(pcm)
        g._passes_gate = _count
    rng = np.random.default_rng(1)
    noise = lambda k: rng.normal(0, 0.004, k).astype(np.float32)
    sig = np.concatenate([noise(SR), speech(), noise(SR)])
    segs = []
    for i in range(0, len(sig) - BLOCK + 1, BLOCK):
        segs.extend(g.accept(sig[i:i + BLOCK]))
    segs.extend(g.flush())
    return segs, g, calls


print("=== ① 被门限拒绝的段，也必须把快照消耗掉 ===")
segs, g, calls = run(monkey_gate_false=True)
check("确实有段被弹出来过（门限被调用）", len(calls) > 0, True)
check("跑完 _pre_snap 已被清空", g._pre_snap, None)

print("\n=== ② 正常路径：段仍然出得来（回归，别改坏）===")
segs2, g2, calls2 = run(monkey_gate_false=False)
print(f"   （出了 {len(segs2)} 段，门限被调用 {len(calls2)} 次）")
check("有段通过门限", len(segs2) > 0, True)
check("跑完 _pre_snap 也已清空", g2._pre_snap, None)

print("\n=== ③ 边界钳制：拼接长度不会超过快照，也不会凭空变长 ===")
print("   （`hi <= snap.shape[0]` 那条守卫——Python 超界切片不报错，只会静默截断）")
cfg_pre = Config.load().vad_pre_roll_ms / 1000.0 * SR
raw = speech()
over = [len(s) for s in segs2 if len(s) / SR > len(raw) / SR + (cfg_pre / SR) + 0.3]
check("没有段被拼得超过「语音 + 预滚」上限", over, [])

print()
if FAIL:
    print(f"❌ {len(FAIL)} 项失败：{FAIL}")
    sys.exit(1)
print("✅ 全部通过")
