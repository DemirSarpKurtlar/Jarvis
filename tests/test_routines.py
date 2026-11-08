"""Rutinler: depo (SQLite geçişi), doğrulama, yerel tetikleme, çalıştırma (tek onay), Claude araçları."""

import asyncio
import json

import pytest

from jarvis.config.settings import SettingsStore
from jarvis.core.chat_service import ChatDone
from jarvis.core.events import EventBus, Topics
from jarvis.core.state import StateMachine
from jarvis.core.tool_service import ToolService
from jarvis.security.audit_logger import AuditLogger
from jarvis.security.permission_manager import PermissionManager
from jarvis.storage.database import MIGRATIONS, Database
from jarvis.tools import intents
from jarvis.tools import registry as regmod
from jarvis.tools import routines as rt
from jarvis.tools.defaults import build_registry
from jarvis.tools.registry import ToolError
from tests.tool_fakes import ConfirmRecorder, make_ctx


@pytest.fixture
def win(monkeypatch):
    monkeypatch.setattr(regmod, "CURRENT_PLATFORM", "win32")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(rt.time, "sleep", lambda s: slept.append(s))
    yield slept
    intents.set_routine_matcher(None)


def setup(tmp_path, permissions=None):
    ctx = make_ctx(tmp_path)
    db = Database(tmp_path / "r.db")
    ctx.extra["db"] = db
    calls = []
    backends = {"locker": lambda: calls.append("kilit"), "power_fn": lambda a, d: calls.append((a, d))}
    reg = build_registry(ctx, audit=AuditLogger(db), permissions=permissions, action_backends=backends)
    return ctx, reg, ctx.extra["routines"], calls


def run_in_loop(ctx, coro_fn):
    async def go():
        ctx.scheduler.bind(asyncio.get_running_loop())
        return await coro_fn()
    return asyncio.run(go())


# ---------- depo ve geçiş ----------
def test_migration_seeds_disabled_examples(tmp_path):
    db = Database(tmp_path / "a.db")
    assert db.schema_version == len(MIGRATIONS)
    store = rt.RoutineStore(db)
    names = {r.name: r for r in store.list()}
    assert set(names) == {"Çalışma modu", "Oyun modu", "İyi geceler"}
    assert not any(r.enabled for r in names.values())
    assert names["Çalışma modu"].steps[0] == {"tool": "open_app", "args": {"name": "Visual Studio Code"}}
    # Örnekler kayıtlı araçların şemalarına uyuyor
    reg = build_registry(make_ctx(tmp_path))
    for r in names.values():
        rt.validate_definition(reg, r.name, r.phrases, r.steps)
    # Yeniden açmak örnekleri çoğaltmaz, silinen örnek geri gelmez
    store.delete(names["Oyun modu"].id)
    db.close()
    db2 = Database(tmp_path / "a.db")
    assert {r.name for r in rt.RoutineStore(db2).list()} == {"Çalışma modu", "İyi geceler"}


def test_store_crud_and_validation(tmp_path):
    _, reg, store, _ = setup(tmp_path)
    r = store.add("Film modu", ["film modu", "film zamanı"],
                  [{"tool": "set_volume", "args": {"level": 40}}, {"tool": "wait", "args": {"seconds": 2}},
                   {"tool": "say", "args": {"text": "  Keyifli seyirler  "}}])
    assert r.id and r.steps[2]["args"]["text"] == "Keyifli seyirler" and not r.enabled
    assert store.find("FİLM MODU").id == r.id and store.find("film zamani").id == r.id
    assert store.set_enabled(r.id, True) and store.get(r.id).enabled
    up = store.update(r.id, "Film modu", ["sinema"], [{"tool": "set_volume", "args": {"level": 50}}])
    assert up.enabled and store.get(r.id).phrases == ["sinema"]
    assert store.delete(r.id) and store.get(r.id) is None and not store.delete(r.id)


@pytest.mark.parametrize("steps,msg", [
    ([], "en az bir adım"),
    ([{"tool": "yok_boyle_arac", "args": {}}], "diye bir araç yok"),
    ([{"tool": "run_routine", "args": {"name": "x"}}], "rutin adımı olamaz"),
    ([{"tool": "delete_path", "args": {}}], "rutin adımı olamaz"),
    ([{"tool": "set_volume", "args": {"level": 150}}], "1. adım"),
    ([{"tool": "open_app", "args": {"name": "x", "fazla": 1}}], "bilinmeyen alan"),
    ([{"tool": "say", "args": {"text": "   "}}], "boş olamaz"),
    ([{"tool": "wait", "args": {"seconds": 60}}] * 6, "Toplam bekleme"),
    ([{"tool": "lock_screen", "args": {}}] * 21, "en çok 20"),
    ([{"tool": "open_app", "args": "Spotify"}], "JSON nesnesi"),
])
def test_invalid_steps_rejected(tmp_path, steps, msg):
    _, reg, store, _ = setup(tmp_path)
    with pytest.raises(ToolError) as e:
        store.add("Deneme", ["deneme"], steps)
    assert msg in e.value.user_message


