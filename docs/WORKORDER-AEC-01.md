# WO-AEC-01：接入 `AecGate`（免提 AEC）—— 逐文件改动清单

> **性质**：施工单。由 Owen 审阅后，交 ZCode 执行机械编辑，Claude 逐行复核。
> **依据**：`docs/RESEARCH-AEC-20260919.md` §2.2（粗清单）+ `docs/PROBE-AEC-RESULTS-20260919.md`（实测数字：稳态 ERLE 34–36 dB）。
> **本文新增了两个设计文档**没写的坑**：P-1（派生属性会静默吃掉新模式）和 P-2（flush 会让 far 参考跑到未来）。

---

## 0. 前置闸门（**未清，不得开工**）

| # | 闸门 | 现状 | 动作 |
|---|---|---|---|
| G-1 | 仓库无版本控制 | ❌ `~/jarvis-voice/.git` 不存在，678 MB | `git init` + 首次提交（**改造前快照**） |
| G-2 | 明文密钥 | ❌ `mcp-jarvis.json` 有 Tavily key + Obsidian bearer | 先改成 env 引用，**再** init，**再**派活 |
| G-3 | ZCode 默认 yolo | ⚠️ `--prompt` 默认 `yolo`；`--allowed-tools` **实测坏的**（帮助里有、解析器拒） | 只能靠 `--mode edit` + `--cwd` 隔离；**不开 yolo** |

**顺序不能反**：G-2 → G-1 → G-3。

---

## 1. ⚠️ 两个设计文档漏掉的坑

### P-1：`half_duplex` 是「不等于耳机」，加了第三档会静默吃掉它

`config.py:169-172` 现状：

```python
@property
def half_duplex(self) -> bool:
    return self.audio_mode != "headphones"     # ← 白名单的反面
```

**加 `"speaker_aec"` 后，这一行会让它 `return True`** —— 于是 `orchestrator.py:262` 的半双工门照样开，麦克风照样被丢，**AEC 接了个寂寞，而且没有任何报错**。

**必须同时改**（见 §2.1）。这是本项目「单一事实来源」原则的正确实现：**白名单，不是黑名单。**

### P-2：`flush()`（打断）会让 far 参考信号跑到"未来"

`player.py:165-172` 的 `flush()` 清空 `_buf` —— 那些**已经进了 far 环形缓冲、但永远不会被播出来**的帧，就成了**幽灵参考信号**。AEC 会拿一段从未发声的信号去对消麦克风 → 参考错 → **AEC 自我保护性停用**（一手：`echo_cancellation.c`「For really bad systems, don't disable the echo canceller for more than 0.5 sec」）。

**而 `flush()` 恰恰在打断时被调用** —— 也就是**最需要 AEC 的那一瞬间**。

**修法**（见 §2.2）：far 环形缓冲**按绝对帧号索引**，读指针跟随**回调里真正写出去的帧数**，而不是跟随 `write()`。这样 flush 天然正确：没写出去的帧，读指针永远不会走到。

---

## 2. 逐文件改动

### 2.1 `jarvis_voice/config.py`

**改动 1** —— 派生属性改白名单（**同时修 P-1**）：

```python
# 原 line 164-172
@property
def barge_in(self) -> bool:
    """是否允许**自动**打断。耳机与 AEC 免提都可以（AEC 消掉了自己的回声）。"""
    return self.audio_mode in ("headphones", "speaker_aec")

@property
def half_duplex(self) -> bool:
    """播放期间是否忽略麦克风。**只有无 AEC 的免提**才需要。"""
    return self.audio_mode == "speaker"          # ← 白名单，修 P-1
```

**改动 2** —— 新增字段（放在 `audio_mode` 附近，line 111 后）：

```python
    # ---- AEC（仅 audio_mode == "speaker_aec" 生效）----
    aec_stream_delay_ms: int = 150     # 粗值即可，AEC3 自带 delay estimator
    aec_snr_boost: float = 2.0         # 播放期间 vad_min_snr 的乘数（见 §3）
    aec_min_speech_prob: float = 0.5   # speech_probability 双确认门限
```

**改动 3** —— 新增派生属性：

```python
@property
def aec(self) -> bool:
    return self.audio_mode == "speaker_aec"
```

