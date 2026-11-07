"""Gerçek Claude API testleri (ÜCRETLİ, toplam yaklaşık $0,05-0,10).

Varsayılan test çalıştırmasında atlanır. Çalıştırmak için önce JARVIS'te
Ayarlar > Claude bölümünden anahtarını gir, sonra:

    python -m pytest -m live -v -s

Anahtar Windows Credential Manager'dan okunur; test çıktısına yazılmaz.
"""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from jarvis.ai.claude_client import ClaudeClient, ClaudeError, ErrorKind
from jarvis.ai.model_registry import KNOWN_MODELS
from jarvis.ai.prompts import system_blocks
from jarvis.config.settings import SettingsStore
from jarvis.core.chat_service import ChatService
from jarvis.core.events import EventBus
from jarvis.core.state import StateMachine
from jarvis.security.credentials import CredentialStore
from jarvis.storage.database import Database

pytestmark = pytest.mark.live
HAIKU, SONNET = KNOWN_MODELS["claude-haiku-5-5"], KNOWN_MODELS["claude-sonnet-5-5"]


@pytest.fixture(scope="module")
def key():
    k = CredentialStore().get_anthropic_key()
    if not k:
        pytest.skip("Claude API anahtarı yok: önce JARVIS > Ayarlar > Claude bölümünden anahtarını gir.")
    return k


def run(coro):
    return asyncio.run(coro)


def report(title, r):
    u = r.usage
    print(f"\n  [{title}] model={r.model_served} giriş={u.input_tokens} önbellek_yazma={u.cache_creation_tokens} "
          f"önbellek_okuma={u.cache_read_tokens} çıkış={u.output_tokens} arama={u.web_search_requests} "
          f"maliyet=${r.cost_usd:.5f}\n  cevap: {r.text[:160]!r}")


def test_live_key_is_valid(key):
    async def go():
        c = ClaudeClient(key)
        try:
            await c.verify(HAIKU.id)
        finally:
            await c.close()
    run(go())


def test_live_wrong_key_is_reported_as_auth_error():
    async def go():
        c = ClaudeClient("sk-ant-api03-" + "0" * 40, max_retries=0)
        try:
            await c.verify(HAIKU.id)
        finally:
            await c.close()
    with pytest.raises(ClaudeError) as e:
        run(go())
    assert e.value.kind is ErrorKind.AUTH


def test_live_haiku_turkish_answer_and_prompt_cache(key):
    """İki ardışık istek: ikincide sistem istemi önbellekten okunmalı."""
    async def go():
        c = ClaudeClient(key)
        try:
            msgs = [{"role": "user", "content": "Tek kelimeyle cevap ver: Türkiye'nin başkenti neresi?"}]
            r1 = await c.chat(HAIKU, system_blocks(), msgs)
            r2 = await c.chat(HAIKU, system_blocks(), msgs)
            return r1, r2
        finally:
            await c.close()
    r1, r2 = run(go())
    report("Haiku 1", r1)
    report("Haiku 2", r2)
    assert "ankara" in r1.text.lower()
    assert r1.cost_usd > 0 and r1.usage.output_tokens > 0
    assert r1.usage.cache_creation_tokens + r1.usage.cache_read_tokens > 0, "sistem istemi önbelleğe alınmadı"
    assert r2.usage.cache_read_tokens > 0, "ikinci istekte önbellek okunmadı"


def test_live_sonnet_web_search_with_sources(key):
    async def go():
        c = ClaudeClient(key)
        try:
            msgs = [{"role": "user", "content": "İnternetten araştır: Türkiye Cumhuriyet Merkez Bankası'nın "
                                                "şu anki politika faizi yüzde kaç? Kısaca söyle."}]
            return await c.chat(SONNET, system_blocks(), msgs, web_search=True)
        finally:
            await c.close()
    r = run(go())
    report("Sonnet + web", r)
    assert r.text and not r.refused
    assert r.usage.web_search_requests >= 1, "web araması yapılmadı"
    assert r.citations, "kaynak gösterilmedi"


def test_live_full_chat_service_routes_simple_question_to_haiku(key, tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.load()
    svc = ChatService(store, EventBus(), StateMachine(), Database(tmp_path / "j.db"), ThreadPoolExecutor(2))

    async def go():
        await svc.start()
        return await svc.send("Merhaba JARVIS, nasılsın?")

    done = run(go())
    print(f"\n  [Sohbet servisi] {done.model_name} ${done.cost_usd:.5f}: {done.text[:120]!r}")
    assert done.model_name == "Haiku 5.5" and done.text
    assert svc.tracker.status(store.settings.budget).month.requests == 1
