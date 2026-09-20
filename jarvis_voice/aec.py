"""AEC 门：把**我们自己播出去**的声音从麦克风里消掉 → 免提也能打断。

为什么需要：内置扬声器 + 内置麦时，麦克风听到的就是我们自己。没有 AEC 时只能
关掉打断 + 播放期间不听麦克风（`config.half_duplex`），代价是免提下无法插话。
实测本机稳态 ERLE 34–36 dB / ACOM ≈ 40 dB（docs/PROBE-AEC-RESULTS-20260919.md）。
⚠️ 但 **ERLE 只在单讲有意义**，双讲下不适用 —— 它证明不了"打断时识别没问题"。

**far 参考的质量是这里唯一的优势**：我们知道**确切**在播什么（`Player` 的镜像），
比任何通用 AEC 拿到的参考都干净。

## 两个后端（`config.aec_backend`）

| | 干什么 | 双讲时 |
|---|---|---|
| `webrtc`（默认） | 自适应滤波 **＋ NLP 残余抑制** | ⚠️ **把近端一起压掉**（辅音全在高频，先丢）|
| `speex` | **只做线性**抵消（无残余抑制） | ✅ 保留近端；代价是**单讲消得差**（57→11 dB）|

`webrtc` 压近端是**真机定案的根因**（原始麦克风 4–8k 13–17.5% → 过 AEC 剩 0.1–0.3%，
`docs/ROOTCAUSE-BARGEIN-ASR-20260920.md`）。离线 20 个合成双讲场景比转写错误率：
`speex` **8.0%** vs `webrtc` **39.8%**（`tools/aec_ab.py --sweep`）。
⚠️ 合成≠真机 ⇒ 留这个切换开关是为了**真机再 A/B 一次**。

⚠️⚠️ **重采样必须在这一侧（消费侧）做，绝不在 `Player.write()` 里做。**
实测（docs/PROBE-AEC-RESULTS-20260919.md §6.5）：生产侧按块重采样时，
`resample_poly` 的输出长度是 `ceil(L*160/441)`，块长不是 441 的整数倍就每块多半/少半
样本 → 累积漂移 → 12 秒漂 249 样本（5.6ms）→ far 与 near 系统性错位
→ **稳态 ERLE 从 26.5 dB 掉到 13.3 dB**。消费侧的块长是固定的（近端 100ms@16k
= 1600 样本 → 需要 4410 个 44.1k 样本，恰好是 441×10），**精确无漂移**。

⚠️ 本类**只在主循环线程**被调用 —— 后端非线程安全（一手）。
"""
import numpy as np
from scipy.signal import resample_poly

UP, DOWN = 160, 441          # 44.1k → 16k
INT16 = 32768.0


def _ring_write(buf: np.ndarray, w: int, x: np.ndarray) -> int:
    """把 `x` 写进环形缓冲 `buf`（当前写指针 `w`），返回新写指针。"""
    n = x.shape[0]
    cap = buf.shape[0]
    if n >= cap:
        buf[:] = x[-cap:]
        return 0
    end = w + n
    if end <= cap:
        buf[w:end] = x
    else:
        k = cap - w
        buf[w:] = x[:k]
        buf[:end - cap] = x[k:]
    return end % cap


def _ring_read(buf: np.ndarray, w: int, n: int, end_offset: int = 0) -> np.ndarray:
    """从环形缓冲 `buf`（写指针 `w`）取**倒数第 `end_offset + n` 到第 `end_offset`** 个样本。"""
    cap = buf.shape[0]
    end = (w - max(0, int(end_offset))) % cap
    idx = (end - n + np.arange(n)) % cap
    return buf[idx].copy()


# ══════════════════════ AEC 后端（内部接缝；两个实现才叫真接缝） ══════════════════════

