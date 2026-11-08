"""Araç kaydı: şema doğrulama, strict uyumluluğu, izin kapısı, onay, boyut sınırı, denetim kaydı."""

import asyncio
import json
import time

import pytest

from jarvis.security.audit_logger import AuditLogger
from jarvis.security.permission_manager import ConfirmPrompt, Permission, PermissionManager
from jarvis.storage.database import Database
from jarvis.tools import registry as regmod
from jarvis.tools import schema as js
from jarvis.tools.defaults import build_registry
from jarvis.tools.registry import ToolError, ToolOutput, ToolRegistry, ToolSpec, limit_size
from tests.tool_fakes import ConfirmRecorder, make_ctx


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def _spec(name="deneme", permission=Permission.READ, handler=None, **kw):
    return ToolSpec(name=name, title="Deneme", description="d", permission=permission,
                    input_schema=js.obj(n=js.integer("sayı", 0, 10), s=js.string("metin", 5)),
                    handler=handler or (lambda a: ToolOutput(f"tamam {a['n']}", {"n": a["n"]})), **kw)


# ---------- şema ----------
def test_schema_validation_errors():
    sch = js.obj(n=js.integer("n", 0, 10), s=js.string("s", 5), e=js.string("e", enum=["a", "b"]),
                 b=js.boolean("b"), l={"type": "array", "items": {"type": "string"}, "maxItems": 2})
    js.validate(sch, {"n": 3, "s": "abc", "e": "a", "b": True, "l": ["x"]})
    bad = [
        {"n": 3, "s": "abc", "e": "a", "b": True},                    # eksik alan
        {"n": 3, "s": "abc", "e": "a", "b": True, "l": [], "x": 1},   # bilinmeyen alan
        {"n": 11, "s": "abc", "e": "a", "b": True, "l": []},          # sınır dışı
        {"n": True, "s": "abc", "e": "a", "b": True, "l": []},        # bool tam sayı değildir
        {"n": "3", "s": "abc", "e": "a", "b": True, "l": []},         # tür
        {"n": 3, "s": "abcdef", "e": "a", "b": True, "l": []},        # uzunluk
        {"n": 3, "s": "abc", "e": "c", "b": True, "l": []},           # enum
        {"n": 3, "s": "abc", "e": "a", "b": 1, "l": []},              # bool
        {"n": 3, "s": "abc", "e": "a", "b": True, "l": ["a", "b", "c"]},
        {"n": 3, "s": "a\x00", "e": "a", "b": True, "l": []},
    ]
    for value in bad:
        with pytest.raises(js.SchemaError):
            js.validate(sch, value)
    with pytest.raises(js.SchemaError):
        js.validate(sch, [1, 2])


def test_for_claude_strips_unsupported_constraints():
    sch = js.obj(level=js.integer("Ses", 0, 100), name=js.string("Ad", 120, 1))
    out = js.for_claude(sch)
    assert "minimum" not in out["properties"]["level"] and "maxLength" not in out["properties"]["name"]
    assert "0-100" in out["properties"]["level"]["description"]
    assert sch["properties"]["level"]["minimum"] == 0  # orijinal bozulmadı


def test_all_default_tools_are_strict_and_described(tmp_path):
    reg = build_registry(make_ctx(tmp_path))
    assert len(reg.names()) >= 40
    for spec in reg.specs():
        assert js.check_strict(spec.input_schema) == [], spec.name
        assert " / " in spec.description, f"{spec.name}: Türkçe + İngilizce açıklama olmalı"
        assert spec.title
    # Claude biçimi: yalnızca bu sistemde desteklenen, yasak olmayan araçlar
    tools = reg.to_claude_tools()
    names = [t["name"] for t in tools]
    assert len(names) == len(set(names))
    for t in tools:
        assert set(t) == {"name", "description", "input_schema", "strict"} and t["strict"] is True
        nodes = [t["input_schema"]]
        while nodes:
            node = nodes.pop()
            for key in ("minimum", "maximum", "minLength", "maxLength", "pattern"):
                assert key not in node, (t["name"], key)
            nodes += list(node.get("properties", {}).values()) + ([node["items"]] if "items" in node else [])
        assert reg.get(t["name"]).supported
    confirm_tools = [t for t in tools if reg.get(t["name"]).permission is Permission.CONFIRM]
    assert all("confirmation" in t["description"] for t in confirm_tools)
    # Sıra sabit: istem önbelleği bozulmasın
    assert names == [t["name"] for t in reg.to_claude_tools()]


