"""集中配置。所有路径与阈值一处定义，env 可覆盖。"""
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"

# 用户数据目录（与 fish.env / session.json 同处）。放这里的都是**个人数据，不入项目**。
JARVIS_HOME = Path(os.environ.get("JARVIS_HOME", str(Path.home() / ".jarvis")))

SR = 16000  # 全链路统一采样率（SenseVoice / Silero / 多数云 TTS 都吃 16k）


def _env_str(k: str, d: str) -> str:
    return os.environ.get(k, d)


def _env_int(k: str, d: int) -> int:
    try:
        return int(os.environ[k])
    except (KeyError, ValueError):
        return d


def _env_float(k: str, d: float) -> float:
    try:
        return float(os.environ[k])
    except (KeyError, ValueError):
        return d


@dataclass(frozen=True)
class Config:
    # ---- 音频 ----
    sample_rate: int = SR
    mic_blocksize: int = 1600        # 100ms @16k，sounddevice 回调粒度
    mic_queue_max: int = 200         # 有界队列，防积压（丢最老）
    # 设备选择（空 = 系统默认）。可填索引或名字子串，如 "MacBook"。
    # ⚠️ 真免提**必须把输出也切到内置扬声器**，否则麦克风听不到自己的声音，等于还是耳机。
    input_device: str = ""
    output_device: str = ""

    # ---- VAD ----
    vad_backend: str = "silero"      # silero | ten-vad
    vad_model: str = ""
    vad_window: int = 512            # silero 512(32ms) / ten-vad 256(16ms)
    vad_threshold: float = 0.6       # 0.5→0.6：瞬态噪声更容易越过低阈值（实测）
    vad_min_silence: float = 0.5     # 判定"说完了"所需的静音时长
    # ⚠️ **它同时是打断延迟的主要来源**：`is_speech_detected()` 要等这么多秒的
    # 连续语音才翻 True，而打断就是靠这个翻转发动的。
    # 实测（`tests/test_bargein_latency.py`，真人语音、静音 2s 后开口）：
    #     0.25s → **+400ms**   0.10s → +300ms   0.05s → +200ms
    #
    # 🔴 **2026-09-20 深夜试过压到 0.05（省 200ms），真机立刻出回归、已回退：**
    #    「每次开播都被自己的声音打断」—— 因为 **50ms 的门槛挡不住"播放起音那一下
    #      泄漏的残留回声尾巴"**（AEC 才刚开始适应新 far）。0.25s 的长度刚好能滤掉它。
    #    ⚠️ 我当时的安全论证漏了这条：测试只覆盖了「短噪声爆发」（Silero 确实拦得住），
    #      **没覆盖「持续的回声尾巴」**。别再只凭那个测试就给这里放行。
    # ⇒ **要再快，必须另配「播放起音冷却窗」**（Gemini `prefixPaddingMs` /
    #    LiveKit `backchannel_boundary` 同款），而不是继续压这个值。
    # ⚠️ 别调高：0.3s 的「停」也会被丢（旧注释的警告仍然成立）。
    vad_min_speech: float = 0.25
    vad_max_speech: float = 20.0
    # 段级信噪比门限 —— 对抗"没出声自己说话"的**主防线**。
    # 实测：SenseVoice 对任何非语音都幻觉出文本（静音→'그。'、白噪声→'Yeah.'），
    # 文本层无法过滤，只能在音频层拦。
    vad_min_snr: float = 3.0         # 段 RMS 至少是噪声底的 3 倍（≈9.5dB）
    vad_min_rms: float = 0.012       # 绝对下限，防噪声底被估得过低
    # ---- 预滚缓冲（治「首字被 ASR 判错」）----
    # 现象（真机）：短命令的首字被毁 ——「清空上下文」→「轻松/星空/供」。
    # 离线复现（zh.wav，完全可重复）：只喂 VAD 段 →「**派放**时间早上9点至下午5点。」
    #                                完整语音    →「**开饭**时间早上9点至下午5点。」
    #
    # ⚠️ **机制与最初的假设不同，以实测为准**：段本身**没有被切头** ——
    # 能量剖面显示段首就是语音（每100ms RMS [8092, 7158, …]）。缺的是
    # **ASR 的前导上下文**：SenseVoice 从半路开始听就会把首字判错。
    # 拼上预滚后剖面变成 [0, 104, …, 13, 10, 778, 8092] —— 多出 ~800ms 前导静音，
    # 转写随即恢复成完整的「开饭」。
    #
    # 取值依据（离线扫，ASR 已验证确定性 3/3 相同）：
    #   600/700ms → 「开放」（只救回一半）    800/900/1000ms → 「开饭」✅
    # 取 900ms 落在有效带中间。
    vad_pre_roll_ms: int = 900

    # ---- ASR ----
    asr_provider: str = "sensevoice"  # sensevoice（本地免费）| fish（云，$0.36/时，需 API credit）
    asr_model_dir: str = ""
    asr_num_threads: int = 4
    asr_min_utt_sec: float = 0.4     # 短于此时长的语音段不送 ASR
    # 专名纠正表：(听错, 正确)。ASR 之后做确定性替换。
    # 为什么不建设 `rule_fsts`（SenseVoice 其实支持）：那要编译 FST 文件，
    # 对**几个词**来说太重；纯 dict 更简单、可单测、随手能加。
    # 实测错法（2026-09-16 日志）：杜柏林×3 / 都布林 / 欧men。
    asr_fixes: tuple[tuple[str, str], ...] = (
        ("杜柏林", "都柏林"),
        ("都布林", "都柏林"),
        ("艾瑞斯", "Aries"),
        ("阿瑞斯", "Aries"),
        ("阿里斯", "Aries"),
        ("埃里斯", "Aries"),
        ("悠文", "欧文"),
    )

    # ---- 切句（⚠️ 目前**未被使用**：真正生效的是 claude_bridge.py 里的
    #      FIRST_SENT_MIN / NEXT_SENT_MIN 常量，且没有 hard_max 强切。
    #      改这几个值不会有任何效果 —— 要么透传进 bridge，要么删掉。审计发现）----
    first_sent_min: int = 2          # 首句 ≥2 字符即发（治"好的。"被卡）
    next_sent_min: int = 4
    hard_max_chars: int = 24         # 超过则强制在最近标点/空格处切，压首音延迟

    # ---- 安全护栏 ----
    # ⚠️ 这是**防手滑的护栏，不是沙箱**。它拦不住 `bash -c "rm -rf /"`、
    # `find . -delete`、`python -c "shutil.rmtree(...)"` 这类绕道。
    # 目标只是拦住"老实人会犯的灾难性错误"—— 尤其因为**语音转写可能被噪声污染**。
    # 实测（2026-09-16）：`--disallowedTools "Bash(sudo *)"` 确实会拒绝执行，
    # 即便同时带着 --dangerously-skip-permissions。
    bash_deny: tuple[str, ...] = (
        # 递归/强制删除的**宽泛目标**（具体的 `rm -rf ./build` 仍放行）
        "Bash(rm -rf /*)", "Bash(rm -rf ~*)", "Bash(rm -rf *)", "Bash(rm -fr *)",
        # 系统级
        "Bash(sudo *)", "Bash(shutdown*)", "Bash(reboot*)", "Bash(mkfs*)",
        "Bash(dd *)", "Bash(chmod -R 777 *)", "Bash(chown -R *)",
        # git 历史破坏
        "Bash(git push --force*)", "Bash(git push -f *)",
        "Bash(git reset --hard*)", "Bash(git clean -fd*)",
        # 管道执行远端脚本（含无空格拼写 —— 实测匹配语义对空格敏感）
        "Bash(curl * | sh*)", "Bash(curl *| sh*)", "Bash(curl *|sh*)",
        "Bash(curl * | bash*)", "Bash(curl *| bash*)", "Bash(curl *|bash*)",
        "Bash(wget * | sh*)", "Bash(wget *| sh*)", "Bash(wget *|sh*)",
        "Bash(wget * | bash*)", "Bash(wget *| bash*)", "Bash(wget *|bash*)",
        # 通用"管道给 shell"兜底
        "Bash(*| sh)", "Bash(*|sh)", "Bash(*| bash)", "Bash(*|bash)",
    )

    # ---- 音频模式 ----
    # headphones: 戴耳机。麦克风收不到耳机里的声音 → 无回声 → 开启**打断**(barge-in)。
    # speaker:    免提（内置麦 + 扬声器）。麦克风会听到**音箱里的自己**，而我们不做 AEC，
    #             所以必须两件事一起关：① 关自动打断 ② 播放期间不听（**半双工**）。
    #             只关打断是不够的——自己的声音仍会被转写并送去 CC。
    # speaker_aec: 免提 + 软件 AEC。far 参考来自 Player 的镜像（我们**确切知道**在播什么），
    #             WebRTC AEC3 消掉它 → 可以**免提 + 打断**。实测本机稳态 ERLE 34–36 dB
    #             （docs/PROBE-AEC-RESULTS-20260919.md）。
    audio_mode: str = "headphones"

    # ---- AEC（仅 audio_mode == "speaker_aec" 生效）----
    # ⚠️ 这是「far 参考要往前读多少」，**不是**给 AEC3 的 `stream_delay_ms` 提示。
    # 麦克风里的回声来自 ~150ms 前播出的音频 → 参考必须往前读这么久才算对齐。
    # （`AecGate` 里给 `AudioProcessor` 的 `stream_delay_ms` 恒为 **0**：
    #   对齐之后 AEC3 面对的残余延迟 ≈0，让它自估。两边都给 150 = **补偿两次**。）
    #
    # 离线实测（真实回声延迟 150ms，`tmp/verify_delay.py`）：
    #   far 偏移 −150 + stream_delay 150 → **1.04 dB** ❌ ← 曾经的配置
    #   far 偏移 −150 + stream_delay   0 → **39.09 dB** ✅
    #   far 偏移    0（读未来，镜像是空的）→ 生产不可达
    # OCR 代码审查在 `aec.py:66` 独立指出「延迟被补偿了两次」。
    aec_stream_delay_ms: int = 150

    # ---- AEC 后端（可切；仅 audio_mode == "speaker_aec" 生效）----
    #   "webrtc" = pywebrtc-audio AEC3：自适应滤波 **＋ NLP 残余抑制**
    #   "speex"  = pyaec/SpeexDSP：**纯线性**分块频域滤波，**不做残余抑制**
    #
    # 🟢 **保持 "webrtc"。** 2026-09-20 真机实测（`tools/measure_echo_delay.py`：
    # 通过生产同一个 Player 放啁啾、同时录，用户不出声 ⇒ 麦克风里只有回声 ⇒
    # 残差能量是**无歧义**的判据）：
    #      WebRTC AEC3  消掉 **43.3 dB** ✅
    #      Speex 线性   消掉 **−0.5 dB**（等于没消）
    # ⚠️ 曾经基于**合成**场景（纯延迟+增益回声）得到「Speex 8.0% vs WebRTC 39.8%」
    #    的转写错误率并据此推荐 Speex —— **那条推荐已作废**：真实房间混响下
    #    Speex 一分贝都消不掉（它的 8kHz 采样率固有缺陷扛不住）。
    #    **教训：合成基准可以在真实房间上完全翻转，后端选型必须过真机回声。**
    #
    # 开关留着不是为了切默认值，是为了**以后做对照实验**：
    # `JARVIS_AEC_BACKEND=speex ./jarvis.sh start --speaker-aec`
    aec_backend: str = "webrtc"
    # Speex 的回声尾长（样本 @16k）。3200 = 200ms：够覆盖 150ms 物理延迟 + 房间混响。
    # ⚠️ 实测不是越长越好：6400 时转写错误率 11.0%（3200 是 8.0%）—— 抽头多、收敛慢，
    #    而我们每次打断只有几秒音频。
    aec_speex_filter_length: int = 3200

    # 注：`RESEARCH-AEC-20260919.md` §2.3 的三层防线（播放期间抬高 SNR 门限 /
    # speech_probability 双确认）**尚未实现** —— 那两个阈值没有实测数据可依据，
    # 先不设死配置（避免重犯 §7.5「死配置」的坑）。当前靠已有的段级 SNR 门限
    # + `asr_min_utt_sec` + `echoguard` 三层兜底；`orchestrator` 已在播放期间
    # 把 `speech_probability` 报进 `level` 事件，攒够数据再定阈值。

    # ---- 填充音 / 承接句 ----
    # 两条触发路径（见 docs/WORKORDER-LEADIN-01.md）：
    #   ① 兜底：本轮开始后 `filler_delay_ms` 还没出首句 → 播**通用池**
    #   ② 首个 tool 事件且本轮还没出声 → 隔 `filler_tool_delay_ms` 播**按工具类别选的那句**
    filler_enabled: bool = True
    # 900 → 2000（2026-09-20）：照抄 Azure Voice Live `interim_response` 的 `latency`
    # 触发默认值。**副作用是好的** —— 闲聊首句 p50 只有 2.1s，2000ms 的兜底让大部分
    # 闲聊**不再插填充音**，正好避开调研里「filler 用太多反而差评」（Boukaram 2021，
    # 🟡 二手转引）与 OpenAI「用不好反而增加感知延迟」。
    filler_delay_ms: int = 2000
    # ⚠️ **同一轮允不允许播第二次**（2026-09-20 真机修）。
    #
    # 起因：`_filler_played_turn` 原本是「一轮只播一次」，而**兜底（2.0s）总是抢在
    # 第一个工具事件（真机实测中位 3.4s / 最小 3.0s）之前** → 按工具类别选的承接句
    # **一次都没机会播**。真机 4/4 轮全是 `trigger=latency tag=generic`，
    # 用户听到的还是"嗯……/那个……"这些调研里说收益最弱的通用填充音。
    #
    # 改成**时间门**：距上一次播够 `filler_min_gap_ms` 就允许再播一条。
    # 于是 2.0s 播「嗯……」、3.7s 播「我搜一下」—— 两条不同的短句隔 1.7s，
    # 像"先沉吟一下、再动手"，而不是重复。
    # ⚠️ 门限的意义：**快的工具**（事件在 2.1s 就到的）不让它紧接着再播一条
    # （那样两条只隔 0.4s，很吵）。所以这个值不能太小。
    filler_min_gap_ms: int = 1500
    # ⚠️ **不是 0**（偏离 `PLAN-LATENCY-20260919.md` §P1 表的「delay 0」）：
    # Roark 验收清单那条「工具 200ms 就返回 —— 填充音还该响吗？」
    # （*a filler on a fast tool is a self-inflicted second of latency*）。
    # delay 0 时快工具会让承接句播到一半就被 `_cancel_filler()` 切掉 → 用户听到
    # **截断的半句话**，比静音更糟。300ms 宽限让真快工具完全不播；而本机工具步
    # 墙钟 p50 **4.50s**（`PLAN-LATENCY-20260919.md` §1.2），300ms 在正常路径上可忽略。
    filler_tool_delay_ms: int = 300

    # ---- 打断 ----
    # 语音持续超过此时长才算真打断（廉价版 false_interruption_timeout）。
    #
    # ⚠️ 300 → 100（2026-09-20，离线实测）：**这一段确认是冗余的**。
    # `vad.speaking` 取自 sherpa 的 `is_speech_detected()`，而它本身**要等
    # `vad_min_speech`(0.25s) 的连续语音才会翻 True** —— 实测直接量化：
    #   min_speech 0.25 → 首次 True = 语音起点 +300ms（0.15 → +200ms，0.05 → +100ms）
    # 也就是说 VAD 已经替我们做了一次 250ms 的持续确认，这里再加 300ms
    # 是**第二次确认**（与 `aec.py` 那个"延迟补偿两次"同型）。
    # 而它本来要防的瞬态噪声，Silero 自己就拦得住：离线实测 60/100/150/250/400/600ms
    # 的宽带噪声爆发（30× 噪声底）在 min_speech 取 0.25/0.15/0.05 下**都不翻 True**。
    # 感知延迟：打断 = 语音起点 → VAD 翻 True(~min_speech) → 确认窗。
    #   300ms + min_speech 0.25 → **600ms**（旧，用户反馈"打断还是不灵敏"）
    #   100ms + min_speech 0.25 → **400ms**（只压确认窗的效果）
    #   100ms + min_speech **0.05** → **200ms** ← 现在（2026-09-20 再压 VAD 那一层）
    # 两处都压过了；再往下收益很小（被 100ms 的块粒度卡住）。
    interrupt_confirm_ms: int = 100
    # 🆕 **自打断判据的阈值（当前只报不拦）**：`未过 AEC 的近端 − far`，低于它就疑似
    # "麦里只有回声"（=自打断）。真机 11 条捕获实测：自打断 **−8.7 ~ −9.4 dB**、
    # 人声打断 **−1.6 ~ +4.4 dB**，**中间 7.1 dB 空档**，取中点。
    # ⚠️ 现在**只用来打日志/标出"本该拦的"**，不影响行为 —— 先真机取一轮数据再决定。
    # 设成很小的值（如 -99）= 永不标记。见 docs/PLAN-SHORT-TURNS-20260920.md §3。
    bargein_echo_margin_db: float = -5.0
    # 新爆发前的静音门槛：静得比这短 → 视为"同一次说话的延续"，不武装打断。
    # 治的是 VAD 切分长句时 speaking 的 True→False→True 抖动（真机 4 连自打断）。
    interrupt_min_gap_ms: int = 250
    # 误判恢复的宽限窗口（2026-09-20 新增，见 docs/PLAN-HUMANNESS-20260920.md P1）。
    #
    # 问题：打断由 **VAD 起音**驱动，而转写要等用户说完（`min_silence_duration=0.5s`）
    # 再跑 ASR —— 所以 `filler.is_backchannel()` **永远来不及**用。
    # 真机数据：41 次「助手才播不到 3 秒就被打断」里，大量是 `'嗯。'` `'うん。'`
    # `'.'` `'那个。'` —— 用户只是在应答，助手却整段停了（而 `'那个。'` `'让我想想。'`
    # 甚至是我们自己填充音的回声，等于助手把自己打断）。
    #
    # 做法（照抄 LiveKit `false_interruption_timeout` 的语义）：
    #   VAD 起音 → **先暂停播放（保留缓冲）** → 窗口内等转写
    #     · backchannel / 空 / 非语音 → **恢复播放**
    #     · 真心话 → 提交打断（丢弃缓冲 + 作废轮次 + 中断 CC）
    #
    # 取值依据（本机链路时延实测）：打断在起音后 ~400ms 触发；用户说完后 VAD 还要等
    # 0.5s 静音才吐段，再过 ASR ~50–150ms → 转写大约在**打断后 600–800ms** 到。
    # 取 800 兜住它。⚠️ 这段窗口内助手是**静音**的 —— 代价是误判时一句话中间多一个
    # 0.8s 的顿。**调大更稳但顿得更久，调小则拦不住**；要 A/B 再定。
    bargein_grace_ms: int = 800

    # ---- P2：轮次协商（**默认关**，见 docs/PLAN-HUMANNESS-20260920.md P2）----
    # 「说几句 → 主动停一下 → 等接话」。把「一次说完」变成**留出插话的槽位**：
    # 用户不必"抢"话，而是被邀请接话 —— 这才是对话感的来源。
    #
    # ⚠️ **为什么默认 0（关）**：停顿是**净增加**的时间（3 句 × 600ms = +1.8s），
    # 而 CUI'25 实测「延迟 >4s 是头号体验杀手」。**如果用户从不接话，这就是纯亏。**
    # 计划里写死了：**必须先 A/B 量过再定值，甚至可能整体否掉。**
    #
    # A/B 用法（同场景各跑一场）：
    #   JARVIS_TURN_YIELD_MS=600 ./jarvis.sh start --speaker-aec --dashboard
    # 看什么：① 总时长变长多少 ② 用户真的接话了吗 ③ 听感上是「在等我」还是「卡了」
    turn_yield_ms: int = 0        # 0 = 关
    turn_yield_after_sentences: int = 2   # 第几句之后停（只停这一次，不是每句都停）

    # ---- 打断诊断轨迹（`~/.jarvis/bargein-trace.jsonl`）----
    # 常驻记录麦克风级别 + VAD 状态，**只在关键时刻落盘**（播放中检测到语音 /
    # 武装 / 待定 / 撤回 / 提交 / 超时）。平时零盘 IO。
    # 为什么需要：`level` 是瞬时事件**不落盘**，所以「那一刻麦克风多响、
    # VAD 有没有翻」在事后完全看不到 —— 而那正是判断"打断为什么不灵"的唯一依据。
    # 关掉：`JARVIS_BARGEIN_TRACE=0`。
    bargein_trace: bool = True

    # ---- 回声文本护栏（装 AEC 之前的纯文本兜底，零延迟）----
    # 动机：内置扬声器 + 内置麦时，AEC 残余会越过 VAD 门限被转写；若进了脑，
    # 助手就会**回应自己**（HANDOVER §1 明确列为不可接受："不能凭空自言自语"）。
    # 判据：与最近说过的文本做字符级 LCS 相似度。实测分离度：回声 0.91–1.00 /
    # 真人说话含重叠词汇最高 0.60 → 0.75 两边都留了余量（见 tests/test_echoguard.py）。
    # ⚠️ 宁可漏判（自言自语一次）也不误判（吃掉用户真话）——所以阈值偏高 +
    #    最小长度保护 + **每次命中都发 echo_suppressed 事件**（可审计有没有误吃）。
    echo_guard_enabled: bool = True
    echo_guard_threshold: float = 0.75
    echo_guard_min_len: int = 6       # 归一化后短于此长度不判（"好的"/"清空上下文"必须放行）
    echo_guard_window_s: float = 12.0 # 只跟最近这么久内说过的文本比

    brain_model: str = "haiku"
    # ---- 上下文窗口 / 自动压缩 ----
    # ⚠️⚠️ **不设这个值，自动压缩根本不会武装。** 2026-09-20 查实（已用本机 debug 日志
    # 双向验证）：
    #   · Claude Code 的判定函数第一道闸是「窗口来源 ≠ auto」，来源解析顺序是
    #     env > settings > **服务端下发（仅 firstParty）** > … > 内置 model 表 > auto
    #   · 我们走 cc-switch 代理 → 拿不到服务端窗口表 → 来源落到 **auto** → **直接 return**
    #   · 于是阈值**根本不算**，自动压缩永不触发
    # 实测证据：项目历史上 **250+ 个脑会话，`compact_boundary` 与 `isCompactSummary`
    # 统统为 0**；其中一个会话跨 **73.2 小时 / 215 轮 / 累计 370,618 token**（工具输入输出
    # 1,436,496 字符）依然零压缩。
    #
    # 不设它还有第二个后果（更严重）：窗口不设 → 撞上限时只能靠 reactive 兜底
    # （API 返回 too-long 时触发），而**代理会改写错误文案 → 那条兜底不生效** → 脑硬挂。
    #
    # 取值 200000 = Claude Code 对 `claude-haiku-4-5` 的标称窗口（我们报的就是这个模型名）。
    # ⚠️ 真实后端（cc-switch → deepseek-v4.1-flash）实测 **63.9 万 token 的请求仍返回 200**
    #    （本机 `proxy_request_logs` 11,245 条样本）；取 200K 是**保守**选择，不是上限。
    #    保守的代价只是压缩早一点（有损），不保守的代价是撞上限后无法恢复。
    # 为什么需要它：本机实测（同 11,245 条请求）——上下文越大首字越慢：
    #     <20K → p50 **1295ms**（缓存命中 99.5%）   200–300K → p50 **1969ms**（5.7%）
    #     300–600K → p50 **2431ms**（5.3%）
    #   也就是说「上下文只增不减」会直接吃掉项目第一目标（对话感）。
    brain_compact_window: int = 200000

    # ---- 脑进程启动模式（2026-09-19 常驻会话实测标定）----
    # `--bare` 会关掉 hooks / LSP / plugin sync / auto-memory / keychain /
    # CLAUDE.md 自动发现 —— **以及 skills 和 ToolSearch**。
    #
    # 常驻会话实测（同一进程跑 6 轮，稳态中位）：
    #   --bare              929 ms   无 skills / 无懒加载 / 无 WebSearch
    #   非 bare            1483 ms   +554ms，拿到 skills + ToolSearch(99 deferred) + WebSearch
    #   非 bare+插件全关    1085 ms   +156ms   ← **推荐**
    # → 那 400ms 差是插件的启动开销（claude-mem / discernment-nudge / i-have-adhd）。
    #   ⚠️ claude-mem 尤其别放进语音脑：它有自己的「死循环 + 30000ms 超时」史。
    #
    # ⚠️ **别用 `claude --print` 测这个** —— 那是单次进程，每次付冷启动，
    #    会量出「+3.6 秒」的假数（真值只有 156ms）。必须用常驻进程量。
    brain_bare: bool = True

    # ⚠️ **这个 dataclass 默认值是 `False`，但应用实际跑的是「默认开」。**
    # 顺序是：`__main__.py` 在没给 `--fresh` 时 `os.environ.setdefault("JARVIS_RESUME","1")`
    # → `Config.load()` 读 env → 变成 `True`。**只有直接构造 `Config()` 的测试**才是 False。
    # （2026-09-18 的交接文档 §7.1 记过这个"文档说默认关、代码实际默认开"的矛盾，
    #   2026-09-20 统一口径为「默认开」。）
    #
    # 为什么默认开：个人助手要跨天记住上次聊的。
    # ⚠️ 代价是**链式污染**：每次恢复继承上一次全部上下文，**任何**被污染的会话都会
    # 永久烘焙进链条一直传下去 —— 真机踩过两次（助手带着"数到40/在的"这些测试对话醒来，
    # 用户听到完全陌生的记忆）。要干净重来用 `--fresh`。
    # ⚠️ 第二个代价（2026-09-20 实测）：链条**只增不减**，且实测自动压缩**从未武装**
    # （见 `brain_compact_window`）→ 跑了 73 小时 / 215 轮 / 37 万 token 的会话照样在跑。
    resume_session: bool = False

    # ---- 记忆（注入脑层的系统提示）----
    # `--bare` 关掉了 CLAUDE.md 自动发现与 auto-memory（实测 2.1.266 无法在 bare 下恢复），
    # 所以"脑认识主人"这件事必须由我们**显式**注入。两条通道：
    #   persona_file ── 人工维护的身份/世界事实（"Brightspace 是 UCD 课程平台"之类）
    #   memory_file  ── 脑用"记住 X"自己追加的可写记忆（一行一条）
    # 两者合成后写盘，用 `--system-prompt-file` 喂给 claude（一手验证：文件内容真进上下文）。
    persona_file: str = ""
    memory_file: str = ""

    # ---- TTS ----
    tts_provider: str = "fish"       # fish | say
    fish_voice: str = "5c353fdb312f4888836a9a5680099ef0"   # 女大学生（2026-09-16 选定）
    fish_model: str = "s2.1-pro-free"
    fish_sample_rate: int = 44100    # 实测 Fish PCM = 44100Hz/单声道/int16
    fish_latency: str = "balanced"   # balanced 比 normal 更快（实测）
    # ⚠️ 实测（2026-09-19，n=20 交替测排除时段漂移）：**REST 比 WebSocket 快 3.26 倍**
    #   WS   p50=1559ms  p90=1835ms
    #   REST p50= 478ms  p90= 733ms
    # 同一个模型、同一个音色、同一个 key —— **只换传输方式，不动音质**。
    # 所以默认关掉 WS。（`latency` 在兼容 API 上可能被忽略，但两条路都是 balanced，
    # 差别来自传输本身。）
    # 想回 WS：`FISH_WS=1`
    fish_use_websocket: bool = False
    fish_temperature: float = 0.3    # 实测：0.7 时同句时长变异 7.6%，0.1 时降到 2.1%。助手要稳定

    # ---- 兜底 ----
    say_voice: str = "Tingting"

    # ---- 由 audio_mode 派生的行为（单一事实来源，避免两处开关打架）----
    # ⚠️ 必须写成**白名单**。曾经的写法是 `half_duplex = audio_mode != "headphones"`——
    # 那是黑名单：加任何新模式都会静默判 True，麦克风照样被丢，AEC 接了等于没接，
    # **而且不报任何错**。加 "speaker_aec" 时踩到过。
    @property
    def barge_in(self) -> bool:
        """是否允许**自动**打断。耳机与 AEC 免提都可以（AEC 消掉了自己的回声）。"""
        return self.audio_mode in ("headphones", "speaker_aec")

    @property
    def half_duplex(self) -> bool:
        """播放期间是否忽略麦克风。**只有无 AEC 的免提**才需要。"""
        return self.audio_mode == "speaker"

    @property
    def aec(self) -> bool:
        """是否启用软件回声消除。"""
        return self.audio_mode == "speaker_aec"

    @classmethod
    def load(cls) -> "Config":
        backend = _env_str("JARVIS_VAD", "silero")
        if backend == "silero":
            model, window = MODELS / "vad" / "silero_vad.onnx", 512
        else:
            model, window = MODELS / "vad" / "ten-vad.onnx", 256
        return cls(
            audio_mode=_env_str("JARVIS_AUDIO_MODE", cls.audio_mode),
            bargein_echo_margin_db=_env_float("JARVIS_BARGEIN_ECHO_MARGIN_DB",
                                              cls.bargein_echo_margin_db),
            # ⚠️ 2026-09-21 补：这个一直**只写在文档里、代码从没读过** ——
            # 文档/计划里说"打断延迟可调"的地方，只有改代码才生效。
            # 是全仓唯一一个"文档说可设但没接线"的环境变量（有测试守着，见
            # tests/test_env_wiring.py）。
            interrupt_confirm_ms=_env_int("JARVIS_INTERRUPT_CONFIRM_MS",
                                          cls.interrupt_confirm_ms),
            interrupt_min_gap_ms=_env_int("JARVIS_INTERRUPT_MIN_GAP_MS",
                                          cls.interrupt_min_gap_ms),
            bargein_grace_ms=_env_int("JARVIS_BARGEIN_GRACE_MS", cls.bargein_grace_ms),
            # AEC：延迟只给粗值（AEC3 自估）
            aec_stream_delay_ms=_env_int("JARVIS_AEC_DELAY_MS", cls.aec_stream_delay_ms),
            aec_backend=_env_str("JARVIS_AEC_BACKEND", cls.aec_backend),
            aec_speex_filter_length=_env_int("JARVIS_AEC_SPEEX_FL",
                                             cls.aec_speex_filter_length),
            brain_bare=os.environ.get("JARVIS_BARE", "1") == "1",
            brain_compact_window=_env_int("JARVIS_BRAIN_COMPACT_WINDOW",
                                          cls.brain_compact_window),
            turn_yield_ms=_env_int("JARVIS_TURN_YIELD_MS", cls.turn_yield_ms),
            turn_yield_after_sentences=_env_int("JARVIS_TURN_YIELD_AFTER",
                                                cls.turn_yield_after_sentences),
            bargein_trace=os.environ.get("JARVIS_BARGEIN_TRACE", "1") == "1",
            resume_session=os.environ.get("JARVIS_RESUME") == "1",
            persona_file=_env_str("JARVIS_PERSONA", str(JARVIS_HOME / "persona.md")),
            memory_file=_env_str("JARVIS_MEMORY", str(JARVIS_HOME / "memory.md")),
            input_device=_env_str("JARVIS_INPUT_DEVICE", cls.input_device),
            output_device=_env_str("JARVIS_OUTPUT_DEVICE", cls.output_device),
            vad_backend=backend,
            vad_model=_env_str("JARVIS_VAD_MODEL", str(model)),
            vad_window=_env_int("JARVIS_VAD_WINDOW", window),
            vad_threshold=_env_float("JARVIS_VAD_THRESHOLD", 0.6),
            vad_min_silence=_env_float("JARVIS_VAD_MIN_SILENCE", 0.5),
            vad_min_speech=_env_float("JARVIS_VAD_MIN_SPEECH", cls.vad_min_speech),
            vad_min_snr=_env_float("JARVIS_VAD_MIN_SNR", cls.vad_min_snr),
            vad_min_rms=_env_float("JARVIS_VAD_MIN_RMS", cls.vad_min_rms),
            vad_pre_roll_ms=_env_int("JARVIS_VAD_PRE_ROLL_MS", cls.vad_pre_roll_ms),
            # 回声护栏：阈值要能从事件日志调（看 echo_suppressed 的 score 分布）
            echo_guard_enabled=os.environ.get("JARVIS_ECHO_GUARD", "1") == "1",
            echo_guard_threshold=_env_float("JARVIS_ECHO_GUARD_THRESHOLD", cls.echo_guard_threshold),
            asr_model_dir=_env_str(
                "JARVIS_ASR_MODEL",
                str(MODELS / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17")),
            asr_provider=_env_str("JARVIS_ASR", cls.asr_provider),
            tts_provider=_env_str("JARVIS_TTS", cls.tts_provider),            fish_voice=_env_str("FISH_VOICE", cls.fish_voice),
            fish_model=_env_str("FISH_MODEL", cls.fish_model),
            fish_latency=_env_str("FISH_LATENCY", cls.fish_latency),
            fish_use_websocket=os.environ.get("FISH_WS", "0") == "1",            fish_temperature=_env_float("FISH_TEMPERATURE", cls.fish_temperature),
            say_voice=_env_str("JARVIS_SAY_VOICE", "Tingting"),
        )