class _WebrtcBackend:
    """WebRTC AEC3：自适应滤波 **＋ NLP 残余抑制**。

    ⚠️ 损伤来自 NLP —— 双讲时它会把近端语音一起压掉（辅音全在高频，先丢）。
    详见 `docs/ROOTCAUSE-BARGEIN-ASR-20260920.md`。
    """

    name = "webrtc"

    def __init__(self, rate: int):
        from pywebrtc_audio import AudioProcessor   # 延迟导入：不选它就不付代价
        self.ap = AudioProcessor(
            sample_rate=rate,
            echo_cancellation=True,
            # 实测 aec 与 aec_ns 的转写**逐字相同** → NS 不伤语音，可以开。
            noise_suppression=True,
            high_pass_filter=True,
            # ⚠️ AGC 必须关：它会改增益，干扰项目自己的电平统计与 VAD 噪声底。
            auto_gain_control=False,
            # ⚠️ 恒为 0：far 参考已在 `AecGate.accept()` 里往前读 150ms 做过物理对齐，
            # 对齐后残余延迟 ≈0 —— 这里再给 150 = 补偿两次 → ERLE 39 dB 掉到 1 dB。
            stream_delay_ms=0,
        )

    def process(self, near_i16: np.ndarray, far_i16: np.ndarray) -> np.ndarray:
        return np.asarray(self.ap.process(near_i16, far_i16), dtype=np.int16)

    def reset(self) -> None:
        self.ap.reset()

    @property
    def speech_probability(self) -> float:
        """WebRTC 自己的语音概率（0–1）。播放期间可用作第二道确认。"""
        try:
            return float(self.ap.speech_probability)
        except Exception:
            return 0.0