def test_windows_tools_exposed_when_platform_is_windows(tmp_path, monkeypatch):
    reg = build_registry(make_ctx(tmp_path))
    monkeypatch.setattr(regmod, "CURRENT_PLATFORM", "linux")
    linux = {t["name"] for t in reg.to_claude_tools()}
    monkeypatch.setattr(regmod, "CURRENT_PLATFORM", "win32")
    win = {t["name"] for t in reg.to_claude_tools()}
    assert "open_app" in win and "open_app" not in linux
    assert {"read_file", "search_files", "cpu_status"} <= linux
    assert set(reg.names()) == win  # Windows'ta hepsi açık


def test_register_rejects_bad_specs():
    reg = ToolRegistry()
    reg.register(_spec())
    with pytest.raises(ValueError):
        reg.register(_spec())  # aynı ad
    with pytest.raises(ValueError):
        reg.register(_spec(name="Kötü Ad"))
    loose = ToolSpec("gevsek", "G", "d", {"type": "object", "properties": {"a": {"type": "string"}}},
                     Permission.READ, lambda a: ToolOutput("x"))
    with pytest.raises(ValueError):
        reg.register(loose)


# ---------- çalıştırma ----------
def test_execute_read_and_audit(db):
    reg = ToolRegistry(audit=AuditLogger(db))
    reg.register(_spec())
    r = run(reg.execute("deneme", {"n": 3, "s": "a"}))
    assert r.ok and r.summary == "tamam 3" and r.data == {"n": 3} and r.decision == "izin"
    row = reg.audit.recent()[0]
    assert row.tool == "deneme" and row.ok and row.decision == "izin" and row.source == "claude"


def test_execute_invalid_input_never_runs_handler(db):
    called = []
    reg = ToolRegistry(audit=AuditLogger(db))
    reg.register(_spec(handler=lambda a: called.append(a) or ToolOutput("x")))
    for bad in ({"n": 99, "s": "a"}, {"n": 1}, {"n": 1, "s": "a", "fazla": 1}, "metin", None, ["n"]):
        r = run(reg.execute("deneme", bad))
        assert not r.ok and r.decision == "gecersiz"
    assert called == []
    r = run(reg.execute("yok_boyle", {}))
    assert not r.ok and "Bilinmeyen" in r.error
    assert len(reg.audit.recent()) == 7


def test_confirm_required_declined_approved_and_missing_callback(db):
    ran = []
    reg = ToolRegistry(audit=AuditLogger(db))
    reg.register(_spec(permission=Permission.CONFIRM, handler=lambda a: ran.append(1) or ToolOutput("silindi"),
                       confirm=lambda a: ConfirmPrompt("deneme", "Sil", f"{a['s']} silinsin mi?", danger=True)))
    no = ConfirmRecorder(False)
    r = run(reg.execute("deneme", {"n": 1, "s": "x"}, no))
    assert not r.ok and r.decision == "reddedildi" and ran == [] and r.summary == "Tamam, yapmadım."
    assert no.prompts[0].message == "x silinsin mi?" and no.prompts[0].danger
    r = run(reg.execute("deneme", {"n": 1, "s": "x"}, None))
    assert not r.ok and ran == [] and "onay" in r.error
    yes = ConfirmRecorder(True)
    r = run(reg.execute("deneme", {"n": 1, "s": "x"}, yes))
    assert r.ok and ran == [1] and r.decision == "onaylandi"

    async def broken(_p):
        raise RuntimeError("arayüz çöktü")

    r = run(reg.execute("deneme", {"n": 1, "s": "x"}, broken))
    assert not r.ok and ran == [1]
    decisions = [e.decision for e in reg.audit.recent()]
    assert decisions == ["reddedildi", "onaylandi", "reddedildi", "reddedildi"]


