/**
 * 针孔相机 —— 伪 3D 的地基。
 *
 * 元素有世界坐标 (x, y, z)，相机有位置/朝向/FOV，投影到屏幕。
 * 好处：运镜就是「相机参数的时间函数」，推拉摇移环绕全部精确可控，
 * 而且不需要建模、不涉及材质光照（上次 Three.js 翻车正是栽在这两件事上）。
 *
 * 约定：y 轴向上，地面 y = 0，z 轴向前（远处 z 大），单位近似「米」。
 */

export type V3 = {x: number; y: number; z: number};

export type Cam = {
  x: number;
  y: number;
  z: number;
  /** 偏航（左右转头），弧度 */
  yaw: number;
  /** 俯仰（抬头低头），弧度 */
  pitch: number;
  /** 焦距（像素）——越大视角越窄、越"长焦" */
  focal: number;
};

export type Proj = {
  sx: number;
  sy: number;
  /** 每世界单位的像素数：用于把物体的物理尺寸换算成屏幕尺寸 */
  scale: number;
  /** 相机空间深度（>0 才在镜头前） */
  depth: number;
  visible: boolean;
};

export const project = (p: V3, cam: Cam, W: number, H: number): Proj => {
  const dx = p.x - cam.x;
  const dy = p.y - cam.y;
  const dz = p.z - cam.z;

  // 🔴 旋转符号：yaw/pitch 用【正角】做世界→相机的转动。
  //    写成 cos(-yaw)/sin(-yaw) 是最常见的错，后果是所有「低头看」的镜头实际在「抬头」，
  //    近景物体全被甩到画面外（表现为整场全黑）。
  //    自检：pitch>0（抬头）时，正前方的点必须落到画面【下方】（ry<0）。
  const cy = Math.cos(cam.yaw);
  const sy = Math.sin(cam.yaw);
  const rx = dx * cy - dz * sy;
  const rz1 = dx * sy + dz * cy;

  const cp = Math.cos(cam.pitch);
  const sp = Math.sin(cam.pitch);
  const ry = dy * cp - rz1 * sp;
  const rz = dy * sp + rz1 * cp;

  if (rz < 0.2) {
    return {sx: 0, sy: 0, scale: 0, depth: rz, visible: false};
  }
  const scale = cam.focal / rz;
  return {sx: W / 2 + rx * scale, sy: H / 2 - ry * scale, scale, depth: rz, visible: true};
};

/** 大气透视：距离越远 → 越淡、越沉入天色（这一条最能让平面「立起来」） */
export const hazeAt = (depth: number, near = 4, far = 70): number =>
  Math.max(0, Math.min(1, (depth - near) / (far - near)));

/** 景深模糊（像素） */
export const blurAt = (depth: number, focus = 12, k = 0.06): number =>
  Math.min(6, Math.abs(depth - focus) * k);

/** 颜色向天色靠拢（远处的东西被大气"洗淡"） */
export const mixRgb = (a: [number, number, number], b: [number, number, number], p: number): string => {
  const x = Math.max(0, Math.min(1, p));
  const c = a.map((v, i) => Math.round(v + (b[i] - v) * x));
  return `rgb(${c[0]}, ${c[1]}, ${c[2]})`;
};
