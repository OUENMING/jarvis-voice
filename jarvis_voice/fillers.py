"""填充音（filler）：盖住"思考期"的空白。

依据 `docs/RESEARCH-NATURALNESS-20260916.md` §4：🟢 CUI'25 实测**延迟 >4s 是头号杀手**，
且**自然填充词能改善*感知*响应时间**。实测我们的工具调用会把首句推到 **5.3–12s**，
所以这一项收益最大。

三条设计约束（都是踩出来的）：
  1. **必须预渲染** —— 用来盖延迟的东西，自己不能有 1.2s 的合成延迟。
     启动时后台渲染一次，缓存到磁盘，之后是零延迟读取。
  2. **延迟触发** —— 本轮开始后等 `delay_ms`，还没出首句才播。
     首句先到就取消，避免给本来就快的回答平白加一段。
  3. **可被剪切** —— 填充音还在播时真句子到了，必须把它从播放队列里**切掉**，
     否则会把真正的内容往后推（1.5s 回答 + 0.5s 填充 = 更差）。
     用 `Player.cut_tag("filler")`，见 `player.py`。

音色/温度与主 TTS 一致，保证"同一个人在思考"。
"""
import hashlib
import os
import random
import shutil
import threading
import wave

import numpy as np

CACHE_DIR = os.path.expanduser("~/.jarvis/fillers")

# 短、口语、不承诺具体内容。**不要**说"我查一下"——万一没查就尴尬。
TEXTS = ["嗯……", "那个……", "让我想想。", "我看一下。"]


class FillerClips:
    def __init__(self, tts, texts: list[str] | None = None,
                 cache_dir: str = CACHE_DIR):
        self.tts = tts
        self.texts = texts or TEXTS
        self.cache_dir = cache_dir
        self.clips: list[bytes] = []
        self._i = 0
        self._lock = threading.Lock()
        os.makedirs(cache_dir, exist_ok=True)

    # ---- 缓存键：音色/温度/文本变了就必须重渲染 ----
    def _key(self, text: str) -> str:
        sig = f"{getattr(self.tts,'name','?')}|{getattr(self.tts,'voice_id','?')}|" \
              f"{getattr(self.tts,'temperature','?')}|{text}"
        return hashlib.sha1(sig.encode()).hexdigest()[:16]

    def _path(self, text: str) -> str:
        return os.path.join(self.cache_dir, f"{self._key(text)}.wav")

    def _load(self, path: str) -> bytes:
        with wave.open(path, "rb") as w:
            assert w.getframerate() == self.tts.audio_format.sample_rate, "采样率不匹配"
            return w.readframes(w.getnframes())

    def _render_one(self, text: str) -> bytes | None:
        path = self._path(text)
        if os.path.exists(path):
            try:
                return self._load(path)
            except Exception:
                os.remove(path)          # 损坏则重渲染
        pcm = b"".join(self.tts.synthesize(text))
        if not pcm:
            return None
        tmp = path + ".tmp"
        try:
            with wave.open(tmp, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2)
                w.setframerate(self.tts.audio_format.sample_rate)
                w.writeframes(pcm)
            os.replace(tmp, path)        # 原子落盘
        except OSError:
            pass                          # 缓存失败不影响本次使用
        return pcm

    def ensure(self, log=print):
        """把缺的片段渲染好。**应在后台线程调用**（别拖慢启动）。"""
        got = []
        for t in self.texts:
            try:
                c = self._render_one(t)
            except Exception as e:
                log(f"[filler] 渲染失败 {t!r}: {type(e).__name__}: {e}")
                continue
            if c:
                got.append(c)
        with self._lock:
            self.clips = got
        if got:
            log(f"[filler] 就绪 {len(got)} 条，共 {sum(len(c) for c in got)//1024}KB")
        else:
            log("[filler] 无可用片段（将静默降级：不播填充音）")

    def ready(self) -> bool:
        return bool(self.clips)

    def pick(self) -> bytes | None:
        """轮换取一条（不重复到相邻两次听起来一样）。"""
        with self._lock:
            if not self.clips:
                return None
            c = self.clips[self._i % len(self.clips)]
            self._i += 1
            return c

    def clear_cache(self):
        shutil.rmtree(self.cache_dir, ignore_errors=True)
        os.makedirs(self.cache_dir, exist_ok=True)
