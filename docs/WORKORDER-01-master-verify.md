# 工单 01 · master(64d092c) 验证 + listen 守门 A/B

> 发起：Mac 端（经 Owen 授权） · 2026-09-13
> 前置阅读：`E:\jarvis-voice\JARVIS-HANDOVER.md`（项目全貌与红线）

## 0. 一句话

把上游 master（8/27，含 v1.0.22 之后 4 个月的修复）编出来、跑通、做 A/B，用数据决定：**迁移 master 还是留在 v1.0.22；listen 守门要不要移植**。

## 1. 背景：手补 vs master 逐条核实结果（可直接采信，行号均对干净源码数过）

现役 = v1.0.22（`61d8393`，4/29）+ 3 处本地补丁（listen 守门 / 音频队列 / audio_poll 端点）。

| 项 | master 状态 | 证据（干净 master 源码） | 裁决 |
|---|---|---|---|
| length_penalty | ✅ 原生，且比我们的更全（覆盖非双工路径 tts_eos） | `omni.cpp:1353-1373`；`server.cpp:5659-5669`（仍支持**请求级覆盖**） | 用 master 的 |
| force_listen KV bug | ✅ 已修 | `omni.cpp:9841-9846` / `10792-10798`（accept+eval 写 KV） | — |
| 双工滑动 / TTS 状态跨块 | ✅ 已修 | 提交 `1745488`（`git merge-base --is-ancestor` 确认在 master 祖先链） | — |
| wav 文件名复用（丢音根因） | ✅ 已修：编号递增移进 T2W 线程 | 注释 `omni.cpp:6956` / `7560`；递增在 `8640` | audio_poll 大概率可退役 |
| listen 守门 | ❌ 无（grep「守门」= 0） | — | 见 T3 的 A/B |
| top_k 100→20 待办 | ⚠️ **上一版比对报告此条有误，勿划掉** | 见 §2 | 保留 |
| 人设注入通道 | ✅ 未变 | `server.cpp:5876-5886`（voice_clone_prompt / assistant_prompt 请求级覆盖）；`omni.cpp:4064` 双工默认硬编码仍在 | wrapper 不用改 |

## 2. ⚠️ 更正上一版比对报告的一处错误（top_k 行）

上一版写：「master 双工 TTS 采样改为纯 multinomial（omni.cpp:2926），top-k 已不参与该路径 → 待办划掉」。
**不成立**，逐行核对如下：

- `omni.cpp:2926` 位于 `sample_tts_token_simplex()`（2783 起）——这是**单工**函数，调用方是 `generate_audio_tokens_local_simplex()`（5260 起）。
- **双工主管道** `generate_audio_tokens_local()`（5645 起）在第 **5844** 行调用的是 `sample_tts_token()`（2985 起），其采样在 `3236-3240` 行用 `nucleus_sampling_with_min_keep_tts(top_p=0.85, top_k=25)`——**top-k 照常参与**。
- v1.0.22 结构完全相同（单工 → 多分采样；双工 → nucleus），master 在此处**没有任何改变**。
- 且原待办针对的是 **LLM** 的 `--top-k`（wrapper 传 100，对齐目标是官方双工 demo 的 20）；master 全库无 `top_k=20`，`server.cpp:333` 仍是参数透传 → **待办保留**（属 wrapper 侧小改）。

## 3. 任务

### T1 · 构建干净 master（不加任何本地补丁）

- 基准：工作树切到 `master-audioq`（= `64d092c`，与 GitHub master 最新一致）。
  - 当前工作树 = v1.0.22 + 补丁（已确认 `git diff` 与 `E:\jarvis-build\local-patches-v1.0.22-20260913.patch` **逐字节一致**）→ 该 patch 文件即完整备份，切换前留好。
