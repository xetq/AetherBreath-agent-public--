#!/usr/bin/env python3
"""
analyze_audio.py —— 从人声轨提取能量包络，按多档静音间隙切出候选分段。

用法:
    python analyze_audio.py vocals.wav [--gaps 0.25,0.55,1.10] [--out data/vocals_db.npy]

输入建议是**人声分离后**的音轨（Demucs 等），而不是混音——
混音里的鼓和贝斯会让"换气"看起来像"还在唱"。

输出:
    · 终端打印三档粒度的分段结果
    · 能量包络存成 .npy（后面 refine_anchors.py / make_chart.py 会读它）

依赖: numpy
"""
import argparse
import os
import sys
import wave

import numpy as np

# 终端编码兜底：某些环境（Windows GBK 控制台）打印非 ASCII 会直接抛异常
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def load_mono(path):
    """读 wav → (采样率, 单声道 float32 数组)。支持 16/32 bit。"""
    with wave.open(path, "rb") as w:
        sr, n, ch, sw = w.getframerate(), w.getnframes(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(n)
    if sw == 2:
        x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif sw == 4:
        x = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError("unsupported sampwidth: %d bytes" % sw)
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return sr, x


def energy_db(x, sr, hop_s=0.01, win_s=0.04):
    """逐帧 RMS → dB。40ms 的窗足够覆盖一个音节，10ms 的步长给到人耳够用的定位精度。"""
    hop, win = int(sr * hop_s), int(sr * win_s)
    if len(x) < win:
        raise ValueError("audio too short")
    frames = np.lib.stride_tricks.sliding_window_view(x, win)[::hop]
    db = 20 * np.log10(np.sqrt((frames ** 2).mean(axis=1)) + 1e-9)
    return db, hop_s


def voiced_segments(db, rel_db=22.0, min_len=0.30, hop_s=0.01):
    """
    相对阈值二值化：以「响亮部分的 90 分位」为基准往下压 rel_db 分贝。
    用相对阈值而不是绝对阈值，是因为不同歌的录音电平差很多。
    """
    thr = np.percentile(db, 90) - rel_db
    voiced = db > thr
    segs, i = [], 0
    while i < len(voiced):
        if voiced[i]:
            j = i
            while j < len(voiced) and voiced[j]:
                j += 1
            if (j - i) * hop_s >= min_len:
                segs.append([i * hop_s, (j - 1) * hop_s])
            i = j
        else:
            i += 1
    return segs


def merge_by_gap(segs, gap):
    """把间隔小于 gap 的相邻段并起来 —— 换一个 gap 就换一个结构层级。"""
    out = []
    for s in segs:
        if out and s[0] - out[-1][1] < gap:
            out[-1][1] = s[1]
        else:
            out.append(list(s))
    return [tuple(x) for x in out]


def main():
    ap = argparse.ArgumentParser(description="人声能量包络分析 → 候选分段")
    ap.add_argument("vocals", help="人声音轨（wav）")
    ap.add_argument("--gaps", default="0.25,0.55,1.10",
                    help="合并间隙（秒，逗号分隔）。0.55≈句子边界，1.10≈段落边界")
    ap.add_argument("--rel-db", type=float, default=22.0, help="相对阈值（dB，默认 22）")
    ap.add_argument("--min-len", type=float, default=0.30, help="最短段长（秒）")
    ap.add_argument("--out", default=None, help="能量包络存到哪个 .npy")
    args = ap.parse_args()

    sr, x = load_mono(args.vocals)
    db, hop_s = energy_db(x, sr)
    dur = len(x) / sr
    print("音轨: %.2f s / %d Hz / %d 个能量帧" % (dur, sr, len(db)))
    print("能量范围: %.1f ~ %.1f dB" % (db.min(), db.max()))

    segs = voiced_segments(db, args.rel_db, args.min_len, hop_s)
    print("原始发声片段: %d 个\n" % len(segs))

    for gap in [float(g) for g in args.gaps.split(",")]:
        m = merge_by_gap([list(s) for s in segs], gap)
        print("--- gap=%.2fs -> %d 段 ---" % (gap, len(m)))
        for k, (a, b) in enumerate(m, 1):
            print("   %2d  %7.2f -> %7.2f   (%.2fs)" % (k, a, b, b - a))
        print()

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        np.save(args.out, db)
        print("能量包络已存:", args.out)


if __name__ == "__main__":
    main()
