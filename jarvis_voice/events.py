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
                # ⚠️⚠️ 这三条**都必须**满足（ocr 2026-09-20 报的）：
                #   ① 句柄可能已经被 `close()`/`_rotate()` 置成 None（进程收尾、轮转失败）
                #   ② 序列化可能抛 **TypeError**（data 里有 bytes/set/自定义对象）
                #   ③ 写一个**已关闭**的句柄抛的是 **ValueError**，不是 OSError
                # 而本模块有 28 个调用点、其中 `BUS.emit("level")` 就在**实时主循环**里 ——
                # 异常一旦逃出去，麦克风主循环会静默终止（其余线程还活着，
                # 表现为"应用看着正常但不再响应语音"，是最难查的一类故障）。
                if self._fh is not None:
                    try:
                        self._fh.write(json.dumps(ev, ensure_ascii=False,
                                                  default=str) + "\n")
                        # 现在**每条都 flush**：level 已被挡在盘外，剩下的是低频真事件
                        # （一轮对话才几条），而攒批会让崩溃丢掉最多 200 条（测试抓到）。
                        self._fh.flush()
                        if self._fh.tell() > MAX_BYTES:
                            self._rotate()
                    except (OSError, ValueError, TypeError):
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
        """events.jsonl 超过 MAX_BYTES 就轮转（保留一个 .1）。在锁内调用。

        ⚠️⚠️ **失败时也必须留下一个可用的 `_fh`**（ocr 2026-09-20 报的 critical）。
        原写法 `close() → replace() → open()` 三步挤在一个 try 里，任何一步失败都被
        `except OSError: pass` 吞掉，而 `_fh` 此时已指向**关闭了的**文件对象 ——
        下一次 `emit()` 在它上面 write 会抛 **ValueError**，而这个异常不在
        `except OSError` 的覆盖范围里 → 直接逃进调用方（可能是实时音频线程）。
        现在拆成两段：**先尽力轮转，再保证句柄可用**。
        """
        try:
            self._fh.close()
            os.replace(self.path, self.path + ".1")
        except OSError:
            pass
        try:
            self._fh = open(self.path, "a", encoding="utf-8")
        except OSError:
            # 开不了新的就别留一个已关闭的坏句柄：置 None，emit 会跳过写盘
            # （但内存里的 `_recent` 和订阅者照常收事件 —— 掉的是落盘，不是功能）。
            self._fh = None

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
                if self._fh is not None:
                    self._fh.close()
            except OSError:
                pass
            # ⚠️ 必须置 None：`BUS` 是**模块级单例**，进程收尾关掉之后，
            # 任何还在跑的线程（仪表盘、看门狗）再 emit 就会写到已关闭的句柄上
            # → ValueError（不是 OSError，旧的 except 拦不住）。
            self._fh = None


# 全局单例（orchestrator 与 dashboard 共用一个）
BUS = EventBus()
