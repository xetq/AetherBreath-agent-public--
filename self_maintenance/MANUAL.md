# 自维护说明书（MANUAL）

> **谁读**：AB 自己（AetherBreath）。
> **什么时候读**：只有在**要给自己做手术**的时候 —— 主人明确说"维护你自己"、"改你自己的代码"、
> "修一下你自己的 XX"，或把这份手册的路径给了你。
> **平时不要读它**，也不要读本目录其它文件。它是手术室里的东西，不是随身行李。

---

## 〇、三条红线（违反任何一条，这次维护都不该继续）

1. **不得自行决定开始维护。** 必须由主人明确许可（口头授权即可，不必走审批）。你发现了一个
   自己的 bug、想到一个优化 —— 先**报告**，等主人说"改"。
2. **不得在自己身上直接动刀。** 需求还没想清楚、方案还没验证过，就先在 `workbench/` 里试。
   试好了再动生产文件。**先试后改，不是边改边试。**
3. **必须留回滚点。** 动生产文件前先 `snapshot.py begin`。没有快照的维护 = 裸奔。
   回滚永远**只在主人点头后**执行（`--yes`），永不自动回滚。

---

## 一、维护前必做（"先问懂，再维护"）

按顺序做完这四件，再动手：

1. **写清维护目标**（一句话）。写不出来就说明你还没搞懂，别开始。
   - 差的目标："优化日志"
   - 好的目标："`log_digest` 把子代理的会话日志也算进来"

2. **找更好的方案**（偷懒梯子，先低后高）：
   - ① 这件事**真需要做吗**？（YAGNI）
   - ② **现有代码里已经有了吗**？先 grep，别重写
   - ③ **标准库/现有依赖**能覆盖吗？
   - ④ **能不能不改代码做到**？改 `.env` / `config.yaml` / 换调用方式，往往就够了
   - ⑤ 只有上面全不行，才动生产代码
   **能不改代码就不改代码** —— 这不是怕事，是维护成本最低的做法。

3. **读代码，不是读记忆。** 你要改的那条链路，从入口读到出口，把每个调用点找齐
   （改一个共享函数，就要检查所有调用方）。

4. **有不懂的就问主人**（用提问卡）。只问**代码回答不了**的问题（意图、取舍、优先级），
   代码能查的自己查。不要猜，不要"我觉得应该"。

---

## 二、维护流程（照这个顺序走）

```bash
# 1) 先留回滚点（--paths 写你**打算动**的文件或目录）
python self_maintenance/tools/snapshot.py begin --name "一句话说明这次改什么" \
    --paths agent/logger.py agent/approvals

# 2) 动手。需要试的先去 workbench/ 试（那里不受审批约束，见 §六）
#    ……改代码 / 写文件……

# 3) 验收（三层：语法、回归测试、冷导入自检）
python self_maintenance/tools/verify.py

# 4) 收尾：算清这次到底动了哪些文件
python self_maintenance/tools/snapshot.py end

# 5) 复盘：把踩到的坑写进 NOTES.md（哪怕只是"我以为 X，实际是 Y"）

# 6) 反馈给主人（模板见 §三）
```

**第 4 步不能省。** `end` 会告诉你两件事：
- 实际改动清单（和你声明的是否一致）
- **范围外改动**（你没声明却动了的文件）—— 这种**没有前像，回滚救不回来**，必须如实报告

**第 3 步不通过就别继续。** 若需要回滚：

```bash
python self_maintenance/tools/snapshot.py list                      # 看有哪些快照
python self_maintenance/tools/snapshot.py rollback <快照名>          # 只打印计划，什么都不做
python self_maintenance/tools/snapshot.py rollback <快照名> --yes    # 主人点头后才执行
```

回滚前**先教主人手动备份** —— 手动备份永远最原始、也最靠谱。

---

## 三、维护后必做

### 1. 评估效果（别自夸，讲事实）

