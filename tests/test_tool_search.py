"""Araç arama (tool search) ve use_computer aracının sohbete/yerel komutlara bağlanması."""

import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor

import pytest

from jarvis.ai import claude_client as cc
from jarvis.config.settings import SettingsStore
from jarvis.core.chat_service import ChatService
from jarvis.core.events import EventBus, Topics
from jarvis.core.state import AssistantState, StateMachine
from jarvis.core.tool_service import ToolService
from jarvis.security.credentials import CredentialStore
from jarvis.security.permission_manager import Permission
from jarvis.storage.database import Database
from jarvis.tools import registry as regmod
from jarvis.tools import schema as js
from jarvis.tools.defaults import build_registry
from jarvis.tools.registry import (CORE_TOOL_NAMES, MAX_STRICT_TOOLS, TOOL_SEARCH_TOOL, ToolOutput, ToolRegistry,
                                   ToolSpec)
from tests.agent_fakes import make_desktop
from tests.fake_api import FakeAPI, sse, text_stream, tool_use_stream, toolset_stream, validate_request
from tests.tool_fakes import make_ctx

KEY = "sk-ant-api03-" + "T" * 40
GOAL = "Valorant'ı aç ve takımlı ölüm kalım maçı başlat"


@pytest.fixture
def win(monkeypatch):
    monkeypatch.setattr(regmod, "CURRENT_PLATFORM", "win32")


def _spec(name: str, perm=Permission.READ, core=False) -> ToolSpec:
    return ToolSpec(name=name, title=name, description=f"{name} aracı / {name} tool", input_schema=js.obj(),
                    permission=perm, handler=lambda a: ToolOutput("ok"), core=core)


# ---------- istek biçimi ----------
def test_tool_search_shape_defers_non_core_tools(tmp_path, win):
    reg = build_registry(make_ctx(tmp_path))
    tools = reg.to_claude_tools(tool_search=True)
    assert tools[0] == TOOL_SEARCH_TOOL and "defer_loading" not in tools[0]
    by_name = {t["name"]: t for t in tools[1:]}
    for name in ("open_app", "close_app", "set_volume", "launch_game", "use_computer", "web_search_in_browser"):
        assert not by_name[name].get("defer_loading"), name
    deferred = [t for t in tools if t.get("defer_loading")]
    assert len(deferred) >= 20 and all(t["name"] not in CORE_TOOL_NAMES for t in deferred)
    assert sum(1 for t in tools if t.get("strict")) == MAX_STRICT_TOOLS
    assert all(set(t) <= {"name", "description", "input_schema", "strict", "defer_loading"} for t in tools[1:])
    assert reg.to_claude_tools(tool_search=True) == tools          # sabit sıra: istem önbelleği bozulmaz
    assert validate_request({"tools": tools}) is None
    # varsayılan çıktı değişmedi
    assert all("defer_loading" not in t for t in reg.to_claude_tools())


def test_tool_search_skipped_for_small_registries():
    reg = ToolRegistry()
    for i in range(5):
        reg.register(_spec(f"arac_{i}"))
    tools = reg.to_claude_tools(tool_search=True)
    assert tools == reg.to_claude_tools()


def test_core_flag_and_strict_prefers_loaded_tools_at_equal_risk():
    reg = ToolRegistry()
    for i in range(30):
        reg.register(_spec(f"okuma_{i}", core=(i >= 25)))
    tools = reg.to_claude_tools(tool_search=True)
    strict = {t["name"] for t in tools if t.get("strict")}
    assert {f"okuma_{i}" for i in range(25, 30)} <= strict             # çekirdek araçlar önce katı
    assert len(strict) == MAX_STRICT_TOOLS
    assert not any(t.get("defer_loading") for t in tools if t["name"].startswith("okuma_2") and int(t["name"][6:]) >= 25)


def test_fake_api_enforces_real_api_rules():
    t = {"name": "a", "input_schema": {"type": "object"}}
    assert "defer_loading=false" in validate_request({"tools": [{**TOOL_SEARCH_TOOL, "defer_loading": True}]})
    assert "cannot have defer_loading" in validate_request({"tools": [{**TOOL_SEARCH_TOOL, "defer_loading": True}, t]})
    assert "require a tool search" in validate_request({"tools": [{**t, "defer_loading": True}, t]})
    assert "cache_control" in validate_request({"tools": [TOOL_SEARCH_TOOL, {**t, "defer_loading": True,
                                                                             "cache_control": {"type": "ephemeral"}}]})
    assert "unexpected fields" in validate_request({"tools": [{"type": "computer_toolset_20260801", "name": "computer"}]})
    assert "computer_20251124" in validate_request({"tools": [{"type": "computer_20251124", "name": "computer"}]})
    assert "not supported" in validate_request({"tools": [t], "tool_choice": {"type": "any"}})
    msgs = [{"role": "user", "content": "x"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "u1", "name": "screenshot", "input": {},
                                               "toolset_name": "computer"}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "u1", "content": "OK"}]}]
    assert "toolset_name" in validate_request({"messages": msgs})
    msgs[2]["content"][0]["toolset_name"] = "computer"
    assert validate_request({"messages": msgs}) is None
    msgs[2]["content"] = []
    assert "without tool_result" in validate_request({"messages": msgs})


