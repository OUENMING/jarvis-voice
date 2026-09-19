#!/usr/bin/env python3
"""从都柏林实测到各云 TTS / API 端点的裸 TCP RTT。

⚠️ 方法论：不能用 curl 的 time_connect（本地代理会介入，显示 0.3ms 假值）。
   必须裸 socket 直连，且只信**中位数**。
⚠️ 但要牢记：**TCP 到边缘 ≠ API 端到端延迟**。CDN/anycast 会让连接很快建立，
   而真正的首包延迟还取决于源站与排队。这里的数字是**下界**。
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import socket
import statistics
import time

HOSTS = [
    # 已在文档中验证过的基准
    ("api.anthropic.com", 443, "Anthropic (基准:本地边缘)"),
    ("dashscope-intl.aliyuncs.com", 443, "阿里百炼 国际(新加坡)"),
    ("api.minimax.io", 443, "MiniMax 国际"),
    # 国际大厂
    ("texttospeech.googleapis.com", 443, "Google Cloud TTS"),
    ("us-central1-aiplatform.googleapis.com", 443, "GCP Vertex (us-central1)"),
    ("europe-west4-aiplatform.googleapis.com", 443, "GCP Vertex (eu-west4/荷兰)"),
    ("eastus.tts.speech.microsoft.com", 443, "Azure Speech (eastus)"),
    ("westeurope.tts.speech.microsoft.com", 443, "Azure Speech (westeurope/荷兰)"),
    ("polly.eu-west-1.amazonaws.com", 443, "AWS Polly (eu-west-1/爱尔兰!)"),
    ("polly.us-east-1.amazonaws.com", 443, "AWS Polly (us-east-1)"),
    ("api.openai.com", 443, "OpenAI (基准)"),
    # 专做语音的
    ("api.elevenlabs.io", 443, "ElevenLabs"),
    ("api.cartesia.ai", 443, "Cartesia"),
    ("api.deepgram.com", 443, "Deepgram"),
    ("api.fish.audio", 443, "Fish Audio"),
    ("api.hume.ai", 443, "Hume AI"),
    ("api.play.ht", 443, "PlayHT"),
    ("api.rime.ai", 443, "Rime"),
    # 中文厂商国际站
    ("tts.tencentcloudapi.com", 443, "腾讯云 TTS"),
    ("tts.tencentcloudintl.com", 443, "腾讯云 国际 TTS"),
    ("api.z.ai", 443, "智谱 Z.ai (国际)"),
    ("open.bigmodel.cn", 443, "智谱 国内"),
]


def rtt(host: str, port: int, n: int = 5) -> float | None:
    """裸 TCP 三次握手耗时中位数（ms）。含 DNS 解析。"""
    samples = []
    for _ in range(n):
        try:
            ip = socket.gethostbyname(host)
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(4.0)
            t0 = time.perf_counter()
            s.connect((ip, port))
            samples.append((time.perf_counter() - t0) * 1000)
            s.close()
        except Exception:
            return None
    return statistics.median(samples)


print(f"{'端点':38s} {'中位RTT':>9s}  {'解析':>3s}  说明")
print("-" * 82)
rows = []
for host, port, label in HOSTS:
    ms = rtt(host, port)
    try:
        ip = socket.gethostbyname(host)
    except Exception:
        ip = "?"
    if ms is None:
        print(f"{host:38s} {'超时/失败':>9s}  {ip:>3s}  {label}")
    else:
        rows.append((ms, host, ip, label))
        print(f"{host:38s} {ms:7.1f}ms  {ip:>3s}  {label}")

print("\n=== 按延迟排序（仅成功项）===")
for ms, host, ip, label in sorted(rows):
    print(f"  {ms:7.1f}ms  {label}")
