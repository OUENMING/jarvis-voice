"""AEC 门：把**我们自己播出去**的声音从麦克风里消掉 → 免提也能打断。

为什么需要：内置扬声器 + 内置麦时，麦克风听到的就是我们自己。没有 AEC 时只能
关掉打断 + 播放期间不听麦克风（`config.half_duplex`），代价是免提下无法插话。
实测本机稳态 ERLE 34–36 dB / ACOM ≈ 40 dB（docs/PROBE-AEC-RESULTS-20260919.md）。

**far 参考的质量是这里唯一的优势**：我们知道**确切**在播什么（`Player` 的镜像），
比任何通用 AEC 拿到的参考都干净。

⚠️⚠️ **重采样必须在这一侧（消费侧）做，绝不在 `Player.write()` 里做。**
实测（docs/PROBE-AEC-RESULTS-20260919.md §6.5）：生产侧按块重采样时，
`resample_poly` 的输出长度是 `ceil(L*160/441)`，块长不是 441 的整数倍就每块多半/少半
样本 → 累积漂移 → 12 秒漂 249 样本（5.6ms）→ far 与 near 系统性错位
→ **稳态 ERLE 从 26.5 dB 掉到 13.3 dB**。消费侧的块长是固定的（近端 100ms@16k
= 1600 样本 → 需要 4410 个 44.1k 样本，恰好是 441×10），**精确无漂移**。

⚠️ 本类**只在主循环线程**被调用 —— `AudioProcessor` 非线程安全（一手）。
"""
import numpy as np
from scipy.signal import resample_poly

UP, DOWN = 160, 441          # 44.1k → 16k
INT16 = 32768.0


class AecGate:
    """`accept()` 收近端（麦克风）float32，返回消掉回声后的同规格信号。"""

    def __init__(self, cfg, player):
        from pywebrtc_audio import AudioProcessor   # 延迟导入：不开 AEC 时不付代价

        self.cfg = cfg
        self.player = player
        self.rate = cfg.sample_rate
        self.ap = AudioProcessor(
            sample_rate=self.rate,
            echo_cancellation=True,
            # 实测 aec 与 aec_ns 的转写**逐字相同** → NS 不伤语音，可以开。
            noise_suppression=True,
            high_pass_filter=True,
            # ⚠️ AGC 必须关：它会改增益，干扰项目自己的电平统计与 VAD 噪声底。
            auto_gain_control=False,
            stream_delay_ms=cfg.aec_stream_delay_ms,
        )
        self._delay_frames = int(cfg.aec_stream_delay_ms / 1000.0 * 44100)
        self._far_read = 0                   # 最近一次用的 far 读位置（仅用于观测）
        self.fed = 0                         # 喂进去的近端样本数（可观测）

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
        clean = self.ap.process(near_i16, far_i16)
        self.fed += n16
        return np.asarray(clean, dtype=np.float32) / INT16

    # ---- 观测 / 控制 ----
    @property
    def speech_probability(self) -> float:
        """WebRTC 自己的语音概率（0–1）。播放期间可用作第二道确认。"""
        try:
            return float(self.ap.speech_probability)
        except Exception:
            return 0.0

    def reset(self):
        """切模式 / 换设备时调 —— AEC 内部状态重来（读指针每次都是从播放时钟算的）。"""
        self.ap.reset()
        self._far_read = 0
