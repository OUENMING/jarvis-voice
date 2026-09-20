"""元命令：控制**助手自己**的语音指令。

为什么必须本地处理：这些是**编排层的操作**（清上下文 = 让 CC 忘掉历史；
暂停 = 不再听麦克风）。送进 CC 是死循环——你让它清空记忆，它只是"回答"一句
"好的我清空了"，而上下文根本没动。

设计取舍：
  - **宁可漏判，不可误判**。ASR 会把话听错，一个宽松的规则可能把用户
    真说的话吃掉。所以"清空上下文"这类**有破坏性**的命令要求**明确的宾语**
    （上下文/记忆/对话/刚才），光说"重新开始"不算。
  - 匹配前先归一化（去标点、去语气词），容忍 ASR 的轻微差异。
"""
import re
from dataclasses import dataclass

_PUNCT = re.compile(r"[\s，。、！？；：,.!?;:~…~—\-]+")
_FILLER = re.compile(r"(一下|帮我|请|麻烦|你|把|给|了|吧|呢|啊|哦)")


@dataclass(frozen=True)
class MetaCommand:
    name: str
    reply: str       # 本地直接念出来（不走 CC）
    payload: str = ""  # 附加内容（目前只有 remember 用：要记的那句话）


def _norm(text: str) -> str:
    t = _PUNCT.sub("", text or "")
    t = _FILLER.sub("", t)
    return t.lower()


# ---- 清上下文：两条判据 + 两道护栏 ----
# 宾语必须指向**对话本身**。"刚才"单独出现不算——实测把"忘掉刚才那个会议"误判成
# 清上下文，而那可能是指删掉日历里的会议。
_CLEAR_VERBS = ("清理", "清空", "清除", "重置", "忘掉", "忘记", "抹掉", "删掉", "重新开始", "重来")
_CLEAR_OBJS = ("上下文", "记忆", "对话", "历史", "聊天记录", "聊天")
_CLEAR_SOFT = ("刚才说的", "刚才聊的", "刚才讲的", "之前说的", "之前聊的")
# "忘掉刚才" / "忘掉之前" —— 这类短语里"刚才"必须**在句尾**（后面没有别的宾语）才算，
# 否则"忘掉刚才那个会议"会被误判（那多半是指删掉日历里的会议）。
_CLEAR_TAIL = ("刚才", "之前", "前面")

# **判据①：以「上下文」结尾**（2026-09-20 加）。这条是主力。
# 为什么不再要求动词：真机数据（`~/.jarvis/events.jsonl` 历史 188 条转写）——
# 主人试过 5 次，其中 3 次 ASR 把前面的「清空」听成 **轻松 / 星空 / 供**，
# 而「**上下文**」三个字**每次都完好**：
#     '轻松一下上下文。' / '星空上下文。' / '供上下文。'  ← 全部漏判，助手没反应
#     '清空上下文。' / '嗯，清空一下上下文。'              ← 正常触发
# 要求动词 = 高频漏判。
# ⚠️ 为什么必须**以它结尾**而不是"出现即成立"：本项目整天在聊「上下文」
# （`聊上下文管理` 归一化后只有 6 字、也没有疑问词，只靠长度挡不住）。
# 真命令全部满足（`清空上下文` / `轻松上下文` / `星空上下文` / `供上下文`），
# 话题句不满足（结尾是「管理」「窗口」）。倒装句（`把上下文清空`）走判据②。
_CLEAR_NOUN_STRONG = "上下文"

# **判据②：动词 + 宾语**（原有路径，用于不倒装的说法：`清空对话` / `忘掉刚才聊的`）。
#
# ⚠️ 两道护栏（**各有真机证据**），都取「宁可漏判不可误判」的方向 ——
#    漏一次用户再说一遍，误判一次上下文就没了：
#   1) **疑问句不是命令**。实测 `'你还还你还记得清空上下文之前说的什么吗？'`
#      被当成清空命令**执行**了 —— 主人明明是在**问**它记不记得。
#   2) **长句不是命令**。命令都是短的（实测触发的几条 4–9 字）；取上限 10 ——
#      刚好包住真命令，又挡住「我问的是上下文长度够不够」（12 字）。
_CLEAR_QUESTION = ("吗", "呢", "?", "？", "什么", "怎么", "为什么", "哪", "几", "多少",
                   "记不记得", "还记得",
                   # 「上下文」在本项目里是**常被讨论的话题**，再加一组"在聊它"的标记
                   "长度", "多长", "够不够", "窗口", "多大")
_CLEAR_MAX_LEN = 10
# **护栏③：否定句不是命令。** 「别清空上下文」「不要清空上下文」「先别动上下文」
# 归一化后长度也 ≤10、也不含疑问词，会直接命中判据① 而被**真清掉**。
# （ocr 2026-09-20 报的，是我放宽判据① 时引进的。）
_CLEAR_NEGATION = ("别", "不要", "不用", "不需要", "先别", "别再", "勿", "否")
def _nset(items) -> set[str]:
    """把集合成员也过一遍 `_norm`。

    ⚠️ 必须这么做：`match()` 是拿**归一化后**的文本去比集合的。
    若集合里存的是原文（"暂停一下"/"别听了"），归一化会把它们变成
    "暂停"/"别听"，于是**永远匹配不上** —— 集合条目成了死条目（审计发现）。
    """
    return {_norm(i) for i in items}


