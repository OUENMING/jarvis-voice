"""把 CC 的文本清洗成"适合朗读"的文本 —— **纯规则、确定性、不丢内容**。

设计原则（来自调研结论）：
  - 风格/语气是**生成侧(prompt)** 和 **TTS 侧** 的事，这里只做**机械清理**。
  - **绝不用模型**：加一层生成模型 = 多一跳延迟 + 引入改坏事实的风险，
    而本项目第一硬需求是"逐字忠实朗读"。
  - **保真优先**：只改"表现形式"（markdown 记号、空白、URL、emoji），
    **不删任何实义文本**。宁可留下一个多余的标点，也不丢一个数字。
  - 数字/日期/缩写的口语读法**交给 TTS**（SSML `say-as` / `sub`），不在这里猜。

实测动机（test_spoken_style.py）：即使 prompt 明说"禁止列表"，模型仍会吐
`\\n\\n` 空行 —— 朗读时那会变成莫名其妙的长停顿。这类事必须由规则兜住。
"""
import re

# 代码块围栏：```python ... ``` / ~~~ ... ~~~（连内容一起保留，只去围栏）
_FENCE_RE = re.compile(r"^\s*(?:```|~~~)[^\n]*$", re.MULTILINE)
# 行内代码 `x` → 保留 x
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
# 粗体/斜体/删除线
_EMPHASIS_RE = re.compile(r"(\*\*\*|\*\*|__|\*|_|~~)(.+?)\1", re.DOTALL)
# 标题记号
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
# 列表记号（行首的 - * + 或 1. 1) ）
_LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+", re.MULTILINE)
# 引用记号
_QUOTE_RE = re.compile(r"^\s{0,3}>\s?", re.MULTILINE)
# 链接 [文字](url) → 文字；裸 URL → 占位词
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\((?:[^)]*)\)")
_URL_RE = re.compile(r"https?://\S+|www\.\S+")
# 表格分隔行 |---|---|
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]{3,}\|?\s*$", re.MULTILINE)
# emoji / 符号（常见区间）
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF"
    "\U00002B00-\U00002BFF\U0000FE0F\U0000200D]")
# 水平线
_HR_RE = re.compile(r"^\s*(?:[-*_]\s*){3,}$", re.MULTILINE)


def sanitize_for_speech(text: str, url_word: str = "链接") -> str:
    """返回适合朗读的纯文本。只动表现形式，不删实义内容。"""
    if not text:
        return ""
    s = text
    s = _FENCE_RE.sub("", s)
    s = _INLINE_CODE_RE.sub(r"\1", s)
    s = _MD_LINK_RE.sub(r"\1", s)
    s = _URL_RE.sub(url_word, s)
    s = _HEADING_RE.sub("", s)
    s = _QUOTE_RE.sub("", s)
    s = _TABLE_SEP_RE.sub("", s)
    s = _HR_RE.sub("", s)
    s = _LIST_RE.sub("", s)
    s = _EMPHASIS_RE.sub(r"\2", s)
    s = _EMOJI_RE.sub("", s)
    # 空白归一：段内多空格→单空格；空行→单个换行（保留段落感，但不留长停顿源）
    s = re.sub(r"[ \t　]+", " ", s)
    s = re.sub(r"\n{2,}", "\n", s)
    s = "\n".join(line.strip() for line in s.split("\n"))
    return s.strip()


def has_speakable(text: str) -> bool:
    """清理后是否还剩可说内容（用于跳过纯符号/纯 emoji）。"""
    return bool(re.search(r"[\w一-鿿]", text))
