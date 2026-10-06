# 中期交互（mid_turn）搬家 —— 请重启 AB 并验证一次

> **状态（2026-09-22 15:31 更新）**：✅ 主人已重启（bridge PID 25180 启动于 15:28:36，晚于全部改动）
> 并手动验证通过 —— 15:30:14 日志出现「随本批工具返回注入 1 条「用户交代」」，交代以独立 user 消息到达。
> **剩余**：e2e 脚本（E2E.1~E2E.9，含作废场景）需要 AB **空闲**时执行，命令见下。


## 为什么需要你动手
代码改完、离线验收全过了，但**当前跑着的 AB 进程里还是旧代码**：
bridge 是启动时一次性 import agent 并注册注入的（模块每进程只 import 一次），
所以改动只有**重启 AB 子进程**之后才生效。

## 怎么激活
1. WebUI 里点「⏻ 优雅关机」（会话不丢，会在工具/请求边界落盘后退出）
2. 再点「🟢 开机」

（网关窗口不用动；点「开机」就会重新拉起 bridge 子进程。）

## 期望现象
- 状态回到在线，随便发一句 → AB 正常回复（说明搬家没改坏东西）
- 「用户交代」照旧：让 AB 跑一个几秒的工具（如 `execute_shell` 跑 `ping 127.0.0.1 -n 6`），
  跑的**途中**在输入框打字 → 发送按钮应显示**紫色**；发出后消息出现「用户交代」徽标，
  会从「已投递」走到「已注入」

## 想更硬核（可选，一条命令）
重启后跑真机端到端（真模型 + 真工具，覆盖"送达"与"作废"两个场景）：

    agent_webui/venv-gateway/Scripts/python.exe agent_webui/scripts/mid_turn_e2e.py

期望：E2E.1 ~ E2E.9 全部 PASS。

## 失败了怎么办
1. 先手动备份这三个文件（复制到别处）：
   `agent/agent.py`、`agent_webui/backend/bridge.py`、`agent_webui/backend/mid_turn.py`
2. 回滚（先不加 `--yes` 可以只看计划）：
   `venv/Scripts/python.exe self_maintenance/tools/snapshot.py rollback 20260922-145001-mid_turn搬到WebUI侧-agent只留通用钩子 --yes`
3. 旧模块副本另存于：`self_maintenance/workbench/mid_turn.py.removed-20260922`

## 顺带说明（本次的"范围外改动"）
声明快照时只写了 4 个文件，实际还改了 5 处文档 + 新增 2 个文件（探针与 inbox 说明）。
文档的迁移前副本在 `self_maintenance/workbench/doc_backup_20260922/`。