def _is_clear(t: str) -> bool:
    """判「清空上下文」类指令。判据与三道护栏见上面常量区的注释。"""
    if len(t) > _CLEAR_MAX_LEN or any(q in t for q in _CLEAR_QUESTION):
        return False                      # 护栏①②：长句 / 疑问句
    if any(n in t for n in _CLEAR_NEGATION):
        return False                      # 护栏③：否定句（「别清空上下文」）
    if t.endswith(_CLEAR_NOUN_STRONG):
        return True                       # 判据①：以「上下文」结尾 = 「清空」被听坏的那些
    return (any(v in t for v in _CLEAR_VERBS)
            and (any(o in t for o in _CLEAR_OBJS)
                 or any(s in t for s in _CLEAR_SOFT)
                 or t.endswith(_CLEAR_TAIL)))


# ---- 「记住 X」→ 写进 memory.md（2026-09-20 新增）----
# 为什么做这一条：`memory.md` 的注入链路一直是通的（`_compose_system_prompt` 读它），
# 但**全库没有任何写入路径** —— 上线至今 0 条内容，是一个只读的死文件。
# 本项目的 `--resume` 默认开、`/clear` 会丢掉全部历史，所以 `memory.md` 是**唯一**
# 能跨「清空/重启」留下来的记忆。没有写入 = 清了就真没了。
#
# 设计取向（社区做法，🟡 LangChain 2026-04-09 的 `/remember` + LangGraph handbook
# 的「explicit requests + search-before-write」）：**显式请求 → 确定性地追加**，
# 不靠模型自觉。理由：本项目实测提示词规则 14 的遵守率只有 22%（见
# `docs/PLAN-LATENCY-20260919.md` §1.4）—— 把「重要的事能不能记住」押在模型自觉上不可靠。
_REMEMBER_HEADS = ("记住", "记一下", "记下来", "记下", "帮我记", "给我记")
# 句首的礼貌/语气词，匹配前先剥掉（「麻烦帮我记住X」里的「麻烦」）。
_REMEMBER_POLITE = ("麻烦", "那个", "帮忙", "嗯", "哎", "喂", "请")
# 疑问句不是「记住」指令（「你记住我说的话了吗」是在问，不是在让我记）。
_REMEMBER_QUESTION = ("吗", "呢", "?", "？", "什么", "怎么", "为什么")


def _remember_object(raw: str) -> str | None:
    """从原话里剥出要记的内容。剥不出返回 None（照常送 CC）。

    ⚠️ 用**原文**匹配触发词，不用 `_norm` 后的文本：`_FILLER` 会把「一下」去掉，
    于是「你帮我记一下明天买牛奶」归一化后变成「记明天买牛奶」——
    触发词「记一下」被自己的归一化吃掉了（真会踩的坑）。

    ⚠️⚠️ **取「消耗前缀最远」的那个触发词，不能取最长的**
    （ocr 2026-09-20 报的）：`帮我记` 比 `记下` 长，但
    「**帮我记**下明天买牛奶」里真正的触发词是 `记下` —— 按长度排序会先命中
    `帮我记`，payload 变成 **`下明天买牛奶`**（多一个残字）。
    正确判据是「哪个触发词**结束位置**最靠后」，因为触发词是在标记前缀的末尾。
    """
    t = _PUNCT.sub("", raw or "")
    if any(q in t for q in _REMEMBER_QUESTION):
        return None
    for p in _REMEMBER_POLITE:              # 剥句首礼貌词，剥到剥不动为止
        while t.startswith(p):
            t = t[len(p):]
    best_end = -1
    for h in _REMEMBER_HEADS:               # 平局时长的自然胜出（字典序无意义，取最远即可）
        i = t.find(h)
        if 0 <= i <= 3:
            best_end = max(best_end, i + len(h))
    if best_end < 0:
        return None
    obj = t[best_end:]
    return obj if len(obj) >= 2 else None


_PAUSE = _nset({"暂停监听", "先别听", "别听了", "暂停一下", "停听", "先不要听", "别听我说话"})
_RESUME = _nset({"继续监听", "接着听", "开始听", "恢复监听", "继续听我说"})

# "热重连第二大脑"（obsidian-vault MCP）。启动时 Obsidian 常没开 → 之后起来了用这句接上，
# 不用重启脑进程。**刻意不用"打开…"**——那会和"打开 Brightspace"之类的真指令打架。
_RECONNECT = _nset({"连上第二大脑", "重连第二大脑", "刷新第二大脑", "连接第二大脑",
                    "连上笔记", "重连笔记", "把第二大脑连上", "接上第二大脑"})


def match(text: str) -> MetaCommand | None:
    """识别元命令。识别不出返回 None（照常送 CC）。"""
    t = _norm(text)
    if not t:
        return None

    # 「记住 X」→ 写进 memory.md。**放在清空之前**：两者可能同时命中
    # （「记住上下文」同时含「上下文」），而「记住」开头的指令意图更明确。
    obj = _remember_object(text)
    if obj:
        return MetaCommand("remember", "好，记下了。", payload=obj)

    # 清上下文：见 `_is_clear` 的判据与两道护栏
    if _is_clear(t):
        return MetaCommand("clear", "好，刚才聊的我都忘了。")

    if t in _PAUSE:
        return MetaCommand("pause", "好，我先不听。")
    if t in _RESUME:
        return MetaCommand("resume", "好，我听着呢。")
    if t in _RECONNECT:
        return MetaCommand("reconnect", "好，第二大脑接上了。")

    return None
