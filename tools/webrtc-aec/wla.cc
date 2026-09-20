// wla — WebRTC Linear AEC 的最小 C 封装，给 Python ctypes 用。
//
// **为什么存在**：WebRTC AEC3 的非线性残余抑制器会在双讲时压坏近端语音
// （实测：同一段话安静念 ASR 错 7.4%，边放回声边念 23.6%，3.19×）。
// 官方上游承认这是缺陷（issue 442444736，P1/Fixed），修法是 ML-REE；
// **但我们走的更直接**：`export_linear_aec_output` 是公共 API，
// 直接取「自适应滤波器之后、非线性抑制器**之前**」的那一路信号 ——
// 保留实测 43 dB 的线性消回声能力，**完全不碰近端**。
// 残余回声交给已有的文本层护栏（`echoguard`）。
//
// 接口刻意做小：创建 / 处理 / 重置 / 销毁。处理函数收任意长度（160 的整数倍），
// 内部按 10ms 帧循环 —— 与 `AecGate` 现有后端的签名对齐，Python 侧无感。

#include <cstdint>
#include <cstring>
#include <array>
#include <memory>
#include <vector>

#include "webrtc/api/audio/audio_processing.h"
#include "api/array_view.h"

namespace {

constexpr int kFrame = 160;  // 10 ms @ 16 kHz —— WebRTC AP 的硬性帧长

struct Handle {
  rtc::scoped_refptr<webrtc::AudioProcessing> ap;
  int sample_rate = 16000;
  int channels = 1;
  std::vector<std::array<float, kFrame>> linear;   // GetLinearAecOutput 的落点
  std::vector<float> in_buf, out_buf;              // 去交织缓冲（单声道各一份）
  bool enabled_ok = false;
  bool use_linear = true;
};

}  // namespace

