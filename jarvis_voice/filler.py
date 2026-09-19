"""backchannel / 填充词判定。

用途：**不要因为一句"嗯""对"就打断正在播报的助手**。
依据：docs/RESEARCH-NATURALNESS-20260916.md §5——规则过滤足够，
不该为此插模型（去 filler 的收益有证据显示为零）。

⚠️ 关键风险：**"对""好的"也可能是正经回答**（回答"要不要现在做？"时）。
所以本模块只用于**打断门控**，绝不用于丢弃用户输入——
转写照样原样送给 CC，只是不触发 barge-in。
"""
import re

# 纯 backchannel：整段转写（去掉标点/空白后）落在集合里才算
_BACKCHANNEL = {
    "嗯", "嗯嗯", "呃", "哦", "喔", "啊", "呀", "哈", "唉",
    "对", "对对", "对的", "是", "是的", "嗯是", "行", "好", "好的",
    "好吧", "ok", "okay", "嗯嗯嗯", "然后", "那个", "就是",
}
_PUNCT = re.compile(r"[\s，。、！？；：,.!?;:~…~—\-]+")


def is_backchannel(text: str) -> bool:
    """整段是否只是一个应答词（→ 不应打断）。"""
    t = _PUNCT.sub("", text or "").lower()
    return bool(t) and t in _BACKCHANNEL


# 注：曾有一个 `looks_like_question()` 但**全项目零调用**（审计发现），已删 ——
# 死代码留着会让下一个人以为某处逻辑在用它。


# ---- 停口令（本地短路，不送 CC）----
# 实测动机（2026-09-16 真机）：用户说"停一下"，原本会走完整一轮 CC，
# 结果助手回"好，我停了。你说。"——**用户要的是安静，它却又说了一句**，
# 还白烧一轮 CC。停口令应当**就地处理**：闭嘴、不发话。
_STOP_PHRASES = {
    "停", "停下", "停一下", "停一停", "停停", "停停停", "停停停停", "停停停停停",
    "别说了", "不要再说了", "别讲了", "安静", "闭嘴", "打住", "停住",
    "等一下", "等等", "好了好了", "行了行了", "不用了", "可以了",
}
_STOP_MAXLEN = 12   # 整段必须很短，避免"帮我停下那个下载"被误判


def is_stop_command(text: str) -> bool:
    """整段是否是"别说了"类口令。是 → 就地闭嘴，不送 CC。"""
    t = _PUNCT.sub("", text or "")
    if not t or len(t) > _STOP_MAXLEN:
        return False
    if t in _STOP_PHRASES:
        return True
    # "停停停停停" 之类任意长度的重复
    if len(t) >= 2 and len(set(t)) == 1 and t[0] in "停别":
        return True
    return False