**改动 4** —— `Config.load()` 加 env 覆盖（对齐现有 `_env_int`/`_env_float` 风格）：

```python
    aec_stream_delay_ms=_env_int("JARVIS_AEC_DELAY_MS", cls.aec_stream_delay_ms),
```

**验收**：`JARVIS_AUDIO_MODE=speaker_aec` 时打印 `barge_in=True half_duplex=False aec=True`；
`JARVIS_AUDIO_MODE=speaker` 时 `barge_in=False half_duplex=True aec=False`；
`headphones` 时 `True/False/False`。**三种模式都要打印确认**（不改代码凭读代码判断会漏 P-1）。

---

### 2.2 `jarvis_voice/player.py` —— far 参考镜像（含 P-2 修法）

**新增状态**（`__init__`，line 40 附近）：

```python
        # far 参考镜像：AEC 用的"我们到底播了什么"。
        # ⚠️ 按**绝对帧号**索引，读指针跟随**回调真正写出去的帧数**（_emitted），
        #    而不是跟随 write() —— 否则 flush() 后未播出的帧会成为幽灵参考（见 P-2）。
        self._far = np.zeros(0, dtype=np.int16)   # 追加式磁带，容量 AEC_FAR_SEC
        self._far_base = 0        # self._far[0] 对应的绝对帧号
        self._emitted = 0         # 回调真正写到输出的绝对帧数（单写者，同 _uptime）
        self._far_lock = threading.Lock()
```

容量常量：`AEC_FAR_SEC = 3.0`（2s 够用，3s 留余量）。

**`_cb` 里**（line 110 附近，与 `self._uptime += frames` 并列 —— **同一处、同一理由：单写者、不在锁内**）：

```python
            self._emitted += frames
```

**`write()` 里**（line 133 的 `with self._lock:` 之后追加）：

```python
        with self._far_lock:
            self._far = np.concatenate([self._far, arr])   # arr 是 44.1k int16 1-D
            keep = int(self.fmt.sample_rate * AEC_FAR_SEC)
            if self._far.shape[0] > keep:
                drop = self._far.shape[0] - keep
                self._far = self._far[drop:]
                self._far_base += drop
```

⚠️ **`np.concatenate` 每次 write 都复制全量**：写 ≈ 每次 100ms 音频（4410 样本）+ 3s 容量（132300 样本）→ 每次拷 ~140KB。TTS 线程非实时，可接受；**但若 TTS 块很小（<20ms）会变成热点**。**改用固定容量环形数组**（`np.empty(cap)` + 写指针取模）是更稳的写法 —— **交给 ZCode 时明确要求环形数组版本，不要 concatenate。**

**新增方法**：

```python
    def emit_frames(self) -> int:
        """回调真正写到输出的绝对帧数（44.1k）。AEC 的 far 读指针基准。"""
        return self._emitted

    def far_slice(self, start_frame: int, n_frames: int) -> np.ndarray:
        """取 [start_frame, start_frame+n) 的 far 参考（44.1k int16）。
        返回实际可用的部分（可能短于请求 —— 边界处）。"""
```

⚠️⚠️ **`player.py` 只存原始 44.1k int16，绝不做重采样。**
**重采样必须在消费侧（`AecGate.accept()`）做。** 理由见 §2.3 —— 生产侧重采样实测 **ERLE 掉 13 dB**。

**`reopen()`**（line 56-72）里加：清空 `_far`、`_far_base`、`_emitted`（换了设备 = 换了一条声学路径，旧参考全部作废）。

**`flush()`**（line 165）**不加任何东西** —— P-2 靠读指针自然正确，这正是这个设计的意义。

**验收**：新增一个独立脚本断言：
1. write 了 N 帧后、未播放时，`far_slice` 读到的区间在 `[0, emit_frames())` 之内（**不会读到未来**）
2. `flush()` 之后，`far_slice` 仍只返回 `[0, emit_frames())` 内的数据
3. 环形数组滚动 N 圈后，取出的数据与朴素拼接一致（**用固定长度伪随机序列比对**）

---

### 2.3 新文件 `jarvis_voice/aec.py`

职责：**唯一**调用 `pywebrtc-audio` 的地方。**只在主循环线程被调用**（`AudioProcessor` 非线程安全，一手）。

