# compact-gate-probe —— 上下文压缩触发判据探针

**是什么**：离线探针，验证 `agent/context_manager.py::should_compact()` 的判据是
**纯 token 闸门**（不是轮数、不是别的）。

**什么时候用**：改过 `should_compact()` / `trigger_ratio` / `cooldown_rounds` /
`config.yaml` 的 `context_manager` 段之后，**必跑**。

**怎么跑**（任意目录）：
```bash
venv/Scripts/python self_maintenance/packs/compact-gate-probe/probe_compact_gate.py
```

**期望**：末行 `RESULT: PASS (0 failed)`，退出码 0。

**判据覆盖（11 条）**：
- 轮数再多也不压（防线已删）——**两条**
- 闸门边界：恰好 550000 压 / 549999 不压
- 冷却：闸门命中但冷却中 → 不压；冷却过 → 压
- 拿不到 usage（`prompt_tokens=None`）→ 不压
- **事故场景回归**：358k tokens @ 28 轮 → skip（旧规则会压）
- `DEFAULT_PARAMS.trigger_ratio == 0.55`
- `enabled: false` 优先于一切；`cooldown_rounds=0` 时纯 token 说话

**副作用**：无。纯函数调用，不读不写任何会话 / 视图文件，不联网。

**来源**：2026-09-22 维护「上下文压缩删轮数防线、闸门改 55%」产出。
该次先用它跑旧代码拿到 **5 FAIL**（证明判据不是空壳），改完 **11 PASS**。
