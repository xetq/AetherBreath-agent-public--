# pytest-diag —— 从 pytest 进程内部掏状态（诊断用）

**解决的问题**：测试失败时 `--tb` 的 assertion 常被**截断**，而真正的错误详情
（进程退出码、stderr 尾巴、客户端内部状态）**只在测试进程里存在**。
2026-09-17 曾因截断的报错误判根因、追了好几轮（见 NOTES.md 同日后半段第 3 条）。

**怎么用**（项目根）：

    PYTHONPATH=self_maintenance/packs/pytest-diag venv/Scripts/python.exe \
        -m pytest <测试路径> -q -s -p diag_plugin

**能拿到什么**：teardown 时把目标对象的真实状态打到 stderr（示例针对 MCP 客户端：
`dead_reason / stdout_closed / poll / _unparsable / stderr_tail`）。

**副作用**：基本只读；示例里的 `mc.get_connection()` 可能触发一次重连，属可接受副作用。

**改哪里**：`pytest_runtest_teardown` 里的筛选条件与目标对象是按那次排查写的，
换场景要改（模板本身很短）。

**来源**：排查 `tests/test_mcp_client.py::test_stderr_flood_does_not_block_the_call`
时写的（该测试的真实死因只在进程内可见）。
