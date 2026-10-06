# agent_MCP/ —— MCP 服务站（station）

**一个 MCP server = 一个文件夹（station）。文件夹存在即注册。**

设计（决策与理由）：`docs/MCP设计.md` v2。这里只管"怎么用"。

```
agent_MCP/
├── MCP_REGISTRY.md      ← 机器生成：每 station 一行，会话开始时冻结注入系统提示词
├── <station>/
│   ├── STATION.md       ← 【你手写】frontmatter：启动命令/参数/env/开关/超时 + 正文说明
│   └── tools.yaml       ← 【机器生成】该 station 全部工具的完整 schema（mcp_sync 写）
├── mcp_sync.py          ← 从真 server 拉工具清单，写进 `<station>/tools.yaml`
└── README.md            ← 本文件
```

## 一、加一个 station（三步）

**1) 建文件夹 + 写 `STATION.md`**（frontmatter 必须有 `---` 包着）：

```markdown
---
name: myserver                                          # 必填；模型看到的 station 名
description: 一句话用途（会出现在注册表里，模型靠它挑 station）
command: uvx                                            # Python 包用 uvx，Node 包用 npx
args: ["mcp-server-xxx==1.2.3"]                         # 建议锁版本（见下）
env:
  SOME_TOKEN: "${SOME_TOKEN}"                           # 密钥只写占位
enabled: true
timeout: 30
never_parallel: false
origin: hand                                            # hand=你写的；auto=AB 自己集成的
---

正文：给人看的说明（渐进式披露的深层资料，模型按需才会读）。
```

**2) 生成工具清单**（会**真的启动**该 server 一次：握手 → `tools/list` → 退出）：

```bash
venv/Scripts/python agent_MCP/mcp_sync.py --list                 # 有哪些 station、各几个工具
venv/Scripts/python agent_MCP/mcp_sync.py --sync myserver        # 写 <station>/tools.yaml
venv/Scripts/python agent_MCP/mcp_sync.py --check                # 漂移检查（server 升级后 schema 变了会报）
```

**3) 同步注册表并重启 agent**：

```bash
venv/Scripts/python agent/mcp_station.py      # 把 station 目录现状写进 MCP_REGISTRY.md
# 注册表随系统提示词**冻结注入** → 重启 agent（WebUI 重启网关）后新 station 才出现在提示词里
```

> **热启动**：不重启也能用 —— `mcp_search` 是**直接读文件夹**的（实时真相），
> 所以同一会话内新建的 station 立刻能被搜到、被调用；只有"注册表那行"要等下次会话。

## 已装的 station

**开关就在每个 station 自己文件夹里**：`agent_MCP/<name>/STATION.md` 的 `enabled:`（`true`/`false`）。
关掉的站点**仍留在注册表**里、状态标 `off`（"装着但关着"要看得见，模型才能主动问要不要开）；
开着但没配好的（缺 `command` 等）标 `⚠ 不可用`——标 `on` 是骗人（真调不通）。

| station | 工具数 | 状态 | 备注 |
|---|---|---|---|
| `time` | 2 | on | 官方 time server（`uvx mcp-server-time`），免 token |
| `github` | 89 | on | 官方 GitHub MCP server v1.12.1（`github-mcp-server.exe`（需自行安装并加入 PATH））—— **需要 token**，申请与配置步骤见 **[docs/MCP-GitHub接入.md](../docs/MCP-GitHub接入.md)** |

## 二、模型怎么用它（两步，别猜参数）

```
mcp_search(station="myserver")                       → 该 station 的全部工具 + 完整参数 schema
mcp_call(station="myserver", tool="do_thing", arguments={...})   → 调用
```

注册表**刻意不含任何工具 schema**：有的 server 上百个工具（官方 GitHub MCP = 112 个），
全量注入等于每轮都白吃几万 token。**注入体积只与 station 数线性、与工具数无关。**

