"""Claude istemci araç döngüsü (gerçek SDK + sahte HTTP)."""

import asyncio

from tests.fake_api import FakeAPI, text_stream, tool_use_stream
from jarvis.ai.claude_client import MAX_TOOL_ROUNDS, TOOL_RESULT_MAX_CHARS, ToolOutcome
from jarvis.ai.model_registry import KNOWN_MODELS
from jarvis.ai.prompts import system_blocks

HAIKU = KNOWN_MODELS["claude-haiku-5-5"]
TOOLS = [{"name": "open_app", "description": "Uygulama açar", "strict": True,
          "input_schema": {"type": "object", "properties": {"name": {"type": "string"}},
                           "required": ["name"], "additionalProperties": False}}]
MSG = [{"role": "user", "content": "Not defterini aç"}]


def run_chat(api, executor):
    async def go():
        c = api.client()
        try:
            return await c.chat(HAIKU, system_blocks(), MSG, client_tools=TOOLS, tool_executor=executor)
        finally:
            await c.close()
    return asyncio.run(go())


def test_tool_is_executed_and_result_sent_back_append_only():
    calls = []

    async def ex(name, args):
        calls.append((name, args))
        return ToolOutcome("notepad.exe başlatıldı (pid 1234)", summary="Not Defteri açıldı")

    api = FakeAPI([tool_use_stream("open_app", {"name": "notepad"}, text_before="Açıyorum."),
                   text_stream(["Not Defteri açık."])])
    r = run_chat(api, ex)
    assert calls == [("open_app", {"name": "notepad"})]
    assert r.text == "Açıyorum.\n\nNot Defteri açık."
    assert [t.summary for t in r.tool_runs] == ["Not Defteri açıldı"]
    first, second = api.requests[0]["body"], api.requests[1]["body"]
    assert first["tools"][0]["name"] == "open_app" and first["tools"][0]["strict"] is True
    assert second["messages"][: len(first["messages"])] == first["messages"]       # yalnızca ekleme
    assistant_turn, tool_turn = second["messages"][-2], second["messages"][-1]
    assert [b["type"] for b in assistant_turn["content"]] == ["thinking", "text", "tool_use"]  # düşünme korunur
    assert tool_turn["content"] == [{"type": "tool_result", "tool_use_id": "toolu_1",
                                     "content": "notepad.exe başlatıldı (pid 1234)", "is_error": False}]
    assert r.cost_usd > 0 and len(r.cost_lines) == 2


def test_truncated_or_refused_tool_call_is_never_executed():
    async def ex(name, args):
        raise AssertionError("çalıştırılmamalıydı")

    for stop in ("max_tokens", "refusal"):
        api = FakeAPI([tool_use_stream("open_app", {"name": "x"}, stop_reason=stop)])
        r = run_chat(api, ex)
        assert r.tool_runs == [] and len(api.requests) == 1


def test_tool_exception_returns_error_result_to_claude():
    async def ex(name, args):
        raise RuntimeError("bozuk")

    api = FakeAPI([tool_use_stream("open_app", {"name": "x"}), text_stream(["Açamadım."])])
    r = run_chat(api, ex)
    res = api.requests[1]["body"]["messages"][-1]["content"][0]
    assert res["is_error"] is True and "RuntimeError" in res["content"]
    assert r.tool_runs[0].ok is False


def test_tool_rounds_are_bounded():
    async def ex(name, args):
        return ToolOutcome("tamam")

    api = FakeAPI([tool_use_stream("open_app", {"name": "x"})] * (MAX_TOOL_ROUNDS + 1))
    r = run_chat(api, ex)
    assert len(r.tool_runs) == MAX_TOOL_ROUNDS and r.tool_limit_hit
    assert len(api.requests) == MAX_TOOL_ROUNDS + 1


def test_large_tool_result_is_truncated():
    async def ex(name, args):
        return ToolOutcome("x" * (TOOL_RESULT_MAX_CHARS * 3))

    api = FakeAPI([tool_use_stream("open_app", {"name": "x"}), text_stream(["ok"])])
    run_chat(api, ex)
    content = api.requests[1]["body"]["messages"][-1]["content"][0]["content"]
    assert len(content) < TOOL_RESULT_MAX_CHARS + 50 and content.endswith("[...kısaltıldı]")
