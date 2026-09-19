# JARVIS 双工语音助手 · 独立审计报告

- 审计人：独立审计 Agent（ZCode）
- 日期：2026-09-13 02:00–03:00（UTC+8）
- 性质：诊断，未修改任何代码。实验期间未重启服务、未动任何配置文件；结束时已将 `listen_prob_scale` 恢复为现场值 0.6（C++ 直调回执：`restore scale=0.6: True | kv: 303`）。
- 测试对真机的影响：全部实验为无麦探针（合成/预录 WAV 驱动真实双工 API），无外放，不占用麦克风。发起前已确认 Mac 无客户端进程、Windows 服务端自 02:03:26 后无活跃会话。

---

## 1. 我实际验证了什么

### 1.1 版本鉴定（本报告最硬的一条）

**Windows `llama-server.exe` = tc-mb/llama.cpp-omni 仓库 tag `v1.0.22`（commit `61d8393`，2026-04-29 14:50:59 +0800）构建。**

证据链：

1. exe 时间戳：`llama-server.exe LastWriteTime = 2026/4/29 21:57`（实测 `Get-Item`）。
2. tag `v1.0.22` 提交时间 `2026-04-29 14:50:59 +0800`（`git log -1 v1.0.22`，本次 fetch 到本地核实），与 exe 同日、晚 7 小时构建，完全吻合。
3. 行为特征比对（tag 源码 vs 二进制实测日志）全部命中：
   - 二进制日志有 `LLM Duplex: LISTEN during listen state, skip flush` → `git show v1.0.22:tools/omni/omni.cpp` 含同一字符串；
   - 二进制 `update_session_config` 回执含 `sampling:{listen_prob_scale, force_listen_count, max_new_speak_tokens_per_chunk, tts_temperature}` → tag 源码含对应实现（`force_listen_used < force_listen_count` 分支）；
   - exe 内含字符串 `listen_prob_scale`、`max_new_speak_tokens`（`Select-String` 探测）。
4. `build: 74 (4d28373)` 中的短 SHA `4d28373` 在 tc-mb 仓库全部 refs 中不存在（`git log --all` 与 GitHub API 双查）——应为 Comni 打包时的工作区/基线 SHA，不影响上述鉴定。
5. PR 时间线（GitHub API 实查）：PR #47 合入 2026-06-02、PR #78 合入 2026-07-01，**均晚于二进制构建日期 → 二进制物理上不可能包含**；PR #101（base `bench/huawei`）与 PR #108（base `master`）**从未合入**。PR #47/#78 的改动文件恰为 `tools/omni/omni.cpp`（双工路径）。
6. 交接文档说的"Mac 源码停在 PR #29 与二进制关系不明"需要修正：Mac checkout（`feat/web-demo` @ `5202b7b`，PR #29 merge，2026-04-23）与 v1.0.22 是**近亲**——`tts_bos_token_id` 初始化行号 4071-4074（Mac）vs 4081-4084（v1.0.22），仅差 10 行， duplex 逻辑同构。两边都缺 listen 守门。

### 1.2 listen 守门的独立出处验证

官方参考实现 `openbmb/MiniCPM-o-4_5/modeling_minicpmo.py`（HF 下载 214 KB 原文）第 3237-3239 行：

```python
# if current turn not ended, not allowed to listen (only check when not force_listen)
if last_id.item() == self.listen_token_id and (not self.current_turn_ended):
    last_id = torch.tensor([self.tts_bos_token_id], dtype=torch.long, device=self.device)
```

交接文档给的行号（5104-5106）与文件名（`modeling_minicpmo_unified.py`）不准，但**机制本身属实**。语义：`current_turn_ended` 只在 turn 终止 token（turn_eos 等）置 True、正常 speak token 置 False；因此一个 turn 内说完一个 chunk 后**必须继续 speak，不许转听**。

C++ 侧（Mac checkout 与 v1.0.22 一致）：`current_turn_ended` 全文件只写不读（9271/9450/9491 置位，无任何读取点）；采样到 `<|listen|>` 直接 `break`（omni.cpp:9474-9514）；`tts_bos_token_id` 仅 4071-4074 初始化，注释写着"用于双工模式强制继续说话"，生成循环从未使用。**守门缺失在两个代码副本上均成立。**

### 1.3 无麦探针实验（无播放、不重启，脚本 /tmp/audit_probe.py）

