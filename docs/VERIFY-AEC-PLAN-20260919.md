# 独立核实：AEC 计划（2026-09-19）

> **触发**：Owen 计划推进 AEC，要求 ① 核对与 `RESEARCH-UPGRADE-PLAN-20260919.md` 的冲突 ② 独立评估该计划「合不合理 / 是否前沿 / 是否社区热门 / 是否能落地」。
> **口径**：🟢 一手核实（我直查原文/API/仓库）　🟡 单源二手　🔴 未验证。引用旧结论标注日期。
> **被核对象**：`RESEARCH-AEC-20260919.md` + `RESEARCH-BARGEIN-20260916.md`

---

## 0. 四句话结论

1. **计划本身成立、可落地、工程形态完备** —— 零新增依赖（scipy 1.18.1 已在 venv）、改动面 5 文件、有离线 P0 脚本、有决策树与判定线。**我不建议改它的结构。**
2. 🔴 **但它的标题结论「业界没有人做到」不成立**，且这条更正**改变优先级** —— 见 §4。
3. 🔴 **ERLE 判定线需要两条修正**：度量本身与主观相关性弱（PCC 0.31），且**必须分「安静」与「真实桌面」两组测**（文献：背景噪声可把 ERLE 从 30dB 打到 4–8dB）—— 见 §2、§3。
4. ⚠️ **与升级计划有三处撞车**，其中 `vad_min_snr` 是**复合冲突**（他要在播放期把门限提到 6.0，而预滚缓冲会稀释整段 RMS）—— 见 §8。

---

## 1. 🟢 一手核实通过的部分

| 他的断言 | 核实结果 |
|---|---|
| `pywebrtc-audio` 0.2.0，2026-09-03 发布 | ✅ PyPI JSON 直查，上传日 2026-09-03 |
| 有 cp314 macOS arm64 轮子 | ✅ `pywebrtc_audio-0.2.0-cp314-cp314-macosx_11_0_arm64.whl` 存在；0.1.0 只到 cp313，**0.2.0 才补上 cp314** |
| 维护节奏 0.0.1→0.1.0→0.2.0 | ✅ 2026-03-27 / 2026-05-19 / 2026-09-03 |
| `requires_python ≥3.10` | ✅ |
| 项目 `.venv` 是 3.14.7 | ✅ 实测 |
| scipy 已在 venv → 重采样零新增依赖 | ✅ scipy **1.18.1**、numpy **2.5.3** |
| 尚未安装 | ✅ `No module named 'pywebrtc_audio'`（Step 0 就是 `pip install`） |

**🟢 新发现（他未写）**：该包由 **`strands-labs`（AWS Strands Agents）** 维护 —— PyPI 描述里有专门的 `strands_agents_bidi.py`（"Strands BidiAgent with live echo cancellation"）示例，仓库在 `github.com/strands-labs/pywebrtc-audio`，**vendor 了 WebRTC 的 `audio_processing/aec3`**。

→ **这不是业余包，有云厂商背景与明确的 agent 场景动机。** 对他的路径 A 是加分。

⚠️ **一处待补**：PyPI metadata `license: None`。他文档写 "Apache-2.0" —— **需从仓库 LICENSE 文件确认**，不是阻塞项但要落一条核对。

---

## 2. 🔴 更正一：ERLE 作为判定线，文献明说它有局限

**ICASSP 2023 Acoustic Echo Cancellation Challenge 论文**（arXiv 2309.12553）原话：

> **ERLE is only appropriate when measured in a quiet room with no background noise and only for single talk scenarios (not double talk)**, where we can use the processed microphone signal as an estimate for e(n).
>
> Using the datasets provided in this challenge we show that **ERLE and PESQ have a low correlation to subjective tests**（Table 1）

相关性数字（PCC / SRCC，对主观 P.808）：
- **ERLE：0.31 / 0.23**
- PESQ：0.67 / 0.57

→ **ERLE 与"人听起来好不好"相关性弱。** 只盯 ERLE 会做出错误判断。

