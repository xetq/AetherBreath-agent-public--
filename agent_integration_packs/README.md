# agent_integration_packs/ —— 自集成包

**是什么**：AB 集成外部资料的**可复用容器**。一个集成包 = 一个文件夹，
里面装这次集成搞到的东西（脚本、技能、MCP 站点、文档、配置……），外加一份 `PACK.md` 说明书。

**一句话**：`agent_skills/` 管技能、`agent_MCP/` 管 MCP 站点，**这里管"一整套打包好的能力"**。

## 目录结构

```
agent_integration_packs/
├── README.md            ← 本文件（「说明包」：集成方法 / 使用方法 / 管理方法）
├── PACK_REGISTRY.md     ← 机器生成：每包一行（包名 | 用途 | 说明书），会话启动注入提示词
├── .trash/              ← 移除的包（可恢复，不真删）
└── <包名>/              ← 一个包
    ├── PACK.md          ← 【必写】包说明书：frontmatter(name/description) + 正文
    ├── scripts/         ← 包工具（可选，叫什么名字都行）
    ├── skills/          ← 包工具（可选）
    ├── docs/            ← 包文档（可选）
    └── ...              ← 其它文件（配置、数据、前置依赖……）
```

**文件夹存在即注册、消失即注销** —— 扫描只认 `PACK.md`（与 `agent_MCP/` 只认 `STATION.md` 同规矩）。

## 一、AB 怎么用它（两步）

```
1. 会话启动 → 提示词里已有 PACK_REGISTRY.md（每包一行：包名 / 用途 / 说明书路径）
2. 主人点名（例："用 xxx 包做 yyy"）→ read_file 读 <包>/PACK.md → 按里面的说明干活
```

注册表**刻意只有三列**：包里有什么、怎么用，全在该包的 `PACK.md` 里（渐进式披露的下一层）。
理由：注册表每轮都在上下文里，多一列就多一份要维护、会腐烂的状态。

## 二、怎么建一个包

用工具（推荐）：

```
pack_manage(action="create", name="<包名>", description="<一句话用途>")
```

或手工：建文件夹 + 写 `PACK.md`（frontmatter 必须有 `name` 与 `description`，`name` 须与文件夹同名）。

```markdown
---
name: <包名>
description: <一句话用途 —— 会进注册表，AB 靠它判断要不要用这个包>
---

# <包名> —— <一句话>
（正文照下面五个小节写）
```

建完同步一次注册表（用工具会自动同步；手工建的话）：

```bash
venv/Scripts/python agent/integration_pack.py         # 同步注册表
venv/Scripts/python agent/integration_pack.py --list  # 有哪些包、谁没注册、为什么
```

## 三、PACK.md 写什么（五个小节）

| 小节 | 写什么 |
|---|---|
| 一、这个包是干什么的 | 一句话展开 |
| 二、提供了什么（包工具） | 有哪些工具、放在哪、怎么调（**包工具不分地位高低**） |
| 三、怎么用 | 推荐顺序：先并行读说明书/文档/配置，再按任务挑合适的工具 |
| 四、怎么管理 | 生命周期：要不要常驻进程、怎么启停、前置依赖、要不要定期更新 |
| 五、其它 | 来源 / 获取方式、已知坑、集成结果 |

**包内容变了就改说明书** —— 说明书与包对不上，比没有说明书更坏。

## 四、纪律（每个包都适用）

1. **不得自行开始集成**：动手前必须先给主人一份**集成提案**，等他确认。模板：

   ```
   ## 集成提案
   - 目标能力：联网搜索
   - 来源：GitHub / modelcontextprotocol/servers（官方仓库）
   - 方案：接入 Brave Search MCP Server
   - 需要下载：1 个 npm 包（约 2MB）
   - 依赖：Node.js（已安装）
   - 风险：低（官方仓库，无敏感权限）
   - 预期效果：Agent 可以通过 brave_search 工具搜索网页
   ```

2. **包工具由主人提供**（文件或获取方式）；除非主人明说"你自己挑"。
3. **成功失败都要汇报**：集成了什么、缺了什么（例如某个 skill 实在没找到），如实说。
4. **移除 = 挪进 `.trash/`**（可恢复），不真删。

5. **🔴 包必须绝对独立：用这个包时不依赖包外的任何文件。**
   - **自带运行环境**：Python 依赖建**包内 `venv/`**，二进制/工具放包内 ——绝不装进项目根 venv 或系统目录（那会污染 agent 自身，且换台机器就废）。
   - **不引用包外路径**：包内脚本/文档里的路径一律**从包根推导**（`os.path.dirname(__file__)`），不许写死指向包外的位置。
   - **宿主能力可以例外，但必须显式声明**：确有打包不进去的东西（node / 浏览器 / 系统命令 / 外部 API key），**必须在 `PACK.md` 里单独列出并注明「这是宿主依赖」** —— 声明了才算合规，藏着不算。
   - **环境不进 git，但重建命令必须写进 `PACK.md`**：换台机器要能一条命令把 `venv/` 重建出来。

## 五、与其它系统的关系

| 系统 | 单位 | 注册表 | 管什么 |
|---|---|---|---|
| 技能 | `agent_skills/<n>/SKILL.md` | SKILL_REGISTRY.md | 单个可复用技能（读正文照做） |
| MCP | `agent_MCP/<n>/STATION.md` | MCP_REGISTRY.md | 一个外部工具服务（起进程调用） |
| **集成包** | `agent_integration_packs/<n>/PACK.md` | PACK_REGISTRY.md | **一整套打包的能力**（含指路） |

三者同构（文件夹 = 单位、md 作载体、frontmatter 放元数据、注册表会话内冻结注入）。
**包不重复实现另外两套机制**：包内若要放 skill / MCP 站点，怎么让它们生效，
**由该包的 `PACK.md` 自己说明**（系统层不做挂载 —— 那会造出两份真相）。

另外两个容易搞混的地方：

- `self_maintenance/packs/` 是**自维护资料库**（探针 / 模板），不注册、不进上下文 —— 与本目录无关。
- `agent_workspace/<任务>/` 是**一次性任务文件夹**，做完不注册 —— 只有"以后还要复用"的东西才值得做成包。

## 六、排障

```bash
venv/Scripts/python agent/integration_pack.py --list    # 有哪些包、谁没注册、为什么
venv/Scripts/python agent/integration_pack.py --check   # 注册表与目录一致吗（有漂移退出码 1）
venv/Scripts/python agent/integration_pack.py --show    # 看看实际会注入什么
```

**注册表是快照**：新建包 / 改包名后，盘上立刻生效（`read_file` 就能读到），
但注入的那段要**开新对话**才刷新。
