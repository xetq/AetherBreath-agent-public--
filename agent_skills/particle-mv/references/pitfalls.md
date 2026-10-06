# 12 个致命陷阱 —— 完整版

> 全部是实际踩过、并且**每一个都让我返工过一轮**的坑。
> 按"踩坑代价"排序：越前面越隐蔽、越容易浪费一整天。

---

## 1. 投影旋转符号写反（代价最大）

**症状**：所有"低头看"的镜头画面全黑。前奏（朝正前方看）却完全正常，
所以问题被掩盖——你会以为是"素材没生成"或"粒子数不够"。

**原因**：把这个式子写成了 `Math.cos(-cam.yaw)` / `Math.sin(-cam.yaw)`。
符号反了以后，`pitch = -0.9`（低头 52°）实际执行成了"抬头 52°"，
桌面、信纸、手全部被甩到画面外。

**修法**：
```ts
// ❌ 错的
const cy = Math.cos(-cam.yaw);
const sy = Math.sin(-cam.yaw);
const cp = Math.cos(-cam.pitch);
const sp = Math.sin(-cam.pitch);

// ✅ 对的（用正角）
const cy = Math.cos(cam.yaw);
const sy = Math.sin(cam.yaw);
const rx = dx * cy - dz * sy;
const rz1 = dx * sy + dz * cy;

const cp = Math.cos(cam.pitch);
const sp = Math.sin(cam.pitch);
const ry = dy * cp - rz1 * sp;
const rz = dy * sp + rz1 * cp;
```

**自检（必做）**：`pitch > 0`（抬头）时，正前方的点必须落在画面**下方**（`sy > H/2`）。
用具体数字跑一遍，不要靠读代码判断。

---

## 2. 相机 z 放在了物体的同一侧

**症状**：画面全黑，或只看到物体的"背面"。

**原因**：物体放在 `z ≈ 0`，而相机写成了 `z = +1.5`（正值）。
相机默认朝 `+z` 看，物体在 `z=0` 就在它的**背后**。

**修法**：物体在 `z≈0` 附近时，相机 z 必须是**负值**。
```ts
// ❌ 相机跑到桌子后面去了
cam: (t) => ({x: 0, y: 1.6, z: 1.35 - 0.18 * t, ...})

// ✅ 相机在桌子前方，朝 +z 看过来
cam: (t) => ({x: 0, y: 1.6, z: -1.35 + 0.22 * t, ...})
```

**通用自检**：写下相机位置后，问一句"物体相对相机在 +z 侧还是 -z 侧？"

---

## 3. 俯角与距离不匹配（手写弧度的通病）

**症状**：推近到特写时，目标从画面下缘滑出去（或者上缘）。

**原因**：写死了俯角弧度，但推近过程中相机与目标的距离在变，
固定的弧度只在某一个距离上是对的。

**修法**：**精确瞄准目标点**，让俯角随距离自动算：
```ts
cam: (t) => {
  const y = 1.6 - 0.95 * t;
  const z = -1.35 + 0.55 * t;
  const pitch = -Math.atan2(y - TARGET_Y, Math.abs(TARGET_Z - z));
  return {x: 0, y, z, yaw: 0, pitch, focal: 940};
}
```

**推论**：任何"推进到特写"的镜头都要用这个写法。
在俯视场景（看长椅、看桌面）里尤其重要。

---

## 4. 规则网格在透视下产生摩尔纹

**症状**：地面 / 纸面出现**放射状条纹**，像扫描线或条形码。

**原因**：规则网格点在透视投影后间距不均匀，密集处产生干涉图案。

**修法**：用**随机分布**代替规则网格；或加大抖动。
```ts
// ❌ 规则网格 → 放射条纹
const ground = grid3({x: 0, y: 0, z: 16}, 26, 78, 30, 62, 0.35);

// ✅ 随机薄层
const ground = dust3({x: 0, y: -0.01, z: 16}, 30, 0.06, 86, 3200, 77);

// 折中：网格保留但抖动加大
grid3(center, sx, sz, nx, nz, 0.022)   // 第 6 个参数是抖动
```