# ---------- sohbet ----------
def make_chat(tmp_path, api, confirm=True):
    store = SettingsStore(tmp_path / "settings.json")
    store.load()
    bus, state, db = EventBus(), StateMachine(), Database(tmp_path / "j.db")
    CredentialStore().set_anthropic_key(KEY)
    tools = ToolService(store, bus, state, db, ctx=make_ctx(tmp_path))
    chat = ChatService(store, bus, state, db, ThreadPoolExecutor(2),
                       client_factory=lambda k, max_retries=0: api.client(k, max_retries=0))
    chat.tools = tools
    prompts, details = [], []
    state.add_listener(lambda ch: details.append((ch.new, ch.detail)))

    def on_confirm(req):
        prompts.append(req.prompt)
        req.future.set_result(confirm)
    bus.subscribe(Topics.TOOL_CONFIRM, on_confirm)
    return chat, tools, db, prompts, details


def _msgs(api):
    return [r["body"] for r in api.requests if r["path"] == "/v1/messages"]


def test_chat_sends_tool_search_and_falls_back_when_rejected(tmp_path, win, caplog):
    cc._tool_search_warned.clear()
    api = FakeAPI([text_stream(["Merhaba."]), text_stream(["Yine merhaba."])], reject_tool_search=True)
    chat, *_ = make_chat(tmp_path, api)

    async def go():
        await chat.start()
        a = await chat.send("selam, bilgisayarımdaki işlemleri anlat")
        b = await chat.send("tekrar selam, bilgisayarımdaki işlemleri anlat")
        return a, b

    with caplog.at_level(logging.WARNING, logger="jarvis.ai.claude_client"):
        a, b = asyncio.run(go())
    assert a.text == "Merhaba." and b.text == "Yine merhaba."
    assert len(api.rejected) == 1                                   # ikinci sohbette doğrudan aramasız
    sent = _msgs(api)
    assert any(t.get("type") == TOOL_SEARCH_TOOL["type"] for t in sent[0]["tools"])
    for body in sent[1:]:
        assert not any("defer_loading" in t or t.get("type") == TOOL_SEARCH_TOOL["type"] for t in body["tools"])
    assert sum("Araç arama" in r.message for r in caplog.records) == 1   # bir kez loglanır


def _search_then_tool_stream() -> bytes:
    """Claude araç aramasıyla ertelenmiş `memory_status`'ı bulup çağırıyor."""
    return sse(
        {"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant",
                                              "model": "claude-haiku-5-5", "content": [], "stop_reason": None,
                                              "stop_sequence": None, "usage": {"input_tokens": 900, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "server_tool_use", "id": "srvtoolu_1",
                                                                      "name": "tool_search_tool_bm25", "input": {}}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": json.dumps({"query": "bellek durumu"})}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {
            "type": "tool_search_tool_result", "tool_use_id": "srvtoolu_1",
            "content": {"type": "tool_search_tool_search_result",
                        "tool_references": [{"type": "tool_reference", "tool_name": "memory_status"}]}}},
        {"type": "content_block_stop", "index": 1},
        {"type": "content_block_start", "index": 2,
         "content_block": {"type": "tool_use", "id": "toolu_m", "name": "memory_status", "input": {}}},
        {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": "{}"}},
        {"type": "content_block_stop", "index": 2},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
         "usage": {"input_tokens": 900, "output_tokens": 30}},
        {"type": "message_stop"})


