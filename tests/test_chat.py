"""Sohbet servisi ve konuşma yöneticisi (sahte API, para harcamaz)."""

import asyncio
from concurrent.futures import ThreadPoolExecutor

import httpx2
import pytest

from tests.fake_api import FakeAPI, text_stream
from jarvis.config.settings import SettingsStore
from jarvis.core.chat_service import ChatService, ClaudeStatus, ConfirmRequest
from jarvis.core.conversation import ConversationManager
from jarvis.core.events import EventBus, Topics
from jarvis.core.state import AssistantState, StateMachine
from jarvis.security.credentials import CredentialError, CredentialStore
from jarvis.storage.database import Database

KEY = "sk-ant-api03-" + "B" * 40


class Harness:
    def __init__(self, tmp_path, api: FakeAPI, with_key: bool = True):
        self.store = SettingsStore(tmp_path / "settings.json")
        self.store.load()
        self.bus = EventBus()
        self.state = StateMachine()
        self.db = Database(tmp_path / "j.db")
        self.events: list[tuple[str, object]] = []
        for t in (Topics.CHAT_STARTED, Topics.CHAT_DELTA, Topics.CHAT_DONE, Topics.CHAT_FAILED,
                  Topics.NOTICE, Topics.USAGE_UPDATED, Topics.CLAUDE_STATUS, Topics.CONFIRM_REQUEST):
            self.bus.subscribe(t, lambda p, t=t: self.events.append((t, p)))
        if with_key:
            CredentialStore().set_anthropic_key(KEY)
        self.api = api
        self.svc = ChatService(self.store, self.bus, self.state, self.db, ThreadPoolExecutor(2),
                               client_factory=lambda key, max_retries=0: api.client(key, max_retries=0))

    def of(self, topic):
        return [p for t, p in self.events if t == topic]


def run(coro):
    return asyncio.run(coro)


def test_no_key_tells_user_how_to_fix(tmp_path):
    h = Harness(tmp_path, FakeAPI(), with_key=False)

    async def go():
        await h.svc.start()
        await h.svc.send("selam")

    run(go())
    assert h.svc.status is ClaudeStatus.NO_KEY
    assert "anahtarı girilmedi" in h.of(Topics.CHAT_FAILED)[0]
    assert h.api.requests == []


def test_full_chat_flow(tmp_path):
    h = Harness(tmp_path, FakeAPI([text_stream(["Merhaba", "!"])]))

    async def go():
        await h.svc.start()
        return await h.svc.send("selam")

    done = run(go())
    assert h.svc.status is ClaudeStatus.CONNECTED
    assert done.text == "Merhaba!" and done.cost_usd > 0
    assert h.of(Topics.CHAT_DELTA) == ["Merhaba", "!"]
    assert h.of(Topics.CHAT_STARTED)[0].model_name == "Haiku 5.5"
    assert h.state.state is AssistantState.STANDBY
    # maliyet ve konuşma kaydedildi
    month = h.of(Topics.USAGE_UPDATED)[-1].month
    assert month.requests == 1 and month.cost_usd == pytest.approx(done.cost_usd)
    conv = h.svc.conversation
    assert [t.role for t in conv.turns] == ["user", "assistant"]
    assert conv.messages_of(conv.conversation_id)[-1]["text"] == "Merhaba!"
    # Sistem istemi önbellek işaretli gitti, model ve geçmiş doğru
    body = h.api.requests[-1]["body"]
    assert body["model"] == "claude-haiku-5-5" and body["messages"] == [{"role": "user", "content": "selam"}]


def test_second_message_carries_short_history(tmp_path):
    h = Harness(tmp_path, FakeAPI([text_stream(["Adın Demir."]), text_stream(["Tamam."])]))

    async def go():
        await h.svc.start()
        await h.svc.send("Benim adım Demir")
        await h.svc.send("teşekkürler")

    run(go())
    msgs = h.api.requests[-1]["body"]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[1]["content"] == "Adın Demir."  # yalnızca metin, düşünme bloğu yok


def test_offline_is_reported_and_nothing_committed(tmp_path):
    h = Harness(tmp_path, FakeAPI([httpx2.ConnectError("yok")]))

    async def go():
        await h.svc.start()
        await h.svc.send("bugün hava nasıl")

    run(go())
    assert h.svc.status is ClaudeStatus.OFFLINE
    assert "internet" in h.of(Topics.CHAT_FAILED)[0]
    assert h.state.state is AssistantState.ERROR
    assert h.svc.conversation.turns == []
    assert not h.svc.busy


def test_recovers_after_connection_returns(tmp_path):
    h = Harness(tmp_path, FakeAPI([httpx2.ConnectError("yok"), text_stream(["Döndüm"])]))

    async def go():
        await h.svc.start()
        await h.svc.send("selam")
        return await h.svc.send("selam tekrar")

    done = run(go())
    assert done.text == "Döndüm"
    assert h.svc.status is ClaudeStatus.CONNECTED
    assert h.state.state is AssistantState.STANDBY