- 目标达成了吗？用**可复现的证据**说（跑通的命令、真机结果），不要"应该可以了"
- 有没有副作用？有没有别的地方受影响？
- 动了什么文件,在哪里,遗留了什么？诚实列出来

### 2. 反馈给主人（"平衡技术性质与接地气"）

服务对象是**懂 agent 但不太懂维护**的人。所以：技术内容要说准，但话要说人话。

模板：

```
做了什么：<一句话>
改了什么：<文件清单，几个就够>
证据：<命令 + 真实输出摘要>
你需要做什么：<有则写，没有就写"不需要你做任何事">
如果出问题：<回滚命令，或"先手动把 X 备份一份">
遗留：<有则写>
```

**禁止**：只报喜不报忧；用"已优化/已加固"这种没有证据的形容词；把失败说成成功。

### 3. 失败时

1. **先止损**：把自己现在关心的文件手动另存一份
2. **再回滚**：`snapshot.py rollback <名> --yes`（**先给主人看计划，得到许可**）
3. **再复盘**：失败原因写进 `NOTES.md`（这是最有价值的条目）
4. **最后报告**：如实说"这次没成，已还原到维护前"，附上手动的验证方式

---

## 四、工具包速查（`tools/`）

三个脚本都**不是** agent 工具，用 `execute_shell` / `execute_python` 调即可。
（这样设计是有意的：不进常驻工具表 = 不占你每轮的上下文。）

| 脚本 | 干什么 | 常用命令 |
|---|---|---|
| `snapshot.py` | 文件级前像 + 变更报告 | `begin --name X --paths …` / `end` / `list` / `rollback <名> [--yes]` |
| `log_digest.py` | 日志摘要（排查用） | `--list` / `--latest` / `--session <id>` / `--full` / `--dialog` |
| `verify.py` | 维护后验收三层 | 直接跑 / `--snapshot <名>` / `--fast` / `--save-baseline` |

### 排查问题时的正确姿势

```bash
# 先看哪个会话有问题（异常类越多越可疑）
python self_maintenance/tools/log_digest.py --list 30
# 再看那个会话发生了什么
python self_maintenance/tools/log_digest.py --session session_2026xxxx_xxxxxx
# 需要细节再展开
python self_maintenance/tools/log_digest.py --session <id> --full --dialog
```

摘要默认只给**异常骨架**（重复的同一件事压成 `N×` 一行）。实测一个 11.8 万字符的会话
压到 3.8 千字符，异常和失败调用一个不丢。**先看骨架，别一上来 `--full`。**

### 三条工具使用纪律

1. **`snapshot.py begin` 的 `--paths` 写准**。写得越准，`end` 的范围外告警越有意义。
   写目录（`agent/approvals`）比写文件省事，但会让范围外侦测变钝。
2. **密钥不进快照**（`.env`、`*.key` 会被拒收）——这是故意的：快照可能被分享或提交，
   不能把明文密钥存进去。改密钥类文件要**手动备份**。
3. **`verify.py` 说"全过"≠ AB 一定能启动**。它的三层是语法 / 回归测试 / 冷导入，
   **不含真实启动** —— 那条要人来做（见 §五最后一格）。

---

## 五、项目地图（维护定位用，不求全）

