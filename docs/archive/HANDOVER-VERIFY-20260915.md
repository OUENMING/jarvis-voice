# 交接校验报告（2026-09-15 23:08 GMT+1）

> **校验者**：WorkBuddy（Mac 侧），首次接手本项目
> **对象**：`docs/JARVIS-HANDOVER-20260915.md`（22:30 版）+ Obsidian 主笔记 + 记忆三件套
> **方法**：**只读复核** —— `ssh win` 查进程 / 端口 / 防火墙 / 健康检查 / 服务日志 + Mac 直连复测。
> **未改动任何进程、未修改任何既有文件**（本文是新增文件）。
> **引用规矩**：结论不写来源就当没写。本文每条都标了实测证据；**推断一律标 ⚠️**。

---

## 1. 交付物完整性：✅ 三项齐备、链接闭环

| 交付物 | 状态 | 复核方式 |
|---|---|---|
| `docs/JARVIS-HANDOVER-20260915.md` | ✅ 471 行 / 10 节 + 未验证清单，覆盖目的需求、现场状态、架构、决策史、文件地图、问题清单、踩坑红线、参数运维、待办 | 通读 |
| 记忆新增两条 + 索引 | ✅ `jarvis-voice-project.md`（2333 B）、`jarvis-voice-working-rules.md`（1545 B），frontmatter 完整、`originSessionId` 一致；`MEMORY.md` 索引两条齐全；交叉链接双向（`[[model-selection-5060ti]]` ↔ `[[jarvis-voice-project]]` ↔ `[[jarvis-voice-working-rules]]`） | 通读 |
| Obsidian 主笔记 + 工程日志 | ✅ 主笔记「当前状态」已按 22:30 实测重写（Plan B 上线 / 双栈抢端口 / 8006·22400 暴露 / 英文跑偏）；工程日志 1142 行处有 Plan B 补记，**且带来源声明**（"22:40 补记，依磁盘证据重建，非当时记录，未经当事人复核"） | 通读 |
| 旧版交接指向新版 | ✅ `docs/JARVIS-HANDOVER.md` 第 3 行有「本文已部分过时」横幅并链到新版；新版也回指旧版。**双向闭环，无孤儿文档** | 核查 |

**§6.4「persona.txt 有语病」—— 属实，已亲眼核实。** 原文（`~/jarvis-voice/persona.txt`，单行）：

> …不要自言自语、不要自问自答、不要替主人说话。**播报请把下面这段话念给主人听如果收到来自工具系统的消息**（消息里会明确说明是工具系统发来的），按消息里说的做；…

「播报请把下面这段话念给主人听」与后半句之间确实缺标点、语义断裂，是编辑时旧「播报模板」的残留。✅ 该条可执行。

---

## 2. 🚨 现场已漂移：19060 的抢占"收敛"了，但 doc 漏掉了一个**循环**

doc 是 22:30 快照。**35 分钟后的 23:08 实测，情况已经不同。**

### 2.1 实测对照（23:08–23:09 GMT+1，Windows 本机）