**✅ 但他的计划已经比纯 ERLE 好** —— `RESEARCH-AEC-20260919.md` §3.3 是**三条度量**：
1. ERLE（≥15dB 线）
2. **幻觉测试**（残余喂 SenseVoice → 空/单标点 = 安全）
3. **双讲保真**（播放时念一句 → 转写应完整）

**后两条是任务级指标，正是 ICASSP 论文建议的方向**（用更贴近主观/任务的度量）。**这是计划里最有价值的设计，应当保持为重点，ERLE 只作辅助。**

---

## 3. 🔴 更正二：15dB 这个数在文献里的位置 —— 以及一条被漏掉的前提

| 来源 | ERLE 数字 | 条件 |
|---|---|---|
| ITU G.131（经 iieta.org 转述） | 无双讲时要求 **>40dB** | 电信级 |
| Broadcom（VoIP 导则） | 长延迟下需 "**at least 15 dB**" 才不觉得回声 disturbing | 感知线 |
| Zhang et al., Interspeech 2018（NLMS） | **30.97–34.63 dB** | **安静室、无双讲** |
| 同文，加 **10dB SNR 背景噪声** | **4.14–8.03 dB** | 有噪声 |
| Microsoft RES（在 AEC 之上再加非线性残余抑制） | **+7 dB** | 在 AEC 输出之上 |

**判定**：
- **15dB 作为工程入门线是合理的** —— 它落在"噪声环境的现实下限（4–8dB）"与"安静室理论上限（30–35dB）"之间，且与 Broadcom 的"至少 15dB"吻合。**他这条线有独立支持。**
- ⚠️ **但文献明确要求 ERLE 在「静音室」测**，而他的 P0 设计（§3）**没有规定房间条件**，且他实际使用环境有风扇/空调/街噪。**背景噪声能把 ERLE 从 30dB 打到 4–8dB。**

**→ 对 P0 脚本的强制补充**：至少两组环境
1. **安静组**（关门窗、关风扇/空调）—— 取上限
2. **真实桌面组**（他平时的环境）—— **这一组才是决策依据**

**静音室的数字不能外推到真实使用。** 他 §3 写的"同时测第二组『内置麦 + 耳机输出』作上限参照"是**设备维度**的对照，**不替代房间维度**。

---

## 4. 🔴 更正三：「业界没有人做到」不成立 —— 而且这条改优先级

他 09-16 旧文 §0 的标题句：
> **最重要的发现：业界没有人做到。** 全网找不到任何"笔记本内置扬声器 + 不戴耳机 + Python 栈 + 稳定免提打断"的报告……**这不是我们无能，是行业现状。**

**一手反证**（GitHub issue `ksg98/fastaf-ide#7`，2026-08-05，**open**）：
> **Full-duplex barge-in on open laptop speakers is table stakes for comparable voice assistants (ChatGPT Advanced Voice, Gemini Live) — they work on a MacBook with no headphones. The difference is where echo cancellation happens.**
> Why this is **fixable rather than inherent**

**拆开看哪部分成立：**

| 表述 | 裁定 |
|---|---|
| "找不到**现成的 Python 栈**方案" | ✅ **成立** |
| "业界**做不到**" | ❌ **不成立** —— ChatGPT Advanced Voice / Gemini Live 在 MacBook 免提下可用 |
| "**Python 栈必须自带 AEC**"（他 §9.5 第 3 条） | ✅ **成立**，且是正解 |

**⚠️ 而这条更正改变排期**：能让 ChatGPT/Gemini 免提工作的，正是**平台自带的 AEC**（他 §9.2 表 ①：苹果 `setPrefersEchoCancelledInput`、`mode: .voiceChat`、Android `AcousticEchoCanceler`、浏览器 AEC3）。
→ **你的对应物就是 VPIO（路径 C）。**

**他的路径 C 现状**：排在"仅当 pywebrtc 失败且想要系统级 AEC 时再投半天"。

**建议提升为与路径 A 并列**，理由：
- 路径 C 是**已被产品验证过的机制**（ChatGPT/Gemini 用它）
- 路径 A 是"理论可行、ERLE 未知"（他自己标 🔴）
- 他 §9.5 第 2 条其实已经写出了这个洞察（"AEC 的归属被确认：业界把 AEC 交给客户端平台……这独立支持了路径 C 值得一试"）—— **但 §4 决策树仍把 C 排在最后，两处不一致**

