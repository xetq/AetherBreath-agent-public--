#!/usr/bin/env python3
"""
refine_anchors.py —— 把「人给的粗略锚点」对齐到「人声真正起音」的位置。

这是"人在回路"最省力的形态：
人只需要说"这句大概在 15 秒"（听感给出的近似值），
算法在锚点附近找到**嗓子真正发出声音的那一瞬**。

🔴 先说清它测的是什么 —— 这一点非常关键：

  · 它找的是「**人声起音**」：能量从静默抬起来的那一刻。**动画动作**应该落在这里。
  · 它**不是**「歌词行的起点」。人给歌词断点时，习惯把**上一句的尾音和换气**
    都算进下一句的时间段里，所以歌词行边界通常比人声起音**早 0.3~1 秒**。

  两者都必要，但用途不同：
      歌词行时间码  →  文字显示时段、分镜划分
      人声起音      →  动画动作的落点（动作要落在真正发声的瞬间）

  所以看到"精修结果比锚点晚了一秒"时，先别急着当 bug ——
  用 make_chart.py 把能量曲线画出来看一眼，多半会看到那 1 秒里是**真的没人声**。

用法:
    python refine_anchors.py vocals.wav anchors.txt [--window 1.2] [--out refined.json]

anchors.txt 每行（制表符或逗号分隔，空行/# 注释会被跳过）:
    序号    歌词                          近似秒
    1       这是一封离别信                15.0
    2       写下我该离开的原因            18.0

输出:
    · 终端打印 精修结果 + 每句的位移量（位移过大的会标出来，值得人工复听）
    · --out 存 JSON

依赖: numpy（需要先用 analyze_audio.py 生成过能量包络，或者本脚本自己读 wav 算）
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

HOP_S = 0.01


def load_mono(path):
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


def energy_db(x, sr):
    hop, win = int(sr * HOP_S), int(sr * 0.04)
    frames = np.lib.stride_tricks.sliding_window_view(x, win)[::hop]
    return 20 * np.log10(np.sqrt((frames ** 2).mean(axis=1)) + 1e-9)


def onset_near(db, t, window, drop_db=25.0):
    """
    在 t 前后 window 秒内找**起唱点**。

    🔴 判据的选择很关键，这里踩过一个坑：
       最初用「能量爬升最陡的点」（正差分最大），结果系统性偏后约 0.5 秒。
       原因：人声**第一个音节往往是渐强的**（声母轻、韵母才起来），
       "最陡"那一刻经常落在第二个字上，而不是起唱本身。

       改用「窗口内**第一个**越过阈值的帧」——阈值取「峰值 −25 dB」与「静默基线 +10 dB」
       的较大者。这样既能抓到轻起头的音节，又不会被分离残留的底噪骗到。
    """
    i0 = max(0, int((t - window) / HOP_S))
    i1 = min(len(db) - 1, int((t + window) / HOP_S))
    win = db[i0:i1]
    if len(win) < 5:
        return t, 0.0

    sm = np.convolve(win, np.ones(3) / 3, mode="same")     # 3 点平滑压毛刺
    peak = float(sm.max())
    # 🔴 静默基线取 3 分位（而不是 10 分位）：窗口里往往大部分是人声，
    #    10 分位会被抬高成"人声的低音区"，阈值随之偏高 → 起唱被推后约 1 秒。
    base = float(np.percentile(sm, 3))
    thr = max(peak - drop_db, base + 10.0)

    # 🔴 第二步的坑：直接找「第一个越过阈值的帧」会失败 ——
    #    因为窗口左边界处，**上一句的人声往往还在响**，第一个帧就已经超过阈值了，
    #    结果每句都被修到窗口起点（位移恒等于 -window）。
    #    正确做法：先在窗口内找**最深的谷**（那一句唱完、下一句还没起之间的换气），
    #    再从谷底向右找第一个越过阈值的位置 —— 那才是"起唱"。
    valley = int(np.argmin(sm))
    tail = sm[valley:]
    above = tail > thr
    if above.any():
        idx = valley + int(np.argmax(above))
    else:
        idx = valley
    refined = (i0 + idx) * HOP_S
    return refined, refined - t


def parse_anchors(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            parts = [p for p in s.replace("\t", ",").split(",") if p.strip()]
            if len(parts) < 3:
                continue
            idx, text, sec = parts[0].strip(), parts[1].strip(), parts[2].strip()
            try:
                rows.append({"idx": int(idx), "text": text, "user": float(sec)})
            except ValueError:
                continue
    return rows


def main():
    ap = argparse.ArgumentParser(description="粗锚点 → 起音检测精修")
    ap.add_argument("vocals", help="人声音轨（wav）")
    ap.add_argument("anchors", help="锚点表（序号,歌词,近似秒）")
    ap.add_argument("--window", type=float, default=1.2, help="在锚点前后多少秒内搜索（默认 1.2）")
    ap.add_argument("--big-shift", type=float, default=0.6, help="位移超过这个值就标出来提醒复听")
    ap.add_argument("--drop-db", type=float, default=25.0, help="起音阈值：比窗口峰值低多少 dB（默认 25）")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    sr, x = load_mono(args.vocals)
    db = energy_db(x, sr)
    rows = parse_anchors(args.anchors)
    if not rows:
        print("锚点表是空的，检查格式：序号,歌词,近似秒", file=sys.stderr)
        return 1

    print("注意：refined = 人声起音（不一定等于歌词行起点）")
    print("%-4s %-34s %9s %9s %8s  %s" % ("#", "lyric", "anchor", "refined", "shift", "note"))
    print("-" * 82)
    out_rows = []
    for r in rows:
        refined, shift = onset_near(db, r["user"], args.window, args.drop_db)
        note = "[!] shift large - re-listen" if abs(shift) > args.big_shift else ""
        print("%-4d %-34s %9.2f %9.2f %+8.2f  %s" % (r["idx"], r["text"][:32], r["user"], refined, shift, note))
        out_rows.append({"idx": r["idx"], "text": r["text"], "anchor": r["user"], "refined": round(refined, 3)})

    # 单调性：精修后时间必须严格递增，否则说明某个锚点给错了
    print("\n=== 单调性检查 ===")
    bad = [out_rows[k]["idx"] for k in range(1, len(out_rows)) if out_rows[k]["refined"] <= out_rows[k - 1]["refined"]]
    if bad:
        print("  [x] 这些行的时间没有递增，检查锚点:", bad)
    else:
        print("  [ok] 全部递增")

    # 时长异常检查
    odd = []
    for k, r in enumerate(out_rows):
        end = out_rows[k + 1]["refined"] if k + 1 < len(out_rows) else None
        if end is not None and end - r["refined"] < 0.3:
            odd.append(r["idx"])
    if odd:
        print("  [!] 这些句子的间隔过短（<0.3s），可能是重复锚点:", odd)

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        json.dump({"lines": out_rows}, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print("\n已存:", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
