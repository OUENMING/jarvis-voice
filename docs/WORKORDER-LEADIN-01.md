# WO-LEADIN-01：按工具类别播承接句（A 档）

> **性质**：施工单。对应 `PLAN-LATENCY-20260919.md` 的 P1 + P2 合并，方向由 2026-09-20 的
> 行业调研校正（原 P2「把通用填充音加长」被调研否掉，见 §1）。
> **前置已清**：版本控制 ✅、明文密钥 ✅、`config.py` 白名单 ✅。

---

## 0. 一句话

工具轮静默 **p50 14.3s / p90 49.3s / max 113.8s**，而现有填充音只有 **0.96s —— 对三段静默的覆盖率都是 0%**。
「加长通用填充音」是错的方向（§1），正确方向是**用编排器已知的工具名，播一句上下文相关的短承接句**。

---

## 1. 方向校正（2026-09-20 调研，推翻了原 P2）

原 `PLAN-LATENCY-20260919.md` §P2 写的是「文案从『无内容』改『有内容』（处理长静音）」。
调研结论**否掉了「加长通用填充音」这条路**：

| 证据 | 内容 |
|---|---|
| OpenAI 官方 prompting 指南 | preambles「Used well, they reassure… **Used poorly, they become filler and increase perceived latency**」 |
| Maslych et al., CUI'25（arXiv 2507.22352，2025-07-30，n=54）| filler 改善**感知**响应时间，但**没有**改善参与感/胜任感/再交互意愿 |
| ConvFill 用户研究（n=18）| 响应性↑ 但 **Naturalness 偏低**，用户反馈 filler「有时显得重复」 |
| Jeong et al.（经 arXiv 2508.11781 转引）| 用简单 filler 的 agent **被认为更不聪明** |
| Liu（2026-04-28, Purdue）| **上下文相关**的 filler 明显优于通用 filler |

→ **通用型（「嗯…」「那个…」）是收益最弱、副作用最明确的一档**；要的是**上下文相关**。

**行业侧对照**（同期调研）：OpenAI `gpt-realtime-2` 默认就说承接语（厂商基线）；
Azure / ElevenLabs / LiveKit / Vapi **四家都有平台原生 filler**（Azure 独占期已结束）；
前沿已转向**异步工具**（LiveKit 1.6.0 / 2026-06-11；Gemini 3.8 Live `NON_BLOCKING` 默认）——
那条是 B 档，见 `WO-ASYNC-01`（待写）。

---

## 2. 本机事实（决定实现形状）

### 2.1 `claude_bridge` **没有 tool 结束事件**

`claude_bridge.py` 只 yield `sentence | tool | done | interrupted | error`（🟢 grep 确认）。
→ **编排器知道工具何时开始，不知道何时结束。**

**后果**：无法判断「这个工具 200ms 就回来了」。
验收清单里那条「快路径 —— 填充音还该响吗？」（Roark 2026-08-24：*a filler on a fast tool is a
self-inflicted second of latency*）**只能靠「延迟 + 可取消」来兜**，不能靠判断。

→ 所以**保留延迟**：tool 事件触发时**不立刻播**，而是起一个短定时器；真句子先到就 `_cancel_filler()` 取消。
    现有 `_cancel_filler` 机制已经在做这件事，复用它。

### 2.2 工具名分布（真机全量，`events.jsonl`）

| 次数 | 工具 | 类别 |
|---|---|---|
| 68 | `Bash` | `run` |
| 112 | `mcp__browser__*`（computer/javascript_tool/navigate/tabs_context/get_page_text/find/get_page_markdown/read_page/tabs_create）| `browse` |
| 11 | `mcp__anysearch__*` / `mcp__parallel-search__*` / `mcp__tavily__*` | `search` |
| 8 | `mcp__obsidian-vault__*` | `notes` |
| 4 | `ToolSearch` / `Skill` | 通用（认不出） |

