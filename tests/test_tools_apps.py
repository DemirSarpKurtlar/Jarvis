"""Uygulama araçları: bulanık eşleştirme, Steam VDF ayrıştırma, açma/kapama/geçiş, başlangıç uygulamaları."""

import asyncio
import struct
import subprocess
import sys
import time
from datetime import datetime, timezone

import psutil
import pytest

from jarvis.tools import applications as apps
from jarvis.tools import registry as regmod
from jarvis.tools.applications import AppEntry, AppIndex, StartupItem, parse_vdf, startup_enabled, startup_value
from jarvis.tools.defaults import build_registry
from jarvis.tools.registry import ToolRegistry
from jarvis.tools.winapi import WindowInfo
from tests.tool_fakes import FAKE_APPS, ConfirmRecorder, make_ctx


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def win(monkeypatch):
    """Windows'a özgü araçların mantığını sahte arka uçlarla sınamak için platformu Windows say."""
    monkeypatch.setattr(regmod, "CURRENT_PLATFORM", "win32")


# ---------- eşleştirme ----------
@pytest.mark.parametrize("query,expected", [
    ("notepad", "Not Defteri"), ("Not Defteri", "Not Defteri"), ("not defterini", "Not Defteri"),
    ("NOTEPAD", "Not Defteri"), ("hesap makinesi", "Hesap Makinesi"), ("calculator", "Hesap Makinesi"),
    ("tarayıcı", "Google Chrome"), ("chrome", "Google Chrome"), ("google chrome", "Google Chrome"),
    ("spotify", "Spotify"), ("spotfy", "Spotify"), ("discord", "Discord"), ("steam", "Steam"),
    ("vscode", "Visual Studio Code"), ("visual studio code", "Visual Studio Code"), ("firefox", "Mozilla Firefox"),
    ("görev yöneticisi", "Görev Yöneticisi"), ("task manager", "Görev Yöneticisi"), ("ayarlar", "Ayarlar"),
    ("excel", "Excel"), ("kayıt defteri", "Kayıt Defteri Düzenleyicisi"),
])
def test_fuzzy_match(query, expected):
    e = AppIndex(FAKE_APPS).best(query)
    assert e is not None and e.name == expected


@pytest.mark.parametrize("query", ["photoshop", "xyzabc", "oyun", "7-zip"])
def test_no_false_match(query):
    # 7-Zip yalnızca kaldırma kaydından biliniyor: başlatılamaz, "açılabilir" diye seçilmez
    assert AppIndex(FAKE_APPS).best(query) is None


def test_games_are_separate():
    idx = AppIndex(FAKE_APPS)
    assert idx.best("counter strike", kind="game").name == "Counter-Strike 2"
    assert idx.best("euro truck", kind="game").name == "Euro Truck Simulator 2"
    assert idx.best("steam", kind="game") is None


def test_index_caches_and_refreshes():
    calls = []

    def discover():
        calls.append(1)
        return [AppEntry("A", "a.exe", "app_paths")]

    idx = AppIndex(discover=discover, ttl_s=1000)
    idx.entries()
    idx.entries()
    assert len(calls) == 1
    idx.refresh()
    idx.entries()
    assert len(calls) == 2


def test_discover_off_windows_is_empty():
    if sys.platform != "win32":
        assert apps.discover_apps() == []


def test_start_menu_scan(tmp_path):
    d = tmp_path / "Programs"
    (d / "Discord Inc").mkdir(parents=True)
    (d / "Discord Inc" / "Discord.lnk").write_bytes(b"L\x00\x00\x00")
    (d / "Uninstall Discord.lnk").write_bytes(b"L")
    (d / "Hades.url").write_text("[InternetShortcut]\nURL=steam://rungameid/1145360\n", encoding="utf-8")
    (d / "Site.url").write_text("[InternetShortcut]\nURL=https://example.com\n", encoding="utf-8")
    found = {e.name: e for e in apps.scan_start_menu([d])}
    assert set(found) == {"Discord", "Hades"}
    assert found["Hades"].kind == "game" and found["Hades"].target == "steam://rungameid/1145360"


