"""离线验证 bridge._MeteredStream 的计量时序契约（2026-10-02 连踩三次的那个坑）。

为什么必须有这个用例：这条链路出问题时**完全静默** —— 界面只显示"本会话还没有计量"，
不报错、不抛异常；而它的正确性又**只取决于时序**，肉眼 review 看不出来。

做法：直接 import 真实的 bridge 模块取 `_MeteredStream`（**不抠源码、不抄一份** ——
第二版用例就是抄源码抄到与真文件不一致，才把诊断带偏的），然后把 agent 那侧的
三步时序照原样演一遍：
    ① 消费者 for 循环把流排空；
    ② 循环之后才挂 `_ab_usage`；
    ③ 再调 `_ab_usage_sink(usage)`（agent 的显式通知）。

bridge.py 顶层有副作用（抢 stdout、打印启动信息），只临时把 stdout 换成 devnull
顶过 import，随后立刻还原。

用法：set PYTHONUTF8=1 && python tests/check_usage_metering.py
判据：退出码 0 = 全过。
"""
import contextlib
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(ROOT, "agent"), os.path.join(ROOT, "agent_webui", "backend")):
    if p not in sys.path:
        sys.path.insert(0, p)

PASS = 0
FAIL = 0


def ok(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK] " + msg)
    else:
        FAIL += 1
        print("  [FAIL] " + msg)


_buf = io.StringIO()
_real_stdout = sys.stdout
try:
    with contextlib.redirect_stdout(_buf):
        import bridge  # noqa: E402
finally:
    sys.stdout = _real_stdout

MS = bridge._MeteredStream


class FakeUsage:
    def __init__(self, p, c):
        self.prompt_tokens = p
        self.completion_tokens = c


class FakeInner:
    def __init__(self, chunks):
        self._c = list(chunks)
        self.closed = False

    def __iter__(self):
        return iter(self._c)

    def close(self):
        self.closed = True


EVENTS = []


def _patch_accounting():
    """把两个记账出口换成会记录的桩（不打真实账本、不发事件）。"""
    def drain(handed, kwargs):
        EVENTS.append(("drain", getattr(handed, "_ab_usage", None)))

    def sink(usage, kwargs):
        EVENTS.append(("sink", usage))

    bridge._account_streamed = drain
    bridge._account_usage_obj = sink


def simulate(chunks):
    """照 agent 的真实时序走一遍，返回 (事件, 透出的 chunk, 代理)。"""
    EVENTS.clear()
    proxy = MS(FakeInner(chunks), {"model": "m"})
    got = [c for c in proxy]                 # ① 排空
    usage = FakeUsage(300, 61)
    proxy._ab_usage = usage                  # ② 循环之后才挂
    s = getattr(proxy, "_ab_usage_sink", None)
    if callable(s):
        s(usage)                             # ③ 显式通知
    return list(EVENTS), got, proxy


def main():
    _patch_accounting()

    print("【1】透明代理：迭代内容原样透出；代理上挂着 sink")
    ev, got, proxy = simulate(["a", "b", "c"])
    ok(got == ["a", "b", "c"], "三个 chunk 原样取出：%r" % (got,))
    ok(hasattr(proxy, "_ab_usage_sink"), "代理上有 _ab_usage_sink（agent 靠它通知）")
    ok(isinstance(proxy, MS), "交出去的确实是真实 _MeteredStream 实例")

    print("【2】★ 时序契约：结算时必须拿到真 usage，且只记一次")
    valid = [u for _, u in ev if u is not None]
    ok(len(valid) == 1, "恰好 1 次有效结算（事件序列：%s）" % [k for k, _ in ev])
    ok(valid and valid[0].prompt_tokens == 300 and valid[0].completion_tokens == 61,
       "记到的是真 usage（300/61），不是 None")

    print("【3】兜底（排空时）先跑是允许的，但不许把权威信号挡掉")
    ok(any(k == "drain" for k, _ in ev), "兜底照常先跑一次（那一刻还没 usage，拿不到是正常）")
    ok(any(k == "sink" for k, _ in ev), "sink 仍然跑到了 —— 没被兜底立起来的去重标志挡掉")

    print("【4】重复结算护栏：sink 调两次 + close 一次，仍只记一次")
    EVENTS.clear()
    proxy2 = MS(FakeInner(["z"]), {"model": "m"})
    for _ in proxy2:
        pass
    u = FakeUsage(9, 9)
    proxy2._ab_usage = u
    s = getattr(proxy2, "_ab_usage_sink", None)
    if callable(s):
        s(u)
        s(u)
    proxy2.close()
    valid2 = [x for _, x in EVENTS if x is not None]
    ok(len(valid2) == 1, "有效记账恰好 1 次（实际 %d 次：%s）"
       % (len(valid2), [k for k, _ in EVENTS]))

    print("【5】中途异常也要把已经拿到的量结算掉（不漏账）")
    EVENTS.clear()

    class Boom(FakeInner):
        def __iter__(self):
            yield "x"
            raise RuntimeError("流断了")

    proxy3 = MS(Boom([]), {"model": "m"})
    try:
        for _ in proxy3:
            pass
    except RuntimeError:
        pass
    ok(any(k == "drain" for k, _ in EVENTS), "异常路径触发了一次兜底结算")

    print("\n结果：%d 通过 / %d 失败" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
