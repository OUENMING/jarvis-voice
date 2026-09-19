#!/usr/bin/env python3
"""sanitize_for_speech 的单元测试 —— 重点是**保真**：清洗绝不能丢实义内容。

用 test_spoken_style.py 实测得到的真实输出当样本。
用法：.venv/bin/python test_sanitize.py
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import sys

from jarvis_voice.sanitize import has_speakable, sanitize_for_speech

CASES = [
    # (说明, 输入, 期望包含, 期望不含)
    ("粗体+行内代码",
     "超时改成了 **六十秒**，文件是 `/Users/owen/proj/config.py`。",
     ["六十秒", "/Users/owen/proj/config.py"], ["**", "`"]),

    ("空行塌缩（实测发现的真问题）",
     "行，我给你编三个。\n\n然后第二件，回个电话。\n\n还有第三件，修水龙头。",
     ["我给你编三个", "回个电话", "修水龙头"],
     ["\n\n"]),

    ("markdown 链接 → 文字",
     "详见 [官方文档](https://example.com/docs) 的说明。",
     ["官方文档", "说明"], ["https://", "example.com", "(", ")"]),

    ("裸 URL → 占位词",
     "参考 https://cloud.google.com/text-to-speech/pricing 这份价格页。",
     ["链接", "价格页"], ["https://"]),

    ("列表记号被去掉",
     "- 第一件事\n- 第二件事\n1. 第三件事",
     ["第一件事", "第二件事", "第三件事"], ["- 第", "1. "]),

    ("emoji 清掉",
     "好的 👍 马上办 ✅",
     ["好的", "马上办"], ["👍", "✅"]),

    ("标题记号去掉",
     "## 结论\n复利就是利滚利。",
     ["结论", "复利就是利滚利"], ["##"]),

    ("表格分隔行去掉",
     "费用 | 额度\n---|---\n30 | 100万",
     ["费用", "额度", "30", "100万"], ["---"]),

    ("数字与专名保真（关键）",
     "测试 3 个通过了，成本 $0.0036，覆盖率 87.5%，UCD 的 ECS4 专业。",
     ["测试 3 个", "$0.0036", "87.5%", "UCD", "ECS4"], []),
]

FAILS = 0
for name, src, must_have, must_not in CASES:
    out = sanitize_for_speech(src)
    problems = []
    for m in must_have:
        if m not in out:
            problems.append(f"缺 {m!r}")
    for m in must_not:
        if m in out:
            problems.append(f"不该有 {m!r}")
    status = "✅" if not problems else "❌"
    if problems:
        FAILS += 1
    print(f"{status} {name}")
    print(f"   出: {out!r}")
    if problems:
        print(f"   问题: {problems}")

# 边界
print()
edge = [("空串", ""), ("纯 emoji", "🎉🎉"), ("纯空白", "   \n\n  ")]
for name, src in edge:
    out = sanitize_for_speech(src)
    print(f"{'✅' if out == '' else '❌'} 边界[{name}] -> {out!r}  has_speakable={has_speakable(out)}")

print(f"\n{'全部通过' if FAILS == 0 else f'{FAILS} 个用例失败'}")
sys.exit(1 if FAILS else 0)