# ---------- Steam ----------
LIBRARY_VDF = r'''
"libraryfolders"
{
	"0"
	{
		"path"		"%s"
		"label"		""
		"apps"
		{
			"730"		"35000000000"
		}
	}
	"1"
	{
		"path"		"%s"
	}
}
'''

MANIFEST = '''"AppState"
{
	"appid"		"%s"
	"universe"		"1"
	// yorum satırı
	"name"		"%s"
	"StateFlags"		"4"
	"installdir"		"%s"
	"UserConfig" { "language" "turkish" }
}
'''


def _steam_fixture(tmp_path):
    root, lib2 = tmp_path / "Steam", tmp_path / "SteamLibrary"
    for p in (root, lib2):
        (p / "steamapps").mkdir(parents=True)
    esc = lambda p: str(p).replace("\\", "\\\\")  # noqa: E731
    (root / "steamapps" / "libraryfolders.vdf").write_text(LIBRARY_VDF % (esc(root), esc(lib2)), encoding="utf-8")
    (root / "steamapps" / "appmanifest_730.acf").write_text(MANIFEST % ("730", "Counter-Strike 2", "cs2"), encoding="utf-8")
    (root / "steamapps" / "appmanifest_228980.acf").write_text(
        MANIFEST % ("228980", "Steamworks Common Redistributables", "Steamworks Shared"), encoding="utf-8")
    (lib2 / "steamapps" / "appmanifest_1145360.acf").write_text(MANIFEST % ("1145360", "Hades", "Hades"), encoding="utf-8")
    (lib2 / "steamapps" / "appmanifest_1628350.acf").write_text(
        MANIFEST % ("1628350", "Steam Linux Runtime 3.0 (sniper)", "SteamLinuxRuntime_sniper"), encoding="utf-8")
    (lib2 / "steamapps" / "appmanifest_999.acf").write_text("bozuk { dosya", encoding="utf-8")
    return root, lib2


def test_parse_vdf_basic():
    data = parse_vdf('"A" { "b" "1" "c" { "d" "x \\"tırnak\\"" } "bos" "" } // son')
    assert data == {"A": {"b": "1", "c": {"d": 'x "tırnak"'}, "bos": ""}}
    with pytest.raises(ValueError):
        parse_vdf('"a" }')


def test_steam_libraries_and_games(tmp_path):
    root, lib2 = _steam_fixture(tmp_path)
    libs = apps.steam_libraries(root)
    assert libs == [root, lib2]
    games = apps.steam_games(root)
    assert [(g.appid, g.name) for g in games] == [("730", "Counter-Strike 2"), ("1145360", "Hades")]
    entries = apps.game_entries(root)
    assert {e.target for e in entries} == {"steam://rungameid/730", "steam://rungameid/1145360"}
    assert all(e.kind == "game" for e in entries)


def test_steam_old_library_format(tmp_path):
    root, lib2 = tmp_path / "Steam", tmp_path / "Lib"
    (root / "steamapps").mkdir(parents=True)
    (lib2 / "steamapps").mkdir(parents=True)
    (root / "steamapps" / "libraryfolders.vdf").write_text(
        '"LibraryFolders"\n{\n "TimeNextStatsReport" "1"\n "1" "%s"\n}' % str(lib2).replace("\\", "\\\\"), encoding="utf-8")
    assert apps.steam_libraries(root) == [root, lib2]
    assert apps.steam_libraries(tmp_path / "yok") == [tmp_path / "yok"]


# ---------- başlangıç ----------
def test_startup_approved_encoding():
    assert startup_enabled(None) and startup_enabled(b"") and startup_enabled(bytes([2]) + bytes(11))
    assert startup_enabled(bytes([6]) + bytes(11))
    assert not startup_enabled(bytes([3]) + bytes(11)) and not startup_enabled(bytes([7]) + bytes(11))
    assert startup_value(True) == bytes([2]) + bytes(11)
    when = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
    off = startup_value(False, when)
    assert len(off) == 12 and off[0] == 3 and not startup_enabled(off)
    ft = struct.unpack("<Q", off[4:])[0]
    assert ft == int((when - datetime(1601, 1, 1, tzinfo=timezone.utc)).total_seconds() * 10_000_000)


