# win-job-kill —— P11（Job Object 强杀 + 提示词同步）的双向补丁

**为什么有它**：P11 的 `snapshot.py begin` 是在**改动之后**才执行的（流程错误）——
那次快照的前像 = 改后状态，所以它**回滚不了 P11 的代码**。这批改动全是局部、可逆的字符串补丁，
于是补了这个**双向**脚本顶上。

## 文件

| 文件 | 用途 |
|---|---|
| `revert_or_apply_p11.py` | `--to pre` = 回退到 P11 之前（并删除新增的 `agent_tools/win_job.py`）；`--to post` = 应用 P11。默认只检查锚点，加 `--apply` 才写盘 |

## 用法

```bash
# 回退（主人点头后）
venv/Scripts/python self_maintenance/packs/win-job-kill/revert_or_apply_p11.py --root . --to pre --apply
# 重启 AB 即生效（改的是 agent_tools/*）

# 应用回来
venv/Scripts/python self_maintenance/packs/win-job-kill/revert_or_apply_p11.py --root . --to post --apply
```

## 它凭什么可信：**往返逐字节一致**

本次做过验证（2026-09-30）：

```
生产文件 → 复制到临时目录 → --to pre  → win_job.py 已删、全库无 _win_job/_ab_job 残留
                            → --to post → 与生产 4 个文件 **SHA256 全部一致**
```

也就是说：`--to pre` 是 `--to post` 的**精确逆操作**（不是"看起来改回去了"）。
脚本对每个锚点都要求**恰好命中一次**，改完先 `compile()` 再写盘。

## 什么时候还要用它

- 想回退 P11（快照救不了那次改动）
- 以后再把 `_kill_process_tree` 往 Job Object 方向改时，对照这里的 pre/post 两版
