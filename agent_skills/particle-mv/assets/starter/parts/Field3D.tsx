import React, {useEffect, useRef} from 'react';
import {AbsoluteFill} from 'remotion';
import {H, W, clamp01} from '../theme';
import {Cam, V3, project} from '../lib/camera';

/**
 * 3D 粒子场 —— 用粒子「构成」物象的画笔。
 *
 * 每个粒子有真实的世界坐标，所以：
 *   · 相机推 / 摇 / 移 / 环绕时，整个形体产生真实透视变化（3D 感的来源）
 *   · 近处的粒子大而亮、远处的小而淡（景深与大气透视）
 *   · 图层用 lighter 叠加，密集处自然过曝发白
 */

export type Field3DProps = {
  pts: V3[];
  cam: Cam;
  color: string;
  core?: string;
  /** 粒子世界半径（会随距离缩放） */
  size?: number;
  minPx?: number;
  maxPx?: number;
  opacity?: number;
  /** 远景衰减强度 0..1 */
  depthFade?: number;
  /** 抖动幅度（世界单位）——让形体「活着」 */
  jitter?: number;
  seed?: number;
  z?: number;
  /** 整层世界位移 */
  shift?: {x?: number; y?: number; z?: number};
  /** 需要逐帧动画时把帧号传进来（用于抖动相位） */
  frame: number;
};

export const Field3D: React.FC<Field3DProps> = ({
  pts, cam, color, core, size = 0.09, minPx = 0.8, maxPx = 7,
  opacity = 1, depthFade = 0.9, jitter = 0, seed = 1, z = 10, shift, frame,
}) => {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const cv = ref.current;
    if (!cv) return;
    const ctx = cv.getContext('2d');
    if (!ctx) return;
    ctx.clearRect(0, 0, W, H);
    ctx.globalCompositeOperation = 'lighter';

    const coreCol = core ?? '#FFFFFF';
    for (let i = 0; i < pts.length; i++) {
      const p = pts[i];
      // 抖动在**世界坐标**里做——这样它也有透视，不像屏幕偏移那样假
      let x = p.x + (shift?.x ?? 0);
      let y = p.y + (shift?.y ?? 0);
      let zz = p.z + (shift?.z ?? 0);
      if (jitter > 0) {
        const ph = (i * 0.6180339887) % 1;
        x += Math.sin(frame * 0.021 + ph * 26.3) * jitter;
        y += Math.cos(frame * 0.017 + ph * 31.7) * jitter;
        zz += Math.sin(frame * 0.013 + ph * 19.1) * jitter;
      }

      const pr = project({x, y, z: zz}, cam, W, H);
      if (!pr.visible) continue;

      const r = Math.min(maxPx, Math.max(minPx, size * pr.scale));
      // 大气透视：越远越淡
      const atten = 1 - clamp01((pr.depth - 4) / 80) * depthFade;
      const a = opacity * atten * clamp01(pr.depth / 2.2);

      ctx.globalAlpha = a;
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(pr.sx, pr.sy, r, 0, Math.PI * 2);
      ctx.fill();

      // 近处的粒子加一颗更亮的核心（过曝感）
      if (r > 1.1 && atten > 0.35) {
        ctx.globalAlpha = a * 0.55;
        ctx.fillStyle = coreCol;
        ctx.beginPath();
        ctx.arc(pr.sx, pr.sy, r * 0.45, 0, Math.PI * 2);
        ctx.fill();
      }
    }

    ctx.globalCompositeOperation = 'source-over';
    ctx.globalAlpha = 1;
  }, [pts, cam, color, core, size, minPx, maxPx, opacity, depthFade, jitter, seed, shift, frame]);

  return (
    <AbsoluteFill style={{zIndex: z}}>
      <canvas
        ref={ref}
        width={W}
        height={H}
        style={{
          width: W,
          height: H,
          filter: `blur(0.3px) drop-shadow(0 0 6px ${color}) drop-shadow(0 0 16px ${color})`,
        }}
      />
    </AbsoluteFill>
  );
};
