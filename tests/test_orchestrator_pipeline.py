"""
task_orchestrator 管道化优化测试

核心验证：
1. 工具以「模板」形式存在，每次调用创建独立「管道」实例
2. 同一工具可并行多个管道，互不干扰
3. 工具库原函数不被修改（保持只读）
4. 原有编排行为不回归（并行聚合、顺序返回、未知工具、超时、barrier）
"""
import sys
import time
import threading
from pathlib import Path

import pytest

# 确保 agent 目录可导入
AGENT_DIR = Path(__file__).resolve().parents[1] / "agent"
sys.path.insert(0, str(AGENT_DIR))

from task_orchestrator import (  # noqa: E402
    ToolCall,
    TaskOrchestrator,
    ToolTemplate,
    ToolPipeline,
)


# ============ 工具模板 / 管道实例（新概念） ============

def test_template_creates_independent_pipelines():
    """同一模板每次 create_pipeline 返回独立管道实例"""
    template = ToolTemplate(name="echo", func=lambda msg: msg)
    p1 = template.create_pipeline(ToolCall(id="c1", name="echo", arguments={"msg": "a"}))
    p2 = template.create_pipeline(ToolCall(id="c2", name="echo", arguments={"msg": "b"}))
    assert p1 is not p2
    assert p1.tool_call.id == "c1"
    assert p2.tool_call.id == "c2"


def test_pipeline_runs_and_tracks_state():
    """管道 run() 后状态流转 pending→done，结果正确"""
    template = ToolTemplate(name="echo", func=lambda msg: f"hello {msg}")
    pipe = template.create_pipeline(ToolCall(id="c1", name="echo", arguments={"msg": "world"}))
    assert pipe.status == "pending"
    result = pipe.run()
    assert pipe.status == "done"
    assert result.success is True
    assert result.result == "hello world"
    assert pipe.finished_at is not None


def test_pipeline_failure_sets_failed_state():
    """工具抛异常时管道进入 failed，返回失败结果"""
    def boom():
        raise ValueError("boom")

    template = ToolTemplate(name="boom", func=boom)
    pipe = template.create_pipeline(ToolCall(id="c1", name="boom", arguments={}))
    result = pipe.run()
    assert pipe.status == "failed"
    assert result.success is False
    assert "boom" in result.error


def test_template_rejects_non_callable():
    """不可调用的工具无法包装成模板"""
    with pytest.raises(TypeError):
        ToolTemplate(name="bad", func=42)


# ============ 编排器：工具库不变 ============

def test_orchestrator_wraps_plain_functions_into_templates():
    """裸函数传入后内部被包装为 ToolTemplate，而非直接存函数"""
    def add(a, b):
        return a + b

    orch = TaskOrchestrator(tools_map={"add": add}, log_enabled=False)
    assert isinstance(orch.tools_map["add"], ToolTemplate)
    assert orch.tools_map["add"].func is add  # 原函数对象被引用，未被修改


def test_orchestrator_does_not_modify_original_tools_map():
    """执行后原工具库 dict 与函数对象保持原样"""
    def add(a, b):
        return a + b

    original_tools = {"add": add}
    orch = TaskOrchestrator(tools_map=original_tools, log_enabled=False)
    orch.execute([ToolCall(id="c1", name="add", arguments={"a": 1, "b": 2})])
    assert "add" in original_tools
    assert original_tools["add"] is add  # 身份不变
    assert original_tools["add"](1, 2) == 3  # 仍然可正常调用


# ============ 编排器：同一工具多管道并行 ============

def test_same_tool_parallel_pipelines_are_really_parallel():
    """同一工具 4 个并发调用：并行执行而非串行（总耗时接近单次而非 4 倍）"""
    def slow_work(ms: int = 200):
        time.sleep(ms / 1000)
        return ms

    orch = TaskOrchestrator(tools_map={"slow_work": slow_work}, max_workers=8, log_enabled=False)
    calls = [ToolCall(id=f"c{i}", name="slow_work", arguments={"ms": 200}) for i in range(4)]

    start = time.time()
    batch = orch.execute(calls)
    elapsed = time.time() - start

    assert batch.success_count == 4
    # 并行：4 × 200ms 应远小于 800ms；串行则接近 800ms
    assert elapsed < 0.7, f"4 个同工具调用串行化了: {elapsed:.2f}s"
    assert [r.result for r in batch.results] == [200, 200, 200, 200]