| 项 | doc（22:30）说法 | 实测（23:08） | 判定 |
|---|---|---|---|
| 19060 占用者 | Plan A 空壳 PID 11176（179 MB，无模型） | **Plan B 的 14632**（`E:\jarvis-build\llama-demo\` 构建，宿主 5925 MB） | 🔄 **已变**——正确的那一个赢了 |
| Plan A 引擎 | 11176 占着 19060 | **不占 19060**；PID 35 分钟内 **11176 → 7660 → 5100** 换了两轮，工作集恒为 **179 MB** | 🔄 **已变** |
| GPU | 11,402 / 16,311 MiB | **11,672 / 16,311 MiB**，util 1% | ✅ 一致（模型确实在 GPU 上） |
| 栈 A wrapper | 11312 → 24476 @ `127.0.0.1:9060` | ✅ 在，`/health` 返回 `"duplex_mode":true,"restarting":false` | ✅ 一致 |
| 栈 B | 2020→9156 @ `0.0.0.0:8006`；21992→14140 @ `0.0.0.0:22400`，worker idle / model_loaded true / kv 21 | ✅ 完全一致（`kv_cache_length: 21`） | ✅ 一致 |
| 防火墙 Block | 只有 19060 一条 | ✅ 只有 `JARVIS raw llama-server API (19060)` 一条 | ✅ 仍一致（即**未修**） |

→ **doc 的「立即第 1 项」可以改写**：不再是"两个引擎抢 19060、占位的是空壳"，
而是 **"Plan A 栈已整体幽灵化：wrapper 自认健康，但它永远拿不到引擎端口"**。19060 目前由正确的一方持有。

### 2.2 新证据：Plan A wrapper 正在**约 1 次/分钟**地重启它的引擎

日志 `E:\jarvis-voice\MASTER-VERIFY\migrate-live.log`（81,883 B / 1105 行，实测 **23:09:53 仍在写**）：

| 标记 | 次数 |
|---|---|
| `停止 C++ llama-server` | **53** |
| `闲置 60s`（看门狗） | **53** |
| `full reinit` | **106** |

**53 个循环 / 约 53 分钟 ≈ 1 次每分钟。** 每个循环做的事（日志原文顺序，UTF-8 读出的中文有乱码，语义可辨）：

```
[看门狗] 闲置 60s → 重置会话
[会话重置] 完成（清空 KV、重建引擎）
停止 C++ llama-server...
[看门狗] 重置完成: 200 {"success":true,"mode":"restart",...}
[输出清理] 已删除 output 目录: ...\llama-omni-master\tools/omni/output_9060
启动 C++ llama-server: ... --host 127.0.0.1 --port 19060 ... --ctx-size 8192 ...
C++ llama-server 启动成功 (等待 1 秒)
[人设注入] 已创建 voice_clone_prompt
[CPP] Omni HTTP server starting...
```

→ 每轮都拉起一个 `--port 19060` 的新引擎，**但绑不上**（Plan B 的 14632 占着）→ **工作集恒 179 MB，模型从未加载**。

**结论修正**：doc §2.1 把"179 MB 空壳"记成 22:32:42 的**偶发事故**（"副作用是把在跑的引擎换掉了"）。
**实测它是 Plan A 重启循环的稳态产物**——不是一次意外，是每分钟重复一次。doc 的定性需要改。

### 2.3 ⚠️ 由此新增的风险：GPU OOM（doc 未列）

Plan A 每轮都尝试绑 19060 **并加载 `MiniCPM-o-4_5-Q4_K_M.gguf`（~11.5 GB）**。
当前 GPU **已用 11,672 / 16,311 MiB**（Plan B 的引擎）。

> **一旦某个循环正好抢到 19060**（例如 Plan B 引擎重启、或 worker 释放的空档），它会再要 11.5 GB → **GPU OOM，可能连带把 Plan B 正在服务的引擎搞挂。**

这比"两个引擎抢端口"更实际——**每分钟一次的、带有 OOM 概率的掷骰**。
→ 处置方向也因此明确：**要停的是 Plan A 那一环（`mig_run.bat` 链条），不是 Plan B。**
（这与 doc「二选一」的结论方向一致，但优先级依据变了：不是"腾出端口"，而是"解除每分钟一次的 OOM 掷骰"。）

---

## 3. 答上了 doc「未验证清单」的条目

### ✅ 第 1 条（§2.1）："是谁在 22:32:42 拉起了第二个 wrapper 实例"

**答案：整组栈是 22:16:19–22:16:20 由一次手动操作拉起的。**

实测进程血缘（同一个父进程 **PID 19560**，2216:20.619–22:16:20.706 即 **90 ms 内** spawn 了三个 cmd）：

| 时间 | PID | 父 | 命令行 |
|---|---|---|---|
| 22:16:19 | **19560** | 1480 | **`WindowsTerminal.exe`** ← 根 |
| 22:16:20.619 | 23564 | 19560 | `cmd.exe /c "E:\jarvis-voice\MASTER-VERIFY\mig_run.bat"` |
| 22:16:20.632 | 5784 | 19560 | `cmd.exe /c "E:\jarvis-build\run-demo-gateway.bat"` |
| 22:16:20.659 | 17004 | 19560 | `cmd.exe /c "E:\jarvis-build\run-demo-worker.bat"` |
| 22:16:20.646 | 11312 | 23564 | `python.exe -u minicpmo_cpp_http_server.py ... --port 9060 --host 127.0.0.1 --duplex` |
| 22:16:22.610 | 14632 | 14140 | `llama-server.exe --host 0.0.0.0 --port 19060 ...`（llama-demo，持 GPU） |

**两个能落地的结论：**

1. **当前服务 :9060 的 wrapper，不是产线任务，是"master 迁移验证"的启动。**
   父进程是 `cmd.exe /c E:\jarvis-voice\MASTER-VERIFY\mig_run.bat`；该 .bat 内容实测 = 启动 wrapper，
   日志重定向到 `MASTER-VERIFY\migrate-live.log`。
   → **所以它不是 `JarvisOmniServer` 任务起的**（四个任务 `JarvisOmniServer` / `JarvisMigrateTest` / `DemoGateway` / `DemoWorker` 实测 state 全是 `Ready`，即当前都不是"由计划任务正在运行"）。
   ⚠️ 推断（未坐实）：22:32:42 那个"第二个 wrapper 实例"来自同一条迁移验证链路的**重跑**——因为整组栈的根是同一个 WindowsTerminal，且该 wrapper 现在正以 1 次/分钟自我重启（§2.2），天然会反复出现"新实例启动 → 绑 9060 失败"的现象。**doc 里的"cmd 父进程已退出、查不到"，实测口径下应更新为"根是一次手动 WindowsTerminal 操作"。**

2. **"两套栈的顶层进程全部在 22:16:20 同时启动"有了确切解释**：一个人（或一个 agent）在 22:16:19 开了一个 Windows Terminal，一口气把三个 .bat 都拉起来了。不是计划任务、不是自愈机制。

### ✅ 第 2 条（§2.4 / 附）："8006/22400 至今未被封"

**23:08 复测：仍然可达。** 见 §4。**该条从"未验证"可以改为"已验证、仍未修"。**

### ⏳ 未动的条目（本轮未查，保持 doc 的 ⚠️）

- §2.1 两个引擎同时绑 19060 时 Windows 的实际分配规则（未构造复现）
- §3.2 切 Plan B 的完整决策理由
- §6.1 英文跑偏是否与参考音色格式有关
- §6.3 进程内重置哑掉音频的根因
- §6.6 Plan B 上是否也存在 master 的 text 通路缺失

---

## 4. 安全项复测（23:08 GMT+1，从 Mac 直连，不经隧道）

| 目标 | 结果 | 判定 |
|---|---|---|
| `http://10.20.70.180:8006/health` | **HTTP 200** `{"status":"healthy",...}` | 🔴 **仍暴露** |
| `http://10.20.70.180:22400/health` | **HTTP 200** `{"status":"healthy","worker_status":"idle","model_loaded":true,...,"kv_cache_length":21}` | 🔴 **仍暴露**（且回完整 worker 状态） |
| `http://10.20.70.180:19060/health` | 连不上 | ✅ 防火墙 Block 生效 |
| `http://10.20.70.180:9060/health` | 连不上 | ✅ 本就只听 loopback |