```python
class AecGate:
    def __init__(self, cfg, player): ...
        # self.ap = AudioProcessor(sample_rate=16000, echo_cancellation=True,
        #                          noise_suppression=<见下>, stream_delay_ms=cfg.aec_stream_delay_ms)
        # self._far_read = 0     # 44.1k 绝对帧读指针

    def accept(self, chunk_f32_16k: np.ndarray) -> np.ndarray: ...
        # 1. 算出这次要消费的 far 帧区间：从 self._far_read 起，
        #    长度 = len(chunk) * 441/160，**clamp 到 player.emit_frames()**
        # 2. 从 player.far_slice 取 44.1k int16 → resample_poly(x, 160, 441) → 16k
        # 3. self.ap.process(near_int16, far_int16)
        # 4. self._far_read 前进（注意：clamp 后可能没前进满 —— 允许落后）
        # 5. 返回干净信号

    @property
    def speech_probability(self) -> float: ...
```

⚠️ **NS 开不开**：实测 `aec` 与 `aec_ns` 转写**逐字相同**（`PROBE-AEC-RESULTS` §6.1）→ **NS 不伤语音**。但 **AGC 必须关**（会改增益，干扰项目自己的电平统计）。**默认开 NS、关 AGC、开 HP。**

⚠️ **far 落后于 near 是正常态**（clamp 生效时）。AEC3 自带 delay estimator，**允许 far 落后**；不允许的是 far 超前（P-2）。**读指针只增不减。**

✅ **U-1 已实测（2026-09-19，`resample_probe.py` / `resample_probe2.py`）—— 结论推翻了初版设计，务必照做：**

**重采样绝不能放在生产侧（`write()`）。**

| far 参考怎么造 | 长度漂移 | 稳态 ERLE | Δ |
|---|---|---|---|
| 整段一次性（探针做法，不可实现） | — | 26.47 dB | 基准 |
| 生产侧·固定 10ms 块 | +0 | 26.41 dB | −0.06 |
| 生产侧·固定 100ms 块 | +0 | 26.48 dB | +0.01 |
| **生产侧·1024 帧块（= `player.py:20` 的 `BLOCK_FRAMES`）** | **+249** | **13.32 dB** | **−13.15** |
| **生产侧·随机 20–200ms（TTS 真实块）** | **+63** | **23.97 dB** | **−2.50** |
| **消费侧（本单设计）** | **+0** | **26.48 dB** | **+0.01** |

**根因不是边界伪影，是 `ceil()` 累积漂移**：`resample_poly` 输出长 = `ceil(L·160/441)`。
生产侧块长（TTS 来的，不定）不是 441 的整数倍 → 每块多半/少半样本 → 12 秒后漂 249 样本（5.6 ms）
→ far 与 near 系统性错位 → AEC 参考错。

**修法（实测 +0.01 dB，零损失）：读指针 `_far_read` 永远按 44.1k 绝对帧推进，长度由 `n16` 反算，绝不由累加输出长度决定。**
- 近端块固定 1600（100ms@16k）→ 需要 1600×441/160 = **4410 = 441×10，整数** → 精确无漂
- 非整除块长（1584/1552/1280 实测）也 **+0 漂移**（分数推进即可，Δ ≤ 0.05 dB）
- **不需要余量**：无余量实测 +0.01 dB。**先按最简写。** 若真机出现边界问题，加 50ms 余量即可（实测同样 +0.00 dB）——**但先别加**。

---

### 2.4 `jarvis_voice/orchestrator.py`

**改动点**（line 74-75 附近）：建 `AecGate`（仅 `cfg.aec` 时）

```python
        self.aec = AecGate(cfg, self.player) if cfg.aec else None
```

**主循环**（line 249-270）：在 `self.vad.accept(chunk)` **之前**插入：

```python
                chunk = self.mic.read(timeout=0.2)
                if chunk is None:
                    continue
                # ---- AEC：先消掉我们自己播的回声，再进 VAD ----
                if self.aec is not None:
                    chunk = self.aec.accept(chunk)
```

⚠️ **顺序不可换**：必须 **AEC → 电平表 → VAD**。把 AEC 放在电平表之后，仪表盘的电平会显示回声（误导）。