⚠️ 公平提示 VPIO 的两个已知代价（他旧文已录）：
- **工程代价**：无纯 Python 封装，需 Swift/ObjC/C++ helper
- **负面先例**：Fora Soft（2025）报 M1 Max 下**前 3–6 秒音频被静默丢弃**，最终放弃 VPIO
- 他 P1 已列"先花半天验证丢音是否在本机复现" ✅

**⚠️ 仍需核实**：ChatGPT 桌面端是否真用 VPIO（🔴 未验证）—— Apple 文档说 voice processing 路由自带 AEC，但**具体 app 是否走该路由没有直接证据**。

---

## 5. 是否社区热门？（回答"是否热门"）

**半热 —— 但不是他想象的那种热门。**

| 信号 | 读数 |
|---|---|
| **pywebrtc-audio 背后是 `strands-labs`（AWS Strands Agents）** | 🟢 有云厂商推动，且有 agent 场景的明确动机 |
| Chromium AEC3（该包 vendored 的算法） | 🟢 "cut echo incidents by roughly **80%** after its 2019 rollout"（getstream.io，2026-08-07）→ 算法经大规模验证 |
| Pipecat 同类 issue #188「Interrupted by itself when speaker on」 | 🟢 官方回复是"**this is controlled by the transport. it handles audio loopback**" → **框架层的答案是交给传输/平台层** |
| Soniox Voice AI Wiki | 🟢 "acoustic echo cancellation (AEC) is **required** in a full-duplex agent" |
| 同类开源 issue `ksg98/fastaf-ide#7`（2026-08-05，open） | 🟢 有人正在踩同一个坑，且诊断与我们一致（acoustic feedback path，非 VAD 敏感度） |

**结论**：路径 A 走的是**成熟的工业算法 + 活跃的 Python 绑定 + 云厂商背景** —— 不是冷门技术。**但"社区热门方案"的主流答案不是它，而是"用平台 AEC"或"戴耳机"。** 所以你在做的是一件**主流绕开、但有真实需求**的事。

---

## 6. 是否前沿？（回答"是否前沿"）

**不是前沿研究，但这是优点。**

- **AEC 本身是成熟工程领域**（ITU G.168 等标准），不是前沿
- **前沿在神经网络 AEC**，但他旧文已列且已核实"权重未释出"：
  - E2E-AEC（arXiv 2601.16774，ICASSP 2026，通义）—— 无需 LAEC、支持流式
  - LAEC+RES（arXiv 2508.07561，阿里 2025-08）
  - OVC（arXiv 2606.23332，Interspeech 2026）
- **ICASSP AEC Challenge 状态**：他旧文判断"仓库最后提交 2023-10-05，很可能没有 2024–2026 届"。**我未找到反证**（🟢 检索到的仍是 ICASSP 2023 挑战论文）→ 他的判断维持
- **理论侧**：非线性残余回声是公认难题 —— Microsoft 的 RES 论文（IWAENC'05）存在的理由就是"线性 AEC 留下的残余需要额外的非线性回归/神经网络"（+7dB）。**他 §0 第 3 条"结构传声是非线性的，线性 AEC 建模不了"判断正确。**

**结论**：他的方案是**「用成熟工业算法解决一个未被 Python 栈解决的工程问题」** —— **这恰恰是能落地的路径**，不是前沿研究。定位准确。

---

## 7. 能落地吗？（回答"是否能落地"）

**能，且工程形态已经完备。** 逐项：

| 落地要素 | 评估 |
|---|---|
| 依赖 | ✅ 零新增（scipy/numpy 已在） |
| 改动面 | ✅ 5 文件、1–2 天，且 `audio_mode` 派生属性不变（单一事实来源保留） |
| P0 可离线做 | ✅ `tests/test_aec_erle.py` 独立脚本、不碰主链路 —— **符合项目"tests 是独立脚本"惯例** |
| 有判定线 | ✅ ERLE ≥15dB 接入 / 10–15dB 加防线 / <10dB 判死刑 |
| 有决策树 | ✅ 含硬件路线与维持现状的兜底 |
| 有未验证清单 | ✅ 6 条，标了等级 |
| **针对真问题** | ✅ 三层防线直指"SenseVoice 幻觉"这个**他亲测过**的已知行为 |

