"""事件总线：把"发生了什么"结构化地吐出来，供可视化 / 回放 / 排查。

为什么要它：语音链路的故障（打断没生效、线程死了、残留污染）
**在终端日志里都是散落的文本**，很难看出因果。结构化事件可以：
  - 实时渲染成对话视图（谁说了什么、调了什么工具、每段多久）
  - 落 JSONL 供事后回放（"刚才那次为什么没反应"）
  - 未来做量化评测（延迟分布、打断准确率）

零依赖：标准库 + 可选地被 dashboard 订阅。
"""
import json
import os
import queue
import threading
import time
from collections import deque
from typing import Any

DAILY_DIR = os.path.expanduser("~/.jarvis")
DEFAULT_PATH = os.path.join(DAILY_DIR, "events.jsonl")

# 瞬时事件：**只推给在线仪表盘，不落盘、不进历史**。
# 为什么（审计发现 + 实测）：麦克风电平 12Hz，一条对话下来能产出上千条 level ——
# ① events.jsonl 实测 2MB/小时、24 小时 51MB 无界增长；
# ② 最近 2000 条**全是 level**，真正的对话事件被淹没，事后回放毫无用处；
# ③ 每秒 12 次 fsync 跑在麦克风线程上。
_EPHEMERAL = {"level"}

# 落盘上限：超过就轮转一次（保留一个 .1 备份）。
MAX_BYTES = 8 * 1024 * 1024


class EventBus:
    """线程安全。emit() 只做入队+落盘，绝不阻塞（可从实时线程调用）。"""

    def __init__(self, path: str = DEFAULT_PATH, recent: int = 800):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._lock = threading.Lock()
        self._subs: list[queue.Queue] = []
        self._recent: deque[dict] = deque(maxlen=recent)
        self._fh = open(path, "a", encoding="utf-8")
        self.path = path
        self._t0 = time.time()

    # ---- 生产 ----
    def emit(self, kind: str, **data: Any):
        ev = {"t": round(time.time() - self._t0, 3), "kind": kind, **data}
        ephemeral = kind in _EPHEMERAL
        with self._lock:
            # 瞬时事件不给历史（新订阅者不需要补一堆电平），也不落盘
            if not ephemeral:
                self._recent.append(ev)
                try:
                    self._fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
                    # 现在**每条都 flush**：level 已被挡在盘外，剩下的是低频真事件
                    # （一轮对话才几条），而攒批会让崩溃丢掉最多 200 条（测试抓到）。
                    self._fh.flush()
                    if self._fh.tell() > MAX_BYTES:
                        self._rotate()
                except OSError:
                    pass
            for q in self._subs:
                try:
                    q.put_nowait(ev)
                except queue.Full:
                    try:
                        q.get_nowait()
                        q.put_nowait(ev)
                    except (queue.Empty, queue.Full):
                        pass

    def _rotate(self):
        """events.jsonl 超过 MAX_BYTES 就轮转（保留一个 .1）。在锁内调用。"""
        try:
            self._fh.close()
            os.replace(self.path, self.path + ".1")
            self._fh = open(self.path, "a", encoding="utf-8")
        except OSError:
            pass

    # ---- 消费 ----
    def subscribe(self) -> queue.Queue:
        """新订阅者先拿到最近的事件（补上下文），再收增量。"""
        q: queue.Queue = queue.Queue(maxsize=2000)
        with self._lock:
            for ev in self._recent:
                try:
                    q.put_nowait(ev)
                except queue.Full:
                    break
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def close(self):
        with self._lock:
            try:
                self._fh.close()
            except OSError:
                pass


# 全局单例（orchestrator 与 dashboard 共用一个）
BUS = EventBus()
