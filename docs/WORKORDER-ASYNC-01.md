# WO-ASYNC-01：后台工具 + 边跑边说（B 档）

> **性质**：施工单。对应 2026-09-20 行业调研的「前沿方向」= **异步工具**：
> 思路从「掩盖静音」变成「**根本不产生静音**」。
> **依据**：本机实测（§1）+ Claude Code 官方能力核实。
> **前置**：A 档（承接句）已完成，见 `WORKORDER-LEADIN-01.md`。

---

## 0. 一句话

Claude Code **自己就能**做到「启动后台任务 → 立刻回话 → 收到通知 → 自己读输出 → 报结果」。
我们**不需要造这个机制**，只需要把它的「非应答轮」接出来 —— 而现在这个接法是**坏的**（§3）。

---

## 1. 本机实测（一手，`--bare` + 生产同款命令行）🟢

命令：与 `claude_bridge.py:131-179` 的 `start()` 完全一致
（`--input-format stream-json --output-format stream-json --include-partial-messages
--verbose --model haiku --dangerously-skip-permissions --bare --allowedTools Bash`），
`sleep 20` 用 `run_in_background: true`。

```
t= 2.90  TOOL Bash {"run_in_background": true}          ← 模型自己加的参数
t= 3.07  SYSTEM/task_started   {task_id, tool_use_id, is_backgrounded:true, task_type}
t= 3.07  USER(tool_result)     "Command running in background with ID: …"
t= 4.08→4.23  assistant text   "已启动后台任务（ID …, 运行 `sleep 20; echo …`），我不会等它完成"
t= 4.38  ★RESULT num_turns=2   ← 回合 4.4 秒收尾（若前台跑，这里要 23 秒）
t=23.09  SYSTEM/background_tasks_changed  {tasks: []}
t=23.09  SYSTEM/task_updated   {patch:{status:"completed"}}
t=23.09  SYSTEM/task_notification {task_id, tool_use_id, status:"completed",
                                   output_file, summary}
t=23.17  SYSTEM/init           ← ⚠️ 会话中途**又发了一次 init**
t=24.19  TOOL Read             {file_path: <output_file>}   ← CC 自己去读输出
t=25.23  assistant text        "后台任务已完成（退出码 0），输出为 BACKGROUND_DONE。"
t=25.30  ★RESULT
```

**三个结论**：
1. **回合提前结束**：4.4s 收尾，不是 23s —— 静默从根上消失。
2. **CC 自主续跑**：通知进来后它自己起新一轮、自己读输出、自己汇报。**编排器不用喂任何东西**。
3. 本次实验就是 `--bare`（我们的默认），**行为一致**；子调研里那条「`--bare` 差异查不到」已由本次实测补上。

**新发现（子调研没提到的两个 subtype）**：
- `system/status`（`{"status":"requesting"}`）—— 每轮多次，无信息量
- `system/background_tasks_changed`（`{"tasks":[…]}`）—— 唯一能给出**当前在跑的后台任务列表**的帧

---

## 2. 现状：`_pump` 的路由会**污染下一轮**

`claude_bridge.py:226-257` 的 `_pump` 只有三条路：

| 帧 | 去向 |
|---|---|
| `system/init` | 就地处理（存 session_id / `_ready.set()`）|
| `control_response` | 就地处理（interrupt 回执 / mcp_reconnect）|
| **其余全部** | **`_pending`**（含 `task_*` / `status` / 以及**非应答轮的 assistant/result**）|

而 `_read_turn`（`:474-520`）只认 `stream_event`/`assistant`/`user`/`result`，
**没有 `else`** —— 所以 `task_*` 在轮内被静默跳过（无害）。

**⚠️ 真正的 bug 在「没有轮在跑」的时候**：CC 自主续跑那一段
（`t=24.19`–`t=25.30`）的 `assistant` / `stream_event` / `result` **全都会堆进 `_pending`**，
而下一个 `ask()` 会：

1. `_read_turn` 的「丢弃旧 result」循环（`:468-473`）只丢**队头连续**的 result → 挡不住
2. 把**上一轮（CC 自主的）的句子**当成这一轮的回答吐出去
3. 撞上那段的 `result` 帧 → `self._turn_open = False` 并 yield `done` →
   **真正的这一轮被提前结束**

这与 `:382-397` `_drain_until_result` 的注释里写的「上一轮残句漏进下一轮」是同一类，
只是触发源不同（那次是放弃生成器，这次是 CC 主动续跑）。

---

## 3. 逐文件改动

### 3.1 `claude_bridge.py`

**a) 新增 `self._awaiting`（是否有 `ask()` 在等）**

`ask()` 在**写 stdin 之前**置 `True`（CC 可能 170ms 就回 tool_result，实测 `t=3.07`），
`finally` 置 `False`。

**b) `_pump` 分流**

```python
if etype == "system":
    sub = ev.get("subtype")
    if sub == "init":            ...（现状不动）
    elif sub in ("task_started", "task_updated", "task_notification"):
        self._tasks.put(ev)      # 任务生命周期 → 独立队列
        continue
    elif sub == "background_tasks_changed":
        self._tasks.put(ev)
        continue
    else:
        continue                 # status 等：无信息量，直接丢（不再进 _pending）
if etype == "control_response":  ...（现状不动）
# 轮内帧：只有 ask() 在等时才进 _pending；否则是 CC 自主续跑 → 走 async 通道
self._sink(ev)
```

