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


def _norm(text: str) -> str:
    t = _PUNCT.sub("", text or "")
    t = _FILLER.sub("", t)
    return t.lower()


# 「动作词 + 宾语」才成立，避免误伤。
# ⚠️ 宾语必须指向**对话本身**。"刚才"单独出现不算——实测把
# "忘掉刚才那个会议"误判成清上下文，而那可能是指删掉日历里的会议。
_CLEAR_VERBS = ("清理", "清空", "清除", "重置", "忘掉", "忘记", "抹掉", "删掉", "重新开始", "重来")
_CLEAR_OBJS = ("上下文", "记忆", "对话", "历史", "聊天记录", "聊天")
_CLEAR_SOFT = ("刚才说的", "刚才聊的", "刚才讲的", "之前说的", "之前聊的")
# "忘掉刚才" / "忘掉之前" —— 这类短语里"刚才"必须**在句尾**（后面没有别的宾语）才算，
# 否则"忘掉刚才那个会议"会被误判（那多半是指删掉日历里的会议）。
_CLEAR_TAIL = ("刚才", "之前", "前面")

def _nset(items) -> set[str]:
    """把集合成员也过一遍 `_norm`。

    ⚠️ 必须这么做：`match()` 是拿**归一化后**的文本去比集合的。
    若集合里存的是原文（"暂停一下"/"别听了"），归一化会把它们变成
    "暂停"/"别听"，于是**永远匹配不上** —— 集合条目成了死条目（审计发现）。
    """
    return {_norm(i) for i in items}


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

    # 清上下文：动作词 + **指向对话的**宾语（或句尾的"刚才/之前"）
    if any(v in t for v in _CLEAR_VERBS) and (
            any(o in t for o in _CLEAR_OBJS)
            or any(s in t for s in _CLEAR_SOFT)
            or t.endswith(_CLEAR_TAIL)):
        return MetaCommand("clear", "好，刚才聊的我都忘了。")

    if t in _PAUSE:
        return MetaCommand("pause", "好，我先不听。")
    if t in _RESUME:
        return MetaCommand("resume", "好，我听着呢。")
    if t in _RECONNECT:
        return MetaCommand("reconnect", "好，第二大脑接上了。")

    return None