覆盖 **199/203 = 98%**。剩下 2% 落回通用池。

---

## 3. 逐文件改动

### 3.1 `jarvis_voice/fillers.py`

**a) 文案分池**（新增，`TEXTS` 保留为通用池）：

```python
# 通用池：非工具回合的兜底（delay 后还没出声才播），以及认不出的工具。
TEXTS = ["嗯……", "那个……", "让我想想。", "我看一下。"]

# 按工具类别的**意图式**承接句。规则：≤12 字、说意图不说结果、不说"已完成"。
TOOL_TEXTS = {
    "search": ["我搜一下。", "我查一下。"],
    "browse": ["我开个页面看看。", "我翻一下网页。"],
    "notes":  ["我翻下笔记。"],
    "run":    ["我跑一下。", "我看一下。"],
}
```

**b) 纯函数 `tool_tag(name) -> str`**（无依赖 → 可单测）：

```python
_PREFIX_TAGS = (
    ("mcp__anysearch__", "search"),
    ("mcp__parallel-search__", "search"),
    ("mcp__tavily__", "search"),
    ("mcp__browser__", "browse"),
    ("mcp__obsidian-vault__", "notes"),
)

def tool_tag(name: str) -> str:
    """工具名 → 承接句类别。认不出返回 `FillerClips.GENERIC`（= 通用池）。"""
    n = name or ""
    for pref, tag in _PREFIX_TAGS:
        if n.startswith(pref):
            return tag
    return "run" if n == "Bash" else FillerClips.GENERIC
```

**c) `FillerClips` 支持 tag 池**

- `__init__`：`texts` 接受 `list[str]`（= 通用池，**向后兼容**）或 `dict[str, list[str]]`
- `clips`（扁平 list）/ `ready()` / `ensure()` **签名不变**（`tests/test_filler.py:27-33` 依赖）
- 新增 `_clip_tags`（与 `clips` 同序）、`_i`（每 tag 一个轮转指针）、`_last_text`（重复抑制）
- `pick(tag="")`：候选 = 该 tag 的片段，**为空则退回通用池**；返回 `(pcm, text)`
- **不重复**：池内轮转；若整个候选池只有 1 条且它就是上次播的，仍然返回（不能因此不播）

### 3.2 `jarvis_voice/config.py`

`filler_delay_ms: 900 → 2000`（照抄 Azure Voice Live `interim_response` 的 `latency` 触发默认值 2000ms）。
**副作用是好的**：闲聊首句 p50 2.1s，2000ms 的兜底会让大部分闲聊**不再插填充音** ——
正好避开调研里「filler 用太多反而差评」。

### 3.3 `jarvis_voice/orchestrator.py`

**a) `__init__`** 增两行：

```python
self._filler_played_turn: int | None = None   # 本轮已播过填充音（防重复触发）
self._filler_lock = threading.Lock()
```

**b) 抽出 `_play_filler(turn, tag)`**（`_arm_filler.fire()` 与 tool 路径共用）：

```python
def _play_filler(self, turn: int, tag: str = FillerClips.GENERIC) -> None:
    if not (self.fillers and self.fillers.ready()):
        return
    with self._filler_lock:
        if self._filler_played_turn == turn:
            return                      # 本轮已播过 —— tool 路径与兜底定时器只能二选一
        self._filler_played_turn = turn
    if not self.session.is_current(turn) or self.session.state is not State.THINKING:
        self._filler_played_turn = None   # 回滚，让兜底还有机会
        return
    clip, text = self.fillers.pick(tag)
    if not clip:
        return
    self._cancel_filler()
    self.player.write_filler(clip)
    self.echo_guard.note_spoken(text, filler=True)   # ⚠️ 必须登记，见 echoguard 模块 docstring
    BUS.emit("filler", delay_ms=..., text=text, tag=tag)
```

**c) `_arm_filler`** 改为起定时器调 `_play_filler(turn)`（逻辑不变，只是搬了家）。

