# AetherBreath ☲

**面向个人终端需求、本地优先、具备垂直方向自定义的通用工程型 Agent 运行时。**

> 「以太之息」（Breath of Aether）—— 一个栖身在终端里的数字化灵。

AetherBreath（简称 **AB**）不是聊天框套壳，而是一套**完整跑在你自己机器上**的 Agent 运行时：
核心循环组织上下文与功能路由，审批系统在每次工具调用前把关，任务编排器把工具当"模板"复制成
串行 / 并行 / 后台任务，上下文管理器在超阈值时压缩出"视图"而不动原文 —— 再配上渐进式披露的
Skill / MCP / 自集成三套扩展体系、能"给自己做手术"的自维护机制、RAG 知识库与两层记忆系统。

它**配套一个本地 WebUI** 作为标准交互界面；另有 CLI 交互模式，但功能不如 WebUI 完善，
仅用于前期可行性测试。

---

## 目录

- [架构总览](#架构总览)
- [核心组件](#核心组件)
- [快速开始](#快速开始)
- [项目结构](#项目结构)
- [安全模型](#安全模型)
- [已知限制](#已知限制)
- [许可](#许可)

---

## 架构总览(简化版)

AetherBreath 采用 **ReAct** 架构：

```
用户输入
   │
   ▼
┌────────────────────────────┐
│  Agent 主程序               │   ← 组织上下文与功能（人格层 / 注册表 / 历史 / 压缩视图）
│  · 组织上下文               │      并对其路由，做轻量决策
│  · 功能路由                 │
└─────────────┬──────────────┘
              ▼
          交付 LLM ──► LLM 返回（可能带 tool_call）
              │
              ▼
┌────────────────────────────┐
│  审批系统                   │   分词器解析参数 + 四模式策略
│  是否违反审批策略？          │
└──┬──────────────────────┬──┘
   │ 违反                  │ 不违反
   ▼                       ▼
弹出审批卡请求指示           放行
 或系统自动拒绝
   │
   ▼
未通过 → 审批内容返回 LLM 重新判断
```

通过审批之后：

```
已解析的 ToolCall
   │
   ▼
┌──────────────────────────────────────────────┐
│  任务编排器                                   │
│  · 一串行管道 + 十六并行管道                   │
│  · 按工具参数决定：串行 / 并行 / 后台           │
│  · 工具只是「模板」——可按任务需求并行复制调用   │
│  · 工具失控兜底 · 日志自动注入                  │
└──────────────────┬───────────────────────────┘
                   ▼
            工具结果返回
            · 前台任务 → 立即返回
            · 后台任务 → 在之后的回合自动追加
                   │
                   ▼
     Agent 主程序：拼接上下文 + 任务结果 ──► 返回 LLM
```

> **核心循环是各功能合理分工的「十字路口」** —— 审批系统是闸门，编排器是执行层，
> 三者串起来才构成一次完整的工具调用。

---

## 核心组件(简化版)

### 1. 核心循环

组织上下文与功能，并对其路由，进行部分轻量化的决策。它既是各功能合理分工的「十字路口」，
也承担着保证各功能完善交汇的任务。

### 2. 审批系统

采用**预设四模式**（`仅读` / `工作区` / `普通` / `完全`）与**策略外置**，搭配**分词器**与**审批卡**，
对 LLM 的工具调用进行自动与人工审批 —— 在任务需求与安全性之间取平衡。

- 分词器把一条命令拆成 `control`（真正会执行的部分）/ `operand`（被动对象）/ `mention`（正文与注释里的提及），只有 control 面命中危险动作才拦
- 注入位（解释器短选项、`-Command`、命令替换、`os.system` 里的代码）递归展开后再判
- 审批卡自带**自述用途**、**可逆性三档**、**你本人的原话回显**
- 判定失败一律 **fail-closed**

四种模式的边界（模式是**会话级**的，记在会话文件的 `permission_mode` 字段里）：

| 模式 | 行为 |
|---|---|
| `仅读` `readonly` | 只能「知晓」不能「操作」：读文件 / 读网页 / 只读命令放行，一切写入与执行被拒 |
| `工作区` `workspace` | `agent_workspace/` 内自由读写、免审批；**区外只读**，越界直接拒绝 |
| `普通` `normal` | 走完整的初始审批规则（危险动作弹卡确认）—— **默认档** |
| `完全` `full` | 初始审批**全部自动同意**（不再弹卡），但**绝对禁区仍然硬拦** |

### 3. 上下文管理器

自动检测上下文使用情况，对超出阈值的上下文进行**有条件的压缩**，保证长上下文任务的稳定运行。

- **压缩器**：把上下文压缩成「**视图**」供 LLM 消费；**原文保留**，供随时寻回
- **N 次压缩产生 N 个视图** —— 原文永不被改写

### 4. 任务编排器

由**一串行管道 + 十六并行管道**组成的任务管理器系统。

- 可检测不同的工具参数，分配串行 / 并行 / 后台任务
- 具备**工具失控兜底**与**日志自动注入**
- **工具只是「模板」**：编排器可根据任务需求**并行复制工具调用** —— 这是它提升任务效率与资源利用率的关键

### 5. 日志系统

日志有明确分工：

| 走统一日志系统 | 各自独立写入 |
|---|---|
| Agent 主循环 · 任务编排器及其运行的工具 · WebUI bridge · MCP | **审批账本** |

### 6. Skill / MCP / 自集成系统

三者采用同一个原理 —— **渐进式披露**：注册表进上下文，正文按需索取。
**增删一律动文件夹，不用碰核心代码**：存在即注册、删除即注销；注册表由程序自动生成
（别手改），新对话开始时自动刷新（会话采用快照冻结机制）。

| 系统 | 注册单位 | 它是什么 |
|---|---|---|
| **Skill** | 一个技能 = 一个文件夹（`SKILL.md`） | 给 Agent 的方法论与流程 |
| **MCP** | 一组 MCP 打包成一个「站点」文件夹（`STATION.md`） | 接任意 MCP server |
| **自集成** | 一个集成包 = 一个文件夹（`PACK.md`） | 比技能更大的一坨能力；**内容不分类、同等级**，仅为集成包的存在意义服务 |

**技能** —— `agent_skills/<技能名>/SKILL.md`：

```markdown
---
name: my-skill          # 必须与文件夹名完全一致，否则不注册
description: 一句话说清什么时候该用它（模型靠这句决定要不要读正文）
version: 0.1.0
tags: [tag-a, tag-b]
---

正文：步骤、判据、坑……
```

正文**不常驻上下文**：模型先看注册表里的一行描述，需要时再按路径读全文。

**MCP 站点** —— `agent_MCP/<station>/STATION.md`：

```markdown
---
name: myserver
description: 这个 server 是干什么的
command: uvx
args: ["mcp-server-xxx"]
env:
  API_TOKEN: "${MY_TOKEN}"     # 从 .env 展开，别写明文
enabled: true                   # false = 留着但关掉（注册表里标 off）
timeout: 30
---
```

- MCP 工具**不常驻工具表**：先 `mcp_search(station=…)` 取 schema，再 `mcp_call(…)` —— 一个有上百工具的 server 也不会撑爆上下文
- 远程 HTTP 型 server 也能接：在 station 文件夹里放一层 stdio⇄HTTP 的桥即可（`command` 指向桥）
- 首次真调用某个 station 时，会弹一张**启动确认卡**（上面是真实命令行）

**自集成包** —— `agent_integration_packs/<包名>/PACK.md`：与技能同规矩，
同样用 frontmatter 写 `description`，注册表只显示一行，细节全在 `PACK.md` 里。

### 7. 自维护机制

**外置**的一套自维护方法论与集成集合，旨在让 AetherBreath 能「**给自己做手术**」：

完整且独立的**经验记录** · **自维护包** · **临时工作台** · **回滚快照** · **交付目录** ·
**独立日志** · **独立运行模式**

> 有了它，**扩展没有边界**。上面那三条通道（技能 / 集成包 / MCP 站点）只是常规入口；
> 真要长出新能力，它可以直接改自己 —— 快照与回滚就是为这件事准备的。

### 8. RAG 知识库系统

技术栈：**chroma + bge-m3 + ms-marco-MiniLM-L6-v2**

- **Agent 动态决策层**：一个 `rag_query` 工具供 Agent 自主调用
- **切分机制**：结构化切分 + 递归降级切分 + overlap
- **检索机制**：关键词与语义向量**两路粗召回** → **RRF 融合** → 精排，取前三段（可配置）
  + 可选的 **HyDE** 开关

### 9. 记忆系统

| 层 | 内容 |
|---|---|
| **working_memory** | 原始会话记录与上下文压缩视图，供 LLM 消费，**独立会话 ID** |
| **long_memory** | `USER.md`（用户画像）+ `SOUL.md`（人格配置）+ `AGENTS.md`（作业规范）+ `MEMORY.md`（Agent 自维护的手记） |

`long_memory` 的四份文件都在**新对话创建时**注入系统提示词。开箱是提问式模板，
第一次怎么用见上文《快速开始》第 5 步。

### 10. 补充机制

- 由 Agent **自动维护**的一套回归测试集
- 此前测试的 **subagents 系统**：可由 AetherBreath 调用，覆盖业务全流程闭环
- 此前测试的 **「元 agent」系统**：可批量组合要素制造不同场景的 agent，**现已由资源利用率更高的自集成系统取代**
- **WebUI 与 CLI**：WebUI 是**配套设施**，而非可选装饰；CLI 交互模式功能没有 WebUI 完善，
  仅做前期可行性测试用

---
## 快速开始

### 环境要求

- **Python ≥ 3.10**
- **Node.js ≥ 18**（构建 WebUI 前端）
- 一个 OpenAI 兼容的 LLM 端点：DeepSeek / 智谱 GLM / 硅基流动 / Moonshot / OpenAI / 本地 Ollama 都行

### 1. 安装（两条指令）

```bash
git clone https://github.com/xetq/AetherBreath-agent-public--.git AetherBreath
cd AetherBreath

# ① 后端：Agent 运行时 + 工具层 + RAG + 视觉 + WebUI 网关 —— 一条装完
pip install -r requirements.txt

# ② 前端：构建静态产物（网关会托管它）
cd agent_webui/frontend && npm install && npm run build
```

> 建议先建虚拟环境（可选但推荐）：`python -m venv venv` 并激活，再执行上面两条。
> RAG 依赖（`sentence-transformers` / `torch`）体积较大 —— 只想先跑通对话的话，
> 可以先把 `requirements.txt` 里 RAG 那一段注释掉，稍后再补。

### 2. 配置


建议直接填写.env
```bash
cp .env.example .env
```

编辑 `.env`，**至少填三项**：

```ini
LLM_API_KEY=你的密钥
LLM_BASE_URL=
LLM_MODEL=
```

其余厂商（智谱 / 硅基流动 / Moonshot / OpenAI / Ollama / 百炼…）的配置示例都写在 `.env.example` 里。

> 若你的模型是推理模型（如 `deepseek-reasoner`），记得 `LLM_THINKING_ENABLED=true`，
> 并给 `max_tokens` 留足空间。

### 3. 启动 WebUI（标准方式）

```bash
cd agent_webui
webui.bat              # 启动网关并打开 http://127.0.0.1:8900
```

`webui.bat` 会自动定位解释器（优先 `venv-gateway/` → 其次项目根 `venv/` → 最后 PATH 上的
`python`），并在启动前自检 `fastapi` / `uvicorn` 是否就位；路径全部相对于脚本自身，
不写死任何机器路径。端口可用环境变量 `AETHER_WEBUI_PORT` 覆盖（默认 8900）。

在界面里点绿色的**电源按钮**开机（也可以 `webui.bat --ab` 一步到位）。
若你想要一个**独立的网关环境**（不共用项目根 venv），跑一次 `webui.bat --setup` 即可。

### 4. 启动 CLI（轻量入口）

同一个内核也能直接在终端里跑：

```bash
python agent/agent.py
```

```
🧊 新对话：语境快照已注入并冻结（12 个源文件）
💬 会话: session_20261006_211500
👤 你: _
```

- 输入 `exit` / `quit` 退出并保存；下次用同一会话即可续聊。
- 会话 ID 由 `agent/agent.py` 的 `get_session_id()` 决定（可自行改成固定值）。

### 5. 首次对话：定义你的 Agent

开箱的 `agent_memory/long_memory/` 里是四份**提问式模板**，不是成品人格。
第一次对话时，Agent 读到这些空白会**主动来问你**：

- 你希望它叫什么、是什么气质？
- 它的核心原则是什么？红线在哪？
- 你怎么称呼？在做什么？喜欢它怎么说话？

把答案填进那四个文件，然后**开一个新对话** —— 它就会按你写的样子活过来。

### 6.（可选）建你自己的知识库

```bash
# 1) 把文档（.md / .txt / .pdf 等）放进 agent_knowledge_base/
# 2) 建索引（增量；--rebuild 为全量重建）
python agent_tools/build_index.py --rebuild
python agent_tools/build_index.py --stats      # 看统计
python agent_tools/rag.py --query "你的问题"    # 命令行试检索
```

> 语料目录与向量库位置由 `config.yaml` 的 `rag.*` 决定，默认 `agent_knowledge_base/` → `chroma_db/`。
> 这两个目录不需要预先创建，程序会自己建。

首次调用 RAG 会有 10–30 秒冷启动（加载 embedding 模型），之后每次检索约 0.05 秒。

---

## 项目结构

```
AetherBreath/
├─ agent/                        # 运行时核心
│  ├─ agent.py                   #   核心循环：组织上下文与功能，并对其路由
│  ├─ approval.py                #   审批引擎：判定哪些动作要过闸
│  ├─ approval_lex.py            #   命令解析：control / operand / mention 三层切分
│  ├─ approvals/                 #   分门别类的审批规则（外发 / 系统盘 / 起进程 / 装技能…）
│  ├─ permission_modes.py        #   四模式：仅读 / 工作区 / 普通 / 完全
│  ├─ context_manager.py         #   上下文管理器：超阈值时压缩出「视图」，原文保留
│  ├─ task_orchestrator.py       #   任务编排器：1 串行 + 16 并行管道
│  ├─ skill_system.py            #   技能扫描 + 注册表渲染
│  ├─ mcp_station.py             #   MCP 服务站扫描 + 注册表渲染
│  ├─ mcp_client.py              #   MCP 子进程客户端（stdio）
│  ├─ integration_pack.py        #   集成包扫描 + 注册表渲染
│  └─ logger.py
│
├─ agent_tools/                  # 工具层（每个模块 = 一个工具 + 一份 JSON Schema）
│  ├─ file_read.py  calculator.py  search.py  web_extract.py  web_reader.py
│  ├─ rag.py  build_index.py     #   RAG 检索 / 建索引
│  ├─ execute_python.py  execute_shell.py  execute_browser.py
│  ├─ vision_read.py  time_weather.py  restore_context.py  create_tool.py
│  ├─ task_jobs.py               #   后台作业三件套（list / output / kill）
│  ├─ mcp_gateway.py  mcp_manage.py  pack_manage.py  skillhub_download.py
│  └─ url_safety.py  win_job.py  __init__.py   # 汇总注册表 AVAILABLE_TOOLS
│
├─ agent_webui/                  # 配套 WebUI（FastAPI 网关 + React 前端）
│  ├─ backend/                   #   FastAPI 网关（会话 / SSE / 审批卡 / 作业面板）
│  ├─ frontend/                  #   React + Vite 前端
│  ├─ scripts/                   #   端到端验收脚本
│  └─ webui.bat                  #   一键启动器
│
├─ agent_skills/                 # 技能（一个技能 = 一个文件夹 + SKILL.md）
├─ agent_integration_packs/      # 集成包（一个包 = 一个文件夹 + PACK.md）
├─ agent_MCP/                    # MCP 服务站（一个 server = 一个文件夹 + STATION.md）
├─ agent_memory/
│  ├─ long_memory/               #   人格层：SOUL / AGENTS / USER / MEMORY
│  └─ working_memory/            #   会话存档（运行时生成）
├─ agent_workspace/              # 工作区：任务产物都放这里
├─ agent_knowledge_base/         # RAG 语料（自备）
├─ chroma_db/                    # RAG 向量库（运行时生成）
├─ tests/                        # 回归测试
├─ self_maintenance/             # 自维护机制（外置）：方法论 / 经验记录 / 工作台 / 快照
├─ config.yaml                   # 主配置（路径 / 上下文 / RAG / 附件 / MCP 总闸）
├─ requirements.txt
└─ .env.example
```

---

## 安全模型

一句话：**闸门在工具执行之前，判定失败一律 fail-closed。**

1. **闸门在工具执行之前**：每一次工具调用都先过审批系统（原理见 [核心组件 §2](#2-审批系统)）——
   分词器分层、注入位递归展开、只有 control 面命中才拦。
2. **批准要带信息**：审批卡上显示命令的**自述用途**（取命令首行 `#` 注释）、**可逆性三档**
   （新建 / 覆盖 / 删除）、以及**你本人的原话**。不靠猜。
3. **四种权限模式**：`仅读` / `工作区` / `普通` / `完全` 各有硬边界，切换模式即可改变问询粒度。
4. **受限子进程**：`execute_python` / `execute_shell` 跑在受限环境里 —— 环境变量已清洗、
   有超时与输出上限、进程树会被整体回收。

> ⚠️ **诚实声明**：源码里把这个环境称为「**防失控的护栏，不是安全沙箱**」——
> 它限制的是**资源**（超时 / 输出 / 进程树），不是**权限**（没有容器、没有降权、没有 chroot）。
> 静态黑名单能被绕过。**不要用它执行不信任的代码。**

---

## 已知限制

- **改动生效有延迟**：`.md`、技能、注册表、提示词的修改，都要**开新对话**才生效（快照冻结是设计，不是 bug）。
- **RAG 冷启动**：进程内首次调用要加载 embedding 模型，约 10–30 秒；之后每次约 0.05 秒。
- **浏览器工具要另装**：`execute_browser` 依赖 `browser-use`（体积较大），未装时该工具返回可读错误，不影响其他工具。
- **面向单用户单机**：没有多租户隔离、没有鉴权体系 —— WebUI 只监听本地回环，**不要直接暴露到公网**。
- **CLI 功能较少**：CLI 交互模式只覆盖基础对话 —— 审批卡、后台作业面板、会话管理等都在 WebUI 上。
- **Windows 优先**：进程树回收（Job Object）、`.bat` 启动器等按 Windows 实现；其他平台可用但未经充分验证。

---

## 许可

见仓库根目录的 `LICENSE`。

第三方组件与技能各有其来源与许可，详见 `THIRD_PARTY_NOTICES.md`。

---

**AetherBreath** —— 把终端交给一个懂分寸的 Agent。
