# p12-delivery-ui —— P12（交付挂用户卡 + 交付当场可见）的双向补丁

**为什么有它**：P12 的快照是**动手前**建的（这次时机对了），但我**漏声明**了 4 个
"中途才决定要改"的文件，它们没有前像：

- `agent/agent.py`（交付回执 `take_delivered_notices`）
- `agent_webui/backend/bridge.py`（`done` 带 `delivered_jobs`）
- `agent_webui/frontend/src/store/appStore.tsx`（`jobDeliveredAt`）
- `agent_webui/frontend/src/components/ChatView.tsx`（收到回执后重放历史）

于是**只靠那次快照回滚不完整**。这个脚本补上：

```bash
# 回退（主人点头后）：P12 的全部代码改动
venv/Scripts/python self_maintenance/packs/p12-delivery-ui/revert_or_apply_p12.py --root . --to pre --apply
cd agent_webui/frontend && npm run build          # 前端必须重建 + 硬刷新
# 装回来
venv/Scripts/python self_maintenance/packs/p12-delivery-ui/revert_or_apply_p12.py --root . --to post --apply
```

## 它凭什么可信：**往返逐字节一致**

```
生产 4 文件 → 临时目录 → --to pre  → 全库 0 处 P12 痕迹（take_delivered_notices/delivered_jobs/jobDeliveredAt）
                        → --to post → 与生产 **SHA256 全部一致**
```

**踩过的坑（记在这，别再犯）**：脚本第一版对 **TSX** 文件也调了 Python 的 `compile()`
来校验语法 -> 直接 `SyntaxError: invalid character '：'`，那两个文件于是**没写盘**，
而最后的"逐字节一致"比对因为文件没被动过而**假通过**。
教训：**校验手段要匹配文件类型**（`.py` 用 compile，TSX 交给 `tsc`）；
以及"没改动"和"改对了"在比对结果上长得一样 —— 所以要**先看锚点报告**再看比对。
