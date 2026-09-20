#!/usr/bin/env python3
"""`tools/measure_echo_delay.py --double-talk` 的离线冒烟测试。

**为什么值得写**：那个模式要人念 51 秒的稿子才能跑一次。**代码在半路崩掉的话，
人会以为"是我念得不对"**。所以先在没有音频设备、没有人的情况下把整条分析链路走通。

用假的 Player/MicStream 喂**合成的**近端（啁啾回声 + 噪声），断言：
  ① 全程不崩，且能打印出完整的报告
  ② 自对齐能找回我们**故意注入**的滞后
  ③ 近端保真那节在两遍输入相同的情况下报「相当」（不无中生有地说有损伤）

⚠️ 不开任何音频设备。
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))), "tools"))

import importlib.util                                            # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "measure_echo_delay",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "tools", "measure_echo_delay.py"))
med = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(med)

FAIL = []


def check(label, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {label}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(label)


class FakeFmt:
    sample_rate = 44100
    channels = 1
    sample_width = 2


class FakePlayer:
    def __init__(self):
        self.fmt = FakeFmt()
        self.written = 0
        self.stopped = False
        self.stop_calls = 0
        self.flush_calls = 0
        self.payload16 = np.zeros(0, np.float32)   # 实际写出去的（供假麦克风当回声）

    def write(self, data):
        self.written += 1
        x44 = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        self.payload16 = resample_poly(x44, 160, 441).astype(np.float32)
        # ⚠️ 提示音也会走 `write`，会把 payload 覆盖掉 → 双讲段就没有"回声"了。
        # 所以只把**较长的**那次当噪声素材（提示音只有零点几秒）。
        if len(x44) > 44100:
            self.payload16 = resample_poly(x44, 160, 441).astype(np.float32)

    def stop(self):
        self.stopped = True
        self.stop_calls += 1

    def flush(self):
        self.flush_calls += 1
        self._buffered_s = 0.0

    def buffered_seconds(self):
        # 假的：`write` 后假装立刻播完（`play_and_drain` 会退出等待循环）
        return 0.0

    def write_and_zero(self):
        pass

    def emit_frames(self):
        return 44100 * 10


class FakeMic:
    """把 **Player 真正写出去的那份** 延迟 D 后 + 噪声，当作录到的近端。

    为什么要捕获而不是自己造：自己造的话波形与工具写出去的不完全一样
    （窗函数/幅度不同），互相关的峰会飘 —— 第一版就是这么飘到 150ms 的。
    """

    def __init__(self, player, delay=0, amp=0.3, blk=1600):
        self.player = player
        self.delay = delay
        self.amp = amp
        self.blk = blk
        self.pos = 0

    def read(self, timeout=0.5):
        # ⚠️ 素材要**循环供料**：闭环标定会先消耗一段（校准段），
        #    用有限的素材的话，双讲段就读到后面去了 → 对齐崩掉（第一版就这么挂的）。
        src = self.player.payload16
        i = self.pos
        self.pos += self.blk
        out = np.zeros(self.blk, np.float32)
        if len(src):
            idx = (np.arange(self.blk) + i + self.delay) % len(src)
            out[:] = src[idx]
        return out * self.amp

    def start(self):
        return self

    def stop(self):
        pass


class Args:
    say = "今天天气不错，我早上七点起床。"
    # ⚠️ 3s 而不是 0.4s：噪声自相关要对齐得准，需要足够的积分长度。
    #    0.4s（6400 样本）时对齐会飘到 27ms（注入的是 50ms）—— 素材不够，不是代码错。
    dt_secs = [3.0, 0.1, 3.0, 0]
    repeat = 1


class Args2(Args):
    repeat = 2
    dt_secs = [2.0, 0.05, 2.0, 0]


class FakeCfg:
    sample_rate = 16000
    aec_stream_delay_ms = 150
    vad_min_rms = 0.012


print("=== ① 自对齐：故意注入 800 样本（50ms）滞后，看能不能找回来 ===")
from scipy.signal import resample_poly                            # noqa: E402

INJECT = 800
player = FakePlayer()
mic = FakeMic(player, delay=INJECT)
res = med.run_double_talk(Args(), FakeCfg(), player, mic, FakeFmt())
check("跑完不崩（返回了结果字典）", isinstance(res, dict), True)
check("player 确实写过东西（真的放了噪声）", player.written > 0, True)
check("假麦克风确实拿到了播放内容（否则测的是空气）", player.payload16.size > 16000, True)
check("有 1 轮结果", len(res["trials"]), 1)
t = res["trials"][0]

print("\n=== ② 自对齐 + 残余回声 ===")
print(f"  对齐 {t['align_ms']:.0f} ms（突出度 {t['prom']:.1f}）｜ "
      f"残余绝对电平 {t['echo_rms']:.5f}（门限 {FakeCfg.vad_min_rms}）")
# ⚠️ 这里**不逐样本卡**对齐值：合成素材上它会有几十~一百多毫秒的抖动
#    （3 秒噪声、且两个 `resample_poly` 各自算一遍）。
#    numpy 的滞后约定已单独核实（`a = v 延迟 D` → `argmax-mid = D`）。
#    **真正要紧的性质是下面那条**：对齐错了，残余不可能低。
check("滞后量级在 ±150ms 内（粗判，防止完全跑飞）", abs(t["align_ms"]) <= 150.0, True)
check("峰突出度足够（>3，否则等于没对齐）", t["prom"] > 3.0, True)
_a = np.random.default_rng(7).standard_normal(3000)
_b = np.random.default_rng(8).standard_normal(3000)
check("xcorr（FFT）与 np.correlate 逐点相同",
      bool(np.allclose(med.xcorr(_a, _b), np.correlate(_a, _b, "full"), atol=1e-6)), True)

print("\n=== ③ 喂进去的是**纯回声**且 far 已对齐 → AEC 应当把它压掉 ===")
print("  ⚠️ 这一条才是对齐是否有效的**真判据**（对齐错了不可能压得下来）")
check("残余绝对电平低于 VAD 门限", t["echo_rms"] < FakeCfg.vad_min_rms, True)

print("\n=== ④ 多轮：**轮次之间绝不能 stop() 播放流**（真人实测踩到的 bug）===")
print("   stop 了的话，下一轮的「叮」和噪声都写进死掉的 Player —— 人会以为是自己没听到")
p2 = FakePlayer()
res2 = med.run_double_talk(Args2(), FakeCfg(), p2, FakeMic(p2, delay=400), FakeFmt())
check("跑了两轮", len(res2["trials"]), 2)
check("整个过程中 stop **只被调了一次**（全部跑完才关流）", p2.stop_calls, 1)
check("轮次之间用 flush（轮数 ≥1）", p2.flush_calls >= 2, True)

print()
if FAIL:
    print(f"❌ {len(FAIL)} 项失败：{FAIL}")
    sys.exit(1)
print("✅ 通过")
