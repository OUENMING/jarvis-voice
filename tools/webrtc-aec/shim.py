"""自编 WebRTC AEC3 的 ctypes 薄壳（给 `measure_echo_delay.py --aec` 用）。

⚠️ 三件必须记住的事（都踩过，详见 tools/webrtc-aec/README.md）：
  1. **顺序**：`Initialize()` 必须在 `ApplyConfig()` **之前**，否则配置被重置、
     AEC 根本没启用、输出 == 输入（很容易误判成"这个方案没用"）。
  2. 本 build 里 `GetLinearAecOutput()` 拿到的**几乎是输入本身**，不能用。
  3. 库路径由 `JARVIS_WLA_LIB` 指定，默认取本目录下的 `libwla.dylib`。
"""
from __future__ import annotations

import ctypes
import os

import numpy as np

_P16 = ctypes.POINTER(ctypes.c_int16)
_LIB = None


def _lib():
    global _LIB
    if _LIB is None:
        path = os.environ.get("JARVIS_WLA_LIB",
                              os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                           "libwla.dylib"))
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"找不到 {path} —— 先按 tools/webrtc-aec/README.md 编译（或设 JARVIS_WLA_LIB）")
        lib = ctypes.CDLL(path)
        lib.wla_create.restype = ctypes.c_void_p
        lib.wla_create.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
        lib.wla_process.restype = ctypes.c_int
        lib.wla_process.argtypes = [ctypes.c_void_p, _P16, _P16, _P16, ctypes.c_int]
        lib.wla_linear_enabled.restype = ctypes.c_int
        lib.wla_linear_enabled.argtypes = [ctypes.c_void_p]
        lib.wla_destroy.argtypes = [ctypes.c_void_p]
        _LIB = lib
    return _LIB


def available() -> bool:
    try:
        _lib()
        return True
    except Exception:
        return False


def process(rec_i16: np.ndarray, far_i16: np.ndarray, use_linear: int = 0) -> np.ndarray:
    """过一遍自编的 AEC3。`use_linear=0` = 全链（抑制器之后，与生产等价的那路）。"""
    lib = _lib()
    h = lib.wla_create(16000, 1, int(use_linear))
    if not h:
        raise RuntimeError("wla_create 失败")
    if not lib.wla_linear_enabled(h):
        lib.wla_destroy(h)
        raise RuntimeError("线性输出未启用")
    n = len(rec_i16)
    out = np.zeros(n, np.int16)
    C = 1600
    try:
        for i in range(0, n - C + 1, C):
            rc = lib.wla_process(
                h,
                rec_i16[i:i + C].ctypes.data_as(_P16),
                far_i16[i:i + C].ctypes.data_as(_P16),
                out[i:i + C].ctypes.data_as(_P16),
                C)
            if rc:
                raise RuntimeError(f"wla_process rc={rc} @ {i}")
    finally:
        lib.wla_destroy(h)
    return out