def test_deferred_tool_found_by_search_runs_and_history_is_kept(tmp_path):
    api = FakeAPI([_search_then_tool_stream(), text_stream(["Belleğin yarısı dolu."])])
    chat, tools, db, *_ = make_chat(tmp_path, api)

    async def go():
        await chat.start()
        return await chat.send("bellek durumu nasıl, programlar ne kadar kullanıyor")

    done = asyncio.run(go())
    assert done.text.endswith("Belleğin yarısı dolu.")
    second = _msgs(api)[1]
    assistant = second["messages"][-2]["content"]
    assert [b["type"] for b in assistant] == ["server_tool_use", "tool_search_tool_result", "tool_use"]
    results = second["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["toolu_m"]          # sunucu aracına sonuç dönülmez
    assert not api.rejected
    assert db.query("SELECT tool FROM audit_log")[0]["tool"] == "memory_status"


def test_use_computer_from_chat_runs_session_and_returns_summary(tmp_path, win):
    api = FakeAPI([
        tool_use_stream("use_computer", {"goal": GOAL}),
        toolset_stream([("screenshot", {}), ("left_click", {"coordinate": [683, 40]})]),
        text_stream(["Takımlı Ölüm Kalım sırasına girdim."]),
        text_stream(["Valorant'ta Takımlı Ölüm Kalım için sıraya girdin."]),
    ])
    chat, tools, db, prompts, details = make_chat(tmp_path, api)
    desktop = make_desktop()
    chat.computer.desktop_factory = lambda: desktop

    async def go():
        await chat.start()
        return await chat.send("Valorant'ı aç ve takımlı ölüm kalım maçı başlat, bilgisayarı kullanarak yap")

    done = asyncio.run(go())
    assert done.text.endswith("sıraya girdin.")
    assert len(prompts) == 1 and "Esc'ye 2 kez bas" in prompts[0].message and GOAL in prompts[0].message
    bodies = _msgs(api)
    main = bodies[0]
    assert main["tools"][0]["type"] == TOOL_SEARCH_TOOL["type"]
    use = next(t for t in main["tools"] if t.get("name") == "use_computer")
    assert not use.get("defer_loading") and "confirmation" in use["description"]
    assert bodies[1]["tools"][0] == {"type": "computer_toolset_20260801"}
    final_result = bodies[3]["messages"][-1]["content"][0]
    assert final_result["tool_use_id"] == "toolu_1" and final_result["is_error"] is False
    assert "Takımlı Ölüm Kalım sırasına girdim." in final_result["content"]
    assert "untrusted_tool_output" in final_result["content"]          # ekrandan gelen özet güvenilmeyen veri
    assert desktop.input.clicks()
    assert any(st is AssistantState.EXECUTING and "adım" in d for st, d in details)
    audit = [r["tool"] for r in db.query("SELECT tool FROM audit_log ORDER BY id")]
    assert audit[:2] == ["bilgisayar.screenshot", "bilgisayar.left_click"] and audit[-1] == "use_computer"
    rows = db.query("SELECT route_reason FROM usage")
    assert sum(1 for r in rows if r["route_reason"] == "bilgisayar") == 2


def test_local_command_uses_computer_after_confirmation(tmp_path, win):
    api = FakeAPI([text_stream(["Not Defteri'ni açtım."])])
    chat, tools, db, prompts, _ = make_chat(tmp_path, api)
    chat.computer.desktop_factory = lambda: make_desktop()
    done = []
    chat.bus.subscribe(Topics.CHAT_DONE, done.append)

    async def go():
        await chat.start()
        return await tools.handle_local("bilgisayarı sen kullan: Not Defteri'ni aç ve merhaba yaz")

    assert asyncio.run(go()) is True
    assert prompts and done[-1].text == "Not Defteri'ni açtım."
    assert _msgs(api)[0]["messages"][0]["content"].startswith("Kullanıcının hedefi: Not Defteri'ni aç ve merhaba yaz")


def test_local_command_declined_or_gameplay_refused(tmp_path, win):
    api = FakeAPI([])
    chat, tools, db, prompts, _ = make_chat(tmp_path, api, confirm=False)
    chat.computer.desktop_factory = lambda: make_desktop()
    done = []
    chat.bus.subscribe(Topics.CHAT_DONE, done.append)

    async def go():
        await chat.start()
        await tools.handle_local("bilgisayarı sen kullan: Not Defteri'ni aç")
        await tools.handle_local("bilgisayarı sen kullan: Valorant'ta benim yerime oyna ve düşmanları vur")

    asyncio.run(go())
    assert len(prompts) == 1                                  # oyun içi istek için onay bile sorulmadı
    assert done[0].text == "Tamam, yapmadım." and "hile" in done[1].text
    assert _msgs(api) == []


def test_local_command_on_unsupported_os(tmp_path, monkeypatch):
    monkeypatch.setattr(regmod, "CURRENT_PLATFORM", "linux")
    api = FakeAPI([])
    chat, tools, *_ = make_chat(tmp_path, api)
    done = []
    chat.bus.subscribe(Topics.CHAT_DONE, done.append)
    assert asyncio.run(tools.handle_local("bilgisayarı sen kullan: Paint'i aç")) is True
    assert "desteklenmiyor" in done[-1].text