def test_same_tool_concurrent_calls_map_results_by_id():
    """并发调用同一工具时，结果正确回填到各自 tool_call_id（参数不串线）"""
    def identity(x: int):
        time.sleep(0.05)
        return x

    orch = TaskOrchestrator(tools_map={"identity": identity}, max_workers=8, log_enabled=False)
    calls = [
        ToolCall(id="c1", name="identity", arguments={"x": 1}),
        ToolCall(id="c2", name="identity", arguments={"x": 2}),
        ToolCall(id="c3", name="identity", arguments={"x": 3}),
    ]
    batch = orch.execute(calls)
    by_id = {r.tool_call_id: r for r in batch.results}
    assert by_id["c1"].result == 1
    assert by_id["c2"].result == 2
    assert by_id["c3"].result == 3


# ============ 编排器：barrier（模板级 never_parallel） ============

def test_template_never_parallel_forces_serial_layer():
    """模板标记 never_parallel 的工具被强制单独一层（barrier）"""
    def quick(x):
        return x

    barrier_template = ToolTemplate(name="interactive_tool", func=quick, never_parallel=True)
    orch = TaskOrchestrator(tools_map={"quick": quick, "interactive_tool": barrier_template}, log_enabled=False)

    calls = [
        ToolCall(id="c1", name="quick", arguments={"x": 1}),
        ToolCall(id="c2", name="interactive_tool", arguments={"x": 2}),
        ToolCall(id="c3", name="quick", arguments={"x": 3}),
    ]
    from task_orchestrator import DependencyResolver
    layers = DependencyResolver.resolve(calls, tools_map=orch.tools_map)
    barrier_layer = [layer for layer in layers if any(tc.id == "c2" for tc in layer)]
    assert len(barrier_layer) == 1
    assert len(barrier_layer[0]) == 1  # barrier 独占一层


def test_global_never_parallel_set_still_works_without_tools_map():
    """未传 tools_map 时，全局 _NEVER_PARALLEL_TOOLS 依然生效（兼容旧用法）"""
    from task_orchestrator import DependencyResolver
    calls = [
        ToolCall(id="c1", name="terminal", arguments={}),
        ToolCall(id="c2", name="calculator", arguments={"expression": "1+1"}),
    ]
    layers = DependencyResolver.resolve(calls, tools_map=None)
    assert len(layers) == 2  # terminal 独占一层，calculator 另一层


# ============ 回归保护 ============

def test_multi_tool_parallel_aggregation_returns_original_order():
    """多工具混合并行：结果按原始输入顺序返回"""
    def tool_a(x):
        time.sleep(0.15)
        return f"A{x}"

    def tool_b(x):
        time.sleep(0.05)
        return f"B{x}"

    orch = TaskOrchestrator(tools_map={"tool_a": tool_a, "tool_b": tool_b}, log_enabled=False)
    calls = [
        ToolCall(id="c1", name="tool_a", arguments={"x": 1}),
        ToolCall(id="c2", name="tool_b", arguments={"x": 2}),
        ToolCall(id="c3", name="tool_a", arguments={"x": 3}),
    ]
    batch = orch.execute(calls)
    assert [r.tool_call_id for r in batch.results] == ["c1", "c2", "c3"]
    assert batch.results[0].result == "A1"
    assert batch.results[1].result == "B2"
    assert batch.results[2].result == "A3"


def test_unknown_tool_returns_failure():
    """未知工具返回失败而不是崩溃"""
    orch = TaskOrchestrator(tools_map={"known": lambda: 1}, log_enabled=False)
    batch = orch.execute([ToolCall(id="c1", name="ghost", arguments={})])
    assert batch.success_count == 0
    assert batch.results[0].success is False
    assert "未知工具" in batch.results[0].error


def test_timeout_becomes_background_job():
    """超时到点后**转入后台作业**（2026-09-29 语义变更，主人决策 2："超时兜底自动转后台"）。

    旧契约是"判失败 + 超时文案"（本用例原名 test_timeout_marks_failed_with_timeout_error）；
    现在默认行为是"收编成作业，结果有地方回来"。仍然锁的是**到点即结算**：
    绝不许为了等结果而把回合拖住。
    """
    def slow():
        time.sleep(3)
        return "done"

    orch = TaskOrchestrator(tools_map={"slow": slow}, default_timeout=1, log_enabled=False)
    try:
        t0 = time.time()
        batch = orch.execute([ToolCall(id="c1", name="slow", arguments={})])
        elapsed = time.time() - t0
        r = batch.results[0]
        assert r.success is True
        assert "已自动转入后台" in str(r.result), r.result
        assert elapsed < 2.5, "到点即结算，不等它跑完（实测 %.1fs）" % elapsed
        assert orch.jobs.get("j1") is not None, "转后台必须真的登记成作业"
    finally:
        orch.shutdown()