def test_name_and_phrase_clashes(tmp_path):
    _, reg, store, _ = setup(tmp_path)
    ok = [{"tool": "lock_screen", "args": {}}]
    with pytest.raises(ToolError) as e:
        store.add("çalışma MODU", [], ok)
    assert "zaten var" in e.value.user_message
    with pytest.raises(ToolError) as e:
        store.add("Akşam", ["Oyun Modu"], ok)
    assert "oyun modu" in e.value.user_message and "Oyun modu" in e.value.user_message
    with pytest.raises(ToolError):
        store.add("x", [], ok)   # çok kısa ad
    with pytest.raises(ToolError):
        store.add("Ad <script>", [], ok)


# ---------- yerel tetikleme ----------
def test_trigger_candidates():
    c = rt.trigger_candidates("Jarvis, çalışma modunu aç lütfen")
    assert "calisma modu" in c
    assert "oyun modu" in rt.trigger_candidates("oyun moduna geç jarvis")


def test_local_intents_for_routines(tmp_path):
    _, reg, store, _ = setup(tmp_path)
    work = store.find("Çalışma modu")
    # Kapalı rutin sözle tetiklenmez (Claude'a gider)
    assert intents.parse("çalışma modu") is None
    store.set_enabled(work.id, True)
    for text in ("çalışma modu", "Jarvis, çalışma modunu aç", "calisma modu", "ÇALIŞMA MODU!", "çalışma zamanı",
                 "çalışma modu başlat"):
        hit = intents.parse(text)
        assert hit is not None and hit.tool == "run_routine" and hit.args == {"name": "Çalışma modu"}, text
    # Okunuşa toleranslı: konuşma tanıma yabancı adı Türkçe okunuşuyla yazar ("Chill" -> "çil")
    store.add("Chill modu", [], [{"tool": "set_volume", "args": {"level": 20}}], enabled=True)
    assert intents.parse("çil modu").args == {"name": "Chill modu"}
    # Açıkça adıyla: kapalı rutin de istenebilir, araç dürüstçe "kapalı" der
    hit = intents.parse("oyun modu rutinini çalıştır")
    assert hit.tool == "run_routine" and hit.args["name"] == "oyun modu"
    assert intents.parse("rutinleri listele").tool == "list_routines"
    assert intents.parse("hangi rutinler var").tool == "list_routines"
    # Mevcut yerel komutlar bozulmadı
    assert intents.parse("Notepad'i aç").tool == "open_app"
    assert intents.parse("sesi yüzde 30 yap").args == {"level": 30}


def test_matcher_errors_do_not_break_intents():
    intents.set_routine_matcher(lambda t: 1 / 0)
    assert intents.parse("Notepad'i aç").tool == "open_app"
    assert intents.parse("rastgele bir cümle") is None


# ---------- çalıştırma ----------
def test_run_safe_routine(tmp_path, win, no_sleep):
    ctx, reg, store, calls = setup(tmp_path)
    store.add("Film modu", ["film modu"],
              [{"tool": "open_app", "args": {"name": "Spotify"}}, {"tool": "set_volume", "args": {"level": 35}},
               {"tool": "wait", "args": {"seconds": 3}}, {"tool": "say", "args": {"text": "Keyifli seyirler."}},
               {"tool": "lock_screen", "args": {}}], enabled=True)
    asked = ConfirmRecorder(True)
    res = run_in_loop(ctx, lambda: reg.execute("run_routine", {"name": "film modu"}, asked))
    assert res.ok, res.error
    assert asked.prompts == []                     # onay gerektiren adım yok: sorulmadı
    assert ctx.audio.volume == 35 and calls == ["kilit"] and ctx.opener.opened
    assert no_sleep == [3]
    assert res.summary.startswith("Keyifli seyirler.") and "tamamlandı (5 adım)" in res.summary
    assert res.data["ok_steps"] == 5
    assert store.find("film modu").last_run_at
    # Her araç adımı denetim kaydına "rutin" kaynağıyla yazıldı
    log = reg.audit.recent()
    assert [e.tool for e in log if e.source == "rutin"] == ["lock_screen", "set_volume", "open_app"]
    assert log[0].tool == "run_routine"