---

## 5. 用"整层位移"做下雨

**症状**：雨看起来像**同一段动画在循环播放**，机械、重复。

**原因**：把整层粒子统一下移一个周期然后复位。所有雨点同时跳回去，
观众的视觉系统立刻识破这是循环。

**修法**：**每滴雨有自己的高度和速度相位**。
```ts
type Drop = {x: number; z: number; y0: number; sp: number};

const buildRain = (drops: Drop[], t: number, fall = 9.5, span = 7.5): V3[] => {
  const out: V3[] = [];
  for (const d of drops) {
    const y = d.y0 - ((t * fall * d.sp) % span);   // 每滴各自的相位与速度
    out.push({x: d.x, y, z: d.z});
    out.push({x: d.x + 0.02, y: y - 0.22, z: d.z});
    out.push({x: d.x + 0.04, y: y - 0.44, z: d.z});
  }
  return out;
};
```
**推论**：任何"持续"的自然现象（雨、雪、落沙、飘落的花瓣）都不能用整层位移。

---

## 6. `scale = focal / depth` 在近处爆炸

**症状**：画面里出现一个**贯穿屏幕的巨大黑影**或超粗的柱子。

**原因**：`depth → 0` 时 `scale → ∞`。离相机极近的元素会被放大成天文数字。

**修法**：三重保险。
```ts
// ① 跳过太近的元素
if (a1.depth < 2.5) continue;

// ② 给尺寸设上限
const r = Math.min(maxPx, Math.max(minPx, size * pr.scale));

// ③ 线宽也要设上限
ctx.lineWidth = Math.min(30, Math.max(1.6, 0.27 * base.scale));
```

**注意**：近处的物体**确实**应该很大（物理正确），
所以不是简单地"跳过"就完事——很关键的一点是**别让相机贴到物体上**，
把环绕半径或最小距离留够。

---

## 7. 用线条勾勒人形

**症状**：人形看起来像"机器人"或"凯旋门"（头一个圆 + 身体一个框）。

**原因**：线条描边天然带"示意图"感。人对"人"的识别依赖**剪影的体量**，不是轮廓线。

**修法**：人形用**实心剪影**（填充的 path，让粒子把内部堆实）。
```ts
// ✅ 实心剪影
ctx.fillStyle = '#fff';
ctx.beginPath();
ctx.ellipse(cx, cy - s * 0.74, s * 0.185, s * 0.215, 0, 0, Math.PI * 2);   // 头
ctx.fill();
ctx.beginPath();
ctx.moveTo(cx - s * 0.46, cy + s * 1.1);
ctx.bezierCurveTo(cx - s * 0.46, cy - s * 0.1, cx - s * 0.34, cy - s * 0.52, cx, cy - s * 0.5);
ctx.bezierCurveTo(cx + s * 0.34, cy - s * 0.52, cx + s * 0.46, cy - s * 0.1, cx + s * 0.46, cy + s * 1.1);
ctx.closePath();
ctx.fill();
```

**推论**：形体的辨识度取决于"块状程度"。
块状形体（心、长椅、天平）用粒子堆很好看；细长形体（手指、车、人影轮廓）容易散成一团点。
**选意象时优先块状。**

---

## 8. 手的两个坑

**症状 A**：手聚成一团"花"，看不出五指。
**原因**：掌心画得太大，采样点全落在掌心区域。

**症状 B**：掌心与手指之间有一条黑缝。
**原因**：手指从掌心的**边缘**起笔，两段图形没有重叠。