**主要风险（他自标 🔴，我逐条认可）**：

| 风险 | 我的评估 |
|---|---|
| ERLE 天花板未知（结构传声非线性） | ✅ 判断正确。非线性残余是公认难题 |
| **AEC 残余喂 SenseVoice 幻觉** | ✅ **这是计划里最有价值的认知** —— "AEC 做得『还行』反而可能比半双工更吵" |
| `stream_delay_ms` 最优值未知 | ✅ 设计里扫三档，合理 |
| VPIO 丢音 | ✅ 已列为待验证 |

**⚠️ 他的计划漏了一条风险（我发现的）**：见 §8 第 3 行 —— **AEC 模式会让打断检测首次在播放期间连续运行。**

---

## 8. 与升级计划的三处撞车 + 三处联动

| # | 升级计划 | AEC 计划 | 裁定 |
|---|---|---|---|
| 1 | §1.4(c) `_hd_gated` 看门狗 | `half_duplex = (mode == "speaker")` → **AEC 模式下该分支不走** | **条件性失效**。仍保留给 `speaker` 模式；若他主用 `speaker_aec`，优先级下降 |
| 2 | §2.3 预滚缓冲会**稀释整段 RMS** | §2.3 要在播放期把 `vad_min_snr` 从 3.0 **提到 6.0** | **⚠️ 复合冲突** —— 稀释 + 门限翻倍 = **播放期间真人说话被丢的概率大增**。**必须专测「播放中说话」这一格** |
| 3 | §4.1 `Player` pause/resume | §2.1 far 参考 = `Player.write()` mirror；`stream_delay_ms` 用 `player.buffered_seconds()` 推算 | **必须一起设计** —— pause 会破坏 `buffered_seconds()` 对 AEC 延时估计的语义 |
| 4 | P0-A 候选 3（`reopen()` 未重启流） | 新增第三档模式 → `set_mode()` 调用增多 | **相关性上升**（不是冲突） |
| 5 | §1.5 `/api/state` 观测 | 新增 AEC 层 | **要加 AEC 字段**：`speech_probability`、far 缓冲深度、ERLE 估计 |
| 6 | §1.4(b)「reset 不得归零 `turn_id`」 | `set_mode()`（`orchestrator.py:397-405`）目前**不调 `interrupt()`** | **新模式切换路径也要守这条** |

### 8.1 ⚠️ 计划漏掉的那条风险：打断检测首次在播放期连续运行

AEC 计划 §2.2 写：「`orchestrator.py` …… **打断检测不动（barge_in 已为 True）**」。

**但这句话忽略了一件事实**（🟢 读代码）：

```python
# orchestrator.py:257-261  半双工门控 —— 在 speaker 模式下，播放期间直接 continue
if self.cfg.half_duplex and self.player.is_playing():
    if not self._hd_gated:
        self._hd_gated = True
        self.vad.reset()
    continue                      # ← 整段跳过，包括下面的打断检测
```

**现状**：`speaker` 模式（`half_duplex=True`）下，**播放期间主循环 `continue`，打断检测从未在播放期间运行过**。`headphones` 模式虽然 `barge_in=True`，但那时麦克风听不到自己，等于没有真实回声条件。

**AEC 模式**（`half_duplex=False`、`barge_in=True`）→ **打断检测将首次在「真实播放 + 麦克风能听到自己」的条件下连续运行。**

**这是一条从未被测试过的代码路径**，而它正好要处理最难的情况（残余回声 vs 真打断）。他 §3 的 P0 脚本是**离线**的，**覆盖不到这条**。

→ **建议**：Step3 真机验收（他写的"外放免提、说『停一下』、播报中插话 ×20 次"）**必须显式包含"播放中不说话"这一格**（只测残余回声是否触发假打断），否则测不出 §8 第 2 行的复合冲突。**他的验收设计里目前没有这一格。**