def test_startup_tools(tmp_path, win):
    ctx = make_ctx(tmp_path)
    reg = ToolRegistry()
    t = apps.register(reg, ctx)
    written = []
    t.startup_reader = lambda: [StartupItem("Discord", '"C:\\Discord\\Update.exe" --processStart', "kullanici", True),
                                StartupItem("SecurityHealth", "C:\\Windows\\...", "sistem", True)]
    t.startup_writer = lambda name, enabled: written.append((name, enabled))
    r = run(reg.execute("list_startup_apps", {}))
    assert r.ok and "2 başlangıç" in r.summary and r.untrusted
    no = ConfirmRecorder(False)
    assert not run(reg.execute("set_startup_app", {"name": "discord", "enabled": False}, no)).ok
    assert written == [] and "Discord" in no.prompts[0].message
    r = run(reg.execute("set_startup_app", {"name": "discord", "enabled": False}, ConfirmRecorder(True)))
    assert r.ok and written == [("Discord", False)]
    r = run(reg.execute("set_startup_app", {"name": "SecurityHealth", "enabled": False}, ConfirmRecorder(True)))
    assert not r.ok and "yönetici" in r.error and len(written) == 1


# ---------- açma / kapatma / geçiş ----------
def test_open_app_uses_opener(tmp_path, win):
    ctx = make_ctx(tmp_path)
    reg = build_registry(ctx)
    r = run(reg.execute("open_app", {"name": "not defterini"}))
    assert r.ok and r.summary == "Not Defteri açılıyor." and ctx.opener.opened == ["notepad.exe"]
    r = run(reg.execute("open_app", {"name": "photoshop"}))
    assert not r.ok and "bulamadım" in r.error and len(ctx.opener.opened) == 1


def test_sensitive_app_needs_confirmation(tmp_path, win):
    ctx = make_ctx(tmp_path)
    reg = build_registry(ctx)
    no = ConfirmRecorder(False)
    assert not run(reg.execute("open_app", {"name": "powershell"}, no)).ok
    assert ctx.opener.opened == [] and len(no.prompts) == 1
    assert run(reg.execute("open_app", {"name": "regedit"}, ConfirmRecorder(True))).ok
    assert ctx.opener.opened == ["regedit.exe"]


def test_open_app_not_supported_off_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(regmod, "CURRENT_PLATFORM", "linux")
    ctx = make_ctx(tmp_path)
    r = run(build_registry(ctx).execute("open_app", {"name": "notepad"}))
    assert not r.ok and "desteklenmiyor" in r.error and ctx.opener.opened == []


def test_launch_and_list_games(tmp_path, win):
    ctx = make_ctx(tmp_path)
    reg = build_registry(ctx)
    r = run(reg.execute("launch_game", {"name": "counter strike"}))
    assert r.ok and ctx.opener.opened == ["steam://rungameid/730"]
    r = run(reg.execute("list_games", {"limit": 10}))
    assert r.ok and r.data == ["Counter-Strike 2", "Euro Truck Simulator 2", "Hades"]
    assert not run(reg.execute("launch_game", {"name": "notepad"})).ok


def test_find_apps(tmp_path, win):
    reg = build_registry(make_ctx(tmp_path))
    r = run(reg.execute("find_apps", {"query": "chrome", "limit": 5}))
    assert r.ok and r.data[0]["name"] == "Google Chrome"
    r = run(reg.execute("find_apps", {"query": "", "limit": 200}))
    assert "Not Defteri" in r.data and "7-Zip" not in r.data


@pytest.fixture
def child():
    """Kapatma testleri için gerçek bir alt işlem."""
    # Ad pytest'in kendisinden farklı olmalı (aynı adlı JARVIS/pytest işlemi korumalıdır)
    cmd = ["ping", "-n", "60", "127.0.0.1"] if sys.platform == "win32" else ["sleep", "60"]
    p = subprocess.Popen(cmd)
    time.sleep(0.3)
    yield p
    if p.poll() is None:
        p.kill()
        p.wait()


