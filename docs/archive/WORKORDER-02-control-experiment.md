# 工单 02 · master 崩溃对照实验（**立即执行**）

> 发起：Mac 端（Owen 指示现在执行）· 2026-09-14 凌晨 01:15
> 前置：WORKORDER-01 结果（`MASTER-VERIFY-RESULT.md`）——其"master 死在 omni 初始化"的结论**已被推翻**，见下

## 0. 今晚已确认的事实（Mac 侧 dump 分析，别重复挖）

- **dump 里确实有异常**（上一轮"没检查到异常"是分析工具没读出来）：`0xC0000005` 访问违例、**读地址 0x63**、faulting RIP = `llama-omni-server.exe+0x15c1a`
- 崩溃指令 = `movups xmm0, [r14]`（r14=0x63）——**std::string 拷贝构造在读一个损坏的源 string**（源对象 {ptr=0x63, size=0, cap=垃圾}）
- 调用链（返回地址过滤验证过）：string拷贝 ← `0x14c0`（new(0x30) 节点 + 存 int + 拷 string = 容器插入）← `0x218d0`（操作 `[[r13]]` 的 +0x308/+0x3a0/+0x3c8,+0x3c9 字段）
- **当时在处理的请求 = `POST /v1/stream/decode`**（栈+堆都有该字符串）——**不是 omni_init**；栈帧里全是 HTTP 头字符串（Content-Type / Connection / close / Expect / X-Forwarded- / Transfer-En…）→ **崩溃在 HTTP 层（cpp-httplib）路径**
- 已排除：DLL 混版（部署 = 新构建产物，尺寸逐一吻合）、栈耗尽（editbin /STACK 64MB 无效）、CUDA/资产（配置 #2/#3 双排除）
- ⚠️ **由此推论**：WORKORDER-01 的四配置"崩溃点漂移"全靠日志尾行判断——**日志尾行 ≠ 崩溃点**，四个配置很可能其实都崩在同一处（decode 路径）

## 1. 本工单要回答的问题（悬而未决）

**"master 崩溃"目前无法排除是测试脚本 / 服务交互的问题（两版共有）。** 必须先做对照实验：

> 同一个 t3_ab.py，跑在 **v1.0.22** 上，崩不崩？

## T1 · 对照组（立即执行，~10 分钟）

### T1a：打现在活着的 JARVIS 后端（v1.0.22+补丁，端口 19060）

```
E:\tts-test\.venv\Scripts\python.exe -X utf8 E:\jarvis-voice\MASTER-VERIFY\t3_ab.py --port 19060 --group CONTROL-pat1022 --trials 10 --model-dir "E:\jarvis-voice\models\minicpmo45-gguf" --t2w-device gpu:0 --out-dir "E:\jarvis-voice\app\llama-omni-new\output_ctl"
```

⚠️ 注意事项：
- 这条会给**运行中的** C++ 后端发一次 `omni_init`，重置其会话（可接受——JARVIS 空闲中；wrapper 下次请求会自动 re-init）
- 若后端崩了：按老办法恢复 JARVIS（`schtasks /Run` → 验 `/health`），这**本身就是重要数据**（记录它死在哪个动作）

### T1b：（仅当 T1a 通过）用未打补丁的原版 v1.0.22

停 JARVIS → 起 `E:\jarvis-voice\app\llama.cpp-omni\build\bin\Release\llama-server.exe`（Comni 原版，2026-04-29 构建）在 **19070**（cwd 在 llama.cpp-omni 根、参数参照 run-wrapper.bat）→ 同脚本把 `--port 19070` 跑一遍（`--group CONTROL-comni1022`，out-dir 另给 `output_ctl2`）→ 停它、恢复 JARVIS。

**判定规则：**

| 结果 | 结论 | 动作 |
|---|---|---|
| T1a 或 T1b **崩** | **"master 坏了"推翻**——问题在脚本/服务交互，两版共有 | 立即停止后续，把崩溃记录报回（Mac 侧转向） |
| T1a + T1b **都稳** | master 回归坐实 | 继续 T2 |

## T2 · master 复现（仅 T1 两组都稳时做）

用 master 二进制 + 同脚本复现。**这次 dump 用 mini**：

```
E:\jarvis-build\procdump\procdump64.exe -accepteula -mm -e -x "E:\jarvis-voice\MASTER-VERIFY\dumps2" E:\jarvis-voice\app\llama-omni-master\build\bin\Release\llama-omni-server.exe --host 127.0.0.1 --port 19070 --model "E:\jarvis-voice\models\minicpmo45-gguf\MiniCPM-o-4_5-Q4_K_M.gguf" --ctx-size 8192 --n-gpu-layers 99 --temp 0.7 --top-k 100 --top-p 0.8 --min-p 0.0 --repeat-penalty 1.05 --repeat-last-n 512
```

然后同脚本 `--port 19070 --group MASTER-repro`。

**🔴 本次必须遵守：**
- **不要再用 `-ma`**（14GB 全量 dump 今晚已引发一次整机内存耗尽——dwm 崩溃、黑屏、远程重启才恢复）
- **分析只用流式脚本**（在 `E:\jarvis-voice\MASTER-VERIFY\`，内存占用 <50MB）：
  - `mdparse.py <dump>` — 异常流 + 模块表 + 上下文寄存器 + 栈初扫
  - `dumpp.py <dump> <addr> <len> ...` — 读任意内存（看 string 对象/结构体）
  - `scanmem.py <dump> <start> <end> <pattern> ...` — 找字符串（如 `v1/stream`、`model_dir`）
  - `stackwalk.py <dump> <exe> <rsp> <len>` — 返回地址过滤的栈回溯
  - `disasm2.py <exe> <crash_rva> [caller_rva...]` — capstone 反汇编 + 字符串指纹
- **对照签名**：`0xC0000005` / 读 `0x63` / `+0x15c1a` / 请求 `/v1/stream/decode`

## T3 · 交付

`E:\jarvis-voice\MASTER-VERIFY-RESULT-02.md`：
- T1a / T1b 结论 + 原始输出（t3 的 jsonl / 服务端日志尾部）
- （若走到 T2）新 dump 的异常流 + 栈 + 与对照签名的异同
- 每条结论附日志/行号证据；没验证的写"未验证"

## 红线

- 现役 `llama-omni-new` **文件**一行不改（T1a 只是发请求，不是改文件）
- 实验后恢复 JARVIS 并验 `/health`（healthy + duplex）
- **新红线（今晚事故换来的）**：任何进程 commit 超 ~20GB 立即介入；跑大内存任务前先 `Get-Process | Sort PrivateMemorySize64 -Descending | Select -First 5`
- `.ps1` 必须 ASCII-only；杀进程按命令行点名
- 不碰 Mac 端文件；需要 Mac 配合写在结果文档里

## 附：Mac 侧并行在做的事

`server.cpp` HTTP 层 diff（v1.0.22 `61d8393` vs master `64d092c`），重点嫌疑 `Feat/omni ws rebase (#63)` 那批提交。有结论会同步。