---

## 9. 排期建议

沿用他自己 §4 的纪律（"避免同窗口改两处音频路径"）：

```
1. Phase 0 观测（升级计划）—— 同时服务二者；AEC 会引入新故障面，需要同样的遥测
2. P0-C 注入隔离（升级计划）—— 完全独立，随时可做
3. AEC P0 半天实测（他的；离线脚本，不碰主链路）★ 且补「安静 / 真实桌面」两组
4. P0-A 卡死修复（升级计划）—— 必须在 AEC Step2 之前（同一段音频路径，先修再加层）
5. AEC Step2 接入（他的）
6. P0-B 预滚（升级计划）—— 放最后：SNR 交互必须在 barge_in 打开的 speaker_aec 模式下测
```

**关于 §4 的路径 C 提升**：建议把 VPIO 的"半天验证丢音"**与 AEC P0 并行做**（都是半天、都是音频实验），别等到 pywebrtc 失败才动 —— 因为 VPIO 是**产品验证过的机制**，值得早拿数据。

---

## 10. 未验证清单（本文新增）

- 🔴 **AEC 模式下打断检测连续运行** —— 从未在半双工期跑过的代码路径（§8.1）
- 🔴 **真实桌面噪声下的 ERLE** —— 文献：噪声可致 30dB → 4–8dB，静音室的数不能外推（§3）
- 🔴 **预滚缓冲 + `vad_min_snr=6.0` 的叠加效应** —— 必须专测"播放中说话"（§8 第 2 行）
- 🔴 **pywebrtc-audio 的许可证** —— PyPI metadata 无 license 字段，需查仓库 LICENSE 文件
- 🔴 **ChatGPT/Gemini 桌面端是否真用 VPIO** —— Apple 文档说 voice processing 路由自带 AEC，但具体 app 是否走该路由无直接证据
- 🔴 **`Player.pause()/resume()` 与 AEC 延时估计的交互** —— 尚未设计（§8 第 3 行）

---

## 11. 来源（§1–§10 的核实依据）

**一手（直查）**
- PyPI JSON API `pywebrtc-audio`（版本/上传日/轮子清单/requires_python，2026-09-19）
- `github.com/strands-labs/pywebrtc-audio`（vendor 路径 `vendor/webrtc_audio/audio_processing/aec3`）
- Owen 本机 `.venv` 实测（python 3.14.7 / scipy 1.18.1 / numpy 2.5.3 / pywebrtc_audio 未装）
- `github.com/ksg98/fastaf-ide` issue **#7**（2026-08-05，open）
- `github.com/pipecat-ai/pipecat` issue **#188**
- ICASSP 2023 AEC Challenge 论文 arXiv **2309.12553**（ERLE 适用条件 + 与主观相关性表）
- Zhang et al., Interspeech 2018（ERLE 表 1/2：安静 vs 10dB SNR）
- Microsoft Research, IWAENC'05（RES，+7dB 非线性残余抑制）
- iieta.org（ITU G.131 的 >40dB 要求）
- getstream.io《A Developer's Guide to Echo Cancellation》（2026-08-07）
- soniox.com Voice AI Wiki › Turn-taking and barge-in

**项目内**
- `RESEARCH-AEC-20260919.md`（被核对象）
- `RESEARCH-BARGEIN-20260916.md`（被核对象）
- `RESEARCH-UPGRADE-PLAN-20260919.md`（冲突核对对象）

---

## 12. 🔬 物理边界：ACOM = ERL + ERLE（这是判「能不能达到效果」的真正框架）

**AEC 计划只测 ERLE —— 但决定效果的是 `ACOM`，它由两个量相加。** 一手来源：

**EE Times《Echo Cancellation Part 1》**（工程刊物）：
> ERL measures receive-out signal loss when it is reflected back as echo within the send-in signal. For line EC's the ERL of the echo path should be above **6dB** according to ITU's specifications. **For acoustic ECs, the ERL could be as bad as –12dB.**

