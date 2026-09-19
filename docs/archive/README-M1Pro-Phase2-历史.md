# JARVIS Voice — M1 Pro Phase 2 原型

MiniCPM-o 当「耳朵+嘴巴」+ Claude Code 当「干活的手」+ state.json 上下文同步。

## 架构

```
麦克风 ──> MiniCPM-o server(:9060) 听+情绪+闲聊+说（流式 wav+text）
   │
   ├─ 并行: SenseVoice(CPU ~150ms) 转写你的原话
   │        ├─ 命中 <task> 标签 / 关键词 / 追问接力 → Claude Code 干活
   │        │    ├─ filler「好的我看一下」先播
   │        │    ├─ 结果写 ~/.jarvis/state.json
   │        │    └─ say 播报结果摘要
   │        └─ 闲聊 → 播 MiniCPM-o 原声（整段缓冲+1.15x 拉伸）
   └─ MiniCPM-o 挂了 → 自动降级 Phase 1(SenseVoice+say)
```

## 文件

| 文件 | 作用 |
|---|---|
| `jarvis_omni.py` | Phase 2 主循环（入口）。`omni_stream` 事件流 + `StreamPlayer` 播放器 |
| `claude_bridge.py` | 常驻 claude stream-json 桥（warm ~2.5s，进程死了自动重启） |
| `jarvis_state.py` | ~/.jarvis/state.json 任务状态+用户事实 |
| `jarvis_demo.py` | Phase 1 最小闭环（SenseVoice→claude -p→say） |
| `minicpmo_cpp_http_server.py` | MiniCPM-o FastAPI 封装（:9060，来自 MiniCPM-V-CookBook，**已修 3 个 bug**） |
| `omni-server.sh` | server 启停脚本（start/stop/status） |
| `llama.cpp-omni/` | 编译产物 `build/bin/llama-server`（**feat/web-demo 分支**） |
| `models/minicpmo45-gguf/` | Q4_K_M + audio/tts/token2wav（~7.3GB，无 vision） |

## 启动

```bash
cd ~/jarvis-voice
./omni-server.sh start          # 模型加载 2-3 分钟
./.venv/bin/python jarvis_omni.py
```

## 实测数据（2026-09-09，M1 Pro 16GB）

- MiniCPM-o 单轮：**15-28s**（CPU TTS 必需——GPU TTS 在内存 swap 压力下输出 NaN）
- ASR：~1.2s（SenseVoice CPU，7s 音频 147ms）
- Claude 常驻桥：warm ~2.5s，$0.001-0.003/轮
- **瓶颈在生成端**（RTF 2-4x），播放端已优化到位

## 已修的坑（踩过的）

1. **llama.cpp-omni 必须用 `feat/web-demo` 分支**（master 的 server 秒退）
2. **stream-json 双向模式 init 在首条消息后才返回**（先发再读）
3. **prefill 要完整 WAV 字节**（裸 PCM 报 400）
4. **SSE 里的 wav 是 int16 PCM 字节**，不是文档说的 float32（错解→假 NaN/电流音）
5. **wav 编号错位**：C++ 端 `wav_turn_base` 每轮 +1000（wav_0→wav_1000→wav_2000），wrapper 按局部编号找 → 3 轮后必挂（已修：编号无关认领 + 兜底发送）
6. **CPU TTS 写盘慢于 wrapper 超时窗口**（10s→60s）
7. **RTF>1 时「边收边播」必然断续** → 默认整段缓冲播放；`prebuffer_sec` 留给快速硬件
8. **人设在 C++ 里硬编码**（`omni.cpp` audio_assistant_prompt 两处），改后须重编译

## 下一步

- [ ] **分支合并 master**（PR#37 Metal 优化，首响 -74%，M1 Pro 唯一提速大头）
- [ ] Gemini Live 免费档实测（若能顶替快路径，直接绕过 M1 Pro 瓶颈）
- [ ] 音色统一（filler/结果回流走 MiniCPM-o 原声）
- [ ] 打断：/omni/break 端点 + VAD
- [ ] iPad Voice Satellite 前端
- [ ] 5060 Ti 到位后：同套代码换 CUDA 版 llama-server + 开流式播放
