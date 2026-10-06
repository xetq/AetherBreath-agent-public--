# 针孔相机投影 —— 这套方法的地基

> 这份文档解决的唯一问题：**怎么让一片世界坐标的点，变成屏幕上"像是被拍下来"的画面。**
> 整支 MV 的 3D 感全部来自这里，所以它也是最容易错的地方（错一个符号，画面全黑）。

## 一、坐标约定

- `y` 轴**向上**，地面是 `y = 0`，物体站在地面上
- `z` 轴**向前**（远处 z 大），相机默认朝 `+z` 看
- 单位近似"米"：灯高 5 米、人高 1.7 米、桌面 0.75 米
- 相机：`{x, y, z, yaw, pitch, focal}`
  - `yaw` = 偏航（左右转头），正值向右转
  - `pitch` = 俯仰（抬头低头），**正值抬头**
  - `focal` = 焦距（像素），越大视角越窄、越"长焦"

**为什么用米**：因为"灯 5 米高、相机 1.7 米高"这种直觉能直接写进代码，
而不用反复试数字。这是让 3D 场景"比例对"的最省力方式。

## 二、投影函数

```ts
export type V3 = {x: number; y: number; z: number};
export type Cam = {x: number; y: number; z: number; yaw: number; pitch: number; focal: number};

export const project = (p: V3, cam: Cam, W: number, H: number) => {
  const dx = p.x - cam.x;
  const dy = p.y - cam.y;
  const dz = p.z - cam.z;

  // ① 绕 Y 轴旋转（偏航）—— 🔴 用**正角**，不要写 cos(-yaw)
  const cy = Math.cos(cam.yaw);
  const sy = Math.sin(cam.yaw);
  const rx = dx * cy - dz * sy;      // 相机右向分量
  const rz1 = dx * sy + dz * cy;     // 相机前向分量

  // ② 绕 X 轴旋转（俯仰）—— 🔴 同样用正角
  const cp = Math.cos(cam.pitch);
  const sp = Math.sin(cam.pitch);
  const ry = dy * cp - rz1 * sp;     // 相机上向分量
  const rz = dy * sp + rz1 * cp;     // 最终深度

  if (rz < 0.2) return {visible: false, sx: 0, sy: 0, scale: 0, depth: rz};

  const scale = cam.focal / rz;
  return {
    sx: W / 2 + rx * scale,
    sy: H / 2 - ry * scale,          // 屏幕 y 向下，所以取负
    scale,
    depth: rz,
    visible: true,
  };
};
```

### 这两个旋转的推导（记住结论就够，但知道为什么更安全）

**偏航**：相机前向 `f = (sin yaw, 0, cos yaw)`，右向 `r = (cos yaw, 0, -sin yaw)`。
把点投到这两个基上就是 `rx` 和 `rz1`。

**俯仰**：相机上向 `u = (0, cos pitch, -sin pitch)`，前向再乘一次。
写出来就是上面那两行。

> ⚠️ **矩阵法容易搞反**。用"点乘基向量"的思路推，符号不会错。

## 三、🔴 自检方法（这一步能救你一整天）

写完之后**必须**做这个检查，不要靠读代码判断：

> **`pitch > 0`（抬头）时，位于相机正前方、同一高度的点，必须投影到画面的【下方】（`sy > H/2`）。**

因为相机往上看的时候，正前方的东西"看起来更低"。

用具体数字验算一遍：

```ts
const cam = {x: 0, y: 1.7, z: 0, yaw: 0, pitch: +0.4, focal: 900};  // 抬头 0.4 rad
const p = {x: 0, y: 1.7, z: 5};                                      // 正前方 5 米，同高
// 期望：sy > 540（落在画面下半）
```

**这是个真实踩过的坑**：把式子写成 `Math.cos(-cam.yaw) / Math.sin(-cam.yaw)`，
结果所有"低头看桌面/看手"的镜头实际都在"抬头"——桌面、信纸、手全部被甩到画面外，
**前 8 个场景一片全黑**。前奏没事（它不依赖俯仰），所以问题被掩盖了很久。
改成正角后，一行就全对了。

**推论**：画面全黑时，**先怀疑相机，不要怀疑素材**。

