#!/usr/bin/env python3
"""环境变量接线：**文档里说"可设"的，代码里必须真的读**。

**为什么要这条**：2026-09-21 发现 `JARVIS_INTERRUPT_CONFIRM_MS` 一直**只写在文档里**，
代码从没读过它 —— 于是所有"打断延迟可调"的说法里，**「可调」那半句都是错的**
（只有改代码才生效）。这类幽灵变量最难发现：**设了不报错、也不生效**，你会以为调过了。

**判据**：把 `docs/*.md` + `README.md` 里出现的所有 `JARVIS_*` 名字，与
**全仓 Python 里真正被读的**（三种写法：`_env_*(...)` / `os.environ.get` /
`os.environ[...]`）求差集。差集非空 = 有幽灵。

⚠️ **本测试自带阳性对照**（③）：喂一个假名字进去，扫描器**必须**报出来。
   否则扫描器坏了也会"全通过"——那正是这类检查最危险的失败模式（假阴性）。
"""
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAIL = []


def check(label, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {label}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(label)


# 读环境变量的三种写法（少一种就会误报，2026-09-21 就吃过这个亏）
_PATTERNS = (
    re.compile(r'_env_(?:str|int|float)\(\s*"([A-Z0-9_]+)"'),
    re.compile(r'os\.environ\.(?:get|setdefault)\(\s*"([A-Z0-9_]+)"'),
    re.compile(r'os\.environ\[\s*"([A-Z0-9_]+)"\s*\]'),
)

# 有意为之的例外：**不是配置覆盖**，写在这里要带理由。
ALLOW = {
    # 目录常量（~/.jarvis 的根），不是"给用户调的开关"
    "JARVIS_HOME",
}


def scan_text_for_reads(text: str) -> set[str]:
    out = set()
    for p in _PATTERNS:
        out |= set(p.findall(text))
    return out


def read_env_names() -> set[str]:
    """全仓 Python 里真正被读的环境变量名。"""
    out: set[str] = set()
    for p in REPO.rglob("*.py"):
        if ".venv" in p.parts:
            continue
        out |= scan_text_for_reads(p.read_text(encoding="utf-8", errors="ignore"))
    return out


def mentioned_in_docs() -> dict[str, list[str]]:
    """文档里出现的 JARVIS_*，以及各自出现在哪些文件。"""
    out: dict[str, list[str]] = {}
    files = sorted((REPO / "docs").glob("*.md")) + [REPO / "README.md"]
    for p in files:
        if not p.exists():
            continue
        for name in re.findall(r"\b(JARVIS_[A-Z0-9_]{2,})\b",
                               p.read_text(encoding="utf-8", errors="ignore")):
            out.setdefault(name, []).append(p.name)
    return out


print("=== ① 阳性对照：扫描器必须能认出「假名字」 ===")
print("    （不先证明它会响，就不知道'全通过'是真干净还是它坏了）")
synthetic = 'x = os.environ.get("JARVIS_TOTALLY_FAKE_NAME_XYZ", "1")'
check("三种写法都认得", sorted(scan_text_for_reads(synthetic)),
      ["JARVIS_TOTALLY_FAKE_NAME_XYZ"])
check("_env_int 写法认得",
      sorted(scan_text_for_reads('a=_env_int("JARVIS_FAKE_A", 1)')), ["JARVIS_FAKE_A"])
check("下标写法认得",
      sorted(scan_text_for_reads('b=os.environ["JARVIS_FAKE_B"]')), ["JARVIS_FAKE_B"])

print("\n=== ② 文档提到但代码没读的（幽灵变量）必须为空 ===")
wired = read_env_names()
mentioned = mentioned_in_docs()
ghost = sorted(n for n in mentioned if n not in wired and n not in ALLOW)
print(f"    代码里读了 {len(wired)} 个；文档提到 {len(mentioned)} 个")
if ghost:
    for g in ghost:
        print(f"    ⚠️ {g}  —— 出现在 {', '.join(sorted(set(mentioned[g])))}，"
              f"但全仓 Python 里没有任何地方读它")
check("幽灵变量数", len(ghost), 0)

print("\n=== ③ 关键的那几个必须真的接线（逐个实测生效）===")
sys.path.insert(0, str(REPO))
from jarvis_voice.config import Config                        # noqa: E402

os.environ["JARVIS_INTERRUPT_CONFIRM_MS"] = "250"
check("JARVIS_INTERRUPT_CONFIRM_MS 生效（2026-09-21 修）",
      Config.load().interrupt_confirm_ms, 250)
os.environ.pop("JARVIS_INTERRUPT_CONFIRM_MS")
check("  默认值", Config.load().interrupt_confirm_ms, 100)

os.environ["JARVIS_AEC_BACKEND"] = "speex"
check("JARVIS_AEC_BACKEND 生效", Config.load().aec_backend, "speex")
os.environ.pop("JARVIS_AEC_BACKEND")

print()
if FAIL:
    print(f"❌ {len(FAIL)} 项失败：{FAIL}")
    sys.exit(1)
print("✅ 全部通过")