def _child_entry(proc):
    name = psutil.Process(proc.pid).name().lower()
    return AppEntry("Deneme Uygulaması", "deneme.exe", "builtin", ("deneme",), (name,))


@pytest.mark.skipif(sys.platform == "win32", reason="Linux'ta sahte pencereyle; Windows'ta gerçek test ayrı")
def test_close_app_gracefully_via_window(tmp_path, win, child):
    ctx = make_ctx(tmp_path, apps=[_child_entry(child)])
    ctx.windows.windows = [WindowInfo(42, "Deneme - pencere", child.pid)]
    ctx.windows.on_close = lambda hwnd: child.terminate()  # pencere WM_CLOSE alınca kapanır
    reg = build_registry(ctx)
    r = run(reg.execute("close_app", {"name": "deneme", "force": False}))
    assert r.ok and r.data["closed"] and ctx.windows.closed == [42]
    child.wait(timeout=5)
    r = run(reg.execute("close_app", {"name": "deneme", "force": False}))
    assert not r.ok and "bulamadım" in r.error


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX işlem davranışı")
def test_close_app_force_requires_confirmation(tmp_path, win, child):
    ctx = make_ctx(tmp_path, apps=[_child_entry(child)])
    reg = build_registry(ctx)
    no = ConfirmRecorder(False)
    assert not run(reg.execute("close_app", {"name": "deneme", "force": True}, no)).ok
    assert child.poll() is None and no.prompts[0].danger
    r = run(reg.execute("close_app", {"name": "deneme", "force": True}, ConfirmRecorder(True)))
    assert r.ok and "zorla" in r.summary
    assert child.wait(timeout=5) is not None


def test_close_app_refuses_protected(tmp_path, win, monkeypatch):
    me = psutil.Process()
    entry = AppEntry("Ben", "x.exe", "builtin", (), (me.name().lower(),))
    monkeypatch.setattr(apps, "running_matches", lambda e, q: [me])
    reg = build_registry(make_ctx(tmp_path, apps=[entry]))
    r = run(reg.execute("close_app", {"name": "ben", "force": True}, ConfirmRecorder(True)))
    assert not r.ok and "JARVIS" in r.error


def test_switch_to_app(tmp_path, win, child):
    ctx = make_ctx(tmp_path, apps=[_child_entry(child)])
    ctx.windows.windows = [WindowInfo(7, "Rapor.docx - Word", 999999), WindowInfo(8, "Deneme", child.pid)]
    reg = build_registry(ctx)
    r = run(reg.execute("switch_to_app", {"name": "deneme"}))
    assert r.ok and ctx.windows.focused == [8]
    r = run(reg.execute("switch_to_app", {"name": "rapor.docx"}))  # başlığa göre
    assert r.ok and ctx.windows.focused[-1] == 7
    ctx.windows.focus_ok = False
    r = run(reg.execute("switch_to_app", {"name": "deneme"}))
    assert not r.ok and "öne getiremedim" in r.error
    assert not run(reg.execute("switch_to_app", {"name": "olmayan uygulama"})).ok


@pytest.mark.parametrize("entry,sensitive", [
    (AppEntry("Windows PowerShell", r"C:\ProgramData\...\Windows PowerShell.lnk", "start_menu"), True),
    (AppEntry("Command Prompt", r"C:\...\Command Prompt.lnk", "start_menu"), True),
    (AppEntry("Komut İstemi", "shell:AppsFolder\\{1AC14E77}\\cmd.exe", "apps_folder"), True),
    (AppEntry("Kayıt Defteri Düzenleyicisi", "regedit.exe", "app_paths"), True),
    (AppEntry("Not Defteri", "notepad.exe", "builtin"), False),
    (AppEntry("Spotify", "shell:AppsFolder\\SpotifyAB!Spotify", "apps_folder"), False),
])
def test_sensitive_detection(entry, sensitive):
    assert apps.is_sensitive(entry) is sensitive