```
AetherBreath/
├─ agent/                  ★ 主程序与引擎（改动会弹审批）
│   ├─ agent.py            主循环、系统提示组装（load_system_prompt）、会话存取
│   ├─ approval.py         审批引擎本体（什么会被拦，规则在这里）
│   ├─ approvals/          审批规范包：一类审批 = 一个文件（_ 前缀不加载）
│   │                      _impact.py 是共用判定（后果分区：系统盘/自留地/项目内/项目外）
│   ├─ approval_lex.py     词法层：把命令拆成"动词 + 目标"
│   ├─ task_orchestrator.py 任务编排器：工具真正执行的地方（串行/并行管道、超时）
│   ├─ context_manager.py  上下文管理器：原文永不动，压缩只产出"视图"
│   ├─ logger.py           日志（JSONL、脱敏、异步、按会话分文件）
│   ├─ skill_system.py     技能扫描 + SKILL_REGISTRY.md 注册表注入
│   ├─ mcp_client.py / mcp_station.py   MCP 客户端与 station 注册
│   └─ （agent 不含 mid_turn —— 中期交互 2026-09-22 迁到 WebUI 侧 agent_webui/backend/mid_turn.py；
│        agent 只提供通用挂载点 register_after_tools_hook，见 agent.py「宿主扩展点」）
├─ agent_tools/            ★ 工具实现 + AVAILABLE_TOOLS / TOOLS_SCHEMA 注册表
│   └─ __init__.py         加工具要动这里（注册 + schema + __all__ + 超时表）
├─ agent_skills/           技能库正文；SKILL_REGISTRY.md 是**派生**注册表（自动同步）
├─ agent_MCP/              MCP station：一个 server = 一个文件夹（STATION.md + tools.yaml）
├─ agent_memory/
│   ├─ long_memory/        SOUL.md（身份）/ AGENTS.md（规则）/ MEMORY.md / USER.md
│   ├─ working_memory/     会话原文（次生物，不入快照）
│   └─ .condensed_sessions/ 上下文视图落盘（次生物）
├─ agent_webui/            网关（backend/ FastAPI + frontend/ React）
│   └─ webui.bat           ★ AB 的启动入口
├─ agent_workspace/        工作区：主人的项目 + 你的实验
├─ tests/                  回归测试（判据：一条命令、无需 LLM、不写真实文件）
├─ docs/                   设计与决策文档
├─ self_maintenance/       ★ 本系统（说明书/笔记/工作台/工具/快照/包/交付/日志）
├─ config.yaml             路径、模型、上下文管理器参数
└─ .env                    API key 等（**绝不进快照**）
```

**启动、停止与验收**：

| 动作 | 怎么做 |
|---|---|
| 启动 AB | `agent_webui\webui.bat`（加 `--ab` 顺带发开机请求；默认不起 AB，只在 UI 点「🟢 开机」） |
| 停止 AB（优雅） | UI 的「⏻ 优雅关机」按钮，或 `curl -X POST http://127.0.0.1:8900/api/agent/stop` —— 会在**工具/请求边界**落盘后退，会话不丢 |
| 强制关闭（卡死时） | UI 的「🛑 强制关闭」按钮，或 `curl -X POST http://127.0.0.1:8900/api/agent/kill`（杀整棵进程树） |
| 停网关本身 | 网关窗口按 `Ctrl+C`（会回收它拉起的 bridge，不留孤儿） |
| 回归测试 | `venv/Scripts/python -m pytest tests -q` |
| 代码自检 | `venv/Scripts/python check_deps.py`（依赖完整性） |

更细的启动/停止说明见 `agent_webui/README.md` §1。

**改这些文件会弹审批卡**（`agent/approvals/_impact.py` 的 `SELF_PREFIXES` 清单，属"引擎自留地"）：
`agent/approval.py`、`agent/approval_lex.py`、`agent/approvals/`、`agent/agent.py`、
`agent/task_orchestrator.py`、`agent/logger.py`、`agent/skill_system.py`、`agent_tools/`、
`agent_skills/`、`agent_webui/backend/{approval_adapter,bridge,api,main}.py`、
`agent_memory/long_memory/{SOUL,AGENTS,USER}.md`、`agent_logs/approval*`、`.env`、`.git`、
`tests/test_approval*`。
这不是障碍 —— 弹卡正是主人在**替你确认"这次手术你自己同意"**。卡上写清楚你要干什么。

---

## 六、自维护系统本身（这是什么、怎么用、绝不能做什么）

### 它存在的意义

你可以改自己的代码，但没有安全网的话，**一次改坏就再也打不开了** —— 谁来修？
这套系统就是那张网：快照管回滚、工作台管试错、笔记管别再踩同一个坑、日志管看清现场。

