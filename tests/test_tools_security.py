"""Aşama 5 güvenlik katmanı: yol korumalı alanı, URL/ana makine doğrulama, izinler, denetim kaydı."""

import json
import os
import sys

import pytest

from jarvis.security.action_validator import PathSandbox, ValidationError, validate_host, validate_url
from jarvis.security.audit_logger import AuditLogger, sanitize
from jarvis.security.permission_manager import Permission, PermissionManager
from jarvis.storage.database import Database


@pytest.fixture
def roots(tmp_path):
    desk, dl, outside = tmp_path / "Desktop", tmp_path / "Downloads", tmp_path / "disarisi"
    for d in (desk, dl, outside):
        d.mkdir()
    (outside / "gizli.txt").write_text("gizli", encoding="utf-8")
    return {"desktop": desk, "downloads": dl, "outside": outside}


@pytest.fixture
def sandbox(roots):
    # Windows'ta tmp_path AppData altındadır: test kökleri için açıkça izin verilir
    return PathSandbox({"desktop": roots["desktop"], "downloads": roots["downloads"]}, allow_appdata_roots=True)


def test_resolve_inside_roots(sandbox, roots):
    (roots["desktop"] / "a.txt").write_text("x", encoding="utf-8")
    assert sandbox.resolve(str(roots["desktop"] / "a.txt"), must_exist=True) == (roots["desktop"] / "a.txt").resolve()
    # Türkçe ve İngilizce kök adlarıyla göreli yol
    assert sandbox.resolve("Masaüstü/a.txt") == (roots["desktop"] / "a.txt").resolve()
    assert sandbox.resolve("masaustu\\a.txt") == (roots["desktop"] / "a.txt").resolve()
    assert sandbox.resolve("Downloads/yeni.txt") == (roots["downloads"] / "yeni.txt").resolve()
    assert sandbox.resolve("İndirilenler") == roots["downloads"].resolve()


@pytest.mark.parametrize("bad", [
    "../disarisi/gizli.txt", "Masaüstü/../disarisi/gizli.txt", "Masaüstü/..\\..\\etc",
    "\\\\sunucu\\paylasim\\dosya.txt", "//sunucu/paylasim", "\\\\?\\C:\\Windows", "\\\\.\\PhysicalDrive0",
    "Masaüstü/dosya.txt:gizli", "", "   ", "Masaüstü/a\x00b", "rastgele/yol.txt",
])
def test_rejects_escapes(sandbox, bad):
    with pytest.raises(ValidationError):
        sandbox.resolve(bad)


def test_rejects_outside_absolute(sandbox, roots):
    with pytest.raises(ValidationError) as e:
        sandbox.resolve(str(roots["outside"] / "gizli.txt"))
    assert "erişim" in e.value.user_message


def test_rejects_system_dirs(sandbox):
    target = r"C:\Windows\System32\drivers\etc\hosts" if sys.platform == "win32" else "/etc/passwd"
    with pytest.raises(ValidationError):
        sandbox.resolve(target)


@pytest.mark.skipif(sys.platform == "win32", reason="sembolik bağ oluşturmak Windows'ta yetki ister")
def test_symlink_escape_rejected(sandbox, roots):
    link = roots["desktop"] / "kisayol"
    os.symlink(roots["outside"], link)
    with pytest.raises(ValidationError):
        sandbox.resolve(str(link / "gizli.txt"))
    with pytest.raises(ValidationError):
        sandbox.resolve("Masaüstü/kisayol/gizli.txt")
    # Kök içini gösteren bağ sorun değil
    inner = roots["desktop"] / "ic"
    inner.mkdir()
    os.symlink(inner, roots["desktop"] / "ic_bag")
    assert sandbox.resolve("Masaüstü/ic_bag") == inner.resolve()


@pytest.mark.parametrize("name", ["CON", "nul.txt", "COM1", "lpt9.log"])
def test_reserved_names(sandbox, name):
    with pytest.raises(ValidationError):
        sandbox.resolve(f"Masaüstü/{name}")


def test_invalid_names_for_write(sandbox):
    for name in ("kotu?.txt", "a|b", "sondanokta.", "ara bosluk /x", "y<z"):
        with pytest.raises(ValidationError):
            sandbox.resolve(f"Masaüstü/{name}", for_write=True)


def test_must_exist(sandbox):
    with pytest.raises(ValidationError):
        sandbox.resolve("Masaüstü/yok.txt", must_exist=True)


def test_root_validation(tmp_path):
    with pytest.raises(ValidationError):
        PathSandbox({"x": tmp_path / "yok"})
    sb = PathSandbox({}, extra_roots=[str(tmp_path / "yok")])  # geçersiz ek kök sessizce atlanır, loglanır
    assert sb.roots == []
    drive_root = "C:\\" if sys.platform == "win32" else "/"
    with pytest.raises(ValidationError):
        PathSandbox({"x": drive_root})


