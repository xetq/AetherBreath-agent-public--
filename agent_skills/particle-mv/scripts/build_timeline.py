#!/usr/bin/env python3
"""
build_timeline.py —— 由「锚点表 + 歌词」生成 timeline.json 与 lyrics.ts。

用法:
    python build_timeline.py anchors.txt out/ --mv-end 93.0 --fps 30 \
        --intro-end 15.49 --outro-start 83.59

anchors.txt 每行: 序号,歌词,起始秒   （和 refine_anchors.py 的输入格式一致）

产出:
    out/timeline.json  —— 含每句起止、时长、以及转好的帧号
    out/lyrics.ts      —— TypeScript 数据层，直接丢进前端工程

🔴 这份数据是**唯一权威**：后面的场景调度、动画节奏全部读它，任何地方都不要再重算时间。
"""
import argparse
import json
import os
import sys


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
            try:
                rows.append({"idx": int(parts[0]), "text": parts[1].strip(), "start": float(parts[2])})
            except ValueError:
                continue
    rows.sort(key=lambda r: r["start"])
    return rows


def main():
    ap = argparse.ArgumentParser(description="生成 timeline.json + lyrics.ts")
    ap.add_argument("anchors")
    ap.add_argument("outdir")
    ap.add_argument("--mv-end", type=float, default=93.0, help="成片总时长（秒）")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--intro-end", type=float, default=None, help="第一句起唱前的前奏长度（默认取第一句时间）")
    ap.add_argument("--outro-start", type=float, default=None, help="最后一句收声后的尾奏起点（默认取最后一句结束）")
    args = ap.parse_args()

    rows = parse_anchors(args.anchors)
    if not rows:
        print("锚点表是空的。格式：序号,歌词,起始秒")
        return 1

    lines = []
    for i, r in enumerate(rows):
        end = rows[i + 1]["start"] if i + 1 < len(rows) else (args.outro_start or r["start"] + 3.0)
        lines.append({
            "idx": r["idx"],
            "text": r["text"],
            "start": round(r["start"], 3),
            "end": round(end, 3),
            "dur": round(end - r["start"], 3),
            "startFrame": round(r["start"] * args.fps),
            "endFrame": round(end * args.fps),
        })

    intro_end = args.intro_end if args.intro_end is not None else lines[0]["start"]
    outro_start = args.outro_start if args.outro_start is not None else lines[-1]["end"]

    tl = {
        "mv": {"fps": args.fps, "duration_sec": args.mv_end, "total_frames": int(args.mv_end * args.fps)},
        "structure": {
            "intro": {"start": 0.0, "end": intro_end, "note": "前奏（无人声）—— 建立镜头"},
            "verses": {"start": intro_end, "end": outro_start, "note": "%d 句歌词" % len(lines)},
            "outro": {"start": outro_start, "end": args.mv_end, "note": "尾奏 —— 收束"},
        },
        "lines": lines,
    }

    os.makedirs(args.outdir, exist_ok=True)
    jp = os.path.join(args.outdir, "timeline.json")
    json.dump(tl, open(jp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    # TypeScript 数据层
    ts_rows = ",\n".join(
        "  {idx: %d, text: %s, rows: [%s], start: %.3f, end: %.3f, lang: '%s'}"
        % (l["idx"], json.dumps(l["text"], ensure_ascii=False),
           json.dumps(l["text"], ensure_ascii=False),     # rows 默认整句一行，按原歌词换行手工调整
           l["start"], l["end"],
           "en" if all(ord(c) < 128 or c.isspace() for c in l["text"]) else "cn")
        for l in lines
    )
    ts = (
        "// 歌词数据层 —— 由 build_timeline.py 生成\n"
        "// 🔴 时间码是权威数据，任何地方都不要重算。\n"
        "// rows 是「语义断句」，默认整句一行；请按**原歌词的换行**手工调整。\n\n"
        "export type Lang = 'cn' | 'en';\n\n"
        "export type LyricLineData = {\n"
        "  idx: number;\n  text: string;\n  rows: string[];\n"
        "  start: number;\n  end: number;\n  lang: Lang;\n};\n\n"
        "export const LINES: LyricLineData[] = [\n" + ts_rows + ",\n];\n\n"
        "export const FPS = %d;\n"
        "export const secToFrame = (s: number): number => Math.round(s * FPS);\n"
        % args.fps
    )
    tp = os.path.join(args.outdir, "lyrics.ts")
    open(tp, "w", encoding="utf-8", newline="\n").write(ts)

    print("%-4s %-34s %8s %8s %8s" % ("#", "lyric", "in", "out", "dur"))
    print("-" * 66)
    for l in lines:
        print("%-4d %-34s %8.2f %8.2f %8.2f" % (l["idx"], l["text"][:32], l["start"], l["end"], l["dur"]))
    print("-" * 66)
    print("前奏 0.00 - %.2f  |  歌词 %.2f - %.2f (%d 句)  |  尾奏 %.2f - %.2f"
          % (intro_end, intro_end, outro_start, len(lines), outro_start, args.mv_end))
    print("\n已生成:\n  %s\n  %s" % (jp, tp))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
