"""离线验证 agent._consume_stream：拼装 / 计量 / 增量三条硬要求。

用法（PYTHONUTF8=1，在 agent/ 目录下跑）：
    set PYTHONUTF8=1 && python <此文件>
判据：退出码 0 = 全过。
"""
import json
import os
import sys
import types
from pathlib import Path

# 直接指向 AB 的 agent 目录（不是本文件的目录 —— 那是错的，import agent 会失败）
AGENT_DIR = os.environ.get("AB_AGENT_DIR") or str(
    Path(__file__).resolve().parents[1] / "agent")
sys.path.insert(0, AGENT_DIR)

import agent  # noqa: E402

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


class FakeUsage:
    """形状对齐 SDK 的 CompletionUsage（**关键：它也有 model_dump()**）。

    真机就是 SDK 的 CompletionUsage；这里必须照它有 model_dump 来做，
    否则测不出"日志那条 json.dumps 会不会炸"——用 SimpleNamespace 当替身是**假的替身**。
    """

    def __init__(self, prompt, completion):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.total_tokens = prompt + completion

    def model_dump(self):
        return {"prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens}


def chunk(*, content=None, reasoning=None, tool_calls=None, usage=None, finish=None):
    """造一个形状对齐 SDK 的 chunk（只有 _consume_stream 会读到的字段）。"""
    fn_slices = []
    for tc in (tool_calls or []):
        fn = types.SimpleNamespace(name=tc.get("name"), arguments=tc.get("arguments"))
        fn_slices.append(types.SimpleNamespace(index=tc.get("index", 0),
                                               id=tc.get("id"), type="function",
                                               function=fn))
    delta = types.SimpleNamespace(content=content, reasoning_content=reasoning,
                                  tool_calls=fn_slices or None, model_extra={})
    choice = types.SimpleNamespace(delta=delta, finish_reason=finish)
    has_payload = bool(content or reasoning or tool_calls)
    return types.SimpleNamespace(choices=[choice] if (has_payload or finish) else [],
                                 usage=usage)


def fake_usage(prompt, completion):
    return FakeUsage(prompt, completion)


def t1_text_and_reasoning():
    print("【1】纯文本 + 思考：拼接正确、增量都被回调")
    seen = []
    agent.register_delta_hook(lambda e: seen.append((e["kind"], e["text"])))
    stream = [
        chunk(reasoning="先看天气"),
        chunk(reasoning="再定行程"),
        chunk(content="我去查"),
        chunk(content="一下青岛"),
        chunk(usage=fake_usage(1234, 56), finish="stop"),
    ]
    resp = agent._consume_stream(iter(stream), session_id="sid1", log=None)
    m = resp.choices[0].message
    ok(m.reasoning_content == "先看天气再定行程", "思考增量拼接：%r" % m.reasoning_content)
    ok(m.content == "我去查一下青岛", "正文增量拼接：%r" % m.content)
    ok(m.tool_calls is None, "没有工具调用时为 None")
    ok(resp.usage is not None and resp.usage.prompt_tokens == 1234,
       "usage 从最后一个 chunk 抓到（计量不断）")
    kinds = [k for k, _ in seen]
    ok(kinds.count("reasoning") == 2 and kinds.count("content") == 2,
       "增量回调次数正确：%s" % kinds)


def t2_tool_fragments():
    print("【2】工具调用**分片**：一个调用拆成 2 个 chunk 也要拼成一个")
    stream = [
        chunk(tool_calls=[{"index": 0, "id": "call_abc", "name": "read_file",
                           "arguments": '{"file_pa'}]),
        chunk(tool_calls=[{"index": 0, "arguments": 'th": "a.md"}'}]),
        chunk(usage=fake_usage(10, 2), finish="tool_calls"),
    ]
    resp = agent._consume_stream(iter(stream), session_id="s", log=None)
    tcs = resp.choices[0].message.tool_calls
    ok(len(tcs) == 1, "拼成 1 个工具调用（不是 2 个碎片）：%d" % len(tcs))
    ok(tcs[0].id == "call_abc", "id 保留：%r" % tcs[0].id)
    ok(tcs[0].function.name == "read_file", "名字保留：%r" % tcs[0].function.name)
    ok(tcs[0].function.arguments == '{"file_path": "a.md"}',
       "参数拼全：%r" % tcs[0].function.arguments)
    ok(json.loads(tcs[0].function.arguments)["file_path"] == "a.md",
       "拼出来的参数是合法 JSON")


def t3_parallel_tools():
    print("【3】多个工具调用并行（index 0/1 交错到达）")
    stream = [
        chunk(tool_calls=[{"index": 0, "id": "c0", "name": "time_weather",
                           "arguments": '{"city":'}]),
        chunk(tool_calls=[{"index": 1, "id": "c1", "name": "search",
                           "arguments": '{"q":'}]),
        chunk(tool_calls=[{"index": 0, "arguments": '"青岛"}'}]),
        chunk(tool_calls=[{"index": 1, "arguments": '"教堂"}'}]),
        chunk(usage=fake_usage(1, 1), finish="tool_calls"),
    ]
    resp = agent._consume_stream(iter(stream), session_id="s", log=None)
    tcs = resp.choices[0].message.tool_calls
    ok(len(tcs) == 2, "两个调用各自成一条：%d" % len(tcs))
    ok(tcs[0].function.arguments == '{"city":"青岛"}',
       "第 0 个参数：%r" % tcs[0].function.arguments)
    ok(tcs[1].function.arguments == '{"q":"教堂"}',
       "第 1 个参数：%r" % tcs[1].function.arguments)
    ok(tcs[1].function.name == "search", "第 1 个名字：%r" % tcs[1].function.name)


def t4_missing_id():
    print("【4】缺 id 的工具调用会被补一个（不留'有 tool_call 无响应'的断头）")
    stream = [
        chunk(tool_calls=[{"index": 0, "name": "execute_shell", "arguments": "{}"}]),
        chunk(usage=None, finish="tool_calls"),
    ]
    resp = agent._consume_stream(iter(stream), session_id="s", log=None)
    tcs = resp.choices[0].message.tool_calls
    ok(bool(tcs[0].id), "补了 id：%r" % tcs[0].id)


def t5_usage_attached_to_stream():
    print("【5】usage 也挂回**流对象**（bridge 靠它结账）")

    class FakeStream:
        def __iter__(self):
            return iter([chunk(content="x"),
                         chunk(usage=fake_usage(7, 1), finish="stop")])

    s = FakeStream()
    agent._consume_stream(s, session_id="s", log=None)
    ok(getattr(s, "_ab_usage", None) is not None,
       "流对象上挂到了 _ab_usage（bridge 的 _account_streamed 靠它）")


def t6_model_dump():
    print("【6】model_dump 形状与 SDK 一致（主循环直接落 conversation）")
    stream = [chunk(content="正文", reasoning="想", usage=None, finish="stop")]
    resp = agent._consume_stream(iter(stream), session_id="s", log=None)
    d = resp.choices[0].message.model_dump()
    ok(d["role"] == "assistant" and d["content"] == "正文", "role/content 正确")
    ok(d["reasoning_content"] == "想", "reasoning_content 带上了（progress 播报要用）")
    ok(d["tool_calls"] is None, "无工具调用时为 None（不是空列表 —— 与老口径一致）")


def t7_delta_hook_never_breaks_turn():
    print("【7】宿主钩子抛异常**不许**把回合带崩（咽喉层铁律）")

    def boom(_evt):
        raise RuntimeError("钩子炸了")

    agent.register_delta_hook(boom)
    stream = [chunk(content="照常返回", usage=None, finish="stop")]
    resp = agent._consume_stream(iter(stream), session_id="s", log=None)
    ok(resp.choices[0].message.content == "照常返回", "钩子抛异常后流照样收完")


def t8_response_model_dump():
    print("【8】response.model_dump()（主循环那行 log.debug 会调它，缺了就整个回合崩）")
    stream = [
        chunk(content="回答", reasoning="想一下",
              tool_calls=[{"index": 0, "id": "c9", "name": "f", "arguments": "{}"}]),
        chunk(usage=fake_usage(11, 3), finish="tool_calls"),
    ]
    resp = agent._consume_stream(iter(stream), session_id="s", log=None)

    ok(hasattr(resp, "model_dump"),
       "响应对象有 model_dump（2026-10-02 端到端就是缺它炸的）")
    d = resp.model_dump()
    ok(isinstance(d, dict), "返回 dict")
    # 真机就是这么用的：logger 写盘时会 json.dumps(raw=...) 整个结构
    blob = json.dumps(d, ensure_ascii=False)
    ok(len(blob) > 0, "能 json.dumps（日志通道不会因为不可序列化再炸一次）")
    ok(d["choices"][0]["message"]["content"] == "回答", "choices[0].message.content 在")
    ok(d["choices"][0]["finish_reason"] == "tool_calls", "finish_reason 带上了")
    ok(d["choices"][0]["message"]["tool_calls"][0]["id"] == "c9", "工具调用进了 dump")
    ok(d["usage"] is not None and d["usage"]["prompt_tokens"] == 11,
       "usage 进了 dump（且被转成 dict）")

    print("【9】把主循环真正读过的字段全点一遍（防再漏一个）")
    m = resp.choices[0].message
    ok(isinstance(m.content, str), "message.content 是 str（主循环做 .strip()）")
    ok(hasattr(m, "reasoning_content"), "message.reasoning_content 存在")
    ok(all(hasattr(tc, "id") and hasattr(tc.function, "name")
           and hasattr(tc.function, "arguments") for tc in m.tool_calls),
       "每个 tool_call 都有 .id / .function.name / .function.arguments（主循环这么读的）")
    ok(getattr(resp, "usage", None) is not None,
       "response.usage 存在（_LAST_PROMPT_TOKENS 读它）")
    ok(getattr(resp, "model", None), "response.model 存在（_norm_usage 读它）")


if __name__ == "__main__":
    for fn in (t1_text_and_reasoning, t2_tool_fragments, t3_parallel_tools,
               t4_missing_id, t5_usage_attached_to_stream, t6_model_dump,
               t7_delta_hook_never_breaks_turn, t8_response_model_dump):
        fn()
    print("\n结果：%d 通过 / %d 失败" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)