**Wikipedia（Echo suppression and cancellation）**：
> Most echo cancellers are able to apply **18 to 35 dB ERLE**.
> The total signal loss of the echo (**ACOM**) is the sum of the **ERL and ERLE**.

**⚠️ 负的 ERL 意味着回声比参考信号还响**（麦离扬声器比"参考点"更近）。这是笔记本声学的固有属性。

### 12.1 边界账

| 量 | 性质 | 值 | 谁决定 |
|---|---|---|---|
| **ERL**（本机回声路径损耗） | **物理固有，他改不了** | 声学 AEC 可能差到 **–12dB** | MacBook 的声学设计 |
| **ERLE**（AEC 额外抑制） | 算法能力 | 典型 **18–35dB**；**专用 DSP 可达 54dB** | 选哪个 AEC |
| **ACOM = ERL + ERLE** | **残余回声的响度** | 相加 | — |

**Microchip《Speaker Design Considerations for AEC》**（厂商工程文档）：
> In the real-world, speaker systems output some **non-linear distortion** along with the linear signal which causes **non-linear echo** to appear on the microphone. **Since there is no reference signal for the non-linear echo, the residual output of the AEC to the far-end contains amounts of non-linear distortion.**
> The DSP/measurement noise limit for Timberwolf AECs is about **54dB of ERLE**

→ 🟢 **独立证实他 §0 第 3 条的判断**（"结构传声是非线性的，线性 AEC 建模不了"）—— 而且说明了原因：**非线性回声没有参考信号可减**。
→ 🟢 **也是他路径 B（USB 会议麦）的量化论据**：专用 AEC DSP 的 ERLE 天花板（54dB）远高于笔记本上能拿到的。

### 12.2 ⚠️ 他的计划漏了一个极廉价的测量：ERL

**ERL 可以在不装任何 AEC 的前提下直接测** —— 播一段已知信号、录回来、比能量、不接 AEC。**它给出的是天花板，不是结果。**

**为什么必须先测它**：
1. 如果本机 ERL 是 –12dB，那么即使 ERLE 拿到 30dB，ACOM 也只有 18dB → **残余可能就在你 VAD 门限附近**，成败取决于细节
2. ERL 是**设备属性**，可迁移 —— 换个机型/换 USB 麦，这个数就换了。**它比 ERLE 更能指导"该不该买硬件"**

**→ 建议 P0 脚本加一步：先测 ERL，再测 ERLE。** 两行代码的差别，但**把判决从"事后"变成"事前"**。

---

## 13. 🔬 双讲边界：打断场景恰好是 AEC 最弱的一环

**barge-in 在信号层面就是 double-talk**（远端在播 + 近端在说）。而双讲是 AEC 最难的部分。

**ICASSP 2023 / 2022 Challenge 的剩余改进空间表**（一手，arXiv 2309.12553）：

| Area | Headroom（还剩多少没解决） |
|---|---|
| Single talk near end MOS | 0.60 |
| Single talk far end MOS | 0.19 |
| **Double talk echo** | 0.25 |
| **Double talk other** | **0.57 ← 最大** |
| WAcc | 0.19 |

论文对 "Double Talk Other" 的定义：
> **Double Talk Other Degradations, which includes missing audio, distortions, and cut-outs.**

**→ 双讲期的"其他劣化"（丢音、失真、截断）是整张表里剩余空间最大的一项。** 也就是说：**AEC 领域的短板，正好落在打断发生的那一刻。**

另外两条同源事实：
- **Near-end 语音会破坏自适应滤波器的收敛，甚至导致发散**（Zhang et al., Interspeech 2018）→ 这是 DTD（双讲检测）存在的理由：双讲期**冻结自适应**
- ⚠️ **但冻结意味着 AEC 恰好在你最需要它的时候停止改进**

### 13.1 ⚠️ 判据必须重定义：不是 ERLE，是「双方向分开测」

ICASSP 2023 明说 **ERLE 只适用于单讲**。**而双讲的正确度量是成对的**：

**Interspeech 2022（Ivry et al.）**：
> the stereo SDR **poorly correlates with subjective human ratings** … We introduce a **pair of objective metrics that distinctly assess** the stereo **desired-speech maintained level (SDSML)** and **residual-echo suppression level (SRESL)** during double-talk.

