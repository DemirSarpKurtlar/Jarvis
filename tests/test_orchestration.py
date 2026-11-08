"""Aşama 6: Claude araç kullanımı ToolService üzerinden (güvenlik katmanı, onay, durum)."""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

from tests.fake_api import FakeAPI, text_stream, tool_use_stream
from jarvis.ai.router import classify
from jarvis.config.settings import SettingsStore
from jarvis.core.chat_service import ChatService
from jarvis.core.events import EventBus, Topics
from jarvis.core.state import AssistantState, StateMachine
from jarvis.core.tool_service import ToolService
from jarvis.security.credentials import CredentialStore
from jarvis.storage.database import Database

KEY = "sk-ant-api03-" + "O" * 40


def make(tmp_path, api, confirm_answer=None):
    store = SettingsStore(tmp_path / "settings.json")
    store.load()
    bus, state, db = EventBus(), StateMachine(), Database(tmp_path / "j.db")
    CredentialStore().set_anthropic_key(KEY)
    tools = ToolService(store, bus, state, db)
    chat = ChatService(store, bus, state, db, ThreadPoolExecutor(2),
                       client_factory=lambda k, max_retries=0: api.client(k, max_retries=0))
    chat.tools = tools
    states, confirms = [], []
    state.add_listener(lambda ch: states.append(ch.new))
    if confirm_answer is not None:
        def on_confirm(req):
            confirms.append(req)
            req.future.set_result(confirm_answer)
        bus.subscribe(Topics.TOOL_CONFIRM, on_confirm)
    return store, chat, tools, db, states, confirms


def test_claude_receives_registry_tools_and_runs_read_tool(tmp_path):
    api = FakeAPI([tool_use_stream("memory_status", {}), text_stream(["Belleğin yüzde 30 dolu."])])
    store, chat, tools, db, states, _ = make(tmp_path, api)

    async def go():
        await chat.start()
        return await chat.send("bellek durumu nasıl, bilgisayarımdaki program durumuna bak")

    done = asyncio.run(go())
    first = api.requests[1]["body"] if api.requests[0]["path"].startswith("/v1/models") else api.requests[0]["body"]
    names = [t["name"] for t in first["tools"]]
    assert "memory_status" in names
    assert sum(1 for t in first["tools"] if t.get("strict")) <= 20   # gerçek API sınırı
    assert done.text.endswith("Belleğin yüzde 30 dolu.")
    assert "Yapılan işlemler" in (done.note or "")
    assert AssistantState.EXECUTING in states and states[-1] is AssistantState.STANDBY
    rows = db.query("SELECT tool, source, ok FROM audit_log")
    assert [(r["tool"], r["source"], r["ok"]) for r in rows] == [("memory_status", "claude", 1)]


def test_dangerous_tool_requires_confirmation_and_refusal_is_reported(tmp_path):
    root = tmp_path / "izinli"
    root.mkdir()
    target = root / "sil.txt"
    target.write_text("önemli", encoding="utf-8")
    api = FakeAPI([tool_use_stream("delete_path", {"path": str(target), "permanent": False}),
                   text_stream(["Silmedim, onay vermedin."])])
    (tmp_path / "settings.json").write_text(
        '{"tools": {"extra_allowed_roots": ["%s"]}}' % str(root).replace("\\", "/"), encoding="utf-8")
    store, chat, tools, db, states, confirms = make(tmp_path, api, confirm_answer=False)

    async def go():
        await chat.start()
        return await chat.send("şu dosyayı sil")

    asyncio.run(go())
    tool_result = api.requests[-1]["body"]["messages"][-1]["content"][0]
    assert tool_result["is_error"] is True                  # yapılmadı bilgisi Claude'a gitti
    rows = db.query("SELECT tool, decision, ok FROM audit_log")
    assert len(confirms) == 1                                # kullanıcıya gerçekten soruldu
    assert rows[0]["ok"] == 0 and rows[0]["decision"] == "reddedildi"
    assert target.exists()                                   # dosya silinmedi


def test_tools_can_be_disabled(tmp_path):
    api = FakeAPI([text_stream(["Tamam."])])
    store, chat, *_ = make(tmp_path, api)
    store.settings.tools.claude_can_use_tools = False

    async def go():
        await chat.start()
        await chat.send("selam")

    asyncio.run(go())
    body = [r for r in api.requests if r["path"] == "/v1/messages"][0]["body"]
    assert "tools" not in body


def test_computer_tasks_route_to_sonnet():
    assert classify("İndirilenler klasöründeki pdf dosyalarını bir klasöre topla")[0] == "main"
    assert classify("10 dakika sonra bana su içmeyi hatırlat")[0] == "main"
    assert classify("merhaba")[0] == "light"