def test_refusal_is_billed_but_not_added_to_history(tmp_path):
    h = Harness(tmp_path, FakeAPI([text_stream([], stop_reason="refusal",
                                               stop_details={"type": "refusal", "category": "bio"})]))

    async def go():
        await h.svc.start()
        return await h.svc.send("tehlikeli bir şey")

    done = run(go())
    assert done.refused and "güvenlik" in done.text and "bio" in done.text
    assert h.svc.conversation.turns == []
    assert h.of(Topics.USAGE_UPDATED)[-1].month.requests == 1


def test_budget_block_policy_sends_nothing(tmp_path):
    h = Harness(tmp_path, FakeAPI([]))
    h.store.settings.budget.on_limit = "block"
    h.store.settings.budget.daily_limit_usd = 0.0
    h.store.settings.budget.monthly_limit_usd = 0.000001

    async def go():
        await h.svc.start()
        h.db.execute("INSERT INTO usage (ts_utc, local_day, local_month, model, tier, input_tokens, output_tokens,"
                     " cache_creation_tokens, cache_read_tokens, web_search_requests, cost_usd)"
                     " VALUES ('x', strftime('%Y-%m-%d','now','localtime'), strftime('%Y-%m','now','localtime'),"
                     " 'claude-haiku-5-5','light',0,0,0,0,0,1.0)")
        await h.svc.send("selam")

    run(go())
    assert "bütçe" in h.of(Topics.CHAT_FAILED)[0].lower()
    assert [r for r in h.api.requests if r["path"] == "/v1/messages"] == []


def test_budget_ask_policy_respects_no(tmp_path):
    h = Harness(tmp_path, FakeAPI([]))
    h.store.settings.budget.on_limit = "ask"
    h.store.settings.budget.daily_limit_usd = 0.000001

    h.bus.subscribe(Topics.CONFIRM_REQUEST, lambda req: req.future.set_result(False))

    async def go():
        await h.svc.start()
        h.db.execute("INSERT INTO usage (ts_utc, local_day, local_month, model, tier, input_tokens, output_tokens,"
                     " cache_creation_tokens, cache_read_tokens, web_search_requests, cost_usd)"
                     " VALUES ('x', strftime('%Y-%m-%d','now','localtime'), strftime('%Y-%m','now','localtime'),"
                     " 'claude-haiku-5-5','light',0,0,0,0,0,1.0)")
        await h.svc.send("selam")

    run(go())
    assert isinstance(h.of(Topics.CONFIRM_REQUEST)[0], ConfirmRequest)
    assert "gönderilmedi" in h.of(Topics.NOTICE)[0]
    assert [r for r in h.api.requests if r["path"] == "/v1/messages"] == []


def test_save_key_verifies_before_storing(tmp_path, memory_keyring):
    bad = FakeAPI()

    def reject(req):
        return httpx2.Response(401, json={"type": "error", "error": {"type": "authentication_error",
                                                                     "message": "invalid x-api-key"}})

    bad.handler = reject
    h = Harness(tmp_path, bad, with_key=False)

    async def go():
        await h.svc.save_key("sk-ant-api03-" + "Z" * 40)

    with pytest.raises(CredentialError) as e:
        run(go())
    assert "geçersiz" in e.value.user_message
    assert memory_keyring.data == {}


def test_save_key_success(tmp_path, memory_keyring):
    h = Harness(tmp_path, FakeAPI(), with_key=False)

    async def go():
        return await h.svc.save_key("  sk-ant-api03-" + "Y" * 40 + "  ")

    msg = run(go())
    assert "doğrulandı" in msg
    assert h.svc.status is ClaudeStatus.CONNECTED
    assert list(memory_keyring.data.values()) == ["sk-ant-api03-" + "Y" * 40]


def test_busy_guard_rejects_parallel_requests(tmp_path):
    gate = asyncio.Event()
    api = FakeAPI([text_stream(["bir"])])
    h = Harness(tmp_path, api)

    async def go():
        await h.svc.start()
        t1 = asyncio.create_task(h.svc.send("ilk"))
        await asyncio.sleep(0)
        await h.svc.send("ikinci")
        await t1

    run(go())
    assert any("Önceki isteği" in n for n in h.of(Topics.NOTICE))
    assert len([r for r in api.requests if r["path"] == "/v1/messages"]) == 1


# ---------- Konuşma yöneticisi ----------
def test_conversation_window_starts_with_user_and_is_bounded(tmp_path):
    cm = ConversationManager(Database(tmp_path / "c.db"), max_messages=4)
    for i in range(5):
        cm.log_user(f"s{i}")
        cm.commit_exchange(f"s{i}", f"c{i}", "claude-haiku-5-5", 0.0)
    msgs = cm.api_messages("yeni")
    assert len(msgs) <= 4 and msgs[0]["role"] == "user" and msgs[-1] == {"role": "user", "content": "yeni"}


def test_conversation_idle_reset_creates_new_conversation(tmp_path):
    now = [0.0]
    cm = ConversationManager(Database(tmp_path / "c.db"), idle_reset_s=60, clock=lambda: now[0])
    cm.log_user("ilk")
    cm.commit_exchange("ilk", "cevap", "m", 0.0)
    first = cm.conversation_id
    now[0] = 120.0
    cm.log_user("uzun aradan sonra")
    assert cm.conversation_id != first and cm.turns == []
    convs = cm.list_conversations()
    assert [c["title"] for c in convs] == ["uzun aradan sonra", "ilk"]