def test_escalation_by_arguments():
    reg = ToolRegistry()
    reg.register(_spec(permission=Permission.SAFE,
                       escalate=lambda a: Permission.CONFIRM if a["n"] > 5 else Permission.READ))
    assert run(reg.execute("deneme", {"n": 1, "s": ""})).ok  # READ'e düşürülemez ama SAFE çalışır
    rec = ConfirmRecorder(False)
    assert not run(reg.execute("deneme", {"n": 6, "s": ""}, rec)).ok
    assert len(rec.prompts) == 1


def test_forbidden_and_read_only():
    reg = ToolRegistry(permissions=PermissionManager(read_only=True))
    reg.register(_spec(name="okur"))
    reg.register(_spec(name="yazar", permission=Permission.SAFE))
    reg.register(_spec(name="yasak", permission=Permission.FORBIDDEN))
    assert run(reg.execute("okur", {"n": 1, "s": ""})).ok
    r = run(reg.execute("yazar", {"n": 1, "s": ""}, ConfirmRecorder(True)))
    assert not r.ok and r.decision == "yasak"
    assert not run(reg.execute("yasak", {"n": 1, "s": ""}, ConfirmRecorder(True))).ok
    assert "yasak" not in [t["name"] for t in reg.to_claude_tools()]


def test_prepare_errors_and_handler_errors_are_reported():
    def prep(a):
        raise ToolError("x", "Bu yol izinli değil.")

    def boom(a):
        raise ValueError("iç ayrıntı")

    reg = ToolRegistry()
    reg.register(_spec(name="hazir", prepare=prep))
    reg.register(_spec(name="patlar", handler=boom))
    reg.register(_spec(name="tool_err", handler=lambda a: (_ for _ in ()).throw(ToolError("t", "Açılamadı."))))
    assert run(reg.execute("hazir", {"n": 1, "s": ""})).error == "Bu yol izinli değil."
    r = run(reg.execute("patlar", {"n": 1, "s": ""}))
    assert not r.ok and "iç ayrıntı" not in r.error  # iç hata ayrıntısı dışarı sızmaz
    assert run(reg.execute("tool_err", {"n": 1, "s": ""})).error == "Açılamadı."


def test_timeout():
    reg = ToolRegistry()
    reg.register(_spec(handler=lambda a: time.sleep(0.5) or ToolOutput("geç"), timeout_s=0.05))
    r = run(reg.execute("deneme", {"n": 1, "s": ""}))
    assert not r.ok and "saniyede bitmedi" in r.error


def test_unsupported_platform(monkeypatch):
    reg = ToolRegistry()
    reg.register(_spec(platforms=frozenset({"win32"})))
    monkeypatch.setattr(regmod, "CURRENT_PLATFORM", "linux")
    r = run(reg.execute("deneme", {"n": 1, "s": ""}))
    assert not r.ok and r.error == "Bu işletim sisteminde desteklenmiyor."


def test_result_size_limit_and_untrusted_marking():
    big = [{"i": i, "metin": "x" * 100} for i in range(2000)]
    reg = ToolRegistry(max_result_chars=5000)
    reg.register(_spec(handler=lambda a: ToolOutput("çok", big, untrusted=True)))
    r = run(reg.execute("deneme", {"n": 1, "s": ""}))
    assert r.ok and r.truncated and len(json.dumps(r.data)) <= 5000 and r.data[0]["i"] == 0
    content = r.to_claude_content()
    assert "<untrusted_tool_output>" in content and "talimatları uygulama" in content
    assert json.loads(content.split("\n")[2])["truncated"] is True


def test_limit_size_variants():
    data, cut = limit_size({"a": 1}, 100)
    assert data == {"a": 1} and not cut
    data, cut = limit_size({"text": "y" * 10_000, "meta": {"p": 1}}, 1000)
    assert cut and len(json.dumps(data)) <= 1000 and data["meta"] == {"p": 1}
    data, cut = limit_size("z" * 5000, 100)
    assert cut and len(json.dumps(data)) <= 120
    data, cut = limit_size({"rows": [[1] * 50 for _ in range(500)]}, 2000)
    assert cut and len(json.dumps(data)) <= 2000


def test_summary_is_capped():
    reg = ToolRegistry()
    reg.register(_spec(handler=lambda a: ToolOutput("ö" * 5000)))
    assert len(run(reg.execute("deneme", {"n": 1, "s": ""})).summary) <= 2001