问句 `probe2.wav`（SenseVoice 转写："欧曼阿尔法，你好，请你用中文跟我说一段话，介绍一下你自己多说几句，越详细越好"，8.4s）；任务问句 `qtask.wav`（合成："帮我查一下今天都柏林的天气怎么样，适不适合出去拍星星"，5.9s）。

| Run | 条件 | 产出音频 | burst 结构 | 累积文本 | `<task>` |
|---|---|---|---|---|---|
| 1 | scale=0.6，新会话 | 3.8s | 2.84s + 1.00s（相邻拍连续） | "你好，我是Omen Alpha。" | 无 |
| 2 | scale=0.6，新会话，任务问句 | 2.2s | 单 burst 2.20s | "好的，我为你查一下。" | 无 |
| 3 | scale=1.0（C++ 直调，人设 KV=303 重灌） | 2.3s | 1.00s → **静音拍** → 1.28s | "你好，我是Omen Alpha。"（单词 Omen 被劈成 "…我是O" / "men Alpha。"） | 无 |
| 4 | scale=0.2 但**复用 Run3 会话**（污染） | 1.2s | 1.16s | "好的，" | 无 |
| 4b | scale=0.2，重新 init（干净会话） | 8.6s | **单 burst 8.56s** | "大家好，我是Omen Alpha。在接下来的日子里，我会陪伴您左右，帮助您提升效率。" | 无 |

服务端 `llm_debug/llm_text.txt` 原文取证（Run1/Run2，重启前抓取）与客户端 SSE 收到的文本一致——这几轮未发生文本/WAV 错位。

每拍墙钟实测 0.30–0.90s（decode 快于 1s 音频节拍）；产出均为 1.00s 的整数倍（25 T2W token/拍）。

### 1.4 Windows 日志取证（只读）

- `wav_timing.log` 真机段（01:59:55 会话，**当时 scale 环境变量尚未配置，即默认 1.0**）：12 块 WAV，单块 0.32–1.04s，发送间隔 0.13s–**20.9s**——"1 秒碎片 + 长静音"在服务端日志层直接坐实。
- `run-wrapper.bat` 现值：`OMNI_LISTEN_PROB_SCALE=0.6`、`OMNI_MAX_SPEAK_TOKENS=60`、`MODE_FLAG=--duplex`。`server.log` 回执 `listen_prob_scale set to 0.6000` 确认生效。
- `update_session_config` 直调实验返回 `kv_cache_length: 0`（不带 `voice_audio` 时）→ **证实该调用清 KV cache**（上游 PR #38 的说法成立）；wrapper 的 fast-resume 路径每次都带 `voice_audio` 重灌人设（回执 kv=303），所以实际使用中人设未丢。

### 1.5 未验证（诚实清单）

- 真机回声场景（`_excise_echo`、双阈值插话）——需要外放+麦克风，凌晨不便；保持原 Agent 标注的"未真机验证"。
- text 注入回灌（原 Agent 已实测有效；我仅在代码层复核了 wrapper 转发点 `minicpmo_cpp_http_server.py:1638`，未做行为实验）。
- 跨会话退化（"跑一段后全 LISTEN"）——长时行为未测。注意我的 Run4（复用会话重问）只答了"好的，"提示**会话内上下文状态对开口行为影响很大**，与该现象可能同源。
- scale 各档样本量 n=1，temperature=0.7 有随机性；结论方向可信（1.0→0.6→0.2 burst 变长单调），具体数值不可外推。

---

## 2. 我推翻/确认了原 Agent 的哪些结论

**确认（证据升级）**：
- 模型每拍产约 1.00s 音频（我的全部 Run 吻合：0.84/1.00/1.00/1.28/8.56 均为 1.0s 倍数）。
- 模型从不输出 `<task>` 标签——Run2 独立复现（问"帮我查天气"，答"好的，我为你查一下。"，原始文本无标签）。
- 二进制 = Comni v1.0.22、不含 PR #47/#78（原为推断，现已坐实到 tag + 日期 + 特征三重证据）。
- listen 守门缺失（原为中高置信推断，现已获官方 Python 原文 + 两份 C++ 源码只写不读的证据）。
- 尾部等待双信号改造、text 转发、人设注入：代码层复核属实。

