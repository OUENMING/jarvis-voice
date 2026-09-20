"""流式 VAD 门。

包 `sherpa_onnx.VoiceActivityDetector`，把任意长度的 float32 音频流切成
完成的语音段（utterance）。**这是整条链路的常开入口**——它同时承担
"用户开始说话"（打断信号）与"用户说完了"（端点）两个职责。

API 事实（2026-09-16 实测）：
  - `accept_waveform` 吃 **float32 [-1, 1]**
  - 模型要求固定窗长（silero 512 / ten-vad 256），故内部按窗缓冲
  - `front` 返回 `SpeechSegment`：`.start`（起始采样点）、`.samples`（**list**，非 ndarray）
  - 实测 RTF ≈ 0.0033（1s 音频 ≈ 3.3ms），单流可忽略不计
"""
import threading

import numpy as np
import sherpa_onnx

from .config import Config

# 噪声底初次估计要看的块数（每块 = mic_blocksize/16000 = 100ms）→ 12 块 ≈ 1.2s。
# 为什么需要它：见 `_update_floor` 的注释 —— 直接采信第一块会把 floor 锁成语音电平。
_FLOOR_PROBE_CHUNKS = 12


class VadGate:
    def __init__(self, cfg: Config):
        if cfg.vad_backend == "silero":
            m = sherpa_onnx.SileroVadModelConfig(
                model=cfg.vad_model, threshold=cfg.vad_threshold,
                min_silence_duration=cfg.vad_min_silence,
                min_speech_duration=cfg.vad_min_speech,
                max_speech_duration=cfg.vad_max_speech,
                window_size=cfg.vad_window)
            vcfg = sherpa_onnx.VadModelConfig(silero_vad=m)
        else:
            m = sherpa_onnx.TenVadModelConfig(
                model=cfg.vad_model, threshold=cfg.vad_threshold,
                min_silence_duration=cfg.vad_min_silence,
                min_speech_duration=cfg.vad_min_speech,
                max_speech_duration=cfg.vad_max_speech,
                window_size=cfg.vad_window)
            vcfg = sherpa_onnx.VadModelConfig(ten_vad=m)
        self.cfg = cfg
        self.win = cfg.vad_window
        self.vad = sherpa_onnx.VoiceActivityDetector(vcfg, buffer_size_in_seconds=60)
        self._buf: np.ndarray = np.zeros(0, dtype=np.float32)
        # 噪声底：快速下降、缓慢上升（经典做法）。用于段级信噪比门限。
        # ⚠️ 0.0 = **还没估出来**（哨兵值），不是"噪声底是 0"。
        self._floor = 0.0
        self._floor_probe = 0      # 初次估计已看了多少块（见 _FLOOR_PROBE_CHUNKS）
        self.rejected = 0          # 被 SNR 门限丢弃的段数（**累计量，reset 不清零**：留着看历史）
        # ⚠️ sherpa 的 VAD **不是线程安全的**。仪表盘的"恢复监听"会从 uvicorn 线程
        # 调 reset()，而主线程同时在 accept_waveform —— 并发会让内部状态损坏，
        # 表现为"VAD 从此不再出段 = 助手不响应"（异常还会被线程护栏吞掉，极难查）。
        self._lock = threading.Lock()

    # ---- 噪声底 ----
    def _update_floor(self, chunk: np.ndarray):
        """维护噪声底。快降慢升（经典做法）。

        ⚠️⚠️ **初次估计不能直接采信第一块。** 原写法
        `if self._floor == 0.0 or rms < self._floor: self._floor = rms` 有个 high 级缺陷
        （`ocr` 代码审查 2026-09-20 报的）：
          `_floor == 0.0` 同时表示"未初始化"和"实测为 0"，于是**第一块**（很可能整块
          就是语音，比如刚启动时主人正在说话）直接把 floor 设成语音电平。此后
          `need = max(floor * vad_min_snr, vad_min_rms)` = **3× 语音** 恒大于真语音段 RMS
          → **只要主人在连续说话，段就一直被 `_passes_gate` 丢掉，助手不响应**；
          只有等到某个明显更静的块才把 floor 拉下来。
        这还会**叠加到话首**上：讲话的开头本来就有 VAD 的 min_speech_duration 延迟，
        再叠一层"前几百毫秒被门限丢"，声母更容易没。

        修法：前 `_FLOOR_PROBE_CHUNKS` 块取**最小值**（噪声底就是观测到的最小值），
        并把结果**封顶在 `vad_min_rms`**（项目的绝对下限）—— 这样：
          · 首块是语音 → 封顶后 need 最多 3×0.012=0.036，真语音（~0.05）能过
          · 噪声大的房间 → 也不会因为一次性采信某块而把 floor 抬得过高
        探针期结束后回到原有的快降慢升（那段逻辑没动）。
        """
        rms = float(np.sqrt(np.mean(np.square(chunk)))) if chunk.size else 0.0
        if rms <= 0.0:
            return
        if self._floor_probe < _FLOOR_PROBE_CHUNKS:
            cur = rms if self._floor == 0.0 else min(self._floor, rms)
            # 封顶在绝对下限：防止"首块即语音"把门限抬成 3×语音。
            # `max(..., 1e-6)` 防有人把 vad_min_rms 配成 0（那会让 need 恒为 0，什么都放行）。
            self._floor = min(cur, max(self.cfg.vad_min_rms, 1e-6))
            self._floor_probe += 1
            return
        if rms < self._floor:
            self._floor = rms                      # 立即跟随下降
        else:
            self._floor = 0.995 * self._floor + 0.005 * rms   # 缓慢上升

    def _passes_gate(self, pcm_f32: np.ndarray) -> bool:
        """段级门限：必须**明显高于噪声底**才算真语音。

        为什么必须要这一道（实测 2026-09-16）：SenseVoice 对**任何**非语音输入
        都会幻觉出文本——纯静音→'그。'、白噪声→'Yeah.'、点击→'Okay.'，
        **没有 no-speech 标记**。所以在文本层过滤是不可能的，只能在音频层拦。
        """
        if pcm_f32.size == 0:
            return False
        rms = float(np.sqrt(np.mean(np.square(pcm_f32))))
        floor = max(self._floor, 1e-6)
        need = max(floor * self.cfg.vad_min_snr, self.cfg.vad_min_rms)
        ok = rms >= need
        if not ok:
            self.rejected += 1
        return ok

    # ---- 输入 ----
    def accept(self, chunk_f32: np.ndarray) -> list[np.ndarray]:
        """喂一段 float32 [-1,1]。返回**本次新完成**的语音段（int16 一维数组）。"""
        if chunk_f32.dtype != np.float32:
            chunk_f32 = chunk_f32.astype(np.float32)
        with self._lock:                      # 与 reset() 串行化，见 __init__ 注释
            self._update_floor(chunk_f32)
            self._buf = np.concatenate([self._buf, chunk_f32]) if self._buf.size else chunk_f32
            done: list[np.ndarray] = []
            while self._buf.size >= self.win:
                frame, self._buf = self._buf[:self.win], self._buf[self.win:]
                self.vad.accept_waveform(frame)
                done.extend(self._drain())
            return done

    def flush(self) -> list[np.ndarray]:
        """收尾（停止输入时调用）：把未闭合的语音段也交出来。"""
        with self._lock:
            self.vad.flush()
            return self._drain()

    def _drain(self) -> list[np.ndarray]:
        out = []
        while not self.vad.empty():
            seg = self.vad.front
            # ⚠️ 必须在 pop() **之前**取 samples：front 是指向检测器内部缓冲的引用，
            # pop() 之后它即失效，samples 读出来是空列表 → 语音段被静默丢弃（实测踩过）。
            pcm = np.asarray(seg.samples, dtype=np.float32)
            self.vad.pop()
            if pcm.size and self._passes_gate(pcm):
                out.append(np.clip(pcm * 32767.0, -32768, 32767).astype(np.int16))
        return out

    # ---- 状态 ----
    @property
    def speaking(self) -> bool:
        """当前是否处于语音中——用作"用户开口了"的打断触发信号。

        ⚠️ 必须取锁：仪表盘的"恢复监听"会从 uvicorn 线程 `reset()` 同一个
        sherpa 对象，而它**非线程安全**（这正是 _lock 存在的理由）—— 审计发现
        这里漏了锁，等于前门锁了后门开着。
        """
        with self._lock:
            return bool(self.vad.is_speech_detected())

    def reset(self):
        """重新干净地开始听。**必须连噪声底一起复位** —— 见 `_update_floor` 的注释。

        ⚠️ 原实现只复位 VAD 与 `_buf`，`_floor` 会带着**上一个会话的值**穿过来：
        4 个调用点（orchestrator `:304` `:308` 半双工门、`:426` 仪表盘「恢复监听」、
        `:480` 切模式）之后，新一轮真人语音仍在用旧门限判定 —— 若旧值偏高就继续被丢。
        `rejected` 刻意**不**清零：它是累计诊断量，清零会丢掉历史。
        """
        with self._lock:                      # 与 accept() 串行化（仪表盘线程会调它）
            self.vad.reset()
            self._buf = np.zeros(0, dtype=np.float32)
            self._floor = 0.0                 # 哨兵：让它按新会话重新估计
            self._floor_probe = 0
