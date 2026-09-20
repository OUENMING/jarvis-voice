#!/usr/bin/env python3
"""VAD 噪声底回归测试。

针对 `ocr` 代码审查（2026-09-20）报的 high 级缺陷：
  原 `_update_floor` 的 `if self._floor == 0.0 or rms < self._floor` 里，
  `_floor == 0.0` 同时表示"未初始化"和"实测为 0" → **第一块**（很可能整块就是
  启动时主人正在说的语音）直接把 floor 设成语音电平 → `need = 3×语音` →
  **连续说话时所有段都被丢，助手不响应**。

⚠️ 用例 1/2 在修复前**必须失败**、修复后通过（red-green）。
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis_voice.config import Config          # noqa: E402
from jarvis_voice.vad import VadGate            # noqa: E402

FAIL = []


def check(label, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {label}: got={got} want={want}")
    if not ok:
        FAIL.append(label)


def const(v, n=1600):
    """恒定幅度数组 —— RMS 恰为 v。"""
    return np.full(n, v, dtype=np.float32)


cfg = Config.load()
print(f"  vad_min_rms={cfg.vad_min_rms}  vad_min_snr={cfg.vad_min_snr}")
print()

# ---- 1）首块即语音 → 不能锁死门限（旧代码在这里失败）----
g = VadGate(cfg)
g._update_floor(const(0.15))          # 首块 RMS=0.15（典型语音电平）
speech = const(0.05)                  # 之后的正常语音 RMS=0.05
# 旧：floor=0.15 → need=max(0.45, 0.012)=0.45 → 0.05<0.45 被丢
# 新：floor≤0.012 → need=max(0.036,0.012)=0.036 → 0.05>0.036 放行
check("★首块即语音后，正常语音仍能过门限", bool(g._passes_gate(speech)), True)
# 反向：真正低于门限的应当仍然被拦（别把门限修没了）
check("远低于门限的仍被拦（门限没被修没）",
      bool(g._passes_gate(const(0.002))), False)

# ---- 2）reset() 必须复位噪声底（旧代码失败）----
g2 = VadGate(cfg)
for _ in range(30):
    g2._update_floor(const(0.15))
g2.reset()
check("★reset() 后 _floor 归零（哨兵）", g2._floor, 0.0)
check("reset() 后 _floor_probe 归零", g2._floor_probe, 0)
# reset 后立刻能重新估：再来一块语音，门限仍应放行正常语音
g2._update_floor(const(0.15))
check("reset() 后重新估计仍放行正常语音", bool(g2._passes_gate(speech)), True)

# ---- 3）边界：探针期结束后回到快降慢升（别把原逻辑弄丢）----
g3 = VadGate(cfg)
for _ in range(12):
    g3._update_floor(const(0.02))     # 灌满探针期，floor 封顶在 vad_min_rms
check("探针期结束 floor ≤ vad_min_rms", g3._floor <= cfg.vad_min_rms + 1e-9, True)
before = g3._floor
g3._update_floor(const(0.001))        # 明显更静 → 立即跟随下降
check("探针期后仍能立即跟随下降", g3._floor < before, True)

# ---- 4）边界：vad_min_rms 被配成 0 时不能放行一切 ----
import dataclasses  # noqa: E402
cfg0 = dataclasses.replace(cfg, vad_min_rms=0.0)
g4 = VadGate(cfg0)
g4._update_floor(const(0.15))
# 封顶用 max(vad_min_rms, 1e-6)，所以 floor 最多 1e-6 → need=max(3e-6,0)=3e-6
# 静音块（全零）rms=0 → 仍应被拦（0 >= 3e-6 为假）
check("vad_min_rms=0 时全零段仍被拦", bool(g4._passes_gate(np.zeros(1600, np.float32))), False)

print()
if FAIL:
    print(f"❌ 失败 {len(FAIL)} 项: {FAIL}")
    sys.exit(1)
print("✅ 全部通过")
