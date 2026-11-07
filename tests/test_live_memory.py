"""Aşama 4: gerçek Claude API ile hafıza testleri (ÜCRETLİ, toplam yaklaşık $0,001-0,005).

Varsayılan çalıştırmada atlanır. Önce JARVIS > Ayarlar > Claude bölümünden anahtarını gir, sonra:

    python -m pytest -m live tests/test_live_memory.py -v -s
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from jarvis.ai.claude_client import ClaudeClient
from jarvis.ai.usage import UsageTracker
from jarvis.config.settings import SettingsStore
from jarvis.core.events import EventBus
from jarvis.memory.service import MemoryService
from jarvis.memory.store import ACTIVE, PENDING
from jarvis.security.credentials import CredentialStore
from jarvis.storage.database import Database

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def key():
    k = CredentialStore().get_anthropic_key()
    if not k:
        pytest.skip("Claude API anahtarı yok: önce JARVIS > Ayarlar > Claude bölümünden anahtarını gir.")
    return k


def make(tmp_path, client):
    store = SettingsStore(tmp_path / "settings.json")
    store.load()
    db = Database(tmp_path / "j.db")
    svc = MemoryService(store, EventBus(), db, ThreadPoolExecutor(2), UsageTracker(db), lambda: client)
    return svc, db


def test_live_extracts_user_fact_and_marks_sensitive_pending(tmp_path, key):
    async def go():
        client = ClaudeClient(key)
        try:
            svc, db = make(tmp_path, client)
            await svc.process_exchange(1, "Benim adım Demir, kahveyi her zaman şekersiz içerim.",
                                       "Memnun oldum Demir, not aldım.")
            await svc.process_exchange(1, "Bunu hatırla: kan grubum A Rh+",
                                       "Not aldım; sağlıkla ilgili olduğu için önce onayını isteyeceğim.")
            return svc.store.list(), [dict(r) for r in db.query("SELECT model, cost_usd, route_reason FROM usage")]
        finally:
            await client.close()

    items, usage = asyncio.run(go())
    for m in items:
        print(f"\n  [{m.status}] ({m.category}, önem {m.importance}, çıkarım={m.is_inference}) {m.content}")
    print(f"  maliyet: ${sum(u['cost_usd'] for u in usage):.5f} ({len(usage)} çağrı)")
    assert any("Demir" in m.content and m.status == ACTIVE for m in items)
    blood = [m for m in items if "Rh" in m.content or "kan grub" in m.content.lower()]
    assert blood and all(m.status == PENDING and m.sensitive for m in blood)
    assert usage and all(u["route_reason"] == "hafıza" and u["cost_usd"] < 0.005 for u in usage)


def test_live_summary_of_short_chat_is_cheap(tmp_path, key):
    async def go():
        client = ClaudeClient(key)
        try:
            svc, db = make(tmp_path, client)
            cid = db.execute("INSERT INTO conversations (started_at, title) VALUES (datetime('now'), 'x')")
            for role, text in [
                ("user", "Oyun bilgisayarı için ekran kartı bakıyorum, bütçem 25 bin TL civarı."),
                ("assistant", "Bu bütçeyle RTX 5070 ya da RX 9070 iyi seçenekler."),
                ("user", "1440p oynuyorum, hangisi daha iyi olur sence?"),
                ("assistant", "1440p için ikisi de yeterli; ışın izleme önemliyse RTX 5070'i öneririm."),
                ("user", "Tamam, RTX 5070 alıyorum o zaman."),
                ("assistant", "Güzel seçim. Güç kaynağının en az 650 W olmasına dikkat et."),
            ]:
                db.execute("INSERT INTO messages (conversation_id, ts_utc, role, text) VALUES (?,?,?,?)",
                           (cid, "t", role, text))
            result = await svc.summarize(cid)
            return result, svc.store.list()
        finally:
            await client.close()

    result, items = asyncio.run(go())
    print(f"\n  sonuç={result} " + " | ".join(m.content for m in items))
    assert result == "özet" and items[0].category == "conversation_summary"