## 三、AB 自己装 station（`origin: auto`）

可以，但**只管我自己装的那些**（`origin: auto`）：

| 想干什么 | 怎么做 |
|---|---|
| 找候选 | `mcp_manage action="search"`（离线目录 + 教我联网自己查） |
| 装一个 | `mcp_manage action="add" name="…" command="…" args=[...]` → 建文件夹，**不用重启**就能用 |
| 开关 | `mcp_manage action="set_enabled" name="…" enabled=false`（只改 `enabled:` 那一行） |
| 移除 | `mcp_manage action="remove" name="…"`（整个文件夹挪进 `.trash/`，可恢复） |

**你手写的（`origin: hand`）我一个字节都不改**：撞名、开关、移除都会明确拒绝并告诉你改哪里。
集成失败会**真回滚**（删掉这次建的那份）；连续失败到上限就停手，等你 `force`。

## 四、开关

| 想关什么 | 怎么做 |
|---|---|
| 某一个 station | 它的 `STATION.md` 里 `enabled: false` → 同步注册表 → 重启 agent |
| 整个 MCP（连元工具与注册表都不给） | `config.yaml` 的 `mcp.enabled: false` |
| 停掉跑着的 server 进程 | 随 agent 退出统一 `terminate`；手动清：`tasklist \| findstr /i node` |

## 五、排障

1. `venv/Scripts/python agent/mcp_station.py --check` —— 注册表与目录一致吗？谁没注册、为什么？
2. `venv/Scripts/python agent_MCP/mcp_sync.py --sync <station>` —— 能握手吗？（报错带 **server 的 stderr 尾部**）
3. 对 AB 说「用 `mcp_manage` 测一下 `<station>`」—— 真调一次（需要 token 的 station 到这一步才验得通）
4. 看日志：`agent_logs/<session_id>_<date>.jsonl`，grep `mcp:`（进程起停、握手、耗时、超时 kill）

## 六、安全须知（别跳过）

- 启动一个 MCP server = **执行一个本地可执行文件**，危险级别等同 `execute_shell`。
  第一次真正使用某个 station 时会问你要不要批准，卡上显示真实命令行——**看清楚再批**。
- `args` 建议**锁版本**（`uvx pkg==1.2.3` / `npx pkg@1.2.3`）：不锁 = 上游塞什么就认什么。
- `env` 只写 `${VAR}` 占位，从 `.env`/环境展开；别把密钥明文写进仓库。
- 闸门看得见**调用参数**，看不见 server 拿到参数后在自己进程里干了什么 —— 别配不受信任的 server。

## 七、与技能系统的关系

同构（`agent_skills/` 那套）：**文件夹 = 单位**、md 作载体、frontmatter 放元数据、
注册表会话内**冻结注入**。区别：技能是"读正文照着做"，station 是"把工具接到工具通道上"。

## 八、迁移说明（2026-09-15 完成）

- 旧布局（`servers.yaml` + `servers.auto.yaml` + `tools/<name>.yaml`）**已彻底删除**：
  `time` 与 `github` 都已迁成 `agent_MCP/<name>/`（`STATION.md` 手写 + `tools.yaml` 机器生成）。
- AB 自维护工具 `mcp_manage` 的读写动作**只认 station**：集成 = 建文件夹、开关只改 `enabled:`
  那一行、移除挪进 `.trash/`；只动 `origin: auto` 的，手写文件一个字不改。
- v1 那套"跑同步脚本 + 重启才能用"的机制也一并删净：**热注册**、`mcp_tool_map`、编排器的
  运行期注册、bridge 的 late-wrap 钩子、`AETHER_MCP_REGISTRY` 兼容开关、`mcp_install` 闸门
  —— 现在只有 station 一条路径。
- 想看 v1 长什么样，只能翻 git 历史。**别从旧提交里照抄回来**：让上百个工具重新常驻上下文，
  正是改 v2 的直接原因。