extern "C" {

// 建实例。`enable_aec` 恒为真；这里只暴露采样率与声道，避免调用方配错。
// `use_linear`: 1 = 取抑制器**之前**的线性输出；0 = 取抑制器之后（全链）。
void* wla_create(int sample_rate, int channels, int use_linear) {
  auto h = new Handle();
  h->sample_rate = sample_rate;
  h->channels = channels;

  webrtc::AudioProcessing::Config cfg;
  cfg.echo_canceller.enabled = true;
  // ⚠️ 本封装的**全部意义**在这一行：要「抑制器之前」的线性输出。
  cfg.echo_canceller.export_linear_aec_output = (use_linear != 0);
  h->use_linear = (use_linear != 0);
  // 其余一律关 —— 我们要的是纯净的线性 AEC 输出，不是"好听"的信号。
  // NS / HPF / AGC 都会改变近端，而近端正是我们唯一想保住的东西。
  cfg.noise_suppression.enabled = false;
  cfg.high_pass_filter.enabled = false;
  cfg.gain_controller1.enabled = false;
  cfg.gain_controller2.enabled = false;

  h->ap = webrtc::AudioProcessingBuilder().Create();
  if (!h->ap) { delete h; return nullptr; }
  // ⚠️⚠️ **顺序不能反**：必须 `Initialize` **之后**再 `ApplyConfig`。
  // 反过来的话 `Initialize` 会把刚设的配置**重置掉** → AEC 根本没启用 →
  // `ProcessStream` 原样返回输入（现象是"输出 == 输入"），
  // 而且 `GetLinearAecOutput` 仍返回 true，**很容易误判成"线性输出没用"**。
  //（第一版就是这么错的，白白得出一个错误结论。）
  webrtc::ProcessingConfig pc;
  pc.input_stream()          = webrtc::StreamConfig(sample_rate, channels);
  pc.output_stream()         = webrtc::StreamConfig(sample_rate, channels);
  pc.reverse_input_stream()  = webrtc::StreamConfig(sample_rate, channels);
  pc.reverse_output_stream() = webrtc::StreamConfig(sample_rate, channels);
  if (h->ap->Initialize(pc) != 0) { delete h; return nullptr; }
  h->ap->ApplyConfig(cfg);
  h->enabled_ok = true;

  h->linear.resize(channels);
  h->in_buf.assign(static_cast<size_t>(channels) * kFrame, 0.0f);
  h->out_buf.assign(static_cast<size_t>(channels) * kFrame, 0.0f);
  return h;
}

void wla_destroy(void* p) { delete static_cast<Handle*>(p); }

// 是否真的拿到了线性输出（建好后调一次，用于自检）。
int wla_linear_enabled(void* p) {
  auto h = static_cast<Handle*>(p);
  return (h && h->enabled_ok) ? 1 : 0;
}

// 处理 `n` 个样本（必须是 160 的整数倍）。near/far/out 都是 int16 单声道。
// far 为 NULL 时按**静音参考**处理（等价于"没在播"）。
// 返回 0 成功，非 0 失败。
int wla_process(void* p, const int16_t* near, const int16_t* far, int16_t* out, int n) {
  auto h = static_cast<Handle*>(p);
  if (!h || !near || !out || n <= 0 || (n % kFrame) != 0) return 1;

  webrtc::StreamConfig sc(h->sample_rate, h->channels);
  for (int off = 0; off < n; off += kFrame) {
    // int16 → float，单声道去交织视图
    for (int c = 0; c < h->channels; ++c) {
      float* dst = &h->in_buf[static_cast<size_t>(c) * kFrame];
      const int16_t* src = near + off;
      for (int i = 0; i < kFrame; ++i) {
        dst[i] = static_cast<float>(src[i * h->channels + c]) / 32768.0f;
      }
    }
    if (far) {
      std::vector<float> farf(static_cast<size_t>(h->channels) * kFrame);
      for (int c = 0; c < h->channels; ++c) {
        float* dst = &farf[static_cast<size_t>(c) * kFrame];
        for (int i = 0; i < kFrame; ++i) {
          dst[i] = static_cast<float>(far[off + i * h->channels + c]) / 32768.0f;
        }
      }
      const float* farp[8] = {nullptr};
      for (int c = 0; c < h->channels; ++c) farp[c] = &farf[static_cast<size_t>(c) * kFrame];
      h->ap->AnalyzeReverseStream(farp, sc);
    }

    const float* inp[8] = {nullptr};
    float* outp[8] = {nullptr};
    for (int c = 0; c < h->channels; ++c) {
      inp[c] = &h->in_buf[static_cast<size_t>(c) * kFrame];
      outp[c] = &h->out_buf[static_cast<size_t>(c) * kFrame];
    }
    if (h->ap->ProcessStream(inp, sc, sc, outp) != 0) return 2;

    // 取线性输出（抑制器之前）。⚠️ 必须紧接本次 ProcessStream 调用 ——
    // 它返回的是"最近产出的 10ms"。
    if (!h->use_linear) {                       // 全链模式：直接用处理后的输出
      for (int c = 0; c < h->channels; ++c)
        for (int i = 0; i < kFrame; ++i)
          h->linear[c][i] = h->out_buf[static_cast<size_t>(c) * kFrame + i];
    } else if (!h->ap->GetLinearAecOutput(h->linear)) {
      // 拿不到就退回处理后的输出（不应发生；发生了要有痕迹）
      for (int c = 0; c < h->channels; ++c) {
        for (int i = 0; i < kFrame; ++i) {
          h->linear[c][i] = h->out_buf[static_cast<size_t>(c) * kFrame + i];
        }
      }
    }
    for (int c = 0; c < h->channels; ++c) {
      for (int i = 0; i < kFrame; ++i) {
        float v = h->linear[c][i];
        if (v > 0.9999f) v = 0.9999f;
        if (v < -1.0f) v = -1.0f;
        out[off + i * h->channels + c] = static_cast<int16_t>(v * 32767.0f);
      }
    }
  }
  return 0;
}

void wla_reset(void* p) {
  auto h = static_cast<Handle*>(p);
  // v2.1 的 Initialize() 无参重载会用已 ApplyConfig 的配置重建内部状态。
  if (h && h->ap) h->ap->Initialize();   // 已 ApplyConfig，重建内部状态即可
}

}  // extern "C"
