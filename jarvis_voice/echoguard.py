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

来源对照（独立佐证）：`ksg98/fastaf-ide#7`（2026-08-05，open）在同样场景下给的
建议就是「让打断必须转出实际文本」+「与正在播的 TTS 文本做匹配再抑制」。
"""
import re
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

    ⚠️ 一个实例只服务一个编排器。线程安全：`note_spoken` 从 BrainThread 调，
    `check` 也从 BrainThread 调 —— 同一线程，不需要锁。但为了将来安全，
    内部仍用一次短锁（开销可忽略，`check` 不在实时音频路径上）。
    """

    def __init__(self, threshold: float = 0.75, min_len: int = 6,
                 window_s: float = 12.0):
        self.threshold = threshold
        self.min_len = min_len
        self.window_s = window_s
        self._spoken: list[tuple[float, str]] = []   # (时刻, 归一化文本)
        self.suppressed = 0                          # 统计：抑制了几次（可观测）

    def note_spoken(self, text: str) -> None:
        """记一句「即将/正在念出去」的文本。应当与实际送 TTS 的字符串一致。"""
        t = normalize(text)
        if not t:
            return
        now = time.time()
        self._spoken.append((now, t))
        self._prune(now)

    def _prune(self, now: float) -> None:
        cut = now - self.window_s
        self._spoken = [(ts, t) for ts, t in self._spoken if ts >= cut]

    def check(self, heard: str) -> tuple[bool, float, str]:
        """返回 (是否回声, 最高相似度, 最相似的已说文本)。

        短文本（归一化后 < min_len）直接放行 —— 见模块 docstring 的边界说明。
        """
        h = normalize(heard)
        if len(h) < self.min_len:
            return (False, 0.0, "")
        now = time.time()
        self._prune(now)
        best, best_txt = 0.0, ""
        for _ts, spoken in self._spoken:
            s = similarity(h, spoken)
            if s > best:
                best, best_txt = s, spoken
        hit = best >= self.threshold
        if hit:
            self.suppressed += 1
        return (hit, best, best_txt)
