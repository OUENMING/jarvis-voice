#!/usr/bin/env python3
"""「清空上下文」元命令的漏判与误判回归测试（2026-09-20）。

用例**全部取自真机日志**（`~/.jarvis/events.jsonl` 的历史转写），不是编的。

背景：主人试过 5 次这个命令，3 次漏判（助手完全没反应），另有 1 次误判
（他在**问**助手记不记得，系统却把上下文清了）。

⚠️ 用例 ①②③④ 在旧代码上**必须失败**（red-green）。
⚠️ 纯文本函数，不碰音频设备、不启子进程。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis_voice.commands import match          # noqa: E402

FAIL = []


def check(label, text, want_cmd):
    got = match(text)
    name = got.name if got else None
    ok = name == want_cmd
    print(f"  {'✅' if ok else '❌'} {label}")
    print(f"       {text!r} → {name!r}（期望 {want_cmd!r}）")
    if not ok:
        FAIL.append(label)


print("=== ① 真机漏判的 3 条：ASR 把「清空」听成别的，但「上下文」完好 ===")
check("轻松一下上下文（清空→轻松）", "轻松一下上下文。", "clear")
check("星空上下文（清空→星空）", "星空上下文。", "clear")
check("供上下文（清空→供）", "供上下文。", "clear")

print("\n=== ② 真机误判的那 1 条：他在**问**，不是在下命令 ===")
check("问你记不记得清空之前说了什么（长句 + 疑问）",
      "你还还你还记得清空上下文之前说的什么吗？", None)

print("\n=== ③ 真机正常触发的几条：必须仍然触发 ===")
check("清空上下文", "清空上下文。", "clear")
check("嗯，清空一下上下文", "嗯，清空一下上下文。", "clear")
check("对对对，就是清空对话", "对对对，就是清空对话。", "clear")

print("\n=== ④ 别的词被 ASR 听错时也不能漏（宾语非「上下文」）===")
check("忘掉刚才聊的", "忘掉刚才聊的。", "clear")
# ⚠️ 这条**按设计就是不触发**：commands.py 的 docstring 写明
#    「光说'重新开始'不算，必须有明确宾语」—— 破坏性命令要更严。
check("光说重新开始不算（设计要求）", "我们重新开始吧。", None)

print("\n=== ⑤ 不能误伤的日常话（回归）===")
check("忘掉刚才那个会议（是删日历，不是清对话）", "忘掉刚才那个会议。", None)
check("我问的是上下文长度", "我问的是上下文长度够不够。", None)
check("记忆这个词单独出现不算命令", "你的记忆里有没有我生日？", None)
check("长句里引用上下文不算命令",
      "刚才那个报错是因为上下文太长了对吧我们下次改一下", None)
# ⚠️ 这条最要紧：本项目整天在聊「上下文」，而它归一化后很短、也没有疑问词 ——
#    只靠长度和疑问词挡不住。靠「必须**以**上下文**结尾**」才挡住。
check("聊上下文管理（话题，不是命令）", "聊上下文管理。", None)
check("上下文窗口这件事", "上下文窗口。", None)
check("倒装的说法仍然要认（动词+宾语）", "把上下文清空。", "clear")

print("\n=== ⑤b 否定句**绝不能**清空（ocr 2026-09-20 报的，我放宽判据时引进的）===")
for text in ("别清空上下文。", "不要清空上下文。", "不用清空上下文。",
             "先别动上下文。", "不需要清空上下文。"):
    check(f"否定：{text}", text, None)

print("\n=== ⑥ 其它元命令不受影响（回归）===")
check("暂停监听", "暂停监听。", "pause")
check("继续监听", "继续监听。", "resume")
check("连上第二大脑", "连上第二大脑。", "reconnect")
check("普通问话不该命中", "今天天气怎么样？", None)

print()
if FAIL:
    print(f"❌ {len(FAIL)} 项失败：{FAIL}")
    sys.exit(1)
print("✅ 全部通过")
