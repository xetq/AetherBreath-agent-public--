# self_maintenance/ —— AB 的自维护系统

**一句话**：让 AB 有能力给自己做手术，同时又**不会因为一次手术失败就再也打不开**。

这套东西解决的问题很具体：AB 能改自己的代码，但改坏了没人来修 —— 它自己就是那个"唯一会修
它的人"。所以维护必须有安全网：**能回滚、能试错、能看清现场、能记住上次怎么摔的。**

---

## 目录导航

| 路径 | 是什么 | 谁看 |
|---|---|---|
| `MANUAL.md` | **自维护说明书**：流程、红线、项目地图、工具用法 | AB（维护时读） |
| `NOTES.md` | **踩坑笔记**：每次维护后追加一条 | AB 写，人可看 |
| `tools/` | 三个工具：`snapshot.py`、`log_digest.py`、`verify.py` | 人 / AB 都能跑 |
| `workbench/` | **临时工作台**：实验用，随便造（见下方边界） | AB |
| `snapshots/` | **快照存档**：每个文件夹 = 一次维护的"维护前文件副本 + 变更清单" | AB |
| `packs/` | **自维护包**：辅助维护的清单/模板/脚本（独立于技能系统） | AB |
| `inbox/` | **交付目录**：需要你手动操作的东西放这里，附说明书 | **你** |
| `logs/maintenance.log` | **自维护日志**：一行一条，人话，给你看的 | **你** |
| `baseline_failures.json` | 基线已知失败的测试清单（见下文"第一次用之前"） | 人 / AB |

---

## 怎么让 AB 维护它自己

**关键一句：这套系统平时不注入 AB 的上下文**（不占它每轮的 token）。所以它平时"不知道"
自己有这么个手术室 —— 这是有意的设计。你要维护时，**明确告诉它**：

```
读 self_maintenance/MANUAL.md，然后维护你自己（要求：……）
```

它会先读说明书，再按里面的流程走（先快照 → 动手 → 验收 → 写笔记 → 向你汇报）。
**它不应该自己决定开始维护** —— 如果你发现它擅自改自己的代码，那是个该被记下来的问题。

---

## 三个工具怎么用（你也能自己跑）

在项目根目录：

```bash
# 1. 快照：备份"这次要动的文件"，并记录全项目的变更指纹
venv/Scripts/python.exe self_maintenance/tools/snapshot.py begin ^
    --name "修复XX" --paths agent/logger.py

# 2. 收尾：算出这次到底改了哪些文件（包括你没声明的"范围外改动"）
venv/Scripts/python.exe self_maintenance/tools/snapshot.py end

# 3. 验收：语法编译 + 回归测试 + 冷导入自检
venv/Scripts/python.exe self_maintenance/tools/verify.py

# 4. 回滚（先看计划，再加 --yes 才真执行 —— 永不自动回滚）
venv/Scripts/python.exe self_maintenance/tools/snapshot.py list
venv/Scripts/python.exe self_maintenance/tools/snapshot.py rollback 20260917-205012-修复XX
venv/Scripts/python.exe self_maintenance/tools/snapshot.py rollback 20260917-205012-修复XX --yes

# 5. 排查问题：日志摘要（先看哪个会话不对劲）
venv/Scripts/python.exe self_maintenance/tools/log_digest.py --list 30
venv/Scripts/python.exe self_maintenance/tools/log_digest.py --session session_2026xxxx_xxxxxx
```

（Windows 的 `cmd` 用 `^` 续行，PowerShell 用反引号，git-bash 用 `\`。）

---

## 第一次用之前：登记基线

**这个项目的测试集不是全绿的**（2026-09-17 实测：402 通过 / 4 失败 / 2 跳过）。
那 4 条红属于另一条工作线的未完成部分，与本系统无关。

`verify.py` 已经认得它们（清单在 `baseline_failures.json`），只会把**新增失败**判为
"这次维护弄坏的"。如果你之后修好了其中某条，刷新一下清单：

```bash
venv/Scripts/python.exe self_maintenance/tools/verify.py --save-baseline
```

---

## 附带：AB 的启动与停止

维护完要确认"AB 还活着"，就用到这两条（细节见 `agent_webui/README.md` §1）：

| 动作 | 怎么做 |
|---|---|
| 启动 | `agent_webui\webui.bat`（默认只起网关，在 UI 点「🟢 开机」才起 AB；加 `--ab` 可一键起） |
| 停止（优雅） | UI「⏻ 优雅关机」，或 `POST http://127.0.0.1:8900/api/agent/stop` —— 在工具/请求边界落盘后退出，**会话不丢** |
| 强制关闭 | UI「🛑 强制关闭」，或 `POST …/api/agent/kill` —— 只在卡死时用 |
| 停网关 | 网关窗口 `Ctrl+C` |

---

## 几个你该知道的设计取舍

1. **不走 git。** 目标是让"连 git 都没装"的人也能用 —— git 是更好的网，但它不能是**前提**。
   所以这里自备了一套最小快照：维护前把要动的文件复制一份前像，全项目扫一遍轻量指纹，
   收尾时对比出实际改动。
   👉 **如果你装了 git，请额外用它**（`git add -A && git commit` + `git tag`）。
   两道网比一道强，而且 git 管的是完整历史，这是本系统给不了的。

2. **密钥绝不进快照。** `.env`、`*.key`、`*.pem` 会被**拒收**（快照目录可能被分享或提交，
   明文密钥进去就是事故）。**改这类文件请手动备份。**

3. **回滚永不自动。** 只有你点头（`--yes`）才会执行。自动回滚 = 在没人监督时再做一次手术。

4. **"验收通过"≠"AB 一定能启动"。** 三层自动验收（语法/测试/冷导入）之后，
   还有一条**必须你自己做**：在 WebUI 里开个新对话，让它完成一件小事。
   用被改过的系统去验证它自己，只会给出假信心。

5. **工作台的"免审批"只限文件操作。** 在工作台目录内读写文件不弹审批；
   在工作台里**执行代码**、**发网络请求**仍然走审批。它不是法外之地。

6. **快照不是万能的。** 它只保护你**声明过**的范围（`--paths`）。维护期间"范围外"的改动
   会被 `end` 点名，但那些**没有前像，回滚救不回来** —— 所以 `end` 的输出要真看。

---

## 一次维护长什么样（真实流程）

```
你：读 self_maintenance/MANUAL.md，然后维护你自己。log_digest 看不到子代理的会话。

AB：（读手册）→（查代码）→（问你两个代码答不了的问题）→（snapshot begin）
    →（在 workbench 里试）→（改 agent_tools/…，弹审批卡，你点允许）
    →（verify 全过）→（snapshot end，报告改了 3 个文件、无范围外改动）
    →（写 NOTES.md）→（向你汇报：做了什么/证据/要不要你动手/失败了怎么退）
```

如果中途失败：

```
AB：这次没成。已还原到维护前（快照 XXXX），证据：<命令 + 输出>。
    根因记进 NOTES.md 了。你要不要我再试另一个方案？
```
