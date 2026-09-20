#!/usr/bin/env python3
"""far 参考落盘：让「换 AEC」这件事能离线判定（不用再跑真机）。

**要解决的问题**：已定案「打断时识别差 = WebRTC AEC3 双讲时削近端高频」，
但**换什么**不能靠猜。`cozec/aec` 基准说 FDAF 类近端 SDR 高 10 dB，可那是
**信号级**的、从没测过 ASR；而实测（`tools/aec_ab.py --sweep`）显示
**near-SDR 根本预测不了转写**（三者 SDR 几乎相同，转写差 5 倍）。

⇒ 唯一判据是「同一段音频换个 AEC，转写准不准」。这需要**同一时间窗的
`(未过 AEC 的近端, far 参考)` 一对**。本项目以前只存了近端，缺 far。

**本测试守的不变量**（都是"错了会静默给出错误结论"的那类）：

  ① `raw_slice` / `far_slice` 的**时间窗必须逐样本相同** —— 否则离线 A/B
     是在拿两段不同时间的声音对比，结论无效（这个 bug 写过一次，见
     `ROOTCAUSE-BARGEIN-ASR-20260920.md` §5 的 `end_offset` 教训）。
  ② `end_offset` 不能被忽略（VAD 等静音才吐段，段比"此刻"早 0.5s）。
  ③ 同一次打断的三份落盘必须**共享文件名前缀** —— 否则事后只能靠"时间上相邻"猜配对。

⚠️ 离线，不开任何音频设备。
"""
import os
import sys
import tempfile
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis_voice.aec import AecGate, _ring_read, _ring_write      # noqa: E402
from jarvis_voice.bargein_trace import BargeinTrace                # noqa: E402

FAIL = []