### 目录

| 目录/文件 | 是什么 |
|---|---|
| `MANUAL.md` | 本文件：维护说明书（只在维护时读） |
| `NOTES.md` | 踩坑笔记（**你维护**：每次维护后记一条） |
| `workbench/` | 临时工作台：随便造，**源码之外的地方** |
| `tools/` | 三个工具（snapshot / log_digest / verify） |
| `snapshots/` | 快照存档（每个文件夹 = 一次维护的前像 + manifest） |
| `packs/` | 自维护包：辅助维护的资料/模板/脚本（独立于技能系统） |
| `inbox/` | 交付目录：需要主人手动操作的东西放这里（附说明书） |
| `logs/` | 自维护日志（**给主人看的**，人类可读，一行一条） |

### 何时用

- 你要改 `agent/`、`agent_tools/`、`agent_skills/`、`agent_webui/`、`config.yaml` 等**生产文件**
- 你在排查"AB 自己哪里不对"
- 主人说"维护你自己"

### 怎么用

见 §二 的流程。核心就三句：**先快照，再动手；动完验收；收尾写笔记。**

### 工作台的边界（重要，别记错）

`workbench/` **不受审批约束的部分只有"文件操作"**：

| 在工作台里 | 是否免审批 |
|---|---|
| 读写/删除工作台内的文件 | ✅ 免（就这么设计的） |
| **执行**代码（`execute_python` / `execute_shell`） | ❌ 仍走审批 |
| 网络请求 | ❌ 仍走审批 |
| 拿工作台里的脚本去改生产文件 | ❌ 仍走审批（改的是生产文件，不是工作台） |

也就是说：工作台是"随便放东西的桌面"，不是"法外之地"。

### 绝不能做的事

1. **不能绕过审批去改自留地文件**（换路径、编码、间接写入 —— 都不行）
2. **不能改快照/日志来掩盖失败**（账本是在留痕，不是装饰）
3. **不能把 `workbench/` 里的实验品直接当作已验收的成果**（要落到生产必须走完整流程）
4. **不能删掉 `snapshots/` 里还没验证过的快照**（那是回滚点）
5. **不能在没有快照的情况下动生产文件**（哪怕只是一行）

---

## 七、自维护包（`packs/`）

维护前**先翻一遍 `packs/`** —— 里面放的是能省事的现成东西（检查清单、模板、一次性脚本、
踩坑案例）。有能用的就直接用，别重写。

维护完若有可复用的产出，**把它挪进 `packs/` 并在 `packs/README.md` 里登记一行**。
一次性的失败产物不要留在工作台里积灰。

---

## 八、交付目录（`inbox/`）

有些维护**必须主人手动操作才能生效**（改 `.env`、重启网关、装依赖、双击某个脚本）。
这时把交付物放 `inbox/`，以文件夹分类,并配一份**主人能看懂**的说明：

```markdown
# <这次交付是什么>
## 怎么激活
（一步一条命令；能双击 .bat 就给 .bat，同时也写清纯手动怎么做）
## 期望现象
（做完应该看到什么；看不到说明没生效）
## 失败了怎么办
1. 先手动备份：<具体文件，手动复制到哪>
2. 再回滚：<snapshot.py rollback <名> --yes>
3. 还不行：<诊断命令>
```

**交付是维护的最后一步。** 因为交付可能伴随重启/重载，它可能是"这次维护前版本的最后一次正常
运行" —— 所以做交付前，快照、`verify`、笔记、日志必须都已经到位。

---

## 九、已知坑

看 `NOTES.md`（你维护的那份）。**维护前扫一眼，别重复踩。**

当前登记在案的第一条：**基线测试不是全绿的**（`baseline_failures.json` 里有 4 条既定失败）。
`verify.py` 认得它们，只会把"新增失败"判为你的责任 —— 但你自己心里要有数：
**别把既有红当成"我改坏了"而盲目回滚。**
