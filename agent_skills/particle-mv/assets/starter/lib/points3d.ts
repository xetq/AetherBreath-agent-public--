import {rng} from '../theme';
import {V3} from './camera';

/**
 * 3D 点云生成器 —— 「用粒子构成物象」的原料。
 *
 * 每个函数返回一堆**世界坐标**的点。它们会被 project() 投影到屏幕，
 * 所以相机一动，整个形体就跟着产生真实的透视变化（这是 3D 感的来源）。
 * 全部确定性（rng(seed)），逐帧渲染可复现。
 */

/** 球面（可给 jitter 让粒子稍微离面，形成"体积"而非"壳"） */
export const sphere3 = (c: V3, radius: number, n: number, seed: number, jitter = 0): V3[] => {
  const rnd = rng(seed);
  const out: V3[] = [];
  for (let i = 0; i < n; i++) {
    const u = rnd() * 2 - 1;
    const th = rnd() * Math.PI * 2;
    const s = Math.sqrt(Math.max(0, 1 - u * u));
    const k = radius * (1 - jitter * rnd());
    out.push({
      x: c.x + s * Math.cos(th) * k,
      y: c.y + u * k,
      z: c.z + s * Math.sin(th) * k,
    });
  }
  return out;
};

/** 竖直圆柱（灯柱、树干、栏杆柱） */
export const column3 = (
  x: number,
  z: number,
  y0: number,
  h: number,
  radius: number,
  n: number,
  seed: number
): V3[] => {
  const rnd = rng(seed);
  const out: V3[] = [];
  for (let i = 0; i < n; i++) {
    const th = rnd() * Math.PI * 2;
    const r = radius * (0.75 + rnd() * 0.25);
    out.push({
      x: x + Math.cos(th) * r,
      y: y0 + rnd() * h,
      z: z + Math.sin(th) * r,
    });
  }
  return out;
};

/** 长方体（长椅的板、箱子、纸的厚度） */
export const box3 = (c: V3, sx: number, sy: number, sz: number, n: number, seed: number): V3[] => {
  const rnd = rng(seed);
  const out: V3[] = [];
  for (let i = 0; i < n; i++) {
    // 偏向表面分布（内部点会白吃渲染）
    const face = Math.floor(rnd() * 6);
    const u = (rnd() - 0.5) * sx;
    const v = (rnd() - 0.5) * sy;
    const w = (rnd() - 0.5) * sz;
    let p: V3;
    switch (face) {
      case 0: p = {x: u, y: v, z: w}; break;
      case 1: p = {x: u, y: v, z: w}; break;
      case 2: p = {x: u, y: sy / 2, z: w}; break;
      case 3: p = {x: u, y: -sy / 2, z: w}; break;
      case 4: p = {x: sx / 2, y: v, z: w}; break;
      default: p = {x: -sx / 2, y: v, z: w}; break;
    }
    out.push({x: c.x + p.x, y: c.y + p.y, z: c.z + p.z});
  }
  return out;
};

/** 水平网格面（地面、桌面、纸面） */
export const grid3 = (
  c: V3,
  sx: number,
  sz: number,
  nx: number,
  nz: number,
  jitter = 0
): V3[] => {
  const rnd = rng(Math.round((sx + sz + nx + nz) * 977) + 13);
  const out: V3[] = [];
  for (let i = 0; i < nx; i++) {
    for (let j = 0; j < nz; j++) {
      out.push({
        x: c.x + (i / (nx - 1) - 0.5) * sx + (rnd() - 0.5) * jitter,
        y: c.y,
        z: c.z + (j / (nz - 1) - 0.5) * sz + (rnd() - 0.5) * jitter,
      });
    }
  }
  return out;
};

/** 两点之间的线（手写笔画、雨丝、轨迹） */
export const line3 = (a: V3, b: V3, n: number, jitter = 0, seed = 5): V3[] => {
  const rnd = rng(seed);
  const out: V3[] = [];
  for (let i = 0; i < n; i++) {
    const t = n === 1 ? 0 : i / (n - 1);
    out.push({
      x: a.x + (b.x - a.x) * t + (rnd() - 0.5) * jitter,
      y: a.y + (b.y - a.y) * t + (rnd() - 0.5) * jitter,
      z: a.z + (b.z - a.z) * t + (rnd() - 0.5) * jitter,
    });
  }
  return out;
};

/** 折线（多段路径，用于手写字迹、拱形） */
export const path3 = (pts: V3[], per: number, jitter = 0, seed = 9): V3[] => {
  const rnd = rng(seed);
  const out: V3[] = [];
  for (let i = 0; i < pts.length - 1; i++) {
    for (let k = 0; k < per; k++) {
      const t = k / per;
      out.push({
        x: pts[i].x + (pts[i + 1].x - pts[i].x) * t + (rnd() - 0.5) * jitter,
        y: pts[i].y + (pts[i + 1].y - pts[i].y) * t + (rnd() - 0.5) * jitter,
        z: pts[i].z + (pts[i + 1].z - pts[i].z) * t + (rnd() - 0.5) * jitter,
      });
    }
  }
  return out;
};

/** 一团空气中的浮尘（氛围粒子，不构成任何形状） */
export const dust3 = (c: V3, sx: number, sy: number, sz: number, n: number, seed: number): V3[] => {
  const rnd = rng(seed);
  const out: V3[] = [];
  for (let i = 0; i < n; i++) {
    out.push({
      x: c.x + (rnd() - 0.5) * sx,
      y: c.y + (rnd() - 0.5) * sy,
      z: c.z + (rnd() - 0.5) * sz,
    });
  }
  return out;
};

/** 圆环（涟漪、光环、座圈） */
export const ring3 = (
  c: V3,
  radius: number,
  n: number,
  seed: number,
  axis: 'y' | 'z' = 'y',
  thickness = 0
): V3[] => {
  const rnd = rng(seed);
  const out: V3[] = [];
  for (let i = 0; i < n; i++) {
    const th = rnd() * Math.PI * 2;
    const r = radius + (rnd() - 0.5) * thickness;
    if (axis === 'y') {
      out.push({x: c.x + Math.cos(th) * r, y: c.y, z: c.z + Math.sin(th) * r});
    } else {
      out.push({x: c.x + Math.cos(th) * r, y: c.y + Math.sin(th) * r, z: c.z});
    }
  }
  return out;
};

/** 心形（经典心形参数方程 + 少量厚度），用于「爱你爱到我心痛」 */
export const heart3 = (c: V3, scale: number, n: number, seed: number, thickness = 0.25): V3[] => {
  const rnd = rng(seed);
  const out: V3[] = [];
  for (let i = 0; i < n; i++) {
    const th = rnd() * Math.PI * 2;
    const s = Math.sin(th);
    const x = 16 * s * s * s;
    const y = 13 * Math.cos(th) - 5 * Math.cos(2 * th) - 2 * Math.cos(3 * th) - Math.cos(4 * th);
    // 边缘为主 + 一点点内部填充
    const k = rnd() < 0.78 ? 1 : Math.sqrt(rnd());
    out.push({
      x: c.x + (x / 17) * scale * k,
      y: c.y + (y / 17) * scale * k,
      z: c.z + (rnd() - 0.5) * thickness * scale,
    });
  }
  return out;
};

/** 把多组点云拼成一组（场景 = 若干形体的并集） */
export const merge3 = (...groups: V3[][]): V3[] => {
  const out: V3[] = [];
  groups.forEach((g) => out.push(...g));
  return out;
};