**现代标准是 AECMOS**（"achieved high accuracy in predicting human perception during double-talk"）。

**→ 对他的直接含义**：他的"双讲保真"测试**方向对，但没有指标和阈值**。应改成**两个方向分开测**：
| 方向 | 问题 | 为什么必须分开 |
|---|---|---|
| **保真** | 我的语音还完整吗？ | 消得越狠，越可能把用户也消掉 |
| **抑制** | 残余还越得过门限吗？ | 抑制不够 → 假打断 |

**用一个合起来的数字会掩盖这个权衡**（消得狠则保真差，消得松则残余大）—— 这正是 Interspeech 2022 那篇的论点。

---

## 14. 🎯 结论：能否达到他要的效果

### 14.1 分层判决

| 目标 | 物理/算法边界 | 判决 |
|---|---|---|
| 单讲期消掉自己的回声 | ACOM = ERL + ERLE，典型够 | ✅ **大概率可达** |
| **双讲期（插话那一刻）既保真又消干净** | **AEC 领域最弱环节**（headroom 0.57） | ⚠️ **部分可达，取决于本机 ERL** |
| 通话级的双讲音质 | 需要 40dB（ITU G.131）+ | ❌ **大概率不可达**，也不需要 |

### 14.2 🎯 关键洞察：他的门槛远低于通话级

**他的验收标准不是"双讲期用户语音的通话音质"，而是「残余回声不足以致命」。** 具体只有两件事：

1. **残余不越过 VAD / SNR 门限** → 不触发假打断
2. **他的语音仍能被 SenseVoice 转写出来** → 不丢话

**这比"远端听得舒服"低得多。** 通话级 AEC 要的是 P.808 MOS 达标；他要的是**二值判断**（越不越门限）。

→ **所以答案不是"能不能"，而是"用哪个判据"**：
- 用 **ERLE ≥15dB** 判 → 可能判死（因为 ERLE 在双讲下无效，且噪声能打掉它）
- 用 **"双讲期残余是否触发假打断 + 他的话是否仍被转写"** 判 → **这才是他真正在意的，且门槛低得多**

### 14.3 重定义后的 P0 验收（四条，替代单条 ERLE）

| # | 测什么 | 判据 |
|---|---|---|
| **1** | **ERL**（本机回声路径损耗，不接 AEC） | 只记录，用作后续解释与设备对比的基线 |
| **2** | **单讲 ERLE**（安静 / 真实桌面两组） | ≥15dB 参考；**真实桌面组为准** |
| **3** | **双讲保真**（播放中念一句） | 转写**基本完整**（不缺字）；**单独记，不合并** |
| **4** | **双讲残余 vs 门限**（★ 新增，我上一轮发现的漏洞格） | 播放中**不说话**，残余**不触发** VAD/SNR 门限。**这是判"假打断"的唯一直接证据** |

**第 4 条是新增且最关键的一条** —— 它直接对应 §13 的领域短板，且他的原设计没有。

### 14.4 最诚实的总结

**能不能达到效果，取决于一个他现在还没有的数字**：本机 ERL。

- 若 ERL ≥ 0dB（回声已被机身隔离得比参考弱）→ ACOM 有希望进 25–35dB 区间 → **大概率成**
- 若 ERL 接近 –12dB → 即使 ERLE 拿到 30dB，ACOM 也只有 18dB → **成败取决于 VAD 门限与噪声底**，需要 §14.3 的四条才判得出

**而这个数字可以在半小时内测出来，不需要装任何 AEC。** 见 §12.2。

---

## 15. 社区结论（综合三份调研）

