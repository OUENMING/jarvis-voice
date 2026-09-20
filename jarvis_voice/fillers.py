"""填充音（filler）：盖住"思考期"的空白。

依据 `docs/RESEARCH-NATURALNESS-20260916.md` §4：🟢 CUI'25 实测**延迟 >4s 是头号杀手**，
且**自然填充词能改善*感知*响应时间**。实测我们的工具调用会把首句推到 **5.3–12s**，
所以这一项收益最大。

三条设计约束（都是踩出来的）：
  1. **必须预渲染** —— 用来盖延迟的东西，自己不能有 1.2s 的合成延迟。
     启动时后台渲染一次，缓存到磁盘，之后是零延迟读取。
  2. **延迟触发** —— 本轮开始后等 `delay_ms`，还没出首句才播。
     首句先到就取消，避免给本来就快的回答平白加一段。
  3. **可被剪切** —— 填充音还在播时真句子到了，必须把它从播放队列里**切掉**，
     否则会把真正的内容往后推（1.5s 回答 + 0.5s 填充 = 更差）。
     用 `Player.cut_tag("filler")`，见 `player.py`。

音色/温度与主 TTS 一致，保证"同一个人在思考"。
"""
import hashlib
import os
import random
import shutil
import threading
import wave

import numpy as np

CACHE_DIR = os.path.expanduser("~/.jarvis/fillers")

# 通用池。两处用途：① 非工具回合的兜底（delay 后才播）② 认不出工具名时退回。
# 短、口语、不承诺具体内容。**不要**说"我查一下"——万一没查就尴尬。
TEXTS = ["嗯……", "那个……", "让我想想。", "我看一下。"]

# 按工具类别的**意图式**承接句。三条规则：≤12 字 / 说**意图**不说结果 / 不说"已完成"。
#
# ⚠️ 为什么不是"把通用填充音加长"（2026-09-20 调研，推翻了原 P2 的写法）：
#   · OpenAI 官方：preambles「Used poorly, they become filler and **increase perceived latency**」
#   · CUI'25（arXiv 2507.22352, n=54）：filler 改善**感知**响应时间，但不改善参与感/胜任感
#   · ConvFill（n=18）：响应性↑ 但 Naturalness 偏低，用户说 filler「有时显得重复」
#   · Jeong et al.：用简单 filler 的 agent **被认为更不聪明**
#   · Liu（2026-04-28, Purdue）：**上下文相关**的 filler 明显优于通用 filler
#   → 通用型（「嗯…」「那个…」）收益最弱、副作用最明确。用编排器**已经知道**的工具名
#     选一句对得上的，才是被验证有效的那一档。
#
# 工具名分布（本机 events.jsonl 全量）：Bash 68 / browser* 112 / 搜索类 11 / obsidian 8 / 其他 4
# → 下面这套覆盖 199/203 = 98%。
TOOL_TEXTS: dict[str, list[str]] = {
    # 搜东西（anysearch / parallel-search / tavily）
    "search": ["我搜一下。", "我查一下。"],
    # 开浏览器
    "browse": ["我开个页面看看。", "我翻一下网页。"],
    # 翻第二大脑
    "notes": ["我翻下笔记。"],
    # 跑命令/算数（Bash 在本项目里几乎都是算东西或取数）
    "run": ["我跑一下。", "我看一下。"],
}

# 工具名前缀 → 承接句类别。认不出的落回通用池（`FillerClips.GENERIC`）。
_PREFIX_TAGS: tuple[tuple[str, str], ...] = (
    ("mcp__anysearch__", "search"),
    ("mcp__parallel-search__", "search"),
    ("mcp__tavily__", "search"),
    ("mcp__browser__", "browse"),
    ("mcp__obsidian-vault__", "notes"),
)


def tool_tag(name: str) -> str:
    """工具名 → 承接句类别。认不出返回 `FillerClips.GENERIC`（= 通用池）。

    纯函数、无依赖 → 可单测。见 `docs/WORKORDER-LEADIN-01.md` §3.1。
    """
    n = name or ""
    for pref, tag in _PREFIX_TAGS:
        if n.startswith(pref):
            return tag
    return "run" if n == "Bash" else FillerClips.GENERIC


