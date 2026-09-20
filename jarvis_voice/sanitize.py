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
# 粗体/斜体/删除线 → 只去记号，留内容。
# ⚠️⚠️ 这里原来是一条 `(\*\*\*|\*\*|__|\*|_|~~)(.+?)\1` 配 DOTALL，会**吞掉实义文本**
# （ocr 2026-09-20 报的，四条全部本机复现）。两道病：
#   ① **DOTALL 让 `.` 跨行** → 一个落单的 `*` 会一路匹配到下一段的下一个 `*`，
#      把中间整段当强调体改写（实测：`落单的星号 * 然后换行\n\n下一段的*又一对`
#      被改成 `…然后换行\n下一段的又一对`，丢了内容）。
#   ② **标记两侧没有边界约束** → `a_b_c` 被当成 `_b_` 删成 `abc`；
#      `2*3*4` 被删成 `234`。而这两个都是**要念出来的实义文本**。
# 现在拆成四条各带边界的规则，并按「长的先」逐条应用（`***` 不能被 `*` 先吃掉）。
_EMPH_STRONG = re.compile(r"(\*\*\*|\*\*|__)(?!\s)(?P<c>[^\n]+?)(?<!\s)\1")
_EMPH_STRIKE = re.compile(r"~~(?!\s)(?P<c>[^~\n]+?)(?<!\s)~~")
# 单星/单下划线：两侧不得紧邻**ASCII** 词字符 —— 这样 `a_b_c` / `2*3*4` /
# `snake_case` 原样通过，而 `*斜体*` / `_斜体_` 仍被处理。
# ⚠️ 必须显式写 `[A-Za-z0-9_]`，**不能用 `\w`**：Python 的 `\w` 在 Unicode 下
# 包含中日韩文字，于是 `和_斜体_` 的前置断言会因为前一个字是「和」而失败
# → 中文正文里的斜体标记就处理不掉了（写这条时实测踩到）。
_EMPH_STAR = re.compile(r"(?<![A-Za-z0-9_*])\*(?!\s)(?P<c>[^*\n]+?)(?<!\s)\*(?![A-Za-z0-9_*])")
_EMPH_UNDER = re.compile(r"(?<![A-Za-z0-9_])_(?!\s)(?P<c>[^_\n]+?)(?<!\s)_(?![A-Za-z0-9_])")
# 标题记号
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
# 列表记号（行首的 - * + 或 1. 1) ）
_LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+", re.MULTILINE)
# 引用记号
_QUOTE_RE = re.compile(r"^\s{0,3}>\s?", re.MULTILINE)
# 链接 [文字](url) → 文字；裸 URL → 占位词
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\((?:[^)]*)\)")
# ⚠️ 裸 URL 的字符集**必须排除中日韩文字与全角标点**（ocr 2026-09-20 报的）。
# 原来用 `\S+`：中文没有空格，于是 `详见 https://example.com，这是说明` 里
# `\S+` 会一路吃到句尾，整段替换成「链接」→ **「，这是说明」整句被删掉**（已复现）。
# `www.` 分支还加了前置断言，避免命中 `awww.xyz` 这种词中间的伪 URL。
_URL_TAIL = r"[^\s一-鿿　-〿！-｠]+"
_URL_RE = re.compile(rf"https?://{_URL_TAIL}|(?<![\w.])www\.{_URL_TAIL}")
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
    for _rx in (_EMPH_STRONG, _EMPH_STRIKE, _EMPH_STAR, _EMPH_UNDER):
        s = _rx.sub(lambda m: m.group("c"), s)
    s = _EMOJI_RE.sub("", s)
    # 空白归一：段内多空格→单空格；空行→单个换行（保留段落感，但不留长停顿源）
    s = re.sub(r"[ \t　]+", " ", s)
    s = re.sub(r"\n{2,}", "\n", s)
    s = "\n".join(line.strip() for line in s.split("\n"))
    return s.strip()


def has_speakable(text: str) -> bool:
    """清理后是否还剩可说内容（用于跳过纯符号/纯 emoji）。"""
    return bool(re.search(r"[\w一-鿿]", text))
