# agent_skills — AetherBreath 技能库

以「技能文件夹 = 一个技能」为单位，**复制粘贴即注册**，无需改动任何代码：

```
agent_skills/
├── SKILL_REGISTRY.md        # 技能注册表（自动生成，请勿手动编辑）
├── README.md                # 本文档
└── <技能名>/
    └── SKILL.md             # 技能正文（小写 skill.md 同样兼容）
```

## 如何添加 / 移除 / 更新技能

| 操作 | 做法 | 生效时机 |
|------|------|----------|
| 新增技能 | 把整个技能文件夹拷入 `agent_skills/<技能名>/`（含正文文件） | 下次会话启动自动注册 |
| 移除技能 | 删除对应文件夹 | 下次会话启动自动从注册表移除 |
| 更新技能 | 修改正文 frontmatter（version 递增）或正文内容 | 下次会话启动自动同步 |
| 手动同步 | `python agent/skill_system.py`（`--check` 只看不写） | 立即 |

每次新会话启动时，`agent/agent.py` 会调用 `agent/skill_system.py` 扫描技能库、
与 `SKILL_REGISTRY.md` 比对差异（**多增少删**），并把注册表全文作为
「技能目录快照」注入系统上下文；执行某技能时再按路径读取正文（渐进式披露）。

## 技能正文规范

技能正文为 Markdown 文件，**必须**以 YAML frontmatter 开头：

```markdown
---
name: research                # 必填，须与所在文件夹名一致
description: 一句话说明能力与适用场景，便于注册表检索
version: 1.0.0                # 建议；缺省按 0.1.0 注册
tags: [search, web, 摘要]     # 可选；YAML 数组或逗号字符串均可
---

# 技能说明
（正文：何时使用、操作步骤、注意事项……模型会按需读取这里）
```

约定与容错：

- 正文文件名 `SKILL.md` / `skill.md` 均可（同目录都存在时 `SKILL.md` 优先）。
- 注册表以 frontmatter 的 `name` 为技能名，因此 **name 必须等于所在文件夹名**，
  否则该技能不会注册（保证技能名能唯一定位到正文路径），并在同步日志中提示原因。
- 缺少正文文件 / 无 frontmatter / YAML 解析失败 / 缺 `name`：不注册，日志提示。

## 注册表文件格式（工程师可读、可解析）

```markdown
# 技能注册表

> 自动生成，请勿手动编辑。
> 最后更新: 2026-09-06 14:32:01

| 技能名 | 描述 | 版本 | 标签 |
|--------|------|------|------|
| test-skill | 这个技能用来测试技能模组的加载情况 | 0.1.0 | test, skill-system |
```

注册表由 `agent/skill_system.py` 统一生成与维护；表格正文行无变化时不会因时间戳
而重写文件。解析时跳过 `#` / `>` 注释行与表头、分隔行即可拿到数据行。

## 相关文件

- `agent/skill_system.py` — 技能系统核心（扫描 / 解析 / 渲染 / diff 同步 / 注入块 / CLI）
- `config.yaml` → `paths.skills_dir` / `paths.skill_registry` — 目录与注册表位置配置
