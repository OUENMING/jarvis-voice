# tools/webrtc-aec —— 自己编新版 WebRTC AEC3（含两处补丁）

**为什么要自己编**：项目用的 `pywebrtc-audio` 0.2.0 里的 AEC3 是
**`ewan-xu/AEC3` 2020 年的快照**（证据：`vendor/webrtc_audio/MODIFICATIONS.md` +
`AdjustConfig()` 签名不带 `FieldTrialsView`）。而 WebRTC 官方承认过一个正是我们症状的缺陷
（issue **442444736**，P1/Fixed，2025-09）：「残余抑制**常高估回声 → 过度压制近端、
transparency 差**」。官方修法（ML-REE）**不在** `webrtc-audio-processing` 的打包里，
但**抑制器透明度旋钮**在源码里 —— 只是 `pywebrtc-audio` 不暴露。

⇒ 自己编一份，才能动那两个旋钮。

---

## 一、构建（macOS arm64 实测可过）

```bash
cd /tmp && git clone --depth 1 --branch v2.1 \
    https://gitlab.freedesktop.org/pulseaudio/webrtc-audio-processing.git wap
cd wap
git apply /path/to/tools/webrtc-aec/patches/two-fixes.patch   # 见下面「两处补丁」

pip install meson ninja                                        # 本机没有
PATH="$PWD/../venv/bin:$PATH" meson setup build --buildtype=release
PATH="$PWD/../venv/bin:$PATH" ninja -C build                   # 约 3–5 分钟
# 产物：build/webrtc/modules/audio_processing/libwebrtc-audio-processing-2.1.dylib
```

编译 wrapper：

```bash
clang++ -std=c++17 -O2 -fPIC -shared -o libwla.dylib wla.cc \
  -I"$WAP" -I"$WAP/webrtc" -I"$WAP/subprojects/abseil-cpp-20240722.0" \
  -L"$WAP/build/webrtc/modules/audio_processing" -lwebrtc-audio-processing-2.1 \
  -Wl,-rpath,"$WAP/build/webrtc/modules/audio_processing"
```

⚠️ **include 路径要三个根**：`$WAP`（`webrtc/api/...`）、`$WAP/webrtc`（头文件内部按
`api/array_view.h` 这种相对路径互相引用）、abseil 子项目。

## 二、两处补丁

### 1. `audio_processing_impl.cc` —— 修上游 bug（**必需**，否则段错误）

`AudioProcessing::Config::echo_canceller.export_linear_aec_output` 从没被写进它真正控制的
`EchoCanceller3Config.filter.export_linear_aec_output`。后果：线性输出缓冲被分配了、
AEC3 却不往里写 → **`ProcessStream` 段错误**（`EXC_BAD_ACCESS` @ `BlockFramer`，空指针）。

```cpp
config.filter.export_linear_aec_output =
    config_.echo_canceller.export_linear_aec_output;
```

**这是上游 v2.1 的真 bug**（那个公开 flag 一开就崩），我们的补丁是把它接上。

### 2. `echo_canceller3_config.h` —— 放松高频掩蔽阈值（**待验证**）

上游 main 有字段试用 `WebRTC-Aec3EnforceMoreTransparentNormalSuppressorHfTuning`，
就是把 `normal_tuning` 的**高频**阈值从 `(.07,.1)` 放宽到 `(.3,.4)`。我们的构建里
没有那个字段试用（8 个不相关），所以直接改默认值。

机制（`suppression_gain.cc:202`，源码核实）：
```cpp
float enr = echo[k] / (nearend[k] + 1.f);
float g = 1.0f;                                   // 1.0 = 完全透明
if (enr > enr_transparent && emr > emr_transparent)
  g = (enr_suppress - enr) / (enr_suppress - enr_transparent);
```
⇒ **阈值越高 → 透明区越大 → 近端越少被压。** 默认高频阈值比低频**低 4 倍** ⇒
辅音（全在高频）先被压 —— **与我们实测的症状同型**。

## 三、⚠️ 三个使用陷阱（都踩过）

1. **`Initialize()` 必须在 `ApplyConfig()` 之前。** 反过来的话 `Initialize` 会把刚设的
   配置**重置掉** → AEC 根本没启用 → `ProcessStream` 原样返回输入。
   **现象是"输出 == 输入"，极易误判成"这个方案没用"**（我因此得出过一个错误结论）。
2. **`GetLinearAecOutput()` 拿到的不是"已消回声"的信号。** 本 build 实测：
   输入 RMS 0.141 → 线性输出 0.140（**几乎原样**），而全链输出 0.026。
   ⇒ **「绕开抑制器、只取线性输出」这条路走不通** —— 抑制器在做必需的活。
3. `ApplyConfig` 返回 `void`（不是 int）；`Initialize()` 无参重载会用已设配置。

## 四、目前的实测结论（合成场景，4 段语音两两配对）

| 变体 | ASR 字符错误率 |
|---|---|
| raw（不过 AEC） | 359% |
| 2020 快照全链（生产现行） | **90.7%** |
| v2.1 全链 | 88.9% |
| v2.1 全链 **+ 高频阈值补丁** | **90.7%（无区别）** |

⚠️ **合成场景判不了这个补丁** —— 它没有房间混响，掩蔽阈值不是瓶颈。
**要判它，必须用真机双讲协议**（`tools/measure_echo_delay.py --double-talk --repeat 3`，
现有基线 **7.4% → 23.6%，3.19×**）。

wrapper 的 C 接口（`wla.cc`）：
```c
void* wla_create(int sample_rate, int channels, int use_linear);  // use_linear: 1=线性抽头 0=全链
int   wla_process(void*, const int16_t* near, const int16_t* far, int16_t* out, int n);
void  wla_reset(void*); void wla_destroy(void*); int wla_linear_enabled(void*);
```
`n` 必须是 160 的整数倍（10ms@16k）。
