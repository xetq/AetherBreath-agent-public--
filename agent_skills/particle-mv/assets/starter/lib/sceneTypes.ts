import {Cam, V3} from './camera';

/** 一层粒子：一组点 + 它的外观 */
export type Layer3D = {
  pts: V3[];
  color: string | ((t: number) => string);
  /** 粒子世界半径 */
  size?: number;
  minPx?: number;
  maxPx?: number;
  opacity?: number | ((t: number) => number);
  depthFade?: number;
  jitter?: number;
  z?: number;
  /** 整层粒子的世界位移（雨下落、花瓣上升等） */
  shift?: (t: number) => {x?: number; y?: number; z?: number};
};

/** 一个场景：相机怎么动 + 由哪几层粒子构成 */
export type Scene3D = {
  idx: number;
  /** t ∈ [0,1] 是这一句内部的进度，dur 是窗口总秒数 */
  cam: (t: number, dur: number) => Cam;
  layers: (t: number) => Layer3D[];
};

/** 常用相机：沿街向前 + 轻微摆动（"走在街上"） */
export const walkCam = (
  t: number,
  opts: {z0: number; z1: number; x?: number; sway?: number; yaw?: number; y?: number; pitch?: number; focal?: number}
): Cam => ({
  x: (opts.x ?? 0) + Math.sin(t * Math.PI * 1.6) * (opts.sway ?? 1.1),
  y: (opts.y ?? 1.72) + Math.sin(t * Math.PI * 2.2) * 0.04,
  z: opts.z0 + (opts.z1 - opts.z0) * t,
  yaw: Math.sin(t * Math.PI * 1.3) * (opts.yaw ?? 0.1),
  pitch: opts.pitch ?? -0.05,
  focal: opts.focal ?? 950,
});

/** 常用相机：绕某点横移 + 转头（"绕着看"） */
export const orbitCam = (
  t: number,
  opts: {cx?: number; cz?: number; radius: number; from: number; to: number; y?: number; pitch?: number; yawK?: number; focal?: number}
): Cam => {
  const a = opts.from + (opts.to - opts.from) * t;
  return {
    x: (opts.cx ?? 0) + Math.sin(a) * opts.radius,
    y: opts.y ?? 1.74,
    z: (opts.cz ?? 0) + Math.cos(a) * opts.radius * 0.55,
    yaw: a * (opts.yawK ?? 0.42),
    pitch: opts.pitch ?? -0.035,
    focal: opts.focal ?? 900,
  };
};

/** 常用相机：极缓推近（"靠近看"） */
export const dollyCam = (
  t: number,
  opts: {x?: number; y?: number; z0: number; z1: number; yaw?: number; pitch?: number; focal?: number}
): Cam => ({
  x: opts.x ?? 0,
  y: opts.y ?? 1.7,
  z: opts.z0 + (opts.z1 - opts.z0) * t,
  yaw: opts.yaw ?? 0,
  pitch: opts.pitch ?? -0.02,
  focal: opts.focal ?? 900,
});

/** 常用相机：固定机位 + 缓摇（"看着它离开"） */
export const panCam = (
  t: number,
  opts: {x: number; y?: number; z: number; yaw0: number; yaw1: number; pitch?: number; focal?: number}
): Cam => ({
  x: opts.x,
  y: opts.y ?? 1.7,
  z: opts.z,
  yaw: opts.yaw0 + (opts.yaw1 - opts.yaw0) * t,
  pitch: opts.pitch ?? -0.03,
  focal: opts.focal ?? 900,
});
