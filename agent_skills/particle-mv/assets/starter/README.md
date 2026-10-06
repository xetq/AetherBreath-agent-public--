# Starter —— 可复制的工程骨架

这套文件是「粒子 MV」的最小可运行地基。它们**不依赖任何具体项目**，
可以原样拷进一个新工程开始干活。

## 起步

```bash
# 1) 建工程（用 Remotion 的模板或手工建都行，只要有 react + remotion）
npm init -y
npm install remotion@4 @remotion/cli@4 react@19 react-dom@19

# 2) 把这些文件拷进 src/
#    theme.ts  →  src/theme.ts
#    lib/      →  src/lib/
#    parts/    →  src/parts/
#    text/     →  src/text/
```

## 文件职责

| 文件 | 做什么 | 什么时候改 |
|---|---|---|
| `theme.ts` | 画布尺寸（横/竖可切换）、调色曲线、字体、确定性随机 | 换成你的主题色时 |
| `lib/camera.ts` | **地基**：针孔相机投影 + 大气透视 | 基本不用改 |
| `lib/points3d.ts` | 3D 点云生成器（球/柱/盒/面/线/环/心形） | 需要新形体时加一个函数 |
| `lib/sceneTypes.ts` | 场景类型 + 常用相机运动（walk / orbit / dolly / pan） | 需要新运镜时加一个 |
| `lib/shotTiming.ts` | 段窗口 + 交叉溶解的不透明度计算 | 基本不用改 |
| `parts/Field3D.tsx` | 3D 粒子渲染器（投影 + 景深衰减 + 辉光） | 想调粒子质感时 |
| `parts/Frame.tsx` | 电影黑边 / 暗角 / 胶片颗粒 | 基本不用改 |
| `text/LyricLine.tsx` | 歌词逐字动效 | 改字体/配色/节奏时 |

## 一个场景长什么样

场景 = **相机怎么动** + **由哪几层粒子构成**。两者解耦，改运镜不用动画面。

```tsx
import {V3} from '../lib/camera';
import {column3, sphere3, dust3} from '../lib/points3d';
import {Scene3D, orbitCam} from '../lib/sceneTypes';

// 世界里的东西（y=0 是地面，单位≈米）
const LAMPS = [{x: 3.0, z: 2}, {x: -3.4, z: -9}];
const POLES: V3[] = LAMPS.flatMap((l, i) => column3(l.x, l.z, 0, 6, 0.1, 1700, 11 + i * 7));
const HEADS: V3[] = LAMPS.flatMap((l, i) => sphere3({x: l.x, y: 6, z: l.z}, 0.34, 950, 51 + i * 13, 0.3));
const DUST: V3[] = dust3({x: 0, y: 2.4, z: 14}, 30, 9, 46, 800, 31);

export const sceneStreet: Scene3D = {
  idx: 1,
  // t ∈ [0,1] 是这一句内部的进度
  cam: (t) => orbitCam(t, {cx: 1.3, cz: 2.4, radius: 8.6, from: -1.05, to: 1.05, y: 1.74}),
  layers: (t) => [
    {pts: DUST, color: '#24344E', size: 0.035, opacity: 0.7, jitter: 0.07, z: 5},
    {pts: POLES, color: '#18233A', size: 0.055, opacity: 0.95, jitter: 0.015, z: 10},
    {
      pts: HEADS,
      // 让灯忽明忽暗：颜色/亮度做成 t 的函数
      color: t % 1 < 0.5 ? '#FFBA60' : '#6EA5E8',
      core: '#FFFFFF',
      size: 0.075, maxPx: 9, opacity: 0.45 + 0.55 * Math.abs(Math.sin(t * Math.PI * 7)),
      depthFade: 0.4, jitter: 0.02, z: 12,
    },
  ],
};
```

## 把它们串成一支片子

```tsx
// scenes/LyricMV3D.tsx
const SEGS = [
  {id: 'pre', from: 0, to: 489, scene: sceneIntro},
  {id: 's1',  from: 441, to: 672, scene: sceneStreet},   // ← 段窗口故意重叠 → 交叉溶解
  // …
];

export const LyricMV3D: React.FC<{vertical?: boolean}> = ({vertical = false}) => {
  setCanvas(vertical ? 1080 : 1920, vertical ? 1920 : 1080, vertical, vertical ? 0 : 110);
  const frame = useCurrentFrame();
  return (
    <AbsoluteFill style={{background: '#03050B'}}>
      {SEGS.map((s) => {
        const op = segOpacity(s, frame);          // 淡入淡出 → 零硬切
        if (op <= 0.001) return null;
        return <SegView key={s.id} seg={s} frame={frame} opacity={op} />;
      })}
      {/* 歌词层、颗粒、暗角、黑边 */}
    </AbsoluteFill>
  );
};
```

## 两条一定要记住的

1. **画面全黑时，先怀疑相机，不要怀疑素材。**
   把相机位置和朝向打印出来，用 `pitch>0 时正前方的点应落在画面下方` 自检一遍。

2. **别用 `Math.random()`。** 逐帧渲染要求确定性，用 `rng(seed)` 和 `sin(frame*freq + phase)`。