→ 与 doc §2.4 **完全一致**：**没有任何东西挡住 8006 / 22400**（22:31 与 23:08 两次复测都可达）。
doc 给的两条 `New-NetFirewallRule` 建议仍待执行。

---

## 5. 发现的文档缺陷（**未改，等 Owen 拍板**）

### 🟡 5.1 Obsidian 主笔记「架构（当前实跑）」还是 Plan A 单栈图，且**端口标错**

主笔记第 44 行：

```
[Windows] minicpmo_cpp_http_server.py (:19060，绑 loopback)
```

**错。** 实测：wrapper 听 **127.0.0.1:9060**；**19060 是它拉起的 C++ `llama-server` 的端口**。
同一张图把两层的端口压成了一层。而现在实际跑的是**两套栈**（Plan B 才是前台），
图却只描述 Plan A —— 与它上面「当前状态」段落自相矛盾。
→ 建议按 `JARVIS-HANDOVER-20260915.md` §3.1 的两栈图替换。
（这正是项目规矩第 3 条要防的那类错：端口/行号不核实就转抄。）

### 🟡 5.2 工程日志的日期口径与顺序错位

`实时视觉语音机器人-工程日志.md` 的 `## ` 标题实测序列（尾部）：

```
998   ## 2026-09-15 深夜 · ✅ 五项修复落地…
1027  ## 2026-09-16 凌晨 · 🔬 根因报告…
1053  ## 2026-09-16 · 🎯 彻底方案定案…
1069  ## 2026-09-16 · ✅ 方案 v4 落地…
1093  ## 2026-09-16 · 🔴 进程内快重置的严重副作用…
1121  ## 2026-09-16 · 🛑 反自言自语护栏…
1142  ## 2026-09-15 凌晨 · 🧭 补记：切 Plan B…
```

两个问题：

1. **口径不一致**：现在 Mac 本地是 **2026-09-15 23:08 GMT+1**，而"2026-09-16"的条目已经存在
   → 那批标题用的是**北京时间**（GMT+1 23:08 = 北京 09-16 06:08）。而另一些条目（如"09-15 凌晨"）看起来是本地口径。**混用。**
