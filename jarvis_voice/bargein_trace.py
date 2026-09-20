"""打断诊断轨迹：**常驻**记录麦克风级别 + VAD 状态，只在关键时刻落盘。

## 为什么需要它

用户报「要喊几遍才打断」时，`events.jsonl` 里**什么都看不到** ——
因为 `level` 事件被刻意标成 `_EPHEMERAL`（不落盘、不进历史），
理由是 12Hz 全量写盘会让 events.jsonl 涨到 2MB/小时（实测过）。
于是最该看的那个数（**那一刻麦克风到底有多响、VAD 有没有翻**）恰恰没有留痕。

## 做法：环形缓冲 + 事件触发落盘

平时只往内存里的环形缓冲塞（10 次/秒，零盘 IO）；**只有**下面这些时刻
才把**前 `_PRE_SEC` 秒**的轨迹整段倒出来：

  · 播放期间 VAD 说「有语音」  ← **这就是"用户在试图打断"的定义**，无论成没成
  · VAD 爆发**武装**（`[打断-武装]`）
  · 打断进入待定窗口 / 撤回 / 提交 / 超时

所以失败的尝试**也会留下记录** —— 这正是它存在的意义。
（成功的打断本来就有日志，缺的是**没触发**的那些。）

## 读法

`~/.jarvis/bargein-trace.jsonl`，一行一次落盘：

```json
{"reason": "speech_during_playback", "state": "speaking", "playing": true,
 "armed": false, "pending": false, "gap_ms": 812,
 "samples": [[dt_ms, rms_x10000, speaking, playing], ...]}   // 由旧到新
```

`samples` 是**落盘前 `_PRE_SEC` 秒**的轨迹（最后一个是当下）。看三点：
  1. `rms` 在语音段有没有**明显**抬起来（没抬 → 麦克风/增益问题，不是判据问题）
  2. `speaking` 有没有翻 True（没翻 → 卡在 VAD 阈值或静音门槛）
  3. `armed` 是不是 false（是 → 卡在**静音门槛**：两句之间没静够 250ms）

⚠️ 本模块**不做任何判断、不影响行为** —— 纯观测。写盘失败一律吞掉：
诊断工具绝不能把主链路带崩。
"""
import json
import os
import threading
import time
import wave
from collections import deque

# 保留多久的轨迹（秒）。要够看清"从安静到开口"的整个过程。
_PRE_SEC = 15.0
# 每秒采样数 —— 与主循环的块长对齐（1600 样本 @16k = 100ms → 10/s）。
_SAMPLE_HZ = 10
# 落盘上限：超过就轮转一次（保留一个 .1）。与 events.py 同款策略。
MAX_BYTES = 4 * 1024 * 1024
# 打断音频最多留几个（别把磁盘塞满）
_KEEP = 60


def _default_path() -> str:
    return os.path.join(os.path.expanduser("~/.jarvis"), "bargein-trace.jsonl")


class BargeinTrace:
    """环形缓冲 + 落盘。**线程安全**（主循环写、可被别的线程落盘）。"""

    def __init__(self, path: str | None = None, enabled: bool = True):
        self.path = path or _default_path()
        self.enabled = enabled
        self._lock = threading.Lock()
        self._buf: deque = deque(maxlen=int(_PRE_SEC * _SAMPLE_HZ))
        self._t0 = time.time()
        self.dumps = 0                    # 统计：落了几次（可观测）

    def note(self, rms: float, speaking: bool, playing: bool,
             far_rms: float = 0.0) -> None:
        """主循环每块调一次。只入内存，不碰盘。

        `far_rms` = **扬声器侧**（far 参考）的电平。有它才能算出**真实房间里的 ERLE**：
        `20log10(far_rms / rms)`，只在"没人在说话"的时段取。离线探针报的 34–36 dB
        是**探针环境**的数；真机里音量/麦位/回声延迟都可能不同，打断识别差时
        第一件要确认的就是这个。
        """
        if not self.enabled:
            return
        with self._lock:
            self._buf.append((round((time.time() - self._t0) * 1000.0),
                              int(min(rms, 1.0) * 10000), int(bool(speaking)),
                              int(bool(playing)), int(min(far_rms, 1.0) * 10000)))

    def dump(self, reason: str, **extra) -> str | None:
        """把前 `_PRE_SEC` 秒的轨迹整段落盘。返回文件路径（失败/关闭时 None）。

        ⚠️ **调用方别传 `reason=`** —— 它是第一个形参，重名会
        `TypeError: got multiple values for argument 'reason'`（写这条时真踩过）。
        """
        if not self.enabled:
            return None
        with self._lock:
            samples = list(self._buf)
        if not samples:
            return None
        rec = {"reason": reason, "t": round(time.time() - self._t0, 3), **extra}
        # 压成相对毫秒（相对本次落盘），比绝对时刻好读
        now = samples[-1][0]
        rec["samples"] = [[ms - now, r, sp, pl, fr] for ms, r, sp, pl, fr in samples]
        self.dumps += 1
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            if os.path.exists(self.path) and os.path.getsize(self.path) > MAX_BYTES:
                os.replace(self.path, self.path + ".1")
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass                          # ⚠️ 诊断工具绝不把主链路带崩
        return self.path

    # ---- 打断音频落盘 ----
    def new_stem(self) -> str:
        """生成一个共享前缀，供**同一次打断**的多份落盘文件用。

        为什么要它：一次打断要存 3 份（过 AEC / 未过 AEC / far 参考）。各自调
        `save_pcm` 会各自取时间戳，毫秒一差名字就对不上，事后只能靠"时间上相邻"猜配对。
        用同一个 stem 之后，配对就是看文件名 —— 不用猜。
        """
        return f"{time.strftime('%H%M%S')}-{int(time.time() * 1000) % 1000:03d}"

    def save_pcm(self, pcm_int16, tag: str, sample_rate: int = 16000,
                 stem: str | None = None) -> str | None:
        """把一段语音存成 wav，供事后**离线复听 / 重转写**。

        为什么要它：用户报「它一说话我打断，识别就很差」。看电平只能猜到"有干扰"，
        **听到那段音频**才能分清是下面哪一种 —— 三种的修法完全不同：
          · 用户的声音被压低/变了形（AEC 双讲副作用）
          · 混进了它自己的残余回声（回声没消干净）
          · 段被截头去尾（ASR 缺前导上下文）

        存到 `~/.jarvis/bargein-audio/`，只保留最近 `_KEEP` 个。
        `stem` 见 `new_stem()`：同一次打断的多份必须传**同一个** stem 才能配对。
        """
        if not self.enabled or pcm_int16 is None:
            return None
        d = os.path.join(os.path.dirname(self.path), "bargein-audio")
        try:
            os.makedirs(d, exist_ok=True)
            pre = stem or (f"{time.strftime('%H%M%S')}-"
                           f"{int(time.time() * 1000) % 1000:03d}")
            path = os.path.join(d, f"{pre}-{tag}.wav")
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(sample_rate)
                w.writeframes(bytes(pcm_int16))
            # ⚠️ **先写后裁**（写之前裁会差一个：第 N 次写完后总数是 N，而不是 N-1）
            old = sorted(f for f in os.listdir(d) if f.endswith(".wav"))
            for f in old[:-_KEEP]:
                try:
                    os.unlink(os.path.join(d, f))
                except OSError:
                    pass
            return path
        except OSError:
            return None