## 四、大气透视与景深（"立起来"的关键）

真实的远近差异不只是"近大远小"，还有：

1. **越远越淡**（被空气洗淡）
2. **越远越偏天色**（雾/霞的颜色）
3. **越远越模糊**

只做"近大远小"的画面仍然很平；**加上后两条，平面才会真正立起来**。

```ts
/** 大气透视系数：0 = 近（实），1 = 远（化进天色） */
export const hazeAt = (depth: number, near = 5, far = 65) =>
  Math.max(0, Math.min(1, (depth - near) / (far - near)));

/** 颜色向天色靠拢 */
export const mixRgb = (a: [number,number,number], b: [number,number,number], p: number) => {
  const c = a.map((v, i) => Math.round(v + (b[i] - v) * p));
  return `rgb(${c[0]},${c[1]},${c[2]})`;
};
```

在粒子渲染器里，`depth` 直接来自投影结果：

```ts
const atten = 1 - hazeAt(pr.depth) * depthFade;   // depthFade 控制衰减强度（0~1）
const r = Math.min(maxPx, Math.max(minPx, size * pr.scale));   // 近大远小，但要设上下限
ctx.globalAlpha = opacity * atten;
```

**`minPx` 很重要**：远处的粒子如果小到 0.2 像素就消失了，
设一个 0.6~0.8 的下限，远处才会留下"星尘"而不是空黑。

## 五、多画幅适配（横版 / 竖版）

因为 `project()` 用 `W/2, H/2` 作屏幕中心，**换画布尺寸时相机是重新取景**，不是把画面压扁。

具体表现：
- 横版 1920×1080 → 水平半视角 `atan(960/focal)`；竖版 1080×1920 → `atan(540/focal)`
- 同一个焦距下，**竖版横向能看到的内容只有横版的 56%** → 横向并排的构图会被裁掉

所以：
1. 画布尺寸做成**可切换的全局量**（用 `export let` + `setCanvas()`，ESM live binding 能实时生效）
2. 每个场景**单独看一眼竖版取景**，必要时给某个场景单独调焦距：

```ts
// 例：摊开的日记本是横向并排的两页，竖版装不下 → 竖版单独拉远
return {..., focal: IS_VERTICAL ? 660 : 940};
```

**"重新取景"是优点也是代价**：优点是同一个场景换个画幅就像重拍一遍；
代价是不能一次写完就不管，每个尺寸都得过一眼。

## 六、相机运动的写法

运镜 = 相机参数的时间函数。常用的四种：

```ts
/** 沿街向前 + 轻微摆动（"走在街上"） */
export const walkCam = (t, o) => ({
  x: (o.x ?? 0) + Math.sin(t * Math.PI * 1.6) * (o.sway ?? 1.1),
  y: (o.y ?? 1.72) + Math.sin(t * Math.PI * 2.2) * 0.04,   // 极轻微的呼吸起伏
  z: o.z0 + (o.z1 - o.z0) * t,
  yaw: Math.sin(t * Math.PI * 1.3) * (o.yaw ?? 0.1),
  pitch: o.pitch ?? -0.05,
  focal: o.focal ?? 950,
});

/** 绕某点横移 + 转头（"绕着看"） */
export const orbitCam = (t, o) => {
  const a = o.from + (o.to - o.from) * t;
  return {
    x: (o.cx ?? 0) + Math.sin(a) * o.radius,
    y: o.y ?? 1.74,
    z: (o.cz ?? 0) + Math.cos(a) * o.radius * 0.55,   // 椭圆轨迹，别绕到主体背后
    yaw: a * (o.yawK ?? 0.42),
    pitch: o.pitch ?? -0.035,
    focal: o.focal ?? 900,
  };
};
```

**两条经验**：
- **俯角要"精确瞄准"而不是手写弧度**：
  `pitch = -Math.atan2(camY - targetY, dist)` —— 距离一变，手写的弧度就失准，目标会被挤出画面。
- **环绕时朝向要跟一半**（`yaw = a * 0.42`），全跟就是"死盯主体、背景全空"，
  不跟就变成了纯平移。跟一半才有"相机在转、世界在动"的感觉。