def test_extra_root_and_describe(tmp_path, roots):
    extra = tmp_path / "Projeler"
    extra.mkdir()
    sb = PathSandbox({"desktop": roots["desktop"]}, extra_roots=[str(extra)], allow_appdata_roots=True)
    assert [r["ad"] for r in sb.describe()] == ["Masaüstü", "Projeler"]
    assert sb.resolve(str(extra / "x.txt")) == (extra / "x.txt").resolve()


def test_appdata_closed_inside_wider_root(tmp_path, monkeypatch):
    home = tmp_path / "kullanici"
    appdata = home / ".config" if sys.platform != "win32" else home / "AppData"
    appdata.mkdir(parents=True)
    import jarvis.security.action_validator as av

    monkeypatch.setattr(av, "appdata_dirs", lambda: [appdata])
    sb = PathSandbox({"home": home})
    assert sb.resolve(str(home / "belge.txt"))
    with pytest.raises(ValidationError):
        sb.resolve(str(appdata / "anahtarlar.json"))


@pytest.mark.parametrize("url", ["https://www.youtube.com", "http://example.com/a?b=1", "https://192.168.1.1:8080/"])
def test_valid_urls(url):
    assert validate_url(url) == url


@pytest.mark.parametrize("url", ["file:///C:/Windows/win.ini", "javascript:alert(1)", "ms-settings:", "ftp://x.com",
                                 "https://user:pass@site.com", "https://", "https://exa mple.com", "x" * 3000])
def test_invalid_urls(url):
    with pytest.raises(ValidationError):
        validate_url(url)


@pytest.mark.parametrize("host,ok", [("google.com", True), ("8.8.8.8", True), ("::1", True), ("a-b.example", True),
                                     ("google.com; rm -rf /", False), ("-bad.com", False), ("a" * 70 + ".com", False),
                                     ("x && calc", False), ("$(whoami)", False)])
def test_host_validation(host, ok):
    if ok:
        assert validate_host(host) == host
    else:
        with pytest.raises(ValidationError):
            validate_host(host)


def test_permission_manager_levels():
    pm = PermissionManager()
    assert pm.decide("t", Permission.READ).allowed
    assert pm.decide("t", Permission.SAFE).allowed
    d = pm.decide("t", Permission.SAFE, Permission.CONFIRM)
    assert not d.allowed and d.needs_confirmation and d.level is Permission.CONFIRM
    # Girdi seviyeyi asla düşüremez
    assert pm.decide("t", Permission.CONFIRM, Permission.READ).needs_confirmation
    f = pm.decide("t", Permission.FORBIDDEN)
    assert not f.allowed and not f.needs_confirmation
    ro = PermissionManager(read_only=True)
    assert ro.decide("t", Permission.READ).allowed
    assert not ro.decide("t", Permission.SAFE).allowed
    assert "Salt okunur" in ro.decide("t", Permission.CONFIRM).reason
    off = PermissionManager(disabled_tools={"t"})
    assert not off.decide("t", Permission.READ).allowed


def test_sanitize_never_keeps_secrets():
    data = {"password": "hunter2", "api_key": "abc", "nested": {"token": "xyz", "ok": "deger"},
            "text": "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUV", "content": "uzun " * 100, "list": ["Bearer abcdefghijkl"]}
    out = sanitize(data)
    blob = json.dumps(out)
    for secret in ("hunter2", "abc\"", "xyz", "ABCDEFGHIJKLMNOP", "abcdefghijkl"):
        assert secret not in blob
    assert out["nested"]["ok"] == "deger"
    assert out["content"].startswith("<") and "karakter" in out["content"]


def test_audit_rows(tmp_path):
    db = Database(tmp_path / "a.db")
    audit = AuditLogger(db)
    rid = audit.record(source="yerel", tool="open_app", permission="güvenli", decision=AuditLogger.ALLOWED, ok=True,
                       args={"name": "notepad", "secret": "s3cr3t"}, summary="Not Defteri açılıyor.", duration_ms=12)
    assert rid > 0
    audit.record(source="claude", tool="delete_path", permission="onaylı", decision=AuditLogger.DECLINED, ok=False,
                 args={"path": "x"}, error="Kullanıcı onaylamadı")
    rows = audit.recent()
    assert [r.tool for r in rows] == ["delete_path", "open_app"]
    assert rows[1].ok and rows[1].summary == "Not Defteri açılıyor." and rows[1].duration_ms == 12
    assert "s3cr3t" not in rows[1].args and "***" in rows[1].args
    assert rows[0].decision == "reddedildi" and not rows[0].ok
    # Geçiş tablosu idempotent: tekrar açılınca veri durur
    db.close()
    assert len(AuditLogger(Database(tmp_path / "a.db")).recent()) == 2
