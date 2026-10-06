

/**
 * 每句在时间轴上的窗口 + 交叉溶解区间。
 *
 * 零硬切的做法：相邻两句的窗口**故意重叠** 48 帧——
 * 前一句还在淡出时，后一句已经开始淡入。
 * 重叠期里两个场景同时存在，观众看到的是「上一个画面化进下一个画面」。
 */

/** 淡入/淡出各占的帧数 */
const FADE = 18;
/** 窗口在句首前 / 句尾后各外扩多少帧 */
const PAD = 24;

export type ShotWin = {
  idx: number;
  /** 歌词本身的起止帧 */
  lineStart: number;
  lineEnd: number;
  /** 场景窗口 */
  w0: number;
  w1: number;
  /** 不透明度：淡入 [in0,in1]，稳定 [in1,out0]，淡出 [out0,out1] */
  in0: number;
  in1: number;
  out0: number;
  out1: number;
};

/**
 * 由「每句歌词的起止帧」生成段窗口。
 * @param lines 每句的 {startFrame, endFrame}
 */
export const buildWindows = (lines: {startFrame: number; endFrame: number}[]): ShotWin[] =>
  lines.map((l, i) => {
  const lineStart = l.startFrame;
  const lineEnd = l.endFrame;
  const w0 = lineStart - PAD;
  const w1 = lineEnd + PAD;
  return {
    idx: i + 1,
    lineStart,
    lineEnd,
    w0,
    w1,
    in0: w0,
    in1: w0 + FADE,
    out0: w1 - FADE,
    out1: w1,
  };
});

/** 该场景在这一帧的不透明度（0 = 不渲染） */
export const shotOpacity = (w: ShotWin, frame: number): number => {
  if (frame < w.in0 || frame > w.out1) return 0;
  if (frame < w.in1) return (frame - w.in0) / (w.in1 - w.in0);
  if (frame > w.out0) return 1 - (frame - w.out0) / (w.out1 - w.out0);
  return 1;
};

/** 该场景的局部时间（秒），用于场景内部的动画曲线 */
export const shotLocalSec = (w: ShotWin, frame: number, fps = 30): number => (frame - w.w0) / fps;