def test_risky_steps_single_confirmation(tmp_path, win):
    ctx, reg, store, calls = setup(tmp_path)
    store.add("Kapanış", ["kapanış"],
              [{"tool": "set_volume", "args": {"level": 10}},
               {"tool": "power_action", "args": {"action": "shutdown", "delay_seconds": 60}},
               {"tool": "open_app", "args": {"name": "powershell"}}], enabled=True)
    no = ConfirmRecorder(False)
    res = run_in_loop(ctx, lambda: reg.execute("run_routine", {"name": "Kapanış"}, no))
    assert not res.ok and len(no.prompts) == 1
    msg = no.prompts[0].message
    assert "2." in msg and "3." in msg and "Kapat / yeniden başlat" in msg and "1. Sesi" not in msg
    assert ctx.audio.volume == 50 and calls == []   # hiçbir adım çalışmadı
    yes = ConfirmRecorder(True)
    res = run_in_loop(ctx, lambda: reg.execute("run_routine", {"name": "Kapanış"}, yes))
    assert res.ok and len(yes.prompts) == 1          # adımlar için ayrıca sorulmadı
    assert calls == [("shutdown", 60)] and ctx.audio.volume == 10 and ctx.opener.opened
    # Onay istenemiyorsa (ör. zamanlayıcı) riskli rutin çalışmaz
    res = run_in_loop(ctx, lambda: reg.execute("run_routine", {"name": "Kapanış"}, None))
    assert not res.ok and calls == [("shutdown", 60)]


def test_unexpected_confirmation_is_denied(tmp_path, win, monkeypatch):
    ctx, reg, store, calls = setup(tmp_path)
    store.add("Gizli", ["gizli"], [{"tool": "power_action", "args": {"action": "restart", "delay_seconds": 30}},
                                   {"tool": "set_volume", "args": {"level": 20}}], enabled=True)
    # Ön kestirim yanılırsa (adım çalışırken onay isterse) o adım onaylanmış sayılmaz
    monkeypatch.setattr(rt.RoutineRunner, "_step_level", lambda self, step: (None, ""))
    yes = ConfirmRecorder(True)
    res = run_in_loop(ctx, lambda: reg.execute("run_routine", {"name": "gizli"}, yes))
    assert yes.prompts == [] and calls == [] and ctx.audio.volume == 20
    assert res.ok and "önceden onaylanmamış" in res.summary and res.data["ok_steps"] == 1


def test_disabled_unknown_and_forbidden(tmp_path, win):
    ctx, reg, store, _ = setup(tmp_path)
    res = run_in_loop(ctx, lambda: reg.execute("run_routine", {"name": "Oyun modu"}, ConfirmRecorder(True)))
    assert not res.ok and "kapalı" in res.error
    res = run_in_loop(ctx, lambda: reg.execute("run_routine", {"name": "uzay modu"}, ConfirmRecorder(True)))
    assert not res.ok and "bulamadım" in res.error and "Çalışma modu" in res.error
    # Salt okunur mod: rutin de, adımları da çalışmaz
    (tmp_path / "ro").mkdir()
    ctx2, reg2, store2, _ = setup(tmp_path / "ro", permissions=PermissionManager(read_only=True))
    store2.set_enabled(store2.find("İyi geceler").id, True)
    res = run_in_loop(ctx2, lambda: reg2.execute("run_routine", {"name": "iyi geceler"}, ConfirmRecorder(True)))
    assert not res.ok


def test_unsupported_os_steps_report_honestly(tmp_path):
    ctx, reg, store, _ = setup(tmp_path)   # Linux: Windows'a özgü adımlar desteklenmiyor
    store.set_enabled(store.find("Çalışma modu").id, True)
    res = run_in_loop(ctx, lambda: reg.execute("run_routine", {"name": "çalışma modu"}, ConfirmRecorder(True)))
    assert res.ok
    assert "5 adımın 1 tanesi yapıldı" in res.summary and "desteklenmiyor" in res.summary
    assert res.summary.startswith("Çalışma modu hazır.")


def test_runner_not_ready_without_loop(tmp_path, win):
    ctx, reg, store, _ = setup(tmp_path)
    store.set_enabled(store.find("Çalışma modu").id, True)
    res = asyncio.run(reg.execute("run_routine", {"name": "çalışma modu"}, ConfirmRecorder(True)))
    assert not res.ok and "hazır değil" in res.error


