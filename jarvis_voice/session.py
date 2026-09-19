"""会话状态与轮次（turn）管理。

存在的唯一理由：**并发下的"这一轮还算不算数"**。
打断发生时，正在跑的 BrainThread / TTSThread 必须立刻知道
"我这一轮作废了"，否则会出现"打断后旧台词还在播"（双脑时代踩过的坑）。

做法：单调递增的 `turn_id`。每个工作线程在每一轮开始时取一次 id，
每次产出前校验 `session.is_current(id)`；打断 = 递增 id → 旧的一律失效。
"""
import threading
import time
from enum import Enum


class State(str, Enum):
    IDLE = "idle"          # 在听
    THINKING = "thinking"  # CC 在跑，还没出声
    SPEAKING = "speaking"  # 在播


class Session:
    def __init__(self):
        self._lock = threading.Lock()
        self._state = State.IDLE
        self._turn_id = 0
        self._started_at = 0.0
        self.interrupts = 0          # 统计：被打断了几次

    # ---- 状态 ----
    @property
    def state(self) -> State:
        with self._lock:
            return self._state

    def set_state(self, s: State):
        with self._lock:
            self._state = s

    # ---- 轮次 ----
    @property
    def turn_id(self) -> int:
        with self._lock:
            return self._turn_id

    def begin_turn(self) -> int:
        """开始新一轮：**递增** id 并返回。旧 id 立即失效。"""
        with self._lock:
            self._turn_id += 1
            self._started_at = time.time()
            return self._turn_id

    @property
    def turn_started_at(self) -> float:
        """本轮开始的时刻。用于判断"这段语音是不是本轮开始之后才开口的"。

        真机 bug（2026-09-16）：用户说长句时 VAD 会把一句切成多段，
        第 1 段一送到 CC 状态就变 THINKING，而用户**还在继续说** →
        被打断计时器当成"插话"，把自己的话打断了。
        判据必须是"爆发起点晚于本轮起点"，而不是"此刻是否忙"。
        """
        with self._lock:
            return self._started_at

    def is_current(self, turn_id: int) -> bool:
        with self._lock:
            return turn_id == self._turn_id

    def finish_turn(self, turn_id: int) -> bool:
        """**原子**收尾：仍是我这轮 → 置 IDLE 并返回 True。

        为什么必须原子（审计发现）：`if is_current(t): set_state(IDLE)` 是两次独立加锁的
        检查-后-使用。若 TTS 判定通过后、置 IDLE 前，BrainThread 已 begin_turn(N+1) 并进入
        SPEAKING，则新轮正在播而 state 被踩成 IDLE → 主循环的 barge-in 判断失效、
        填充音的 THINKING 校验也失效。必须同锁内比对+置位。
        """
        with self._lock:
            if turn_id != self._turn_id:
                return False
            self._state = State.IDLE
            return True

    def interrupt(self) -> int:
        """作废当前轮，回到 IDLE。返回新的 turn_id。"""
        with self._lock:
            self._turn_id += 1
            self._state = State.IDLE
            self.interrupts += 1
            return self._turn_id

    def __repr__(self):
        return f"<Session {self.state.value} turn={self.turn_id} interrupts={self.interrupts}>"