**推翻/修正（重要）**：
1. **Core Problem 的表述不成立**。原文："双工一拍本身就要约 1.0–1.06 秒，因此模型在连续说话时无法在相邻拍上保持发声"。实测每拍墙钟仅 0.30–0.90s（**快于**实时），且 Run1（0.6）出现 3 连说话拍、Run4b（0.2）出现横跨 9 拍的 8.56s 连续 burst——**模型可以跨拍连续说话**。碎片化的决定因素是模型是否输出 `<|listen|>`（决策+采样偏置），不是节拍墙钟。候选根因 #3（"每拍 1 秒切换是设计行为"）同样被 Run4b 证伪。
2. **候选根因 #4（客户端播放/缓冲策略）基本排除**：供应端 RTF 0.3–0.9（<<1），缓冲不会抽干。真机"卡"的主因在服务端切听，不在播放。
3. **发现两个原 Agent 未报告的新 bug（见 §3）**，其中缩进 bug 直接废掉整条任务路由。

---

## 3. Root cause

**症状 A："卡顿/说一半被切断"**（证据充分）

Root cause：**运行中的二进制（Comni v1.0.22）缺少官方参考实现的 listen 守门**——turn 未结束时模型采样出 `<|listen|>` 即被直接接受并结束本拍，把一句话拦腰切成长约 1s 的碎片、中间夹静音拍。官方 Python 版此场景会强制回 `tts_bos` 继续说话；C++ 侧 `tts_bos_token_id` 只初始化、从未在生成循环使用。

支持证据：Run3（scale=1.0）单词 "Omen" 被劈开 + 中间静音拍；真机 wav_timing 01:59 段（当时 scale=1.0）1s 碎片 + 7–21s 间隔；v1.0.22 源码无守门；官方 Python 有守门且注释明说 "not allowed to listen"。
部分缓解已验证：`listen_prob_scale` 压低 listen 概率 → burst 变长变连续（0.6/0.2 均改善）。**注意：01:59 真机测试时 0.6 还没配置生效，所以"0.6 没用"的结论从未成立过——0.6 从未在真机上被测过。**

**症状 B："不回应我说话""语音让 CC 干活从未打通"**（证据充分，三 bug 叠加）

1. 🔴 **新发现：VAD/ASR 死循环缩进 bug**。`jarvis_omni.py:787-811`——用户语音收集（`user_pcm.append`）与 ASR 触发（`_run_asr`）整块缩进在 `if beats >= 10:` 之内（indent=16 vs 外层 12），**每 10 拍（约 10 秒）才采样一次用户语音**。后果：`TASK_RE` 关键词路由几乎必死、ASR 转写 `[你]` 几乎从不打印。这解释了"不回应我说话了直接"的很大一部分。原 Agent 修过 `_run_asr` 里的括号 bug（已确认修对），但没发现这个缩进问题。
2. **`<task>` 标签机制不可行**（Run2 复现）：9B 语音模型对 XML 标签分布外，人设指令只实现了"先应一句"，后半永不出现。
3. **新发现：意图正则接不住模型的实际话术**。`TASK_HINT_RE`（jarvis_omni.py:53-56）要求"让我查/我来查/帮你查/查一下哈/稍等"；模型实际说"好的，我**为你查**一下。"——不匹配。即使修掉缩进 bug，意图路由也接不住 Run2 里模型真实说过的话。

**Root cause unknown 的部分**：跨会话退化的根因未查（未做长时实验）；回声对模型行为的实际影响未量化。

---

## 4. 故障树更新

| 层/项 | 原状态 | 审计后 |
|---|---|---|
| 二进制版本（含哪些 PR） | 不明 | **已确认**：v1.0.22（61d8393, 4/29）；不含 #47/#78；#101/#108 从未合入 |
| C++ 缺 listen 守门 | 中高置信推断 | **已确认**（官方 Python 原文 + 两份 C++ 源码 + 行为实验） |
| "1 拍=1s 墙钟→无法跨拍连续" | Core Problem 表述 | **已排除**（实测拍均 0.3–0.9s，8.56s 连续 burst 存在） |
| 客户端播放/缓冲策略 | 中置信候选 | **基本排除**（供应 RTF 0.3–0.9） |
| listen_prob_scale 是否有效 | 未测试 | **已验证有效**（0.6/0.2 均显著延长连续 burst；0.6 未在真机测过） |
| 模型不吐 `<task>` | 已确认 | 已确认（复现） |
| VAD/ASR 缩进 bug | **未知** | **新发现，已确认**（jarvis_omni.py:787-811） |
| TASK_HINT_RE 与实际话术不匹配 | **未知** | **新发现，已确认**（模型说"我为你查"） |
| update_session_config 清 KV | 未测试 | **已确认**（直调 kv→0）；wrapper 带 voice_audio 重灌，实际无恙 |
| 模型每拍 1s 音频 | 已确认 | 已确认（复现） |
| text 注入回灌 | 已确认 | 代码层复核（1638 行转发）；行为层未复测 |
| `_excise_echo`/真机回声 | 未测试 | 未测试（维持） |
| 跨会话退化 | 已确认存在，根因未知 | 未测；补充线索：Run4 显示会话内状态强烈影响开口 |
| force_listen_count=3 | 未提及 | **新发现**：开局前 3 拍强制 LISTEN（omni.cpp:9261-9285），解释"开头几拍永远听" |