2. **顺序错位**：`2026-09-15 凌晨` 的 Plan B 补记排在所有 `2026-09-16` 条目**之后**。append-only 导致的，但它描述的窗口（09-15 02:19–03:27）在时间线上最靠前。**照标题排序会读错因果。**

→ 建议：日志顶部加一行口径声明（"本日志日期为北京时间"或"本地时间"），补记条目加"（补记，条目位置=写入时间，非事件时间）"。

### 🟢 5.3 doc §2.1 的现场快照已过期（35 分钟）

不是"错"，是**快照类内容的固有半衰期**。建议在 §2 标题里加"（快照，会过期，引用前先 `ssh win` 复核）"。
本次 §2 的实测结果可直接作为新版快照。

---

## 6. 复核命令（可重放）

```bash
# 从 Mac：安全项直连复测
for p in 8006 22400 19060 9060; do curl -s -m 6 -o /dev/null -w "$p => %{http_code}\n" http://10.20.70.180:$p/health; done

# 从 Mac：Windows 现场快照（PowerShell 经 -EncodedCommand 传，避开引号/编码坑）
ssh win "powershell -NoProfile -EncodedCommand <base64(UTF-16LE)>"
#   脚本体见本报告 §2.1/§3 所依据的三段查询：
#   ① Win32_Process 过滤 Name/CommandLine + CreationDate + WorkingSetSize
#   ② Get-NetTCPConnection -State Listen 过滤 9060,19060,8006,22400
#   ③ Get-NetFirewallRule -Direction Inbound -Action Block -Enabled True
#   ④ Invoke-RestMethod 三个 /health

# 重启循环证据
ssh win "powershell -NoProfile -EncodedCommand ..."   # Get-Content migrate-live.log + Select-String 计数
```

⚠️ 踩过的坑（与 doc §8.2 一致，本轮再验证一次）：
- PowerShell 输出中文必乱码（`-EncodedCommand` 传进去没事，**回显**是 GBK）→ 判读时只看 ASCII 部分与语义锚点。
- 读取 `$_.CommandLine.Substring()` 对**没有 CommandLine 的进程**（如部分系统进程）会抛 `ArgumentOutOfRangeException`，要先判空/判长。
- `$_.ProcessId -in 1,2,3` 对**已退出 PID** 静默不返回 → 反过来说明"探测间隔内进程没了"（本轮正是靠这个发现 7660 已被替换为 5100）。

---

## 7. 建议的立即动作（**未执行，等 Owen 拍板**）

| # | 动作 | 理由 | 风险 |
|---|---|---|---|
| 1 | **停掉 Plan A 那条链**：`cmd.exe /c MASTER-VERIFY\mig_run.bat`（PID 23564）→ 11312 → 24476 → 当前 llama-server | 每 60 s 一次的带 OOM 概率的掷骰（§2.3）；且它服务的就是"迁移验证"，不是产线 | 停了 :9060 就没了 Plan A 回退；**Owen 需确认当前是否还要 Plan A** |
| 2 | 堵防火墙 8006 / 22400 | §4 两次复测仍暴露；校园网内任何人可开双工会话、驱动机器人 | 低（隧道走 loopback，不受影响，19060 已是此模式） |
| 3 | 修 `persona.txt` 语病 | §1 已核实属实；人设 621 token 直接影响模型行为 | 低；改前按惯例 `.bak-<主题>` |
| 4 | 更新 doc §2.1 + 主笔记架构图 + 日志日期口径 | §2 / §5 | 低（纯文档） |

**停 Plan A 的正确做法**（沿用 doc §8.2 的规矩，**绝不 `taskkill /IM python.exe`**）：

```powershell
Get-CimInstance Win32_Process | Where-Object {
  $_.CommandLine -like '*minicpmo_cpp_http_server*' -or
  $_.Name -eq 'llama-server.exe'
} | ForEach-Object { "would kill PID $($_.ProcessId) :: $($_.Name)" }   # 先 dry-run 打印
```

> ⚠️ 上面故意只打印不杀。**确认清单无误、且 Owen 同意放弃 Plan A 之后**再执行 `Stop-Process`。
> 注意这条会**同时命中 Plan B 的 14632**（它也是 `llama-server.exe`）——必须先按命令行区分两套栈，别把有模型的那个杀了。

---

*本报告为新增文件，未修改任何既有交付物。*