class FillerClips:
    GENERIC = ""      # 通用池的 tag

    def __init__(self, tts, texts=None, cache_dir: str = CACHE_DIR):
        self.tts = tts
        # `texts` 接受两种形状：list（= 通用池，向后兼容）或 dict[tag, list]。
        if isinstance(texts, dict):
            self.pools = {str(k): list(v) for k, v in texts.items()}
            self.pools.setdefault(self.GENERIC, list(TEXTS))
        else:
            self.pools = {self.GENERIC: list(texts or TEXTS)}
        self.cache_dir = cache_dir
        self.clips: list[bytes] = []
        # ⚠️ 这三个列表**必须同序**：渲染失败的文本会被跳过，
        # 所以不能拿 `pools[tag][i]` 去配 `clips[i]`（下标会错位）。
        self._clip_tags: list[str] = []
        self._clip_texts: list[str] = []
        self._i: dict[str, int] = {}          # 每个池一个轮转指针
        self._last_text = ""                  # 刚播过的文本（防同一句连着播两次）
        self._lock = threading.Lock()
        os.makedirs(cache_dir, exist_ok=True)

    # ---- 缓存键：音色/温度/文本变了就必须重渲染 ----
    def _key(self, text: str) -> str:
        sig = f"{getattr(self.tts,'name','?')}|{getattr(self.tts,'voice_id','?')}|" \
              f"{getattr(self.tts,'temperature','?')}|{text}"
        return hashlib.sha1(sig.encode()).hexdigest()[:16]

    def _path(self, text: str) -> str:
        return os.path.join(self.cache_dir, f"{self._key(text)}.wav")

    def _load(self, path: str) -> bytes:
        with wave.open(path, "rb") as w:
            assert w.getframerate() == self.tts.audio_format.sample_rate, "采样率不匹配"
            return w.readframes(w.getnframes())

    def _render_one(self, text: str) -> bytes | None:
        path = self._path(text)
        if os.path.exists(path):
            try:
                return self._load(path)
            except Exception:
                os.remove(path)          # 损坏则重渲染
        pcm = b"".join(self.tts.synthesize(text))
        if not pcm:
            return None
        tmp = path + ".tmp"
        try:
            with wave.open(tmp, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2)
                w.setframerate(self.tts.audio_format.sample_rate)
                w.writeframes(pcm)
            os.replace(tmp, path)        # 原子落盘
        except OSError:
            pass                          # 缓存失败不影响本次使用
        return pcm

    def ensure(self, log=print):
        """把缺的片段渲染好。**应在后台线程调用**（别拖慢启动）。"""
        got, got_tags, got_texts = [], [], []
        for tag, texts in self.pools.items():
            for t in texts:
                try:
                    c = self._render_one(t)
                except Exception as e:
                    log(f"[filler] 渲染失败 {tag}/{t!r}: {type(e).__name__}: {e}")
                    continue
                if c:
                    got.append(c)
                    got_tags.append(tag)
                    got_texts.append(t)
        with self._lock:
            self.clips = got
            self._clip_tags = got_tags
            self._clip_texts = got_texts
        if got:
            by = {}
            for tag in got_tags:
                by[tag] = by.get(tag, 0) + 1
            log(f"[filler] 就绪 {len(got)} 条，共 {sum(len(c) for c in got)//1024}KB"
                f"（按类别 {by}）")
        else:
            log("[filler] 无可用片段（将静默降级：不播填充音）")

    def ready(self) -> bool:
        return bool(self.clips)

    def pick(self, tag: str = "") -> tuple[bytes | None, str]:
        """取一条该 `tag` 的片段，返回 `(PCM, 文本)`。

        该类别没有片段时**退回通用池**（认不出的工具名走这条路）。
        池内轮转，并跳过"刚播过的那一句"（池里多于 1 条时），治
        「同一句话 20 秒内播两次」（`PLAN-LATENCY-20260919.md` 的验收清单）。

        必须连**文本**一起返回：播出去后会被麦克风收回去转写它自己的字
        （`'那个……'` → `'那个。'`），要登记进回声护栏才拦得住。见 `echoguard`。
        """
        with self._lock:
            if not self.clips:
                return (None, "")
            idx = [i for i, t in enumerate(self._clip_tags) if t == tag]
            if not idx:
                idx = [i for i, t in enumerate(self._clip_tags) if t == self.GENERIC]
            if not idx:
                idx = list(range(len(self.clips)))      # 极端兜底：任意一条
            p = self._i.get(tag, 0)
            pick_i = idx[p % len(idx)]
            if len(idx) > 1 and self._clip_texts[pick_i] == self._last_text:
                pick_i = idx[(p + 1) % len(idx)]        # 别连着播同一句
            self._i[tag] = p + 1
            self._last_text = self._clip_texts[pick_i]
            return (self.clips[pick_i], self._clip_texts[pick_i])

    def clear_cache(self):
        shutil.rmtree(self.cache_dir, ignore_errors=True)
        os.makedirs(self.cache_dir, exist_ok=True)