**修法**：
```ts
// ① 掌心要小
ctx.ellipse(px, py, s * 0.128, s * 0.1, 0, 0, Math.PI * 2);

// ② 手指从掌心【中心】向外生长（这样自然重叠）
fingers.forEach(([ang, len]) => {
  const a = -Math.PI / 2 + ang;
  ctx.lineWidth = s * 0.034;                       // 手指要细
  ctx.beginPath();
  ctx.moveTo(px + Math.cos(a) * s * 0.02, py + Math.sin(a) * s * 0.02);   // 从中心起
  ctx.lineTo(cx + Math.cos(a) * s * len, cy + s * 0.05 + Math.sin(a) * s * len);
  ctx.stroke();
});

// ③ 掌心最后画，盖住指根，消除接缝
ctx.beginPath();
ctx.ellipse(px, py, s * 0.128, s * 0.1, 0, 0, Math.PI * 2);
ctx.fill();
```

---

## 9. 相机绕原点转，却不看向原点

**症状**：主体在镜头背后，画面全黑。

**原因**：环绕时只改了相机位置，忘了改朝向。
```ts
// ❌ 相机绕圈但一直朝 +z 看
{x: Math.sin(a) * R, z: Math.cos(a) * R, yaw: a}

// ✅ 始终朝向原点
{x: Math.sin(a) * R, z: Math.cos(a) * R, yaw: a + Math.PI}
```

**为什么容易忘**：如果主体在 `z=0` 两侧都分布（比如两侧都有路灯），
朝 `+z` 看也能看到东西，问题被掩盖。等遇到"主体集中在原点"的场景（天平、长椅）才暴露。

---

## 10. 换了图层，忘了改引用

**症状**：你以为已经去掉的旧场景还在片子里出现。

**原因**：场景配置（`scenes3d_*.ts`）和调度表（`SEGS`）是两处。
只改了配置或只改了调度，另一处还指着旧的。

**修法**：**调度表是唯一的渲染入口**。改完之后把段表打印出来读一遍：
```ts
SEGS.forEach(s => console.log(s.id, s.from, s.to, s.scene.idx));
```
确认每一段指向的场景是你想要的那个。

**排查用户反馈的"某某画面还在"时**：
先读调度表确认；如果表里确实没有，那就是**别的东西长得像它**——
例如给一辆行驶中的车加的"扬尘"，在暗场里看起来就是"落沙"。
  不要先把反馈当成"代码没改"，也不要轻易断言"用户看错了"——去把那几帧调出来看。

---

## 11. 帧内用了 `Math.random()`

**症状**：每次渲染出来的片子都不一样；或同一段视频播放时画面疯狂闪烁。

**原因**：Remotion 是逐帧渲染的，如果每帧都随机，帧与帧之间就不连续。
（另外并发渲染时，不同进程的随机序列也不一致。）

**修法**：一律确定性随机。
```ts
export const rng = (seed: number) => {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 4294967296;
  };
};
// 用 rng(固定种子) 代替 Math.random()
```
会随帧变化的动（呼吸、抖动）用 `sin(frame * 频率 + 相位)`，
相位用粒子的下标算，这样既自然又确定。

---

## 12. Composition id 带下划线

**症状**：Remotion 直接抛错：
`Composition id can only contain a-z, A-Z, 0-9, CJK characters and -`

**修法**：用连字符代替下划线。
```ts
<Composition id="LyricMV3D-V" ... />   // ✅
<Composition id="LyricMV3D_V" ... />   // ❌
```

---

## 附：其它值得记的小坑

| 坑 | 修法 |
|---|---|
| 执行环境有超时限制 | 分 5 段渲染（每段 600 帧），再 concat |
| `ffmpeg` 默认不覆盖已存在文件 | 加 `-y` |
| Remotion CLI 入口是 `remotion-cli.js` | 不是 `dist/index.js`（后者是库入口，跑了会静默退出） |
| 非 ASCII 路径在 shell 里变乱码 | 让 shell 自己解析路径（写本地编码的脚本），或用 Python 传路径 |
| 大文件一次性读取会卡住 | 分批读、分批写 |
| 缩放预览会"伪造"问题 | 检查静帧一律看 **1:1 原图** |