- 配方照旧：VS2022 + CUDA 12.9 + `-DCMAKE_CUDA_ARCHITECTURES=120a-real -DLLAMA_CURL=OFF -DLLAMA_OPENSSL=OFF`。
- ⚠️ **target 名先确认**：`tools/server/CMakeLists.txt:1` 现在写的是 `set(TARGET llama-server)`，但 README 里提过 `llama-omni-server`——先跑 `cmake --build <build-dir> --target help` 看实际 target 列表。
- 产物放**独立目录** `E:\jarvis-voice\app\llama-omni-master\`（结构照 `llama-omni-new`：`build\bin\Release` + `tools\omni\assets` + `output_*`）。
- 跑前在 `bin\Release` 放齐 `cudart64_12.dll` / `cublas64_12.dll` / `cublasLt64_12.dll`，否则零输出猝死。

### T2 · 裸机冒烟（先不接 wrapper），四项逐条记录

1. **能否启动不秒退**——Mac 端 9/8 有「master 的 server 秒退」记录，成因未查明，**Windows 从未验证过，这是本工单最大的未知数**。
2. **端点清单**：`grep -n "svr->Post\|svr->Get" tools/server/server.cpp | grep stream`，确认 `/v1/stream/omni_init`、`/v1/stream/prefill`、`/v1/stream/decode`、`/v1/stream/break` 都在（`audio_poll` 没有是正常的，那是我们的发明）。
3. **wav 输出**：`round_*/tts_wav/wav_N.wav` 结构是否不变；**并实测编号修复**——打断一次后，新一段的 wav 编号应继续递增而不是归零复用（这是丢音事故的根因修复）。
4. **人设注入**：init 请求带 `voice_clone_prompt`（前缀格式 `<|im_start|>system\n{PERSONA}\n<|audio_start|>`），看 C++ 日志是否打出完整 Omen Alpha 人设（v1.0.22 注入口在 `server.cpp:5878-5883`，master 同位置，见 §1 表末行）。

### T3 · listen 守门 A/B（核心实验）

统一条件：`listen_prob_scale=1.0`（与现役一致）。

- **A 组：干净 master（不加守门）**。复现判据：让模型说含「Omen Alpha」的句子（如问它自己叫什么），看是否劈成 `…我是O` + 一拍静音 + `men Alpha。`。连测 **10 次**，记录劈裂次数。
- **B 组：master + 守门移植**：
  - 移植点①：`sample_with_hidden_and_token()`（1325 起）中 `common_sampler_sample` 之后——**master 第 1379 行**附近；
  - 移植点②：`stream_decode()` 的 token 类型分派处（**master 约 11022-11045**，现有「LISTEN: 不设置 current_turn_ended」那段的 else 支）——补 NORMAL/SPEAK → `current_turn_ended = false`；
  - 代码直接抄 `local-patches-v1.0.22-20260913.patch` 的第 1、第 3 个 hunk（注释可保留，便于日后对照）；重编后再测 10 次。
- **裁决规则**：
  - A 组劈裂 ≈ 0 → 滑窗修复已治根因，**守门不用移植**；
  - A 组仍劈、B 组归零 → **带守门迁移**；
  - 两组都劈 → 守门只解决一部分，记录新的复现样本再报。

### T4 · 交付

- 结果写 `E:\jarvis-voice\MASTER-VERIFY-RESULT.md`：T2 四项 + T3 的 A/B 原始记录（10 次结果、关键日志行）+ 结论（迁移与否 + 守门去留）。
- 每个结论附日志/行号证据；没验证的写「未验证」，**不写「应该没问题」**。

## 4. 红线

- 现役 `llama-omni-new\`、wrapper、计划任务：**一行不动**。
- 切换分支/打补丁前先确认已导出补丁（`local-patches-v1.0.22-20260913.patch`，已比对逐字节一致）。
- `.bak-audioq` 三个文件保留不动。
- `.ps1` 必须 ASCII-only；杀进程按命令行点名（见交接文档 §8）。
- 不碰 Mac 端文件；需要 Mac 侧配合（如 wrapper 改动）时写进结果文档，不要自己改。

## 5. 完成判据

T2 四项全部有记录 + T3 有 10 次×2 组对照数据 + 结论明确（迁移/不迁移 + 守门去留）落到结果文档。