`_sink`：`self._awaiting` → `_pending`；否则 → **`_async_in`**（交给 async 翻译器）。

**c) 非应答轮的「帧 → 句子」翻译**

复用 `_split_sentences`（已是纯 staticmethod）。新增一个小状态机
（**只有 buf + 切句**，约 10 行），**不复制 `_read_turn` 的排空/中断/超时逻辑** ——
那些只对应答轮有意义（深度优先：把可复用的抽出来，而不是把整个 `_read_turn` 抄一遍）。

产出事件形状与 `_read_turn` 对齐：`{"type":"sentence"|"tool"|"done", ...}`。

**d) 对外新接口（只加一个）**

```python
def next_async(self, timeout: float | None = None) -> dict | None:
    """取一条**非应答轮**事件（后台任务通知 / CC 自主续跑的句子）。
    没有则阻塞至多 timeout 秒；超时返回 None。"""
```

`_tasks` 的事件原样透出（`subtype` = started/updated/notification/changed）。

### 3.2 `jarvis_voice/orchestrator.py`

**a) 新线程 `AsyncThread`**（与 `BrainThread`/`TTSThread` 并列，同一套「打不死」结构）

```
while not stop:
    ev = brain.next_async(timeout=0.5)
    · task_started / background_tasks_changed → 只更新状态 + 日志，**不出声**
    · sentence → sanitize → _note_spoken → sent_q（复用现有 TTS 链路）
    · done     → 收尾（若本轮一句话都没说，不产生任何声音）
```

**b) 轮次归属（**最容易出错的地方**）**

CC 自主续跑的句子**不属于用户发起的任何一轮**。要单独 `session.begin_turn()`，
否则 `sent_q` 里的 `(turn, text)` 会被 `is_current()` 判为过期而丢弃。

⚠️ 但**不能**在用户正在说话/正在播报时抢占 —— 规则：
`state is not IDLE` 时，把自主轮的句子**排队等**（或直接丢弃，见 §4 的待定项）。

**c) 提示词（`SYSTEM_PROMPT`）新增两条**

实测 CC 刚才说的是：
> 「已启动后台任务（ID b3tbmiy67，运行 `sleep 20; echo BACKGROUND_DONE`），我不会等它完成」

**这句念出来是灾难**（念任务 ID、念 shell 命令）。必须加：

```
16. **耗时命令用 Bash 的 run_in_background 跑**（预计超过约 5 秒的：搜索、抓网页、
    大文件处理、多个 curl）。启动后**立刻**回一句人话，**不要等它跑完**。
17. ⛔ **绝不要念出任务 ID、文件名、shell 命令、退出码。** 启动时说「这个我去后台跑，
    好了跟你说」；跑完收到通知时直接说**结果**。
    ❌「已启动后台任务（ID b3tbmiy67，运行 `sleep 20`）」
    ✅「这个我放后台跑，好了喊你。」
    ✅「查到了，明天阴天，不下雨。」
```

**d) `sanitize_for_speech` 兜底**（可选，见 §4）

`sanitize.py:27` 的 `_INLINE_CODE_RE` 会**保留**反引号里的内容
（`` `sleep 20` `` → `sleep 20`）—— 所以命令**会**被念出来。
规则层无法可靠判断「这是命令还是正常词」，所以**主要靠提示词**；
sanitize 层只在 §4 的实验证明必要后再动。

---

## 4. 待定项（**需要 Owen 拍板**）

| # | 问题 | 选项 |
|---|---|---|
| Q-1 | **用户没问任何东西时，后台跑完了要不要主动开口？** | (a) 要（CC 的本能；也是异步工具的价值）(b) 只在「这一轮的延续」里说 —— 但我们现在分不出 |
| Q-2 | 用户正在说话 / 正在播报时，自主轮的结果怎么办？ | (a) 排队等空闲 (b) 直接丢 (c) 走打断逻辑抢话 |
| Q-3 | 打断一个**后台任务**怎么办？现在 `_do_interrupt()` 只作用于当前轮 | 新增 `TaskStop` 控制？还是等它自己完 |

---

## 5. 验收

**可测（离线）**
- `next_async()` 在无应答轮时不把帧漏进 `_pending`（**red-green**：旧代码上必失败）
- 非应答轮的句子能被切出来、且**不**污染随后 `ask()` 的输出（这次 bug 的直接回归）
- `task_notification` 事件能透出，且 `output_file` 字段完整

**真机**
- 问一个「要跑很久」的问题（如让 CC 跑多步计算），同场对照：
  - 改前：用户说完 → 静默 14–70 秒 → 结果
  - 改后：用户说完 → ~2s「这个我去后台跑」→ 结果回来 → 说结果
- ⚠️ 关键风险：**CC 会不会滥用后台**（把本该 1 秒的事也丢后台 → 多一轮往返，反而更慢）。
  这条只能真机看，验收时数「单工具短任务被后台化的比例」。
