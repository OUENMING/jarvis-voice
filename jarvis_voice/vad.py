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

        # ---- 预滚缓冲（治「首字被 ASR 判错」，见 config.vad_pre_roll_ms 的注释）----
        # 实测机制**不是**「话首被切」（能量剖面显示段首就是语音），而是
        # **ASR 缺前导上下文** —— 补 ~900ms 前导静音后转写从「派放」恢复成「开饭」。
        # ⚠️ 切片用 `seg.start`，它的坐标系我没彻底定清；但实测在 zh/yue/en 三个文件上
        #    都不重叠、且恢复正确。若换 ASR/VAD 版本后出现首字重复，先怀疑这里。
        # ⚠️ **快照时机是关键**：VAD 要等 min_silence_duration 静音之后才吐段，
        #    所以 `_drain()` 那一刻环里最新的是「尾部静音」，拼上去没用。
        #    必须在**段刚起**（is_speech_detected False→True）时快照 ——
        #    那一刻环里最新 N 样本正好是「话首之前」。
        # ⚠️ 环不能只等于预滚长度：快照是在**检测到语音**那一刻取的，而检测比
        #    段起点晚约 0.8s（实测）。所以环要留出这段延迟 + 余量，才能在发段时
        #    切出「段起点之前的 N 毫秒」而**不与段本身重叠**（重叠会让 ASR 把
        #    开头吐两遍：「开饭」→「开放开放」）。
        self._pre_keep = max(0, int(cfg.vad_pre_roll_ms / 1000.0 * cfg.sample_rate))
        self._pre_n = self._pre_keep + int(1.2 * cfg.sample_rate) if self._pre_keep else 0
        self._pre_ring = np.zeros(self._pre_n, dtype=np.float32) if self._pre_n else None
        self._pre_w = 0            # 环形写指针
        self._pre_filled = 0       # 已写入样本数（< _pre_n 表示还没填满）
        self._fed_total = 0        # 已喂给 VAD 的样本总数（与 sherpa 的 seg.start 同一坐标）
        self._pre_snap: np.ndarray | None = None   # 段起那一刻的环快照
        self._pre_snap_fed = 0     # 取快照时的 _fed_total
        self._was_speaking = False
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

    # ---- 预滚环形缓冲 ----
    def _push_pre(self, chunk: np.ndarray):
        """把这块输入写进预滚环。绝对位置 p 映射到 `p % _pre_n`。"""
        n = chunk.shape[0]
        if n == 0:
            return
        if n >= self._pre_n:
            self._pre_ring[:] = chunk[-self._pre_n:]
            self._pre_w = 0
        else:
            end = self._pre_w + n
            if end <= self._pre_n:
                self._pre_ring[self._pre_w:end] = chunk
            else:
                k = self._pre_n - self._pre_w
                self._pre_ring[self._pre_w:] = chunk[:k]
                self._pre_ring[:end - self._pre_n] = chunk[k:]
            self._pre_w = end % self._pre_n
        self._pre_filled = min(self._pre_n, self._pre_filled + n)

    def _snapshot_pre(self) -> np.ndarray | None:
        """取环里**最新**（即刚写进来的）那些样本 —— 段起时刻它就是「话首之前」。"""
        if not self._pre_n or self._pre_filled == 0:
            return None
        n = self._pre_filled
        idx = (self._pre_w - n + np.arange(n)) % self._pre_n
        return self._pre_ring[idx].copy()

    # ---- 输入 ----
    def accept(self, chunk_f32: np.ndarray) -> list[np.ndarray]:
        """喂一段 float32 [-1,1]。返回**本次新完成**的语音段（int16 一维数组）。"""
        if chunk_f32.dtype != np.float32:
            chunk_f32 = chunk_f32.astype(np.float32)
        with self._lock:                      # 与 reset() 串行化，见 __init__ 注释
            if self._pre_ring is not None:
                self._push_pre(chunk_f32)     # 先入预滚环（必须在 VAD 之前）
            self._update_floor(chunk_f32)
            self._buf = np.concatenate([self._buf, chunk_f32]) if self._buf.size else chunk_f32
            done: list[np.ndarray] = []
            while self._buf.size >= self.win:
                frame, self._buf = self._buf[:self.win], self._buf[self.win:]
                self.vad.accept_waveform(frame)
                self._fed_total += frame.shape[0]
                # ⚠️ **段刚起时快照** —— 不能等 `_drain()` 再取：那时 VAD 已等完
                #    min_silence_duration，环里最新的是「尾部静音」，拼上去没用。
                # ⚠️ 直接读 `self.vad.is_speech_detected()`，**不走 property** ——
                #    那个 property 自己也要拿 `self._lock`，而我们已经持锁 → 死锁。
                sp = bool(self.vad.is_speech_detected())
                if sp and not self._was_speaking:
                    self._pre_snap = self._snapshot_pre()
                    self._pre_snap_fed = self._fed_total
                self._was_speaking = sp
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
            # ⚠️⚠️ **门限必须在拼预滚之前判**（RESEARCH-UPGRADE-PLAN §2.3 的陷阱）：
            # 预滚是语音**之前**的静音，会**稀释整段 RMS** → 先拼再判会让段落掉到
            # `floor*snr` 以下 → 被丢弃 = 「加了 padding 反而更常丢话」的反直觉回归。
            # 强制顺序：sherpa 出段 → ① 在**未 padding 的原始段**上判门限
            #                     → ② 通过了才拼预滚 → ③ 送 ASR
            if pcm.size and self._passes_gate(pcm):
                snap, snap_fed = self._pre_snap, self._pre_snap_fed
                self._pre_snap = None          # 用完即清：下一段会在段起时重新快照
                if snap is not None and snap.size and self._pre_keep:
                    # 快照末尾（= snap_fed）到段起点（seg.start）的距离
                    off = snap_fed - int(getattr(seg, "start", 0) or 0)
                    hi = snap.shape[0] - off
                    lo = hi - self._pre_keep
                    if lo >= 0 and hi > lo:
                        # 只拼**段起点之前**的那一段 —— 拼多了会和段本身重叠，
                        # ASR 会把开头吐两遍（实测「开饭」→「开放开放」）。
                        pcm = np.concatenate([snap[lo:hi], pcm])
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
            # 预滚环必须一起清 —— 否则上一轮的音频会被当成本轮的「话首之前」拼上去
            if self._pre_ring is not None:
                self._pre_ring[:] = 0
            self._pre_w = 0
            self._pre_filled = 0
            self._pre_snap = None
            self._pre_snap_fed = 0
            self._fed_total = 0
            self._was_speaking = False