def check(label, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {label}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(label)


def sig(v):
    """用 (首, 尾, 和) 代表一个数组 —— 别打印整个数组，会把日志刷爆。"""
    return (int(v[0]), int(v[-1]), int(v.sum())) if v.size else None


# ─────────────────── 假 player：只提供 far 镜像所需的两个方法 ───────────────────
class FakePlayer:
    def __init__(self, far_frames: int = 44100 * 5):
        self._emitted = 0
        self._far = np.zeros(far_frames, dtype=np.int16)
        # 填一个可辨认的图案：值 = (索引 % 30000) + 1
        self._far[:] = (np.arange(far_frames) % 30000) + 1

    def emit_frames(self):
        return self._emitted

    def far_slice(self, start_frame, n_frames):
        s = max(0, int(start_frame))
        e = min(len(self._far), s + int(n_frames))
        if s >= len(self._far):
            return np.zeros(0, dtype=np.int16)
        return self._far[s:e].copy()

    def advance(self, frames):
        self._emitted += frames


class FakeAP:
    """假的 AudioProcessor：不消回声，只把近端原样返回（本测试与 AEC 效果无关）。"""

    def __init__(self):
        self.calls = 0

    def process(self, near_i16, far_i16):
        self.calls += 1
        assert len(near_i16) == len(far_i16), "近端与 far 必须等长"
        return near_i16

    def reset(self):
        pass


class FakeCfg:
    sample_rate = 16000
    aec_stream_delay_ms = 150


def make_gate(cap=16000 * 3):
    """造一个可用的 AecGate：绕过 __init__（不然会真的建 AudioProcessor）。"""
    g = object.__new__(AecGate)
    g.cfg = FakeCfg()
    g.rate = 16000
    g.player = FakePlayer()
    g.be = FakeAP()
    g._delay_frames = int(150 / 1000.0 * 44100)
    g._far_read = 0
    g.fed = 0
    g._raw_cap = cap
    g._raw = np.zeros(cap, dtype=np.int16)
    g._raw_w = 0
    g._far = np.zeros(cap, dtype=np.int16)
    g._far_w = 0
    return g


print("=== ① 环形缓冲原语：写入 / 带偏移读取 / 回绕 ===")
buf = np.zeros(1000, dtype=np.int16)
w = 0
a = np.arange(1, 3001, dtype=np.int16)          # 3000 个样本，超过容量
w = _ring_write(buf, w, a)
check("写超容量后只剩尾部 1000 个", sig(_ring_read(buf, w, 1000)),
      sig(np.arange(2001, 3001, dtype=np.int16)))
check("带偏移：跳过最后 50 个取 100 个", sig(_ring_read(buf, w, 100, 50)),
      sig(np.arange(2851, 2951, dtype=np.int16)))
check("取 0 个 → 空", _ring_read(buf, w, 0).size, 0)

print("\n=== ② `far_slice` 与 `raw_slice` 的时间窗必须**逐样本相同** ===")
print("   ⚠️ 这是整个离线 A/B 的地基：窗错一点，比的就是两段不同时间的声音")
g = make_gate()
near_chunks, far_chunks = [], []
# 每次喂 1600 样本（= 生产的块长），near 与 far 用**不同**的图案，
# 这样"取错了 buffer"或"窗对不上"都会立刻暴露。
for i in range(4):
    n = np.full(1600, 1000 + i, dtype=np.int16)
    g.player.advance(4410)                       # far 读指针锚在播放时钟上
    g.accept(n.astype(np.float32) / 32768.0)
    got_far = g.far_slice(1600, 0)
    near_chunks.append(n)
    far_chunks.append(got_far)

near_all = np.concatenate(near_chunks)
far_all = np.concatenate(far_chunks)

check("raw_slice 全量 == 喂进去的近端", sig(g.raw_slice(6400)), sig(near_all))
check("far_slice 全量 == AEC 当时实际收到的 far", sig(g.far_slice(6400)), sig(far_all))
check("⚠️ far 与 near **不是**同一份数据（否则这个测试是空转）",
      bool(np.array_equal(far_all, near_all)), False)

print("\n=== ③ `end_offset` 必须真的生效（段比'此刻'早 0.5s）===")
g2 = make_gate()
for i in range(4):
    g2.player.advance(4410)
    g2.accept(np.full(1600, 1000 + i, dtype=np.int16).astype(np.float32) / 32768.0)
# 最后一块（第 4 块，值 1003）之后往前 1600 → 应是第 3 块（值 1002）
check("带 end_offset=1600 取 1600 个 → 取到**上一块**",
      int(g2.raw_slice(1600, 1600)[0]), 1002)
check("带 end_offset=1600 取 1600 个（far 同步）",
      int(g2.far_slice(1600, 1600)[0]), int(g2.far_slice(3200, 0)[0]))
check("end_offset 超过已喂量 → 空数组不崩", g2.raw_slice(1600, 10 ** 9).size, 0)
check("end_offset 超过已喂量（far 同样）", g2.far_slice(1600, 10 ** 9).size, 0)

print("\n=== ④ far 与 raw 的写指针**始终同值**（长度锁定的构造保证）===")
g3 = make_gate(cap=1000)                          # 故意造回绕
for i in range(10):
    g3.player.advance(441)
    g3.accept(np.full(100, i, dtype=np.int16).astype(np.float32) / 32768.0)
    if g3._raw_w != g3._far_w:
        break
check("回绕 10 次后两个写指针仍相同", g3._raw_w == g3._far_w, True)
# 回绕后取窗正确性：`(n,)` 必须等于 `(2n,)[n:]` —— 一个自洽判据，
# 掩码/索引写错时立刻不等。（别拿 far 去比 near：两者本来就是不同信号。）
check("回绕后 far 的 100 窗口自洽", sig(g3.far_slice(100, 0)),
      sig(g3.far_slice(200, 0)[100:]))
check("回绕后 raw 的 100 窗口自洽", sig(g3.raw_slice(100, 0)),
      sig(g3.raw_slice(200, 0)[100:]))
check("回绕后两个窗等长", g3.far_slice(100, 0).size, g3.raw_slice(100, 0).size)
check("reset() 清掉 far", (g3.reset(), int(g3.far_slice(10, 0).sum()))[1], 0)

print("\n=== ⑤ 同一次打断的三份落盘共享前缀（配对靠文件名，不靠猜）===")
with tempfile.TemporaryDirectory() as tmp:
    tr = BargeinTrace(path=os.path.join(tmp, "trace.jsonl"), enabled=True)
    stem = tr.new_stem()
    p1 = tr.save_pcm(np.zeros(1600, np.int16), "bargein", stem=stem)
    p2 = tr.save_pcm(np.ones(1600, np.int16), "bargein-raw", stem=stem)
    p3 = tr.save_pcm(np.full(1600, 2, np.int16), "far", stem=stem)
    names = sorted(os.path.basename(p) for p in (p1, p2, p3))
    print(f"    落盘：{names}")
    check("三份都带同一个 stem", all(n.startswith(stem) for n in names), True)
    check("tag 各不相同（可区分）",
          len({n.split("-", 2)[-1] for n in names}), 3)
    # ⚠️ far 也必须是真的 wav（否则离线读不了）
    with wave.open(p3, "rb") as w3:
        check("far 是合法的 16k 单声道 wav",
              (w3.getframerate(), w3.getnchannels(), w3.getnframes()), (16000, 1, 1600))
    # 配对规则：stem + "-far.wav" 就能找到对应的 raw
    raw_name = os.path.basename(p2)
    far_name = os.path.basename(p3)
    check("由 raw 文件名能反推 far 文件名",
          far_name, raw_name.replace("-bargein-raw.wav", "-far.wav"))
    # 不传 stem 时仍然能落盘（向后兼容）
    check("不传 stem 也能存", tr.save_pcm(np.zeros(10, np.int16), "solo") is not None, True)

print()
if FAIL:
    print(f"❌ {len(FAIL)} 项失败：{FAIL}")
    sys.exit(1)
print("✅ 全部通过")
