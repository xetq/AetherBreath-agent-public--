/**
 * 视觉体系模板 —— 画布尺寸可切换 + 调色曲线 + 确定性随机。
 *
 * 关于「确定性」：逐帧渲染要求每一帧都由帧号唯一决定，
 * 所以永远不要用 Math.random()（否则每次渲染结果不同、画面会闪）。
 */

export const FPS = 30;

/* -------------------------------------------------------------------------
 * 画布尺寸可切换（横版 / 竖版）
 * 用 export let（ESM live binding）——子组件每次读取都是最新值。
 * 因为投影以 W/2、H/2 为屏幕中心，换尺寸后相机是**重新取景**，不是被压扁。
 * ---------------------------------------------------------------------- */
export let W = 1920;
export let H = 1080;
export let BAR = 110;            // 上下电影黑边（竖版通常设 0）
export let IS_VERTICAL = false;

export const setCanvas = (w: number, h: number, vertical = false, bar = 0): void => {
  W = w;
  H = h;
  IS_VERTICAL = vertical;
  BAR = bar;
};

/* -------------------------------------------------------------------------
 * 调色曲线：让颜色随时间走一条线，而不是用一张固定色板
 * ---------------------------------------------------------------------- */
type RGB = [number, number, number];

const hex2rgb = (s: string): RGB => [
  parseInt(s.slice(1, 3), 16),
  parseInt(s.slice(3, 5), 16),
  parseInt(s.slice(5, 7), 16),
];

const mix = (a: RGB, b: RGB, p: number): RGB => [
  a[0] + (b[0] - a[0]) * p,
  a[1] + (b[1] - a[1]) * p,
  a[2] + (b[2] - a[2]) * p,
];

const css = (c: RGB, alpha = 1) =>
  `rgba(${Math.round(c[0])}, ${Math.round(c[1])}, ${Math.round(c[2])}, ${alpha})`;

export const clamp01 = (p: number): number => (p < 0 ? 0 : p > 1 ? 1 : p);

const ramp = (stops: {at: number; c: string}[], t: number): RGB => {
  const x = clamp01(t);
  if (x <= stops[0].at) return hex2rgb(stops[0].c);
  for (let i = 1; i < stops.length; i++) {
    if (x <= stops[i].at) {
      const a = stops[i - 1];
      const b = stops[i];
      return mix(hex2rgb(a.c), hex2rgb(b.c), (x - a.at) / (b.at - a.at || 1));
    }
  }
  return hex2rgb(stops[stops.length - 1].c);
};

/* 示例色标：黄昏 → 蓝调 → 入夜。按你的主题改这四个数组即可。 */
const SKY_TOP = [
  {at: 0.0, c: '#534478'}, {at: 0.2, c: '#4A3C6E'},
  {at: 0.45, c: '#33305A'}, {at: 0.7, c: '#12203C'}, {at: 1.0, c: '#04070E'},
];
const SKY_MID = [
  {at: 0.0, c: '#C4758F'}, {at: 0.2, c: '#B86A84'},
  {at: 0.45, c: '#8A5A78'}, {at: 0.7, c: '#1E3A58'}, {at: 1.0, c: '#08101C'},
];
const SKY_HORIZ = [
  {at: 0.0, c: '#F2A05E'}, {at: 0.2, c: '#EE8F52'},
  {at: 0.45, c: '#DC6440'}, {at: 0.7, c: '#3E6E92'}, {at: 1.0, c: '#16283C'},
];
const INK = [
  {at: 0.0, c: '#F7EFE4'}, {at: 0.45, c: '#F0EAE4'},
  {at: 0.7, c: '#E4ECF6'}, {at: 1.0, c: '#DCE6F0'},
];

export type Palette = {
  skyTop: string;
  skyMid: string;
  skyHoriz: string;
  ink: string;
  /** 单点暖色：留给情绪最高的那一段 */
  warm: string;
};

/** t ∈ [0,1] = 全片进度 */
export const paletteAt = (t: number): Palette => ({
  skyTop: css(ramp(SKY_TOP, t)),
  skyMid: css(ramp(SKY_MID, t)),
  skyHoriz: css(ramp(SKY_HORIZ, t)),
  ink: css(ramp(INK, t)),
  warm: '#FFC078',
});

/* -------------------------------------------------------------------------
 * 字体（中文衬线在 Chromium 里能直接解析，不需要嵌字体文件）
 * ---------------------------------------------------------------------- */
export const FONT_CN = '"STSong", "SimSun", "Songti SC", serif';
export const FONT_EN = '"Times New Roman", "Georgia", serif';
export const FONT_MONO = '"Consolas", "Courier New", monospace';

/* -------------------------------------------------------------------------
 * 工具
 * ---------------------------------------------------------------------- */

/** 确定性伪随机 —— 逐帧渲染必须可复现，禁用 Math.random() */
export const rng = (seed: number): (() => number) => {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 4294967296;
  };
};

export const lerp = (a: number, b: number, p: number): number => a + (b - a) * p;
export const ease = (p: number): number => 1 - Math.pow(1 - clamp01(p), 3);
export const easeInOut = (p: number): number => {
  const x = clamp01(p);
  return x < 0.5 ? 2 * x * x : 1 - Math.pow(-2 * x + 2, 2) / 2;
};
export const span = (frame: number, a: number, b: number): number =>
  clamp01((frame - a) / Math.max(1, b - a));
