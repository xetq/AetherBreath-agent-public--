# inject-probe/ —— 系统提示注入探针

**是什么**：真 `import agent.agent`，用 stub logger 真调 `load_system_prompt()`，断言系统提示按
`SOUL → AGENTS → USER → MEMORY`（之后才是 SKILL_REGISTRY / MCP_REGISTRY）注入、四份全在、
各只出现一次、顺序正确。是 2026-09-18「把 USER.md 加进系统提示」那次维护的验收工具。

**什么时候用**：改过 `agent/agent.py` 的 `load_system_prompt()` 注入清单，或改过
`config.yaml` 里 `paths.*_file` 这些路径键之后。

**怎么跑**（在项目根；假环境变量是必需的 —— `agent.py` 顶层要求 `LLM_API_KEY` 非空，但不会真发请求）：

```bash
LLM_API_KEY=dummy LLM_BASE_URL=http://127.0.0.1:1/v1 LLM_MODEL=dummy \
  venv/Scripts/python.exe -X utf8 self_maintenance/packs/inject-probe/probe_user_inject.py
```

**期望现象**：最后一行是 `RESULT: PASS`；`injected names` 里 `SOUL.md / AGENTS.md / USER.md / MEMORY.md`
齐全（另含 SKILL_REGISTRY.md、MCP_REGISTRY.md），`head positions` 递增。

**副作用**：调用 `load_system_prompt()` 会顺带同步技能与 MCP 注册表 —— 只在内容真变化时写盘
（`sync_registry` 的比较**不含时间戳**），正常情况零写入。不碰任何生产代码。

**坑（别踩）**：脚本用「向上找到含 `config.yaml` 的那层」定位项目根 —— 这样挪目录不会算错
（最初写在 `workbench/` 时用的是 `parents[2]`，一挪位置就错）。