# ---------- Claude araçları ----------
def test_create_routine_requires_confirmation(tmp_path, win):
    ctx, reg, store, _ = setup(tmp_path)
    args = {"name": "Akşam modu", "phrases": ["akşam modu", "akşam oldu"], "replace_existing": False,
            "steps": [{"tool": "open_app", "args_json": json.dumps({"name": "Steam"})},
                      {"tool": "open_app", "args_json": "{\"name\": \"Discord\"}"},
                      {"tool": "wait", "args_json": "{\"seconds\": 5}"},
                      {"tool": "say", "args_json": "{\"text\": \"İyi eğlenceler\"}"}]}
    no = ConfirmRecorder(False)
    res = asyncio.run(reg.execute("create_routine", args, no))
    assert not res.ok and store.find("Akşam modu") is None
    msg = no.prompts[0].message
    assert "Uygulama aç: Steam" in msg and "Uygulama aç: Discord" in msg and "5 saniye bekle" in msg
    assert "“akşam modu”" in msg
    res = asyncio.run(reg.execute("create_routine", args, ConfirmRecorder(True)))
    assert res.ok and "akşam modu" in res.summary
    r = store.find("akşam modu")
    assert r.enabled and r.steps[0] == {"tool": "open_app", "args": {"name": "Steam"}}
    assert intents.parse("akşam oldu").tool == "run_routine"
    # Aynı ad: replace_existing olmadan reddedilir, onunla güncellenir
    res = asyncio.run(reg.execute("create_routine", args, ConfirmRecorder(True)))
    assert not res.ok and "zaten var" in res.error
    args2 = {**args, "replace_existing": True, "steps": [{"tool": "set_volume", "args_json": "{\"level\": 20}"}]}
    yes = ConfirmRecorder(True)
    res = asyncio.run(reg.execute("create_routine", args2, yes))
    assert res.ok and "güncelle" in yes.prompts[0].message and len(store.find("akşam modu").steps) == 1


@pytest.mark.parametrize("steps,msg", [
    ([{"tool": "open_app", "args_json": "{bozuk"}], "geçerli JSON"),
    ([{"tool": "run_routine", "args_json": "{\"name\": \"x\"}"}], "rutin adımı olamaz"),
    ([{"tool": "set_volume", "args_json": "{\"level\": 500}"}], "en çok 100"),
])
def test_create_routine_invalid(tmp_path, steps, msg):
    _, reg, store, _ = setup(tmp_path)
    yes = ConfirmRecorder(True)
    res = asyncio.run(reg.execute("create_routine", {"name": "Bozuk", "phrases": [], "steps": steps,
                                                     "replace_existing": False}, yes))
    assert not res.ok and msg in res.error and yes.prompts == []


def test_list_routines_and_schemas(tmp_path):
    _, reg, store, _ = setup(tmp_path)
    res = asyncio.run(reg.execute("list_routines", {}))
    assert res.ok and "Kapalı rutinler" in res.summary and len(res.data) == 3
    tools = {t["name"]: t for t in reg.to_claude_tools()}
    assert {"list_routines", "run_routine", "create_routine", "morning_briefing"} <= set(tools)
    assert "confirmation" in tools["create_routine"]["description"]


def test_no_db_is_honest(tmp_path):
    reg = build_registry(make_ctx(tmp_path))
    res = asyncio.run(reg.execute("list_routines", {}))
    assert not res.ok and "hazır değil" in res.error


# ---------- ToolService üzerinden yerel komut ----------
def test_local_command_runs_routine_through_tool_service(tmp_path, win):
    store = SettingsStore(tmp_path / "settings.json")
    store.load()
    bus, state, db = EventBus(), StateMachine(), Database(tmp_path / "j.db")
    ctx = make_ctx(tmp_path)
    ts = ToolService(store, bus, state, db, ctx)
    routines = ctx.extra["routines"]
    routines.add("Sessiz mod", ["sessiz mod"], [{"tool": "set_volume", "args": {"level": 5}},
                                                 {"tool": "say", "args": {"text": "Sessiz mod açık."}}], enabled=True)
    done = []
    bus.subscribe(Topics.CHAT_DONE, done.append)

    async def go():
        ts.bind_loop(asyncio.get_running_loop())
        return await ts.handle_local("Jarvis, sessiz modu aç")

    assert asyncio.run(go()) is True
    assert ctx.audio.volume == 5
    assert isinstance(done[-1], ChatDone) and done[-1].text.startswith("Sessiz mod açık.")
    ts.shutdown()
