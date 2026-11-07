"""Aşama 4: uzun süreli hafıza (sahte API, para harcamaz)."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from tests.fake_api import FakeAPI, json_message, text_stream
from jarvis.ai.prompts import STABLE_SYSTEM_PROMPT
from jarvis.ai.usage import UsageTracker
from jarvis.config.settings import SettingsStore
from jarvis.core.chat_service import ChatService
from jarvis.core.events import EventBus, Topics
from jarvis.core.state import StateMachine
from jarvis.memory.extractor import detect_command, worth_checking
from jarvis.memory.retrieval import MemoryRetriever
from jarvis.memory.service import MemoryService
from jarvis.memory.store import ACTIVE, PENDING, Candidate, MemoryStore, SecretRejected
from jarvis.memory.text import contains_secret, is_near_duplicate, normalize
from jarvis.security.credentials import CredentialStore
from jarvis.storage.database import Database

KEY = "sk-ant-api03-" + "M" * 40


class Clock:
    def __init__(self, now=None):
        self.now = now or datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


def mem(content, category="preference", **kw):
    return Candidate(content=content, category=category, **kw)


def run(coro):
    return asyncio.run(coro)


# ---------------- metin araçları ----------------

def test_turkish_normalization():
    assert normalize("IŞIK") == normalize("ışık") == normalize("isik") == "isik"
    assert normalize("İSTANBUL'da") == "istanbul da"
    assert normalize("ŞEKERSİZ Çay, Ğ ü ö") == "sekersiz cay g u o"


def test_near_duplicate_rules():
    assert is_near_duplicate("Kullanıcının adı Demir.", "kullanıcının adı demir")
    assert is_near_duplicate("Kahveyi şekersiz içiyor.", "Kahveyi şekerli içiyor")      # düzeltme sayılır
    assert not is_near_duplicate("Annesinin doğum günü 5 Mart.", "Babasının doğum günü 5 Mart.")
    assert not is_near_duplicate("Steam kullanıyor.", "Cevapları kısa tercih ediyor.")


def test_secret_detection():
    assert contains_secret("anahtarım sk-ant-api03-abcdefghijklmnopqrstu")
    assert contains_secret("şifrem Kedi2024!")
    assert contains_secret("kartım 4111 1111 1111 1111")
    assert contains_secret("ghp_" + "a1" * 15)
    assert not contains_secret("Saat 14:30'da toplantım var")
    assert not contains_secret("şifremi unuttum, ne yapayım")


def test_local_gate_and_commands():
    assert not worth_checking("İstanbul'un nüfusu kaç")
    assert not worth_checking("Python'da liste nasıl sıralanır?")
    assert worth_checking("Benim adım Demir")
    assert worth_checking("Cuma günü sunumu teslim etmem lazım")
    assert worth_checking("Bunu hatırla: kahveyi şekersiz içerim")
    assert not worth_checking("şifrem Kedi2024! bunu hatırla")       # gizli bilgi: hiç gönderilmez
    assert detect_command("Bunu hatırla: kahveyi şekersiz içerim").kind == "remember"
    assert detect_command("Bunu unutma, yarın toplantı var").kind == "remember"
    assert detect_command("kahve tercihimi unut").kind == "forget"
    assert detect_command("Hafızandan sil: kahve").kind == "forget"
    assert detect_command("şifremi unuttum") is None
    assert detect_command("bunu unut").deictic


# ---------------- depo ----------------

def test_persists_across_reopen(tmp_path):
    db = Database(tmp_path / "j.db")
    r = MemoryStore(db).add(mem("Cevapları kısa ve net tercih ediyor.", importance=4))
    assert r.action == "eklendi" and r.memory.status == ACTIVE
    db.close()
    db2 = Database(tmp_path / "j.db")
    assert db2.schema_version == 2
    items = MemoryStore(db2).list()
    assert [m.content for m in items] == ["Cevapları kısa ve net tercih ediyor."]
    assert items[0].importance == 4
    # Arama dizini de kalıcı
    assert MemoryRetriever(MemoryStore(db2)).search("kısa cevap")[0].memory.id == items[0].id
    db2.close()


def test_existing_phase2_database_is_upgraded(tmp_path, monkeypatch):
    import jarvis.storage.database as dbmod
    full = list(dbmod.MIGRATIONS)
    monkeypatch.setattr(dbmod, "MIGRATIONS", full[:1])
    old = Database(tmp_path / "j.db")
    assert old.schema_version == 1
    old.execute("INSERT INTO conversations (started_at, title) VALUES ('2026-11-01T10:00:00+00:00', 'eski')")
    old.close()
    monkeypatch.setattr(dbmod, "MIGRATIONS", full)
    new = Database(tmp_path / "j.db")
    assert new.schema_version == 2
    assert new.query("SELECT title FROM conversations")[0][0] == "eski"   # eski veri korunur
    assert MemoryStore(new).add(mem("Steam kullanıyor.", "app")).action == "eklendi"
    new.close()


def test_duplicate_is_merged_not_inserted(tmp_path):
    s = MemoryStore(Database(tmp_path / "j.db"))
    a = s.add(mem("Kullanıcının adı Demir.", "personal", importance=3))
    b = s.add(mem("kullanıcının adı Demir", "personal", importance=5))
    assert b.action == "birleştirildi" and b.memory.id == a.memory.id
    assert s.count() == 1 and s.get(a.memory.id).importance == 5
    # Kullanıcının düzeltmesi: en yeni ifade geçerli
    c = s.add(mem("Kahveyi şekersiz içiyor.", importance=3))
    d = s.add(mem("Kahveyi şekerli içiyor.", importance=3))
    assert d.memory.id == c.memory.id and s.get(c.memory.id).content == "Kahveyi şekerli içiyor."
    # Benzer kalıp ama farklı bilgi: ayrı kayıt
    s.add(mem("Annesinin doğum günü 5 Mart.", "personal"))
    s.add(mem("Babasının doğum günü 5 Mart.", "personal"))
    assert s.count() == 4


def test_expiry_removes_temporary_memories(tmp_path):
    clock = Clock()
    s = MemoryStore(Database(tmp_path / "j.db"), clock)
    s.add(mem("Cuma günü sunum dosyasını bitirecek.", "task", expires_at=clock.now + timedelta(days=2)))
    s.add(mem("Steam kullanıyor.", "app"))
    assert len(s.list()) == 2
    clock.now += timedelta(days=3)
    assert [m.content for m in s.list()] == ["Steam kullanıyor."]   # süresi dolan görünmez
    assert MemoryRetriever(s).search("sunum dosyası") == []
    assert s.purge_expired() == 1
    assert s.count() == 1


def test_sensitive_goes_pending_even_if_model_says_no(tmp_path):
    s = MemoryStore(Database(tmp_path / "j.db"))
    r = s.add(mem("Kan grubu A Rh+.", "personal", sensitive=False))      # yerel denetim yakalar
    assert r.memory.status == PENDING and r.memory.sensitive and r.memory.sensitive_kind == "health"
    r2 = s.add(mem("Bankadaki birikimi 200 bin TL.", "personal", sensitive=True, sensitive_kind="finance"))
    assert r2.memory.status == PENDING
    # Onay bekleyen hassas bilgi aramada ve bağlamda asla kullanılmaz
    assert MemoryRetriever(s).search("kan grubum ne") == []
    s.confirm(r.memory.id)
    assert MemoryRetriever(s).search("kan grubum ne")[0].memory.id == r.memory.id


def test_inference_is_never_stored_as_confirmed(tmp_path):
    s = MemoryStore(Database(tmp_path / "j.db"))
    r = s.add(mem("Muhtemelen yazılımcı.", "personal", is_inference=True, source="çıkarım"))
    assert r.memory.status == PENDING and r.memory.is_inference
    # Aynı çıkarım tekrar gelse de onaylanmaz
    r2 = s.add(mem("Muhtemelen yazılımcı.", "personal", is_inference=True, source="çıkarım", importance=5))
    assert r2.memory.id == r.memory.id and r2.memory.status == PENDING
    # Çıkarım, onaylı bir kaydın metnini değiştiremez
    ok = s.add(mem("Steam ve Discord kullanıyor.", "app"))
    s.add(mem("Steam ve Discord kullanıyor", "app", is_inference=True))
    assert s.get(ok.memory.id).status == ACTIVE and not s.get(ok.memory.id).is_inference
    # Kullanıcı kendisi söylerse doğrulanmış olur
    r3 = s.add(mem("Muhtemelen yazılımcı", "personal"))
    assert r3.memory.status == ACTIVE and not r3.memory.is_inference


def test_secrets_are_never_stored(tmp_path):
    s = MemoryStore(Database(tmp_path / "j.db"))
    assert s.add(mem("API anahtarı sk-ant-api03-abcdefghijklmnopqrstuvwx", "personal")).action == "reddedildi"
    assert s.add(mem("Wi-Fi şifresi elmaarmut.", "personal")).action == "reddedildi"
    assert s.add(mem("Kart numarası 4111 1111 1111 1111.", "personal")).action == "reddedildi"
    assert s.count() == 0
    ok = s.add(mem("Steam kullanıyor.", "app"))
    with pytest.raises(SecretRejected):
        s.update(ok.memory.id, content="Steam şifresi Kedi2024!")
    assert s.get(ok.memory.id).content == "Steam kullanıyor."


def test_user_edit_and_delete(tmp_path):
    s = MemoryStore(Database(tmp_path / "j.db"))
    r = s.add(mem("Muhtemelen öğretmen.", "personal", is_inference=True, source="çıkarım"))
    m = s.update(r.memory.id, content="Mühendis.", importance=5, category="personal")
    assert m.content == "Mühendis." and m.status == ACTIVE and not m.is_inference and m.importance == 5
    assert MemoryRetriever(s).search("mühendis")[0].memory.id == m.id
    assert s.delete(m.id) and s.count() == 0
    assert MemoryRetriever(s).search("mühendis") == []     # dizin de temizlendi


# ---------------- arama ----------------

def test_retrieval_relevance_and_turkish_chars(tmp_path):
    s = MemoryStore(Database(tmp_path / "j.db"))
    for c, cat, imp in [("Kahveyi şekersiz içmeyi tercih ediyor.", "preference", 3),
                        ("Işık açıkken uyuyamıyor.", "personal", 2),
                        ("Akşamları en çok Steam ve Discord'u açıyor.", "app", 3),
                        ("GPU 85 °C üstüne çıkınca fan eğrisi ayarı sorunu çözmüştü.", "technical", 3),
                        ("Cevapları kısa ve net tercih ediyor.", "preference", 5)]:
        s.add(mem(c, cat, importance=imp))
    r = MemoryRetriever(s)
    assert r.search("KAHVEMİ nasıl içerim?", fill_core=False)[0].memory.content.startswith("Kahveyi")
    assert r.search("İŞIK açık kalsın mı", fill_core=False)[0].memory.content.startswith("Işık")
    assert r.search("isik", fill_core=False)[0].memory.content.startswith("Işık")
    assert r.search("discord açılmıyor", fill_core=False)[0].memory.category == "app"
    assert r.search("ekran kartı fanı çok ses yapıyor, GPU ısınıyor", fill_core=False)[0].memory.category == \
        "technical"
    # İlgisiz soru: yalnızca temel bilgiler (yüksek önemli tercih) eklenir
    res = r.search("bugün hava nasıl")
    assert [x.memory.content for x in res] == ["Cevapları kısa ve net tercih ediyor."] and not res[0].matched
    assert len(r.search("tercih kahve steam discord gpu ışık kısa", k=3)) == 3


def test_retrieval_prefers_important_recent_and_used(tmp_path):
    clock = Clock()
    s = MemoryStore(Database(tmp_path / "j.db"), clock)
    old = s.add(mem("Python projelerinde VS Code kullanıyor.", "app", importance=2)).memory
    clock.now += timedelta(days=200)
    new = s.add(mem("Python projelerinde PyCharm kullanmaya başladı.", "app", importance=4)).memory
    res = MemoryRetriever(s).search("python projesi", fill_core=False)
    assert [x.memory.id for x in res] == [new.id, old.id]
    s.mark_used([old.id])
    assert s.get(old.id).use_count == 1 and s.get(old.id).last_used_at == clock.now


# ---------------- servis (sahte API ile) ----------------

class MemHarness:
    def __init__(self, tmp_path, responses=(), clock=None):
        self.store = SettingsStore(tmp_path / "settings.json")
        self.store.load()
        self.bus = EventBus()
        self.db = Database(tmp_path / "j.db")
        self.events = []
        for t in (Topics.MEMORY_CHANGED, Topics.MEMORY_PENDING, Topics.USAGE_UPDATED):
            self.bus.subscribe(t, lambda p, t=t: self.events.append((t, p)))
        self.api = FakeAPI(list(responses))
        self.tracker = UsageTracker(self.db)
        self.client = None
        self.svc = MemoryService(self.store, self.bus, self.db, ThreadPoolExecutor(2), self.tracker,
                                 lambda: self.client, clock=clock or Clock())

    def of(self, topic):
        return [p for t, p in self.events if t == topic]

    def usage_rows(self):
        return [dict(r) for r in self.db.query("SELECT * FROM usage")]


def extraction(*items):
    return json_message({"memories": [dict({"importance": 3, "sensitive_kind": "none", "is_inference": False,
                                            "expires_in_days": None, "replaces_id": None}, **i) for i in items]})


def test_extraction_calls_haiku_with_structured_output_and_records_cost(tmp_path):
    h = MemHarness(tmp_path, [extraction({"content": "Adı Demir.", "category": "personal", "importance": 5})])

    async def go():
        h.client = h.api.client()
        await h.svc.process_exchange(1, "Benim adım Demir", "Memnun oldum Demir!")

    run(go())
    body = h.api.requests[-1]["body"]
    assert body["model"] == "claude-haiku-5-5" and not body.get("stream")
    assert body["output_config"]["effort"] == "low"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["thinking"] == {"type": "disabled"}
    assert "Benim adım Demir" in body["messages"][0]["content"]
    items = h.svc.store.list()
    assert [(m.content, m.status, m.source) for m in items] == [("Adı Demir.", ACTIVE, "konuşma:1")]
    rows = h.usage_rows()
    assert len(rows) == 1 and rows[0]["route_reason"] == "hafıza" and rows[0]["model"] == "claude-haiku-5-5"
    assert rows[0]["cost_usd"] == pytest.approx((1500 * 0.10 + 120 * 0.50) / 1e6)
    assert h.of(Topics.USAGE_UPDATED)[-1].today.cost_usd == pytest.approx(rows[0]["cost_usd"])
    assert h.of(Topics.MEMORY_CHANGED)


def test_trivial_messages_never_call_api(tmp_path):
    h = MemHarness(tmp_path)

    async def go():
        h.client = h.api.client()
        await h.svc.process_exchange(1, "İstanbul'un nüfusu kaç?", "Yaklaşık 16 milyon.")
        await h.svc.process_exchange(1, "selam", "Merhaba!")

    run(go())
    assert h.api.requests == [] and h.usage_rows() == []


def test_sensitive_and_inference_candidates_wait_for_user(tmp_path):
    h = MemHarness(tmp_path, [extraction(
        {"content": "Kan grubu A Rh+.", "category": "personal", "sensitive_kind": "health"},
        {"content": "Muhtemelen sağlıkçı.", "category": "personal", "is_inference": True},
        {"content": "Wi-Fi şifresi Kedi2024!", "category": "personal"},
    )])

    async def go():
        h.client = h.api.client()
        await h.svc.process_exchange(3, "Benim kan grubum A Rh+, hastanede çalışıyorum", "Not aldım.")

    run(go())
    items = {m.content: m for m in h.svc.store.list()}
    assert set(items) == {"Kan grubu A Rh+.", "Muhtemelen sağlıkçı."}      # şifre kaydedilmedi
    assert items["Kan grubu A Rh+."].status == PENDING and items["Kan grubu A Rh+."].sensitive
    assert items["Muhtemelen sağlıkçı."].status == PENDING and items["Muhtemelen sağlıkçı."].source == "çıkarım"
    assert len(h.of(Topics.MEMORY_PENDING)) == 2
    assert not any(m.status == ACTIVE for m in items.values())


def test_secret_in_message_skips_extraction_entirely(tmp_path):
    h = MemHarness(tmp_path)

    async def go():
        h.client = h.api.client()
        await h.svc.process_exchange(1, "Benim şifrem Kedi2024! bunu hatırla", "Şifreni kaydetmiyorum.")

    run(go())
    assert h.api.requests == [] and h.svc.store.count() == 0


def test_disabled_memory_makes_no_calls_and_no_context(tmp_path):
    h = MemHarness(tmp_path)
    h.svc.store.add(mem("Cevapları kısa ve net tercih ediyor.", importance=5))
    h.store.settings.memory.enabled = False

    async def go():
        h.client = h.api.client()
        await h.svc.process_exchange(1, "Benim adım Demir, bunu hatırla", "Tamam.")
        assert h.svc.after_exchange(1, "Benim adım Demir", "Tamam.") is None
        assert await h.svc.summarize(1) is None
        assert h.svc.schedule_maintenance(None) is None
        return await h.svc.context_for("Cevaplarını nasıl istiyorum?", 1)

    ctx = run(go())
    assert h.api.requests == []
    assert "hafıza kapalı" in ctx.lower() and "kısa ve net" not in ctx
    assert h.svc.store.list()[0].use_count == 0


def test_explicit_remember_works_offline_with_local_fallback(tmp_path):
    h = MemHarness(tmp_path)   # istemci yok: anahtar girilmemiş / internet yok

    async def go():
        ctx = await h.svc.context_for("Bunu hatırla: kahveyi şekersiz içerim", 1)
        await h.svc.process_exchange(1, "Bunu hatırla: kahveyi şekersiz içerim", "Not aldım.")
        return ctx

    ctx = run(go())
    assert "hatırlamanı istedi" in ctx
    [m] = h.svc.store.list()
    assert m.content == "Kahveyi şekersiz içerim." and m.status == ACTIVE and not m.is_inference


def test_forget_command_deletes_matching_memory_before_reply(tmp_path):
    h = MemHarness(tmp_path)
    keep = h.svc.store.add(mem("Steam kullanıyor.", "app")).memory
    h.svc.store.add(mem("Kahveyi şekersiz içmeyi tercih ediyor.", importance=4))

    async def go():
        ctx = await h.svc.context_for("Kahve tercihimi unut", 1)
        missing = await h.svc.context_for("Doğum günümü unut", 1)
        return ctx, missing

    ctx, missing = run(go())
    assert [m.id for m in h.svc.store.list()] == [keep.id]
    assert "kalıcı olarak silindi" in ctx and "Kahveyi şekersiz" in ctx
    assert "bulunamadı" in missing
    assert h.api.requests == []      # unutma tamamen yerel


def test_forget_this_deletes_latest_memory_of_conversation(tmp_path):
    h = MemHarness(tmp_path)
    h.svc.store.add(mem("Steam kullanıyor.", "app", source="konuşma:1"))
    last = h.svc.store.add(mem("Cuma günü sunumu var.", "task", source="konuşma:2")).memory

    run(h.svc.context_for("bunu unut", 2))
    assert last.id not in [m.id for m in h.svc.store.list()] and h.svc.store.count() == 1


def test_correction_replaces_wrong_memory(tmp_path):
    h = MemHarness(tmp_path)
    wrong = h.svc.store.add(mem("Adı Deniz.", "personal", importance=5)).memory
    h.api.responses.append(extraction({"content": "Adı Demir.", "category": "personal", "importance": 5,
                                       "replaces_id": wrong.id}))

    async def go():
        h.client = h.api.client()
        await h.svc.process_exchange(4, "Hayır, benim adım Deniz değil, Demir", "Düzelttim.")

    run(go())
    assert "[%d] (personal) Adı Deniz." % wrong.id in h.api.requests[-1]["body"]["messages"][0]["content"]
    [m] = h.svc.store.list()
    assert m.id == wrong.id and m.content == "Adı Demir." and m.status == ACTIVE


def test_budget_block_skips_memory_calls(tmp_path):
    h = MemHarness(tmp_path)
    h.store.settings.budget.on_limit = "block"
    h.store.settings.budget.daily_limit_usd = 0.000001
    h.db.execute("INSERT INTO usage (ts_utc, local_day, local_month, model, tier, input_tokens, output_tokens,"
                 " cache_creation_tokens, cache_read_tokens, web_search_requests, cost_usd)"
                 " VALUES ('x', strftime('%Y-%m-%d','now','localtime'), strftime('%Y-%m','now','localtime'),"
                 " 'claude-haiku-5-5','light',0,0,0,0,0,1.0)")

    async def go():
        h.client = h.api.client()
        await h.svc.process_exchange(1, "Benim adım Demir", "Merhaba Demir.")

    run(go())
    assert h.api.requests == [] and h.svc.store.count() == 0


def test_api_failure_and_refusal_are_safe(tmp_path):
    h = MemHarness(tmp_path, [(500, {"type": "error", "error": {"type": "api_error", "message": "x"}}),
                              json_message(None, stop_reason="refusal", raw_text="")])

    async def go():
        h.client = h.api.client()
        await h.svc.process_exchange(1, "Benim adım Demir", "Merhaba.")
        await h.svc.process_exchange(1, "Benim adım Demir", "Merhaba.")

    run(go())
    assert h.svc.store.count() == 0
    assert [r["route_reason"] for r in h.usage_rows()] == ["hafıza"]   # ret de faturalanır, kaydedilir


def _add_conversation(db, n_exchanges, text="Oyun bilgisayarım için ekran kartı seçiyorum, bütçem orta seviye."):
    cid = db.execute("INSERT INTO conversations (started_at, title) VALUES (?, ?)",
                     (datetime.now(timezone.utc).isoformat(timespec="seconds"), "x"))
    for i in range(n_exchanges):
        db.execute("INSERT INTO messages (conversation_id, ts_utc, role, text) VALUES (?,?,?,?)",
                   (cid, "t", "user", f"{text} ({i})"))
        db.execute("INSERT INTO messages (conversation_id, ts_utc, role, text) VALUES (?,?,?,?)",
                   (cid, "t", "assistant", "RTX 5070 iyi bir seçim olur, fiyat performans oranı yüksek."))
    return cid


def test_summarizer_skips_short_and_summarizes_long_conversations(tmp_path):
    h = MemHarness(tmp_path, [json_message({"worth_keeping": True, "importance": 3,
                                            "summary": "Oyun bilgisayarı için ekran kartı seçildi: RTX 5070."}),
                              json_message({"worth_keeping": False, "importance": 1, "summary": ""})],
                   clock=Clock(datetime.now(timezone.utc)))
    short = _add_conversation(h.db, 1, "selam")
    long = _add_conversation(h.db, 3)
    chat = _add_conversation(h.db, 3, "Bugün nasılsın, canım sıkılıyor biraz, sohbet edelim mi seninle?")

    async def go():
        h.client = h.api.client()
        r = [await h.svc.summarize(short), await h.svc.summarize(long), await h.svc.summarize(chat)]
        r.append(await h.svc.summarize(long))   # ikinci kez özetlenmez
        await h.svc.summarize_pending(None)     # hepsi işaretli: yeni çağrı yok
        return r

    assert run(go()) == ["kısa", "özet", "gereksiz", None]
    assert len(h.api.requests) == 2
    assert "<konusma>" in h.api.requests[0]["body"]["messages"][0]["content"]
    [m] = h.svc.store.list()
    assert m.category == "conversation_summary" and m.source == f"konuşma:{long}" and m.expires_at is not None
    assert [r["route_reason"] for r in h.usage_rows()] == ["hafıza", "hafıza"]
    # Kullanıcı özeti silse de bir sonraki açılışta yeniden oluşturulmaz
    h.svc.store.delete(m.id)
    run(h.svc.summarize_pending(None))
    assert h.svc.store.count() == 0 and len(h.api.requests) == 2


def test_conversation_switch_triggers_summary_of_previous(tmp_path):
    h = MemHarness(tmp_path, [json_message({"worth_keeping": True, "importance": 3,
                                            "summary": "Ekran kartı seçimi konuşuldu."})],
                   clock=Clock(datetime.now(timezone.utc)))
    first = _add_conversation(h.db, 3)

    async def go():
        h.client = h.api.client()
        h.svc.after_exchange(first, "selam", "merhaba")
        h.svc.after_exchange(first + 1, "selam", "merhaba")    # 30 dk sessizlik sonrası yeni konuşma
        await h.svc.wait_idle()

    run(go())
    assert [m.content for m in h.svc.store.list()] == ["Ekran kartı seçimi konuşuldu."]


# ---------------- sohbet servisiyle birlikte ----------------

class ChatHarness:
    def __init__(self, tmp_path, responses):
        self.store = SettingsStore(tmp_path / "settings.json")
        self.store.load()
        self.bus = EventBus()
        self.state = StateMachine()
        self.db = Database(tmp_path / "j.db")
        CredentialStore().set_anthropic_key(KEY)
        self.api = FakeAPI(list(responses))
        ex = ThreadPoolExecutor(2)
        self.svc = ChatService(self.store, self.bus, self.state, self.db, ex,
                               client_factory=lambda key, max_retries=0: self.api.client(key, max_retries=0))
        self.mem = MemoryService(self.store, self.bus, self.db, ex, self.svc.tracker, lambda: self.svc._client)
        self.svc.memory = self.mem
        self.details = []
        self.state.add_listener(lambda ch: self.details.append(ch.detail))

    def chat_bodies(self):
        return [r["body"] for r in self.api.requests if r["body"].get("stream")]


def test_memory_injected_only_in_dynamic_block_and_cache_stays_intact(tmp_path):
    h = ChatHarness(tmp_path, [
        text_stream(["Memnun oldum Demir!"]),
        extraction({"content": "Adı Demir.", "category": "personal", "importance": 5}),
        text_stream(["Tabii."]),
    ])

    async def go():
        await h.svc.start()
        await h.svc.send("Benim adım Demir")
        await h.mem.wait_idle()
        await h.svc.send("Bana bir şiir yaz")
        await h.svc.shutdown()

    run(go())
    first, second = h.chat_bodies()
    # Önbelleklenen sabit blok bayt bayt aynı, işaretli ve hafıza içermiyor
    assert first["system"][0] == second["system"][0] == {
        "type": "text", "text": STABLE_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
    assert "Demir" not in second["system"][0]["text"]
    dyn = second["system"][1]
    assert "cache_control" not in dyn
    assert "Uzun süreli hafıza" in dyn["text"] and "[doğrulanmış, kullanıcı söyledi] Adı Demir." in dyn["text"]
    assert "Bu mesajla ilgili kayıt yok" in first["system"][1]["text"]
    assert any("hafızadan 1 kayıt kullanılıyor" in d for d in h.details)
    # Geçmiş yine yalnızca metin; hafıza mesajlara karışmıyor
    assert [m["role"] for m in second["messages"]] == ["user", "assistant", "user"]
    reasons = [r["route_reason"] for r in h.db.query("SELECT route_reason FROM usage ORDER BY id")]
    assert reasons == ["basit istek", "hafıza", "basit istek"]


def test_unconfirmed_inference_is_labeled_and_sensitive_pending_is_hidden(tmp_path):
    h = ChatHarness(tmp_path, [text_stream(["Tamam."])])
    h.mem.store.add(mem("Muhtemelen Python ile çalışıyor.", "technical", is_inference=True))
    h.mem.store.add(mem("Python projelerinde diyabet takibi uygulaması yazıyor.", "task"))   # hassas: onay bekler

    async def go():
        await h.svc.start()
        await h.svc.send("Python sürümümü nasıl güncellerim")
        await h.svc.shutdown()

    run(go())
    dyn = h.chat_bodies()[0]["system"][1]["text"]
    assert "[doğrulanmamış çıkarım, kesin bilgi gibi sunma] Muhtemelen Python ile çalışıyor." in dyn
    assert "diyabet" not in dyn


def test_chat_service_without_memory_is_unchanged(tmp_path):
    from tests.test_chat import Harness
    h = Harness(tmp_path, FakeAPI([text_stream(["Merhaba"])]))

    async def go():
        await h.svc.start()
        await h.svc.send("Benim adım Demir")

    run(go())
    assert h.svc.memory is None
    assert h.api.requests[-1]["body"]["system"][1]["text"].startswith("Bugünün tarihi")
    assert "hafıza" not in h.api.requests[-1]["body"]["system"][1]["text"].lower()
