"""回声文本护栏：把「自己刚说过的话又被转写回来」的段落挡在脑之外。

**动机（一手）**：内置扬声器 + 内置麦时，AEC 残余会越过 VAD 门限被转写。
若它进了脑，助手就会**回应自己** —— `HANDOVER-20260918.md` §1 把
「不能凭空自言自语」明确列为验收口径。在装 AEC 之前，这一层是纯文本兜底，
**零延迟、零依赖**。

**判据**：与「最近说过的文本」做字符级相似度。为什么是字符不是词：
中文没有空格，且 SenseVoice 的错字是字符级的。用**最长公共子序列（LCS）
占比**而不是集合 Jaccard —— 因为回声段常被 VAD 在中间切开，表现为
「待测文本是说过的话的一个片段」，LCS 对这种**截断 + 错字**的组合最稳。

**它是启发式，不是判据的判据**：
  - 宁可**漏判**（放行一次回声，助手自言自语一次）也不**误判**（吃掉用户真说的话）
  - 所以：阈值偏高、要求最小长度、**每次命中都发事件**（可审计有没有误吃）
  - 边界：短于 `min_len` 的转写一律不判（"好的"/"对" 这类短文本相似度虚高，
    而且用户真说"清空上下文"时可能撞上，必须放行）

**例外：填充音（`filler=True`）。** 上面那条 `min_len` 保护的边界，对填充音是**反的**：
填充音本身就只 1–2 个字（`fillers.TEXTS = ["嗯……", "那个……", …]`），
被麦克风收回去时转写出来的也正是 1–2 个字 —— 正好卡在 `min_len=6` 的保护里永远漏放。

真机证据（2026-09-20，`events.jsonl`）：
  - 打断后的插话转写 vs 平常：中位 **6 字 vs 13 字**，≤2 字碎片占比 **26% vs 10%**
  - 那些碎片是 `'那个。'` / `'嗯。'` / `'嗯ん。'` —— **正是填充音的原文**
  - 且**全部聚在填充音开始后 +1.4~1.7s**（填充音 0.96s 长 + VAD 端点延迟），
    而 ≤2 字碎片里 **47%** 紧跟在填充音之后（正常转写只有 22%）
  → 助手在**回应自己的填充音**，并把用户真正的插话挤在一堆幻听里。

所以填充音走一条**独立的短窗口精确比对**：
  - 不受 `min_len` 保护（它本来就是短的）
  - 只跟**最近 `filler_window_s` 秒内**播过的填充音比（默认 3s）——
    窗口开大就会误吃用户正常说的「那个」/「嗯」
  - 正常句子**绝不**跟填充音比（避免"用户长句碰巧含填充音词"被误判）


来源对照（独立佐证）：`ksg98/fastaf-ide#7`（2026-08-05，open）在同样场景下给的
建议就是「让打断必须转出实际文本」+「与正在播的 TTS 文本做匹配再抑制」。
"""
import re
import threading
import time

_PUNCT = re.compile(r"[\s，。、！？；：,.!?;:~…~—\-]+")


def normalize(text: str) -> str:
    """去标点/空白 + 转小写。与 filler.py / commands.py 的口径一致。"""
    return _PUNCT.sub("", text or "").lower()


def lcs_len(a: str, b: str) -> int:
    """最长公共子序列长度（字符级）。滚动数组，O(min) 空间。

    文本很短（口述一句话），不需要更聪明的算法。
    """
    if not a or not b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    prev = [0] * (len(b) + 1)
    for ca in a:
        cur = [0] * (len(b) + 1)
        for j, cb in enumerate(b, 1):
            cur[j] = prev[j - 1] + 1 if ca == cb else max(prev[j], cur[j - 1])
        prev = cur
    return prev[-1]


def similarity(heard: str, spoken: str) -> float:
    """0..1。用 LCS 占**较短一侧**的比例 —— 因为回声常是被截断的片段。"""
    h, s = normalize(heard), normalize(spoken)
    if not h or not s:
        return 0.0
    return lcs_len(h, s) / min(len(h), len(s))


class EchoGuard:
    """记录最近说过的文本；判断新转写是不是回声。

    ⚠️ **确实需要锁**（原来只写在 docstring 里，代码里没有 —— ocr 2026-09-20 报的）：
    写侧 `note_spoken` 现在有**三条**来源，分属不同线程：
      · BrainThread（正常句子）
      · 元命令路径（`_speak_local`）
      · **填充音/承接句的 `Timer` 线程**（2026-09-20 加「填充音回声」时引入）
    而读侧 `check` 在 BrainThread 上遍历同一个 `_spoken` 列表 ——
    一边 append/重建、一边遍历，会读到半成品或漏掉刚记的句子。
    锁很便宜，且 `check` 不在实时音频路径上。
    """

    def __init__(self, threshold: float = 0.75, min_len: int = 6,
                 window_s: float = 12.0, filler_window_s: float = 3.0):
        self.threshold = threshold
        self.min_len = min_len
        self.window_s = window_s
        self.filler_window_s = filler_window_s
        # (时刻, 归一化文本, 是不是填充音)
        self._spoken: list[tuple[float, str, bool]] = []
        self.suppressed = 0                          # 统计：抑制了几次（可观测）
        self._lock = threading.Lock()

    def note_spoken(self, text: str, filler: bool = False) -> None:
        """记一句「即将/正在念出去」的文本。应当与实际送 TTS 的字符串一致。

        `filler=True` 用于**填充音** —— 它们会在 `check()` 里走独立通道，
        因为短、而且只在最近 `filler_window_s` 内有效。见模块 docstring。
        """
        t = normalize(text)
        if not t:
            return
        now = time.time()
        with self._lock:
            self._spoken.append((now, t, filler))
            self._prune(now)

    def _prune(self, now: float) -> None:
        """⚠️ 只在持有 `self._lock` 时调用。"""
        cut, fcut = now - self.window_s, now - self.filler_window_s
        self._spoken = [(ts, t, f) for ts, t, f in self._spoken
                        if ts >= (fcut if f else cut)]

    def check(self, heard: str) -> tuple[bool, float, str]:
        """返回 (是否回声, 最高相似度, 最相似的已说文本)。

        两条通道：
          · 短转写（归一化后 < min_len）→ **只**跟最近的填充音比（否则放行）
          · 正常转写 → 只跟正常句子比（填充音太短，跟长句比毫无意义且只会稀释）
        """
        h = normalize(heard)
        if not h:
            return (False, 0.0, "")
        now = time.time()
        with self._lock:
            self._prune(now)
            entries = list(self._spoken)     # 快照：别持锁跑相似度计算
        short = len(h) < self.min_len
        if short and not any(f for _ts, _t, f in entries):
            return (False, 0.0, "")     # 没有填充音在窗口内 → 照旧放行短文本
        best, best_txt = 0.0, ""
        for _ts, spoken, is_filler in entries:
            if short != is_filler:
                continue                # 短转写只配填充音；长转写只配正常句子
            s = similarity(h, spoken)
            if s > best:
                best, best_txt = s, spoken
        hit = best >= self.threshold
        if hit:
            with self._lock:
                self.suppressed += 1
        return (hit, best, best_txt)
