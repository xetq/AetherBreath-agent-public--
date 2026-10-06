# P14 消息导航栏（右侧竖轨）—— 一条用户消息一条杠，悬停预览 / 点击跳转 / 滚动联动

对应来源：`agent_workspace/DSH框架参考对AB优化/参考DSH框架后,对AB的优化目标.txt` 第 2 条
（交接报告 §六 待办 3「二期：消息导航栏」）。**本次只动前端，不需要重启 AB、也不需要重启网关。**

## 怎么激活

| 改了什么 | 需要做什么 |
|---|---|
| 只动了前端（`lib/msgNav.ts`、`components/MessageNav.tsx`、`MessageList.tsx`、`lib/turns.ts`、`styles.css`） | **已重建 `dist`**（`tsc --noEmit && vite build` 通过）→ 浏览器 **Ctrl+F5 硬刷新** |

- 网关**不用动**（它只 serve `frontend/dist`，新产物已就位）
- AB **不用重启**（一个 `.py` 都没改）

## 期望现象

1. **对话区右侧边缘出现一条常驻的竖轨**：你发过的**每一条**用户消息 = 轨道上一根小横杠。
   - 只有 **≥2 条**用户消息的会话才显示（只发过一条时不显示，免得一根杠孤零零）
   - 轨道不高过可视区的 72%，超出时轨道**自己能滚**，当前那条会自动带进视野
   - `「用户交代」`/`后台作业交付` 挂在用户卡里，**不单独占一根杠**（它们不是独立消息）
   - 滑到底时高亮**最后一条**（哪怕最后一条在屏幕中线以下）

2. **悬停**任一根横杠 → 弹出**摘要预览**：
   - 第一行：`第 N 条消息 · M 字 / K 个附件`
   - 正文：该消息的**纯文本**摘要（Markdown 记号已去掉），最多 4 行、上限 140 字，超了带 `…`
   - 预览框**不会跑出屏幕**（贴视口左边或上下边时会自动夹回来）

3. **点击**任一根横杠 → 对话区**平滑滚动**到那条消息，那张卡**闪一下青绿光环**（约 1.5 秒后自动消失）。

4. **滚动联动**：你上下翻对话时，轨道上对应那根杠**自动高亮**（青绿加长），
   实时告诉你"现在读到哪一条"。第一条的前后语义是"我还在最上面"。

## 失败了怎么办

```
cd agent_webui\frontend
npm run build                                              # 期望：tsc 零错 + vite build 成功
node ..\scripts\verify_msgnav_ui.mjs                       # 期望 26/26 全过（只需网关在线，不用开机）
```

**现象 → 最可能的原因**：

| 现象 | 原因 |
|---|---|
| 右侧什么都没有 | ① 浏览器没硬刷新（旧 `dist` 还在缓存里）② 当前会话只有 1 条用户消息（本该不显示） |
| 竖轨在、但点了没反应 | 极可能是旧产物（这个 bug 的形态一模一样）→ 硬刷新，再看 `dist/assets/*.js` 时间戳 |
| 悬停不弹预览 | 同上；另外确认没开"减少动画"类扩展干扰 |
| 滚动时不高亮 | 同上 |
| 位置偏了 / 压住滚动条 | 窗口极窄或缩放异常；竖轨宽度只有 16px，正常不压消息 |

## 回滚（需要你点头）

```
# 1) 代码回滚到动工前（快照，含前像）
venv\Scripts\python self_maintenance\tools\snapshot.py rollback 20260930-115313-消息导航栏-右侧竖轨_悬停预览_点击跳转_滚动联动_ --yes

# 2) git 兜底（动工前已提交检查点 d25724e，含被改的 MessageList/ChatView/styles）
git checkout d25724e -- agent_webui/frontend/src
git clean -fd agent_webui/frontend/src/components/MessageNav.tsx   # 两个新文件按需删

# 3) 重建前端 + 硬刷新
cd agent_webui\frontend && npm run build
```

⚠️ **快照漏声明了 2 个文件**（`lib/turns.ts`、`scripts/verify_msgnav_ui.mjs`）——
它们**没有前像**，只能靠上面第 2 步的 git 检查点 `d25724e` 还原。新建的那个删掉即可。

## 我（DSH）已经做了什么

| 项 | 结果 |
|---|---|
| 快照 | `20260930-115313-消息导航栏-右侧竖轨_悬停预览_点击跳转_滚动联动_`（收尾已 `end`） |
| git 检查点 | `d25724e`（只提交了动工前的 3 个目标文件，其余工作区改动一律没碰） |
| 前端构建 | `npm run build` ✅（tsc 零错 + vite build 成功，产物 hash 已更新） |
| 真机验收 | `node agent_webui/scripts/verify_msgnav_ui.mjs` → **26/26 全过**（headless Edge + 真网关历史，不需要 AB 开机/LLM） |
| 全量验收 | `verify.py` → 语法 ✅ / 冷导入 ✅ / 回归 **609 passed / 1 failed**（那 1 条是 09-24 既有红，与本次无关） |
| 目视截图 | `agent_workspace\DSH框架参考对AB优化\消息导航栏_截图.png`（竖轨 + 悬停预览同框） |
| 笔记 | `self_maintenance/NOTES.md` 顶部新增 P13 条（六个 bug 的根因与教训） |
| 改动文件 | 改 4：`MessageList.tsx`、`styles.css`、`turns.ts`、`MessageNav.tsx`(占位→实现)；新增 2：`lib/msgNav.ts`、`scripts/verify_msgnav_ui.mjs` |

## 一句话总结

**用小横杠把"你发过的每条消息"变成可导航的索引** —— 悬停看摘要、点击跳过去、滚动时自动告诉你读到哪。
实现上两个关键决定：预览框必须**渲染在轨道之外**（`overflow-y:auto` 在两个轴上都裁剪），
以及锚点必须用**消息在原始流里的下标**（历史被 `limit=400` 截断时，"第几个回合"会有两种解释）。