def test_timeout_still_fails_for_interactive_tools():
    """反向用例：不能转后台的（交互/等待类）仍走老契约 —— 判失败 + 超时文案。

    没有这条，上面的"转后台"就可能被误当成"所有超时都不算失败"（放水）。
    """
    def slow():
        time.sleep(3)
        return "done"

    orch = TaskOrchestrator(tools_map={"ask_user": slow}, default_timeout=1, log_enabled=False)
    try:
        batch = orch.execute([ToolCall(id="c1", name="ask_user", arguments={})])
        assert batch.results[0].success is False
        assert "超时" in batch.results[0].error
        assert orch.jobs.list_jobs() == []
    finally:
        orch.shutdown()


def test_orchestrate_tool_calls_helper_still_works(monkeypatch):
    """便捷入口 orchestrate_tool_calls 兼容裸函数 dict。

    该入口会绕过主循环的审批闸门，已被 W1 守卫默认禁用（这是刻意的安全设计）。
    本用例测的是它的 dict 兼容性本身，所以显式打开守卫提供的逃生开关。
    """
    from task_orchestrator import orchestrate_tool_calls

    monkeypatch.setenv("AETHER_AUDIT_ALLOW_UNGATED", "1")

    def add(a, b):
        return a + b

    out = orchestrate_tool_calls(
        [{"id": "c1", "function": {"name": "add", "arguments": '{"a": 1, "b": 2}'}}],
        tools_map={"add": add},
        log_enabled=False,
    )
    assert out[0]["tool_call_id"] == "c1"
    assert out[0]["success"] is True
    assert out[0]["content"] == "3"


# ============ 常驻双通道：串行管道 / 并行管道 ============

def test_single_call_routes_to_serial_pool():
    """单工具调用走串行管道（orch-serial worker，并发=1）"""
    def whoami():
        return threading.current_thread().name

    orch = TaskOrchestrator(tools_map={"whoami": whoami}, log_enabled=False)
    try:
        batch = orch.execute([ToolCall(id="c1", name="whoami", arguments={})])
        assert batch.success_count == 1
        assert "orch-serial" in batch.results[0].result
    finally:
        orch.shutdown()


def test_multi_call_routes_to_parallel_pool():
    """多工具批走并行管道（orch-parallel worker，并发=8）"""
    def whoami(x):
        return threading.current_thread().name

    orch = TaskOrchestrator(tools_map={"whoami": whoami}, max_workers=8, log_enabled=False)
    try:
        batch = orch.execute([
            ToolCall(id="c1", name="whoami", arguments={"x": 1}),
            ToolCall(id="c2", name="whoami", arguments={"x": 2}),
        ])
        assert batch.success_count == 2
        for r in batch.results:
            assert "orch-parallel" in r.result
    finally:
        orch.shutdown()


def test_orchestrator_resident_pools_reused_across_execute():
    """常驻：同一实例多次 execute 复用同一双通道线程池（不反复建销）"""
    orch = TaskOrchestrator(tools_map={"echo": lambda x: x}, log_enabled=False)
    try:
        serial_id = id(orch._serial_pool)
        parallel_id = id(orch._parallel_pool)
        orch.execute([ToolCall(id="c1", name="echo", arguments={"x": 1})])
        orch.execute([
            ToolCall(id="c2", name="echo", arguments={"x": 2}),
            ToolCall(id="c3", name="echo", arguments={"x": 3}),
        ])
        assert id(orch._serial_pool) == serial_id
        assert id(orch._parallel_pool) == parallel_id
    finally:
        orch.shutdown()


def test_shutdown_blocks_further_execution():
    """shutdown 后编排器拒绝新任务（返回中断失败，而非复用已关池）"""
    orch = TaskOrchestrator(tools_map={"echo": lambda x: x}, log_enabled=False)
    orch.shutdown()
    batch = orch.execute([ToolCall(id="c1", name="echo", arguments={"x": 1})])
    assert batch.is_interrupted is True
    assert batch.results[0].success is False


def test_set_logger_swaps_log_instance():
    """常驻单例跨会话复用：set_logger 替换日志实例归属"""
    class FakeLog:
        def __init__(self):
            self.calls = []
        def info(self, msg):
            self.calls.append(msg)
        def warning(self, msg):
            self.calls.append(msg)
        def error(self, msg):
            self.calls.append(msg)

    log_a, log_b = FakeLog(), FakeLog()
    orch = TaskOrchestrator(tools_map={"echo": lambda x: x}, log_enabled=True, log_instance=log_a)
    try:
        assert orch.log_instance is log_a
        orch.set_logger(log_b)
        assert orch.log_instance is log_b
        orch.execute([ToolCall(id="c1", name="echo", arguments={"x": 1})])
        assert any("串行管道" in msg for msg in log_b.calls)  # 日志落到新实例
    finally:
        orch.shutdown()
