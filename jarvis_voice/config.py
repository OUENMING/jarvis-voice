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
    vad_min_speech: float = 0.25     # ⚠️ 不要调高：0.3s 的"停"也会被丢
    vad_max_speech: float = 20.0
    # 段级信噪比门限 —— 对抗"没出声自己说话"的**主防线**。
    # 实测：SenseVoice 对任何非语音都幻觉出文本（静音→'그。'、白噪声→'Yeah.'），
    # 文本层无法过滤，只能在音频层拦。
    vad_min_snr: float = 3.0         # 段 RMS 至少是噪声底的 3 倍（≈9.5dB）
    vad_min_rms: float = 0.012       # 绝对下限，防噪声底被估得过低

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
        ("欧men", "Omen"),
        ("欧门", "Omen"),
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
    # 延迟粗值即可 —— AEC3 自带 delay estimator，实测 0 与 100 无差别。
    aec_stream_delay_ms: int = 150
    # 注：`RESEARCH-AEC-20260919.md` §2.3 的三层防线（播放期间抬高 SNR 门限 /
    # speech_probability 双确认）**尚未实现** —— 那两个阈值没有实测数据可依据，
    # 先不设死配置（避免重犯 §7.5「死配置」的坑）。当前靠已有的段级 SNR 门限
    # + `asr_min_utt_sec` + `echoguard` 三层兜底；`orchestrator` 已在播放期间
    # 把 `speech_probability` 报进 `level` 事件，攒够数据再定阈值。

    # ---- 填充音 ----
    filler_enabled: bool = True
    filler_delay_ms: int = 900       # 本轮开始后等这么久还没出首句才播（避免给快回答平白加一段）

    # ---- 打断 ----
    interrupt_confirm_ms: int = 300   # 语音持续超过此时长才算真打断（廉价版 false_interruption_timeout）
    # 新爆发前的静音门槛：静得比这短 → 视为"同一次说话的延续"，不武装打断。
    # 治的是 VAD 切分长句时 speaking 的 True→False→True 抖动（真机 4 连自打断）。
    interrupt_min_gap_ms: int = 250

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
    # ⚠️ **默认关闭**（血的教训）。`--resume` 会**链式**生成新会话，每次恢复都继承
    # 上一次的全部上下文 —— 于是**任何**污染过的会话都会永久烘焙进链条，一直传下去。
    # 真机踩过两次：助手带着"数到40/在的/我还活着"（我的测试对话）醒来，
    # 用户听到的完全是陌生记忆。**这个失败模式比"重启失忆"严重得多。**
    # 需要长期记忆时显式加 `--resume`（或 JARVIS_RESUME=1）。
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
            # AEC：延迟只给粗值（AEC3 自估）
            aec_stream_delay_ms=_env_int("JARVIS_AEC_DELAY_MS", cls.aec_stream_delay_ms),
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
            vad_min_snr=_env_float("JARVIS_VAD_MIN_SNR", cls.vad_min_snr),
            vad_min_rms=_env_float("JARVIS_VAD_MIN_RMS", cls.vad_min_rms),
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
            fish_use_websocket=os.environ.get("FISH_WS", "0") == "1",
            fish_temperature=_env_float("FISH_TEMPERATURE", cls.fish_temperature),
            say_voice=_env_str("JARVIS_SAY_VOICE", "Tingting"),
        )
