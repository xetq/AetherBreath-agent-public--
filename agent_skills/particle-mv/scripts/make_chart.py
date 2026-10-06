#!/usr/bin/env python3
"""
make_chart.py —— 出「波形 + 能量包络 + 切点标记」对照图，交给人类核对。

为什么需要它：让人对着**一张图**就能判断"我标的线歪没歪"，
比让他在播放器里来回拖进度条快十倍。

用法:
    python make_chart.py vocals.wav out.png \
        --duration 100 \
        --cuts cuts.json \
        --marks "15.49:第1句起唱,83.59:人声段结束,93.0:成片结束"

cuts.json 可以是 refine_anchors.py 的输出（{"lines":[{"idx":..,"refined":..}]}），
也可以是一个纯数组 [15.49, 18.15, ...]。

依赖: numpy + Pillow
"""
import argparse
import json
import os
import sys
import wave

import numpy as np

# 终端编码兜底：某些环境（Windows GBK 控制台）打印非 ASCII 会直接抛异常
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]


def pick_font(size):
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def load_mono(path):
    with wave.open(path, "rb") as w:
        sr, n, ch, sw = w.getframerate(), w.getnframes(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(n)
    if sw == 2:
        x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif sw == 4:
        x = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError("unsupported sampwidth")
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return sr, x


def main():
    ap = argparse.ArgumentParser(description="波形 + 包络 + 切点 对照图")
    ap.add_argument("vocals")
    ap.add_argument("out")
    ap.add_argument("--duration", type=float, default=None, help="画多少秒（默认全曲）")
    ap.add_argument("--cuts", default=None, help="切点 JSON（refine_anchors 输出，或纯数组）")
    ap.add_argument("--marks", default="", help="额外标记 '秒:名字,秒:名字'")
    ap.add_argument("--width", type=int, default=1900)
    args = ap.parse_args()

    sr, x = load_mono(args.vocals)
    total = args.duration or (len(x) / sr)
    x = x[: int(total * sr)]

    W, H = args.width, 460
    PAD_L, PAD_R, PAD_T = 60, 40, 30
    WAVE_H, ENV_H = 200, 130
    plot_w = W - PAD_L - PAD_R

    img = Image.new("RGB", (W, H), (12, 14, 20))
    d = ImageDraw.Draw(img)
    f_small, f_big = pick_font(15), pick_font(18)

    def tx(t):
        return PAD_L + int(t / total * plot_w)

    # ① 波形
    mid = PAD_T + WAVE_H // 2
    step = max(1, len(x) // plot_w)
    for i in range(plot_w):
        a, b = i * step, min((i + 1) * step, len(x))
        if a >= len(x):
            break
        seg = x[a:b]
        if len(seg) == 0:
            continue
        y0 = mid - int(float(seg.max()) * WAVE_H * 0.48)
        y1 = mid - int(float(seg.min()) * WAVE_H * 0.48)
        d.line([(PAD_L + i, y0), (PAD_L + i, y1)], fill=(255, 176, 103))

    # ② 能量包络
    hop, win = int(sr * 0.01), int(sr * 0.04)
    frames = np.lib.stride_tricks.sliding_window_view(x, win)[::hop]
    env = 20 * np.log10(np.sqrt((frames ** 2).mean(axis=1)) + 1e-9)
    env_top, env_bot = PAD_T + WAVE_H + 46, PAD_T + WAVE_H + 46 + ENV_H
    d.rectangle([PAD_L, env_top, W - PAD_R, env_bot], outline=(60, 70, 90))
    pts = []
    for i in range(plot_w):
        idx = min(int(i / plot_w * len(env)), len(env) - 1)
        v = max(-70.0, min(0.0, float(env[idx])))
        pts.append((PAD_L + i, env_bot - int((v + 70.0) / 70.0 * ENV_H)))
    if len(pts) > 1:
        d.line(pts, fill=(121, 199, 255), width=2)

    # ③ 刻度
    tick = 10 if total > 40 else 5
    for t in range(0, int(total) + 1, tick):
        X = tx(t)
        d.line([(X, PAD_T - 6), (X, env_bot + 6)], fill=(70, 80, 100), width=1)
        d.text((X - 14, env_bot + 10), "%ds" % t, font=f_small, fill=(150, 160, 180))

    # ④ 切点
    if args.cuts and os.path.exists(args.cuts):
        raw = json.load(open(args.cuts, encoding="utf-8"))
        cuts = [r["refined"] for r in raw["lines"]] if isinstance(raw, dict) else list(raw)
        for t in cuts:
            X = tx(t)
            d.line([(X, PAD_T - 20), (X, env_bot + 28)], fill=(255, 214, 102), width=2)

    # ⑤ 额外标记
    for m in [s for s in args.marks.split(",") if ":" in s]:
        sec_s, label = m.split(":", 1)
        try:
            t = float(sec_s)
        except ValueError:
            continue
        X = tx(t)
        d.line([(X, PAD_T - 22), (X, env_bot + 30)], fill=(140, 255, 170), width=2)
        d.text((min(X + 6, W - PAD_R - 160), PAD_T - 26), label, font=f_small, fill=(140, 255, 170))

    d.text((PAD_L, 4), "vocals stem — waveform + RMS envelope + cut points", font=f_big, fill=(237, 233, 226))
    d.text((PAD_L, PAD_T + WAVE_H + 16), "RMS energy (dB)", font=f_small, fill=(121, 199, 255))

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    img.save(args.out)
    print("对照图已生成:", args.out, os.path.getsize(args.out), "bytes")


if __name__ == "__main__":
    main()
