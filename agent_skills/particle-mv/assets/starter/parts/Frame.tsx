import React, {useMemo} from 'react';
import {AbsoluteFill} from 'remotion';
import {BAR, H, W, rng} from '../theme';

/** 电影黑边 2.2:1（与《城市一夜》同规格） */
export const Letterbox: React.FC<{z?: number}> = ({z = 60}) => {
  if (BAR <= 0) return null;   // 竖版不加黑边
  return (
    <>
      <div style={{position: 'absolute', left: 0, right: 0, top: 0, height: BAR, background: '#000', zIndex: z}} />
      <div style={{position: 'absolute', left: 0, right: 0, bottom: 0, height: BAR, background: '#000', zIndex: z}} />
    </>
  );
};

/** 暗角：把视线压向中心 */
export const Vignette: React.FC<{strength?: number; z?: number}> = ({strength = 0.8, z = 50}) => (
  <AbsoluteFill
    style={{
      zIndex: z,
      background: `radial-gradient(ellipse 76% 68% at 50% 52%, rgba(0,0,0,0) 18%, rgba(0,0,0,${strength}) 100%)`,
      pointerEvents: 'none',
    }}
  />
);

/** 胶片颗粒（静态噪点，逐帧不变，确定性） */
export const Grain: React.FC<{opacity?: number; seed?: number; z?: number}> = ({
  opacity = 0.045,
  seed = 1337,
  z = 55,
}) => {
  const url = useMemo(() => {
    const size = 256;
    const canvas = document.createElement('canvas');
    canvas.width = size;
    canvas.height = size;
    const ctx = canvas.getContext('2d');
    if (!ctx) return '';
    const img = ctx.createImageData(size, size);
    const r = rng(seed);
    for (let i = 0; i < img.data.length; i += 4) {
      const v = Math.floor(r() * 255);
      img.data[i] = v;
      img.data[i + 1] = v;
      img.data[i + 2] = v;
      img.data[i + 3] = 255;
    }
    ctx.putImageData(img, 0, 0);
    return canvas.toDataURL('image/png');
  }, [seed]);
  if (!url) return null;
  return (
    <AbsoluteFill
      style={{
        zIndex: z,
        opacity,
        backgroundImage: `url(${url})`,
        backgroundRepeat: 'repeat',
        backgroundSize: '256px 256px',
        pointerEvents: 'none',
      }}
    />
  );
};

/** 画面整体色偏（暖→冷）——用叠加层统一色温，避免各组件各调一套 */
export const Grade: React.FC<{color: string; opacity: number; z?: number; blend?: string}> = ({
  color,
  opacity,
  z = 52,
  blend = 'soft-light',
}) => (
  <AbsoluteFill style={{zIndex: z, background: color, opacity, mixBlendMode: blend as any, pointerEvents: 'none'}} />
);

export const _unused = {W, H};
