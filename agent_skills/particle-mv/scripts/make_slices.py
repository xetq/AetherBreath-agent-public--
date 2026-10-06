#!/usr/bin/env python3
"""
make_slices.py —— 按时间轴把每句切成一个独立音频文件，供人类试听核对。

关键：**每句前后各留一段缓冲**（默认 0.8 秒）。
留缓冲非常重要——人需要听到"这句是怎么收的、下一句是怎么进来的"，
才判断得出边界对不对。切得太紧，反而没法判断。

用法:
    python make_slices.py song.wav timeline.json slices/ [--pad 0.8] [--ffmpeg ffmpeg]

timeline.json 需要含 {"lines":[{"idx":1,"text":"...","start":15.49,"end":18.15}, ...]}

输出:
    slices/line_01.wav ...  以及 slices/manifest.txt（时间码清单，一起交给对方）
"""
import argparse
import json
import os
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def main():
    ap = argparse.ArgumentParser(description="按时间轴切片（带缓冲）")
    ap.add_argument("audio", help="源音频（整首歌）")
    ap.add_argument("timeline", help="timeline.json")
    ap.add_argument("outdir", help="输出目录")
    ap.add_argument("--pad", type=float, default=0.8, help="每句前后各留多少秒缓冲")
    ap.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg 可执行文件路径")
    args = ap.parse_args()

    tl = json.load(open(args.timeline, encoding="utf-8"))
    lines = tl["lines"] if isinstance(tl, dict) else tl
    os.makedirs(args.outdir, exist_ok=True)

    manifest = []
    for r in lines:
        st, en = float(r["start"]), float(r["end"])
        a = max(0.0, st - args.pad)
        dur = (en - st) + args.pad * 2
        out = os.path.join(args.outdir, "line_%02d.wav" % r["idx"])
        cmd = [args.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
               "-ss", "%.3f" % a, "-t", "%.3f" % dur, "-i", args.audio,
               "-c:a", "pcm_s16le", out]
        try:
            subprocess.run(cmd, check=True)
        except FileNotFoundError:
            print("找不到 ffmpeg。用 --ffmpeg 指定完整路径。", file=sys.stderr)
            return 1
        except subprocess.CalledProcessError as e:
            print("切片失败 (%s): %s" % (r["idx"], e), file=sys.stderr)
            continue
        manifest.append("%02d  %7.2f -> %7.2f  (%5.2fs)  %s"
                        % (r["idx"], st, en, en - st, r.get("text", "")))
        print("  ->", out)

    mp = os.path.join(args.outdir, "manifest.txt")
    open(mp, "w", encoding="utf-8", newline="\n").write("\n".join(manifest) + "\n")
    print("\n共 %d 段，清单: %s" % (len(manifest), mp))
    print("把整个目录交给对方试听，让他只需回答「第 N 句早了/晚了」。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