class _SpeexBackend:
    """SpeexDSP 的**纯线性**分块频域自适应滤波 —— **不做残余抑制**。

    **为什么选它**：`speex_echo_cancellation()` 的输出是 `麦克风 − 滤波后回声`，
    它**本身不做频谱后滤波**（源码 `mdf.c`：`power_1` 是自适应步长，不是输出增益）。
    会压近端的那级残余抑制在 `speex_preprocess_run()` 里 —— 而 aec-rs 的
    `enable_preprocess` 参数正好能把它关掉。**关掉就是我们要的"只做线性抵消"**。

    离线 20 个合成双讲场景（660 字）转写错误率：本后端 **8.0%** vs WebRTC **39.8%**。
    ⚠️ 合成 ≠ 真机，所以这个切换开关存在的意义就是**在真机上再 A/B 一次**。

    ⚠️ **aec-rs 的固有缺陷**（不是用法错误，改不掉）：它从不调
    `SPEEX_ECHO_SET_SAMPLING_RATE`，而 `libaec.dylib` **只导出 3 个 Aec* 符号**
    （`nm -gU` 核实）→ Speex 内部**永远认为采样率是 8000**。我们喂 16k 时
    `beta0 = 2*frame_size/8000` 比正确值大一倍（泄漏翻倍）→ 收敛上限被压低。
    实测纯回声下 WebRTC 能到 57 dB、本后端只有 11 dB。**这正是"牺牲单讲换双讲"**。

    ⚠️ 要求**定长帧**（`FRAME`）。`mic_blocksize=1600` 恰好 = 10 帧，整除，
    所以正常路径下不需要缓冲、也没有额外延迟；万一不整除，余数**原样透传**
    并记进 `unframed`（可观测），不会静默出错。
    """

    name = "speex"
    FRAME = 160                      # 10ms @16k；Speex 的标定帧长

    def __init__(self, rate: int, filter_length: int):
        try:
            from pyaec import Aec        # 延迟导入
        except ImportError as e:         # 响亮失败，别静默退回 webrtc
            raise ImportError(
                "aec_backend=speex 需要 pyaec：`.venv/bin/pip install pyaec`"
                "（或把 JARVIS_AEC_BACKEND 改回 webrtc）") from e
        self.rate = rate
        self._aec = Aec(frame_size=self.FRAME, filter_length=filter_length,
                        sample_rate=rate, enable_preprocess=False)
        self.filter_length = filter_length
        self.unframed = 0            # 没走 AEC 的样本数（正常恒为 0）

    def process(self, near_i16: np.ndarray, far_i16: np.ndarray) -> np.ndarray:
        n = int(near_i16.shape[0])
        k = (n // self.FRAME) * self.FRAME
        out = np.array(near_i16, dtype=np.int16)      # 余数先按原样占位
        for i in range(0, k, self.FRAME):
            out[i:i + self.FRAME] = self._aec.cancel_echo(
                near_i16[i:i + self.FRAME].tolist(),
                far_i16[i:i + self.FRAME].tolist())
        self.unframed += n - k
        return out

    def reset(self) -> None:
        # pyaec 不暴露 reset → 重建（它没有需要保留的外部状态）。
        from pyaec import Aec
        self._aec = Aec(frame_size=self.FRAME, filter_length=self.filter_length,
                        sample_rate=self.rate, enable_preprocess=False)
        self.unframed = 0

    @property
    def speech_probability(self) -> float:
        return 0.0                    # Speex 不提供；调用方已按 0 处理


def _make_backend(cfg) -> _WebrtcBackend | _SpeexBackend:
    kind = (cfg.aec_backend or "webrtc").lower()
    if kind == "webrtc":
        return _WebrtcBackend(cfg.sample_rate)
    if kind == "speex":
        return _SpeexBackend(cfg.sample_rate, cfg.aec_speex_filter_length)
    raise ValueError(f"未知 aec_backend={kind!r}（可选 webrtc / speex）")


class AecGate:
    """`accept()` 收近端（麦克风）float32，返回消掉回声后的同规格信号。"""

    def __init__(self, cfg, player):
        self.cfg = cfg
        self.player = player
        self.rate = cfg.sample_rate
        # 后端可切（`aec_backend`）—— 选谁完全藏在 `accept()` 后面，调用方无感。
        self.be = _make_backend(cfg)
        self._delay_frames = int(cfg.aec_stream_delay_ms / 1000.0 * 44100)
        self._far_read = 0                   # 最近一次用的 far 读位置（仅用于观测）
        self.fed = 0                         # 喂进去的近端样本数（可观测）
        # 🆕 **原始麦克风**（未过 AEC）的环形缓冲，只用于诊断 A/B。
        # 为什么要它：真机观测到「打断时说的话高频被削 4-5 倍」，但**成因有两类**——
        #   ① AEC 在双讲时压近端（可离线复现：4-8k 掉 5.7×）
        #   ② 笔记本麦 + 距离本身就丢高频（物理原因）
        # 两者的修法完全不同，只能靠**同一句的 AEC 前后对照**分开。
        # ⚠️ 近端不过重采样（进出都是 16k 等长），所以原始流与输出流**同时钟同长度**，
        #    取"最近 N 个样本"就是同一时间窗，不需要额外的位置映射。
        self._raw_cap = self.rate * 30       # 留 30s 够用
        self._raw = np.zeros(self._raw_cap, dtype=np.int16)
        self._raw_w = 0
        # 🆕 **far 参考**（实际喂给 AEC3 的那份）的环形缓冲。
        # 为什么要它：判「近端是被 AEC 压坏的，还是本来就差」需要**同一时间窗**的
        # far。没有 far 就只能猜，有了它才能离线用别的 AEC 重跑同一段音频。
        # ⚠️ 长度与 `_raw` **逐样本锁定**：`far_i16` 在下面被裁/补到与 `near_i16`
        #    等长（`n16`），所以两个写指针永远同值 —— 这是构造保证，不是约定。
        self._far = np.zeros(self._raw_cap, dtype=np.int16)
        self._far_w = 0

    # ---- 主入口 ----
    def accept(self, chunk_f32: np.ndarray) -> np.ndarray:
        """`chunk_f32`：16k float32 [-1,1]。返回同规格、已消回声的信号。"""
        n16 = int(chunk_f32.shape[0])
        if n16 == 0:
            return chunk_f32
        need44 = n16 * DOWN // UP

        # far 读位置**锚定在播放时钟上**（`emit_frames`），不自由推进。
        #
        # ⚠️ 一开始写的是自由推进（每次 `_far_read += need44`），那是错的：
        #    它默认了 accept() 一定按实时速率被调用。一旦主循环赶工、或麦克风队列
        #    积压后一次性排空，读指针就会**跑到播放位置之前** → `far_slice` 长期
        #    返回空 → AEC 参考全零 → 什么都不消。是"两个时钟"那类 bug 的反方向。
        #    锚定之后最坏只是**瞬时**偏移（AEC3 的 delay estimator 能吸收），
        #    不会长期失准。
        # 稳态下两者等价：近端每 100ms 一块 → 播放也正好推进 4410 帧。
        # ⚠️⚠️ **这里的偏移和 `stream_delay_ms` 是同一个延迟，只能补一次。**
        #
        # 离线实测（`tmp/verify_delay.py`，真实回声延迟 150ms）：
        #   far 偏移 −150 + stream_delay 150 → **1.04 dB** ❌ ← 曾经的配置（补了两次）
        #   far 偏移 −150 + stream_delay   0 → **39.09 dB** ✅ ← 现在
        #   far 偏移    0 + stream_delay 任意 → 39 dB（**但生产不可达**：far 镜像是
        #       回调写的，只有**已播出**的帧 —— 读 `emit_frames()` 等于读未来，恒空）
        #
        # 物理上：麦克风里的回声来自 ~150ms 前播出的音频，所以参考必须**往前读**
        # 150ms 才能对齐。对齐之后 AEC3 面对的残余延迟 ≈0，**所以 `stream_delay_ms`
        # 必须是 0（= 让它自估）**。给它 150 等于让它再找 150ms，直接失效。
        #
        # 历史：偏移本身是对的，错的是我同时把 `stream_delay_ms` 也设成 150。
        # OCR 代码审查在 `aec.py:66` 独立指出「延迟被补偿了两次」。
        target = max(0, self.player.emit_frames() - self._delay_frames)
        self._far_read = target

        far44 = self.player.far_slice(target, need44)
        # 启动瞬间/被环覆盖时可能短 → 前面补零（补零 = 参考"当时是静音"，比错位安全）
        if far44.shape[0] < need44:
            far44 = np.concatenate(
                [np.zeros(need44 - far44.shape[0], dtype=np.int16), far44])
        far16 = resample_poly(far44.astype(np.float32), UP, DOWN)
        # 长度对齐到近端（整除时精确相等；不整除时裁/补）
        if far16.shape[0] < n16:
            far16 = np.concatenate([far16, np.zeros(n16 - far16.shape[0], np.float32)])
        elif far16.shape[0] > n16:
            far16 = far16[:n16]

        # 读指针按**近端块长反算**推进，绝不由重采样输出长度决定 —— 这是零漂移的关键。
        self._far_read += need44

        near_i16 = np.clip(chunk_f32 * INT16, -INT16, INT16 - 1).astype(np.int16)
        far_i16 = np.clip(far16 * INT16, -INT16, INT16 - 1).astype(np.int16)
        # 存原始近端与 far（诊断用，见 __init__ 的说明）。O(1)，无分配。
        self._raw_w = _ring_write(self._raw, self._raw_w, near_i16)
        self._far_w = _ring_write(self._far, self._far_w, far_i16)
        clean = self.be.process(near_i16, far_i16)
        self.fed += n16
        return np.asarray(clean, dtype=np.float32) / INT16

    # ---- 观测 / 控制 ----
    @property
    def backend_name(self) -> str:
        return self.be.name

    @property
    def speech_probability(self) -> float:
        """后端自己的语音概率（0–1）；Speex 不提供，恒 0。播放期间可作第二道确认。"""
        return self.be.speech_probability

    def raw_slice(self, n: int, end_offset: int = 0) -> np.ndarray:
        """取**未过 AEC**的原始近端：从 `end_offset + n` 个样本前，到 `end_offset` 个样本前。

        诊断 A/B 用。⚠️ `end_offset` 是必需的，不能只取"最近 n 个"：
        VAD 要等 `min_silence_duration`(0.5s) 静音之后才吐段，所以**段里的音频
        比"此刻"早至少 0.5 秒** —— 直接取最近 n 个会拿到段之后的窗口，对不上。
        （写第一版时就是漏了这个，互相关相关度只有 0.1-0.3，根本没法比。）
        """
        n = max(0, int(n)); off = max(0, int(end_offset))
        avail = max(0, self.fed - off)
        n = min(n, self._raw_cap, avail)
        if n == 0:
            return np.zeros(0, dtype=np.int16)
        return _ring_read(self._raw, self._raw_w, n, off)

    def far_slice(self, n: int, end_offset: int = 0) -> np.ndarray:
        """取**实际喂给 AEC 的 far 参考**，时间窗与 `raw_slice(n, end_offset)` 完全相同。

        配对使用才有意义：`(raw_slice(...), far_slice(...))` 就是「那一刻 AEC 看到的
        近端与远端」。拿到这一对，就能离线用**别的** AEC 重跑同一段音频，
        直接比谁的输出转写更准 —— 不用再跑真机。
        """
        n = max(0, int(n)); off = max(0, int(end_offset))
        avail = max(0, self.fed - off)
        n = min(n, self._raw_cap, avail)
        if n == 0:
            return np.zeros(0, dtype=np.int16)
        return _ring_read(self._far, self._far_w, n, off)

    def reset(self):
        """切模式 / 换设备时调 —— AEC 内部状态重来（读指针每次都是从播放时钟算的）。"""
        self.be.reset()
        self._raw[:] = 0
        self._raw_w = 0
        self._far[:] = 0
        self._far_w = 0
        self._far_read = 0
        # ⚠️ `fed` 必须一起清 —— 它只被 `raw_slice`/`far_slice` 当"可用量"用。
        # 留着旧值的话，reset 后那两个方法会**返回一大段全零**，看起来像"有数据，
        # 只是安静"，而真相是"还没有数据"。诊断工具给出误导性数据比报错更糟。
        self.fed = 0