---

## 5. 架构判断：修补 vs 重构

**修补，不重写。** 理由：

1. 双工方向本身被证明可行：官方 demo 同架构在跑；本机实测 decode 快于实时（拍均 0.3–0.9s）、声码器 RTF 0.16–0.30、8.56s 连续语音可产出。**"1 拍=1s 零余量"不是死穴**——余量实际是 2–3 倍。
2. 三层跨机 + SSH 隧道 + 目录轮询确实过度复杂，但**它现在不是主要矛盾**，传输延迟实测 23–98ms。重写（WebRTC 等）解决不了模型行为问题，先治根再谈架构。
3. 真正选错的是**机制**而非架构：靠 9B 语音模型输出 `<task>` 标签做工具调用，实测不可行。正确机制其实已经埋在代码里——Mac 本地 SenseVoice ASR + `TASK_RE` 关键词路由（单工路径就是这么工作的）——只是被缩进 bug 废掉了。修好它，任务主线不需要模型配合。
4. 唯一需要"大动"的是守门缺失（要新二进制或自编译），但它有低成本的运行时缓解（listen_prob_scale），可以排队。

---

## 6. 最小可验证修复方案（按序，一次一处）

1. **【1 行，零风险】修缩进 bug**：把 `jarvis_omni.py:794-811` 的 VAD 块整体 dedent 到与 `beats += 1`（781 行）同级。
   预期观测：用户说话后 `[你] <转写>` 立即打印（而不是几乎从不出现）；含"查一下/天气/几点"的话直接触发 `[jarvis-结果]`。
2. **【1 行】对齐意图正则**：`TASK_HINT_RE` 增加 `为你(查|找|搜)` 等分支，或放宽为 `(查|搜|看)一下`。
   预期观测：模型说"好的，我为你查一下。"后出现 `[意图] 模型说要查 → 派活: …`。
3. **【配置，已验证方向】真机测 listen_prob_scale=0.6**（现已生效，用户直接真机测即可；若仍碎片再试 0.3/0.2——改 run-wrapper.bat 后 `./run.sh --restart`）。
   预期观测：真机不再出现"一个字说一半被切断"；副作用是插话变难，靠现有 barge-in（flush+break）兜底。wav_timing.log 间隔应从 7–21s 的碎片化长尾变为成段连续块。
4. **【机制收敛】人设里删掉 `<task>` 标签指令**（消除分布外提示），任务分流完全依赖 1+2 修复后的客户端 ASR+规则。
   预期观测：`llm_text.txt` 无变化（本来就不吐标签），任务派发不受影响。
5. **【重，最后做】补齐守门**：优先找 master（8/27，含 #47/#78）的预编译 Windows 版；否则装工具链自编译（~10GB，需先写风险与回退方案）。
   预期观测：scale=1.0 下句中不再转听（Run3 场景复测单词不被劈开）。

## 7. 如果只能做一件事

**修缩进 bug（方案 1）。** 理由：它是原 Agent 完全没发现、却直接废掉"语音让 Claude Code 干活"这条主线的 bug；一行 dedent、零风险、可立即真机验证；且修复后任务主线不再依赖模型吐标签（机制上绕开了已证明不可行的 `<task>` 路线）。

---

*附：审计过程产物 —— 探针脚本 /tmp/audit_probe.py；官方参考实现原文 /tmp/mming_45.py；本地 checkout 特征比对基于 git fetch 的 v1.0.22/comni-1.0/comni-2.0/master 四个 refs。所有实验原始输出已在上文逐条引用。*
