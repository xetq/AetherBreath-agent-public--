# -*- coding: utf-8 -*-
"""诊断插件：只在排查时用，不进生产、不进测试目录。"""
import sys


def pytest_runtest_teardown(item, nextitem):
    if "stderr_flood" not in item.name:
        return
    import os
    out = sys.stderr
    out.write("\n[DIAG] ==== stderr_flood teardown ====\n")
    out.write("[DIAG] AETHER_MCP_STATIONS=%r\n" % os.environ.get("AETHER_MCP_STATIONS"))
    out.write("[DIAG] sys.executable=%r\n" % sys.executable)
    try:
        import mcp_client as mc
        out.write("[DIAG] mcp_client=%r\n" % mc.__file__)
        try:
            conn = mc.get_connection("stub_flood")
            out.write("[DIAG] alive=%r stdout_closed=%r poll=%r\n" % (
                conn.alive(), conn._stdout_closed,
                (conn._proc.poll() if conn._proc else None)))
            out.write("[DIAG] dead_reason=%r\n" % conn.dead_reason())
            out.write("[DIAG] unparsable=%r\n" % (conn._unparsable[:5],))
            out.write("[DIAG] stderr_kept=%d tail=%r\n" % (
                len(conn._stderr_tail), conn.stderr_tail(2)))
        except Exception as e:
            out.write("[DIAG] get_connection failed: %r\n" % (e,))
    except Exception as e:
        out.write("[DIAG] import failed: %r\n" % (e,))