| 问题 | 答案 |
|---|---|
| **社区热门吗** | 🟡 **半热。** 成熟工业算法 + AWS 背景的活跃 Python 绑定；但**主流答案是"用平台 AEC"或"戴耳机"**，自建 Python 栈属少数派 |
| **前沿吗** | ❌ **不是前沿，但这是优点** —— 能用工业算法解决一个未被 Python 栈解决的工程问题，正是可落地的路径 |
| **有人在做吗** | ✅ 有，且诊断一致：`ksg98/fastaf-ide#7`（2026-08-05, open）、`pipecat-ai/pipecat#188` |
| **框架层答案** | Pipecat 官方回复："**this is controlled by the transport. it handles audio loopback**" → 交给平台/传输层 |
| **产品级反证** | ChatGPT Advanced Voice / Gemini Live 在 MacBook 免提下**可用** → 靠**平台 AEC**（§4） |
| **唯一没有量化数据的选项** | 🔴 **VPIO 的实测 AEC 质量 —— 全球范围查不到任何量化报告**（我本轮专项搜 macOS VPIO 实测，**零结果**）。而他 §8 已列同一条。**这是整张图里最大的未知数，且它恰好是产品验证过的路径** |

---

## 16. 本文新增的未验证项（补 §10）

- 🔴 **本机 ERL** —— 半小时可测，且是判决"能不能达到效果"的**前置数字**（§12.2）
- 🔴 **双讲残余是否越过 VAD/SNR 门限** —— §14.3 第 4 条，原设计缺失
- 🔴 **双讲保真的分向度量** —— 应拆成"保真"与"抑制"两个数，而非合成一个（§13.1）
- 🔴 **macOS VPIO 的实测 AEC 质量** —— 本轮专项检索**零结果**，全球无量化报告（§15）

---

## 17. 补充来源（本轮）

- **EE Times**《Echo Cancellation Part 1: The Basics and Acoustic Echo Cancellation》—— 声学 ERL 可差到 **–12dB**；线路 EC 需 >6dB
- **Wikipedia**「Echo suppression and cancellation」—— **ERLE 典型 18–35dB**；**ACOM = ERL + ERLE**
- **Microchip**《Speaker Design Considerations for AEC》—— **非线性回声无参考信号可减**；专用 AEC DSP **ERLE 54dB**
- **ICASSP 2023 AEC Challenge**（arXiv 2309.12553）—— headroom 表；**"Double Talk Other" = 0.57 最大**；ERLE 仅适用单讲
- **Interspeech 2022**（Ivry et al.）—— 双讲需**成对指标**（SDSML / SRESL），单数（SDR）与主观相关性差；**AECMOS** 是现代标准
- **Zhang et al., Interspeech 2018** —— 近端语音致自适应滤波器发散；NLMS ERLE 30–35dB（安静）→ 4–8dB（10dB SNR）
- **Dialogic** glossary（ERL 定义，明确需先估 ERL 才能分解衰减来源）

*本文写于 2026-09-19。§12–§14 是对 AEC 计划的物理边界分析与验收判据重定义；§15 是综合结论。*

- PyPI JSON API `pywebrtc-audio`（版本/上传日/轮子清单/requires_python，2026-09-19）
- `github.com/strands-labs/pywebrtc-audio`（vendor 路径 `vendor/webrtc_audio/audio_processing/aec3`）
- Owen 本机 `.venv` 实测（python 3.14.7 / scipy 1.18.1 / numpy 2.5.3 / pywebrtc_audio 未装）
- `github.com/ksg98/fastaf-ide` issue **#7**（2026-08-05，open）
- `github.com/pipecat-ai/pipecat` issue **#188**
- ICASSP 2023 AEC Challenge 论文 arXiv **2309.12553**（ERLE 适用条件 + 与主观相关性表）
- Zhang et al., Interspeech 2018（ERLE 表 1/2：安静 vs 10dB SNR）
- Microsoft Research, IWAENC'05（RES，+7dB 非线性残余抑制）
- iieta.org（ITU G.131 的 >40dB 要求）
- getstream.io《A Developer's Guide to Echo Cancellation》（2026-08-07）
- soniox.com Voice AI Wiki › Turn-taking and barge-in

**项目内**
- `RESEARCH-AEC-20260919.md`（被核对象）
- `RESEARCH-BARGEIN-20260916.md`（被核对象）
- `RESEARCH-UPGRADE-PLAN-20260919.md`（冲突核对对象）

*本文写于 2026-09-19。§2–§4 是对 AEC 计划的三处更正；§8 是两计划的冲突矩阵。*