**d) `_brain_once` 的 `tool` 分支**加触发：

```python
elif et == "tool":
    if first:                       # 本轮一次都还没出声 = CC 没说 preamble → 我们补
        self._arm_lead_in(turn, tool_tag(ev.get("name") or ""))
```

**e) `_arm_lead_in(turn, tag)`** —— tool 路径与兜底共用同一个 `_play_filler`，**但带一个短宽限**：

```python
def _arm_lead_in(self, turn: int, tag: str) -> None:
    """tool 事件触发的承接句。带 `filler_tool_delay_ms` 宽限。

    ⚠️ **不是 delay 0**（偏离计划 P1 表的「delay 0」，理由在此）：
    Roark 验收清单那条「工具 200ms 返回 —— 填充音还该响吗？」
    （*a filler on a fast tool is a self-inflicted second of latency*）。
    delay 0 时，快工具会让承接句播到一半就被 `_cancel_filler()` 切掉 →
    用户听到**截断的半句话**，比静音更糟。
    宽限 300ms：真快工具（<300ms 就出首句）完全不播；而本机工具步 p50 4.50s，
    300ms 在正常路径上可以忽略。
    """
    self._cancel_filler()
    self._filler_timer = threading.Timer(self.cfg.filler_tool_delay_ms / 1000.0,
                                         lambda: self._play_filler(turn, tag))
    self._filler_timer.daemon = True
    self._filler_timer.start()
```

⚠️ **与 CC 自带承接语的互斥**：`first` 为假说明 CC 已经说过话 → **绝不补**（否则一次两个承接句）。

---

## 4. 验收

### 4.1 单测（无音频设备，red-green）

| 用例 | 判据 |
|---|---|
| `tool_tag()` 五种类别 + 认不出的名字 | 映射正确；空串不炸 |
| `pick(tag)` 只在对应池里取 | 拿到的 text ∈ `TOOL_TEXTS[tag]` |
| 认不出的 tag 退回通用池 | text ∈ `TEXTS` |
| 池内轮转不连续重复 | 连续两次 pick 的 text 不同 |
| `_play_filler` 同一 turn 只播一次 | 第二次是 no-op（**旧代码上必失败**：旧代码无此守卫）|

### 4.2 真机（§3 的验收表，必须带对照）

改前改后各跑一场**同场景**（问一个要调工具的问题），对比：
- **工具回合首句延迟**（目标：从 p50 14.3s 显著下降 —— 注意承接句**不减少**工具时间，
  它减少的是**感知**静默；`first_ms` 这个指标**量不到**，要另记「首条音频时间」）
- ⚠️ **指标修正**：`first_ms` 记的是"首句**真内容**"，承接句不改它。
  真正该看的是 **`BUS.emit("filler")` 的时刻 → 用户听到第一个声音的间隔**。
  这个数目前没有埋点 —— **本次要加**（见 §5 的坑）。
- 一轮两次工具：**同一句话 20 秒内会不会重复播两次**

---

## 5. 已知坑

| # | 坑 | 处理 |
|---|---|---|
| K-1 | **没有 tool 结束事件** → 判不了"快工具" | 延迟 + `_cancel_filler` 兜（§2.1）|
| K-2 | 填充音会被麦克风收回去转写 | `note_spoken(text, filler=True)`；`echoguard` 已有短窗口通道（2026-09-20）|
| K-3 | 一轮内 tool 路径与兜底定时器**双触发** | `_filler_played_turn` + `_filler_lock` |
| K-4 | `first_ms` 量不到承接句的收益 | 验收要看「首次出声时间」，本次补埋点 |
| K-5 | 新文案的**预渲染成本**：池子从 4 条涨到 4+7=11 条，启动要多渲染 7 条 | 已在后台线程（`ensure()`），但首次启动会多花几秒；缓存命中后无成本 |
