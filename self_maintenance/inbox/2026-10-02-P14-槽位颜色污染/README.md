# P14 交付：并行槽位的橙色污染（颜色只认"当前那条"，不是历史）

## 怎么激活

| 改了什么 | 需要做什么 |
|---|---|
| 前端 `OrchStrip.tsx`（颜色判定）+ `appStore.tsx`（结束/落回空闲时摘 `background` 标记） | **已重建 dist** → 浏览器 **Ctrl+F5**（不用重启 AB） |
| `agent_tools/task_jobs.py`（恢复被删掉的诚实口径，见下"顺手修的"） | **重启 AB** 才会进模型上下文 |

## 期望现象

1. 让 AB 用 background 挂一个作业（那一格亮**橙**）
2. 等它结束/交付（橙灯灭）
3. **再在同一格跑并行任务**（让 AB 并行调几个只读工具）
4. **期望**：亮起来的是**绿色**（并行任务），**不再是橙色**；hover 里也不再写「跨回合作业」

## 根因（一句话）

颜色来自一个**粘性槽位标记**：

```ts
if (v.background) s.job = true     // ★ 只要这一格历史上出现过 background 条目就永久置位
// 渲染：on ? (s.job ? 'job' : ...) : 'idle'
```

线程会被复用（同一格先跑后台作业、后跑并行任务是常态），而 store 会把**已结束**的后台条目
留在 `pipes` 里（落回 idle，却保留 `background: true`）—— 于是"橙色"成了这一格的**永久属性**。

**修法**：颜色只由「**当前在跑的那条**」赋值（`s.job = !!v.background`，不是 `|=`）；
非忙条目只贡献"上次是谁"和失败数；同时让 store 在条目结束/落回空闲时**摘掉** `background`。

## 顺手修的（同一类"文案/语义漂移"）

跑全量时抓到一条**新红**：`task_kill` 的提示词被改过，"**线程杀不掉**"这句诚实口径没了
（句子还断了）。这是 P11 专门立的契约测试抓到的 —— 已恢复，并把 `task_list` 的
"**这不是历史表**"口径一起恢复、同时把这句也纳入契约（下次再被删就会红）。

> 结论：**提示词契约测试是有用的**（这次它替我们发现了别人/自己改文案时的语义丢失）。

## 验证

```
node self_maintenance\packs\orch-orange-probe\probe_orch_slots.mjs   # 11/11（新增 9/10/11 三条）
   · 9  槽位跑过后台作业后，再跑前台并行任务必须回到绿色（修前红）
   · 10 陈旧条目排在后面也不许污染（修前红）
   · 11 当前真的在跑后台作业时仍必须橙色（修前就绿：防"修过头"）
venv\Scripts\python -m pytest tests\test_orch_color_pollution.py -q  # 4 passed（含真跑探针）
venv\Scripts\python -m pytest tests -q                              # **615 passed / 0 failed** ✅
cd agent_webui\frontend && npm run build                            # tsc + vite ✅
```

## 回滚（需要你点头）

- **前端这两个文件**：走这次维护的快照
  ```
  venv\Scripts\python self_maintenance\tools\snapshot.py rollback <P14 快照名> --yes
  ```
  然后 `cd agent_webui\frontend && npm run build` + 硬刷新。
- **`agent_tools/task_jobs.py` 与 `tests/test_job_delivery_ui.py`**（这次没写进快照声明范围，
  但它们在你要求的那个提交里是干净的）——用 **git** 回退：
  ```
  git checkout HEAD -- agent_tools/task_jobs.py tests/test_job_delivery_ui.py
  ```
  回退后重启 AB。

## 一句话总结

**"历史上发生过"不能当"此刻正发生"用。** 颜色、状态、标记都一样：
谁描述"现在"，就必须每帧重算，而不是攒一个粘性布尔。
