"""Gerçek Windows testleri (GitHub Actions windows-latest ya da kullanıcının bilgisayarı).

Diğer işletim sistemlerinde otomatik atlanır (bkz. conftest.py, `windows` işareti).
Ekranı kilitleme, kapatma gibi oturumu bozan işlemler burada da çalıştırılmaz.
"""

import asyncio
import sys
import time

import psutil
import pytest

pytestmark = pytest.mark.windows

if sys.platform == "win32":  # pragma: no cover - yalnızca Windows
    from jarvis.security.action_validator import PathSandbox, default_roots
    from jarvis.tools import applications as apps
    from jarvis.tools import hardware, system_info
    from jarvis.tools.defaults import build_registry, default_context
    from jarvis.tools.registry import ToolError
    from jarvis.tools.windows_actions import APPROVED_COMMANDS, run_approved
    from jarvis.tools.winapi import CoreAudio, Win32Windows


def run(coro):
    return asyncio.run(coro)


def _notepad_pids() -> set[int]:
    return {p.pid for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() == "notepad.exe"}


def test_known_folders_exist():
    roots = default_roots()
    assert "documents" in roots or "desktop" in roots


def test_app_discovery_finds_notepad():
    idx = apps.AppIndex()
    entries = idx.entries()
    assert len(entries) > len(apps.BUILTIN_APPS)  # Başlat menüsü / AppsFolder / kayıt defteri bir şey buldu
    e = idx.best("notepad")
    assert e is not None and "not" in e.name.lower()
    assert idx.best("not defteri") is not None
    assert idx.best("hesap makinesi") is not None


def test_launch_and_close_notepad(tmp_path):
    ctx = default_context(PathSandbox({"x": tmp_path}, allow_appdata_roots=True))
    reg = build_registry(ctx)
    before = _notepad_pids()
    r = run(reg.execute("open_app", {"name": "notepad"}))
    assert r.ok, r.error
    deadline = time.time() + 20
    new: set[int] = set()
    while time.time() < deadline:
        new = _notepad_pids() - before
        wins = [w for w in Win32Windows().list_windows() if w.pid in new]
        if new and wins:
            break
        time.sleep(0.3)
    assert new, "Not Defteri işlemi başlamadı"
    try:
        r = run(reg.execute("close_app", {"name": "notepad", "force": False}))
        assert r.ok, r.error
        deadline = time.time() + 10
        while time.time() < deadline and (_notepad_pids() & new):
            time.sleep(0.2)
        assert not (_notepad_pids() & new), "Not Defteri nazikçe kapanmadı"
    finally:
        for pid in _notepad_pids() & new:
            psutil.Process(pid).kill()


def test_volume_roundtrip():
    audio = CoreAudio()
    try:
        original = audio.get_volume()
        muted = audio.is_muted()
    except ToolError as e:
        pytest.skip(f"Ses aygıtı yok (CI makinelerinde olağan): {e.user_message}")
    try:
        target = 37 if original != 37 else 42
        audio.set_volume(target)
        assert abs(audio.get_volume() - target) <= 1
        audio.set_mute(not muted)
        assert audio.is_muted() == (not muted)
    finally:
        audio.set_mute(muted)
        audio.set_volume(original)


def test_keyring_windows_credential_manager():
    import keyring.backends.Windows

    kr = keyring.backends.Windows.WinVaultKeyring()
    service, user = "JARVIS-test", "otomatik-test"
    kr.set_password(service, user, "deger-123")
    try:
        assert kr.get_password(service, user) == "deger-123"
    finally:
        kr.delete_password(service, user)
    assert kr.get_password(service, user) is None


def test_processes_and_system_info():
    from jarvis.tools.processes import list_processes

    rows = list_processes("memory", 10, sample_s=0.2)
    assert rows and any(r["name"].lower() == "python.exe" for r in list_processes("cpu", 0, 0.1))
    assert system_info.os_name().startswith("Windows")
    assert hardware.cpu_name() and hardware.cpu_name() != "bilinmiyor"
    assert system_info.disks()
    temp, why = hardware.cpu_temperature()
    assert (temp is None and why) or (5 <= temp <= 115)


def test_system_event_log_readable():
    events = system_info.recent_system_events(168, 5)
    assert isinstance(events, list)
    for e in events:
        assert e["source"] and e["time"]


def test_startup_list_and_steam():
    items = apps.list_startup()
    assert isinstance(items, list)
    for i in items:
        assert i.scope in ("kullanici", "sistem")
    root = apps.steam_root()
    if root is not None:
        assert isinstance(apps.steam_games(root), list)


def test_approved_command_ipconfig():
    code, out = run_approved(APPROVED_COMMANDS["ip_yapilandirmasi"], "")
    assert code == 0 and ("IP" in out or "Windows" in out)


def test_window_list():
    wins = Win32Windows().list_windows()
    assert isinstance(wins, list)