⚠️ **半双工分支（line 262）不动** —— `cfg.half_duplex` 在 `speaker_aec` 下已为 `False`（§2.1 改动 1）。**不要在这里加 `and not cfg.aec` 之类的条件**，那会让 P-1 的修复失去意义。

---

### 2.5 `jarvis_voice/dashboard.py`

模式按钮加第三档「免提 + AEC」→ `set_mode("speaker_aec")`。

⚠️ `orchestrator.py:387-410` 的 `set_mode()` **不调 `session.interrupt()`**（已记录的待办）。加第三档时**顺带修**：切模式时 `self.aec` 需要重建（`AudioProcessor.reset()`），且正在播报时要走一次 `flush()`。

---

## 3. 第二道防线（`RESEARCH-AEC-20260919.md` §2.3 —— **必做，不是可选**）

**理由**：AEC 不可能 100% 干净。残余只要过 VAD 门限，就会撞上 SenseVoice 的**已知行为：对任何非语音都幻觉出文本**（静音→`그。`、白噪声→`Yeah.`，🟢 项目实测）。**AEC 做得"还行"可能比半双工更吵。**

三层（全部已有基建）：

1. **播放期间抬高段级 SNR 门限**：`vad.py:_passes_gate` 的 `need` 乘 `cfg.aec_snr_boost`
   ⚠️ **必须在 AEC **之后**、且对"原始 near"算 SNR** —— 否则 AEC 压低了段能量，SNR 反而变差，门限会误杀真语音。
2. `asr_min_utt_sec` 已有（滤 <0.4s 段）
3. **`speech_probability` 双确认**：播放期间，段必须 `ap.speech_probability >= cfg.aec_min_speech_prob` 才放行

---

## 4. 验收（接入后，真机）

| # | 场景 | 期望 | 判据 |
|---|---|---|---|
| 1 | 免提，播报中说「停一下」 | 打断成功 | **×20 次计数**，见下 |
| 2 | 免提，播报中不说话 | **零**自问自答 | 日志无 `echo_suppressed` 以外的自触发 |
| 3 | 免提，播报完毕再说 | 正常识别 | 与耳机模式无异 |
| 4 | 播报中说话，内容不被 AEC 吃掉 | 转写命中关键词 | ≥4/5 |

⚠️ **验收纪律**（沿用 `VERIFY-TURN-DETECTION-20260916.md`，**继承而非新造**）：
- **必须带对照**：同场景跑一次 `audio_mode=speaker`（半双工）作基线 —— 只看 AEC 模式会把「他自己说话太快」误判成 AEC 的锅（`PROBE-AEC-RESULTS` §6.1 第 1 轮就是这个陷阱）
- **×20 次不是 ×3 次**：打断成功率是二项分布，3 次全过也可能是 60% 的真实成功率
- **数字先落在纸面**再下结论

---

## 5. 未验证清单（接入前必须补测）

| # | 项 | 为什么 |
|---|---|---|
| U-1 | ~~分段 `resample_poly` 的伪影~~ | ✅ **2026-09-19 已测**：生产侧分段重采样 **ERLE 掉 13.15 dB**（1024 帧块）；消费侧修法 **+0.01 dB**。见 §2.3 |
| U-2 | **AEC 模式下打断检测连续运行** | 从未测过的代码路径（`VERIFY-AEC-PLAN` §8.1） |
| U-3 | **长会话收敛保持** | 探针单轮 15s；真实会话是分钟级 + 人/机器会动 |
| U-4 | **`_paused`/`_hd_gated`/`reopen()` 卡死** | 与本单无关，但**同窗口改音频路径**要先确认现状（`RESEARCH-UPGRADE-PLAN-20260919.md` P0-A） |

---

## 6. 派单方式（给 ZCode）

```bash
# ⚠️ 前置：G-1/G-2 已清；仓库已在 git 下
zcode -p "$(cat WO-AEC-01.md) 请按 §2 逐文件实施。" \
  --cwd ~/jarvis-voice \
  --mode edit \
  --json
```

**不要用 `--mode yolo`。不要用 `--allowed-tools`（实测坏的，会直接报错退出）。**

**派完后 Claude 必做**：`git diff` 逐行复核 + 跑 §2.1/§2.2 的验收断言。
**不通过就不合并** —— 对外部模型的 diff 用与子代理引用相同的标准（「子代理给的引用经常是错的」）。
