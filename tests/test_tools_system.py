"""İşlem, donanım, sistem bilgisi, Windows eylemleri ve zamanlayıcı araçları."""

import asyncio
import os
import subprocess
import sys
import time

import psutil
import pytest

from jarvis.system.monitor import GpuInfo, SystemSnapshot
from jarvis.tools import hardware, processes
from jarvis.tools import registry as regmod
from jarvis.tools.defaults import build_registry
from jarvis.tools.scheduler import TaskScheduler
from jarvis.tools.windows_actions import APPROVED_COMMANDS, SETTINGS_PAGES
from tests.tool_fakes import ConfirmRecorder, make_ctx


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def win(monkeypatch):
    monkeypatch.setattr(regmod, "CURRENT_PLATFORM", "win32")


def snapshot(gpu=True):
    g = GpuInfo("NVIDIA GeForce RTX 4070", 61, 37, 3072, 12288, None) if gpu else None
    return SystemSnapshot(time.time(), 12.5, 7.5, 31.9, 23.5, "C:\\", 300.0, 1000.0, 70.0, 8e6, 1e6, 3600 * 5, g,
                          None if gpu else "NVIDIA ekran kartı bulunamadı")


# ---------- işlemler ----------
def test_list_processes_real():
    rows = processes.list_processes("memory", 5, sample_s=0.05)
    assert 0 < len(rows) <= 5
    assert rows == sorted(rows, key=lambda r: (r["memory_mb"], r["memory_mb"]), reverse=True)
    assert {"pid", "name", "cpu_percent", "memory_mb", "user"} <= set(rows[0])
    me = processes.process_details(os.getpid())
    assert me["pid"] == os.getpid() and me["protected_reason"]


def test_process_details_redacts_command_line(monkeypatch):
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "--api-key=sk-ant-api03-GIZLIANAHTAR12345"])
    try:
        time.sleep(0.2)
        info = processes.process_details(p.pid)
        assert "GIZLIANAHTAR" not in (info["command_line"] or "")
    finally:
        p.kill()
        p.wait()


def test_kill_process_gated_and_protected(tmp_path):
    reg = build_registry(make_ctx(tmp_path))
    if sys.platform == "win32":
        p = subprocess.Popen(["ping", "-n", "60", "127.0.0.1"], stdout=subprocess.DEVNULL)
    else:
        p = subprocess.Popen(["sleep", "60"])
    try:
        time.sleep(0.2)
        name = psutil.Process(p.pid).name()
        args = {"pid": p.pid, "name": name, "force": False}
        no = ConfirmRecorder(False)
        assert not run(reg.execute("kill_process", args, no)).ok and p.poll() is None
        assert name in no.prompts[0].message
        assert not run(reg.execute("kill_process", {**args, "name": "baska.exe"}, ConfirmRecorder(True))).ok
        assert p.poll() is None  # ad uyuşmuyor: PID yeniden kullanılmış olabilir
        r = run(reg.execute("kill_process", args, ConfirmRecorder(True)))
        assert r.ok and r.data["closed"]
        p.wait(timeout=5)
    finally:
        if p.poll() is None:
            p.kill()
    me = psutil.Process()
    r = run(reg.execute("kill_process", {"pid": me.pid, "name": me.name(), "force": True}, ConfirmRecorder(True)))
    assert not r.ok and "JARVIS" in r.error
    r = run(reg.execute("kill_process", {"pid": 4, "name": "System", "force": True}, ConfirmRecorder(True)))
    assert not r.ok
    r = run(reg.execute("kill_process", {"pid": 2**31 - 2, "name": "yok", "force": True}, ConfirmRecorder(True)))
    assert not r.ok and "bulunamadı" in r.error


def test_protection_rules():
    class P:
        def __init__(self, pid, name, user):
            self.pid, self._n, self._u = pid, name, user

        def name(self):
            return self._n

        def username(self):
            if self._u is None:
                raise psutil.AccessDenied(self.pid)
            return self._u

    me = processes._current_user()
    assert processes.protection_reason(P(1, "init", me))
    assert "kritik" in processes.protection_reason(P(900, "lsass.exe", me))
    assert "kritik" in processes.protection_reason(P(900, "MsMpEng.exe", me))
    assert "yönetici" in processes.protection_reason(P(900, "app.exe", "NT AUTHORITY\\SYSTEM"))
    assert "yönetici" in processes.protection_reason(P(900, "app.exe", None))
    assert processes.protection_reason(P(900, "notepad.exe", f"MAKINE\\{me}")) is None


# ---------- donanım ----------
def test_gpu_status_from_snapshot(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.snapshot = snapshot
    reg = build_registry(ctx)
    r = run(reg.execute("gpu_status", {}))
    assert r.ok and "61 °C" in r.summary and "%37" in r.summary and "fan hızı bu kartta okunamıyor" in r.summary
    assert r.data["fan_pct"] is None and r.data["available"]
    ctx.snapshot = lambda: snapshot(gpu=False)
    r = run(reg.execute("gpu_status", {}))
    assert r.ok and "bulunamadı" in r.summary and r.data["available"] is False
    ctx.snapshot = lambda: None
    assert "Henüz" in run(reg.execute("gpu_status", {})).summary


def test_cpu_and_memory_real(tmp_path):
    reg = build_registry(make_ctx(tmp_path))
    r = run(reg.execute("cpu_status", {}))
    assert r.ok and r.data["logical_cores"] >= 1 and len(r.data["per_core_percent"]) >= 1
    if r.data["temperature_c"] is None:
        assert "desteklenmiyor" in r.summary and r.data["temperature_unavailable"]
    else:
        assert 5 <= r.data["temperature_c"] <= 115 and r.data["temperature_source"]
    r = run(reg.execute("memory_status", {}))
    assert r.ok and r.data["total_gb"] > 0


def test_cpu_temperature_rejects_implausible(monkeypatch):
    if sys.platform == "win32":
        pytest.skip("Linux sensör yolu")

    class T:
        def __init__(self, label, cur):
            self.label, self.current = label, cur

    monkeypatch.setattr(psutil, "sensors_temperatures", lambda: {"coretemp": [T("Package id 0", 0.0)]}, raising=False)
    assert hardware.cpu_temperature()[0] is None
    monkeypatch.setattr(psutil, "sensors_temperatures",
                        lambda: {"acpitz": [T("", 27.8)], "coretemp": [T("Package id 0", 48.0), T("Core 0", 45.0)]},
                        raising=False)
    assert hardware.cpu_temperature() == (48.0, "coretemp sürücüsü")
    monkeypatch.setattr(psutil, "sensors_temperatures", lambda: {"acpitz": [T("", 27.8)]}, raising=False)
    assert hardware.cpu_temperature()[0] is None  # ACPI termal bölgesi işlemci sıcaklığı sayılmaz


# ---------- sistem bilgisi ----------
def test_system_info_tools_real(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.snapshot = snapshot
    reg = build_registry(ctx)
    r = run(reg.execute("system_overview", {}))
    assert r.ok and r.data["uptime_s"] > 0 and "açık" in r.summary
    r = run(reg.execute("disk_info", {}))
    assert r.ok and isinstance(r.data, list)
    r = run(reg.execute("network_info", {}))
    assert r.ok and "8.0" in r.summary  # anlık hız son ölçümden
    assert all("mac" not in str(i).lower() for i in r.data)
    r = run(reg.execute("battery_status", {}))
    assert r.ok
    r = run(reg.execute("performance_report", {}))
    assert r.ok and r.data["top_cpu"] and r.data["gpu"]["available"] and "warnings" in r.data


def test_system_events_not_supported_off_windows(tmp_path):
    if sys.platform == "win32":
        pytest.skip("Windows'ta gerçek test")
    r = run(build_registry(make_ctx(tmp_path)).execute("recent_system_events", {"hours": 24, "limit": 5}))
    assert not r.ok and "desteklenmiyor" in r.error


# ---------- Windows eylemleri (sahte arka uçlarla) ----------
def test_volume_tools(tmp_path, win):
    ctx = make_ctx(tmp_path)
    reg = build_registry(ctx)
    assert run(reg.execute("set_volume", {"level": 30})).summary == "Sesi %30 yaptım."
    assert ctx.audio.volume == 30
    assert not run(reg.execute("set_volume", {"level": 130})).ok and ctx.audio.volume == 30
    run(reg.execute("set_mute", {"muted": True}))
    assert ctx.audio.muted
    r = run(reg.execute("change_volume", {"delta": 15}))
    assert ctx.audio.volume == 45 and not ctx.audio.muted and "açtım" in r.summary
    run(reg.execute("change_volume", {"delta": -80}))
    assert ctx.audio.volume == 0
    r = run(reg.execute("get_volume", {}))
    assert r.data == {"volume": 0, "muted": False}


def test_volume_not_supported_off_windows(tmp_path):
    if sys.platform == "win32":
        pytest.skip()
    r = run(build_registry(make_ctx(tmp_path)).execute("set_volume", {"level": 10}))
    assert not r.ok and "desteklenmiyor" in r.error


def test_settings_pages_allowlist(tmp_path, win):
    ctx = make_ctx(tmp_path)
    reg = build_registry(ctx)
    r = run(reg.execute("open_settings", {"page": "bluetooth"}))
    assert r.ok and ctx.opener.opened == ["ms-settings:bluetooth"] and "senin basman" in r.summary
    assert not run(reg.execute("open_settings", {"page": "ms-settings:../../cmd"})).ok
    assert all(uri.startswith(("ms-settings:", "windowsdefender:")) for uri, _ in SETTINGS_PAGES.values())


def test_open_url_and_search(tmp_path, win):
    ctx = make_ctx(tmp_path)
    reg = build_registry(ctx)
    assert run(reg.execute("open_url", {"url": "https://www.youtube.com"})).ok
    for bad in ("file:///C:/Windows/system.ini", "javascript:alert(1)", "ms-settings:privacy"):
        assert not run(reg.execute("open_url", {"url": bad})).ok
    r = run(reg.execute("web_search_in_browser", {"query": "hava durumu & ç", "engine": "google"}))
    assert r.ok and ctx.opener.opened[-1] == "https://www.google.com/search?q=hava+durumu+%26+%C3%A7"
    assert len(ctx.opener.opened) == 2


def test_lock_and_power(tmp_path, win):
    calls = []
    backends = {"locker": lambda: calls.append("kilit"), "power_fn": lambda a, d: calls.append((a, d)),
                "cancel_fn": lambda: calls.append("iptal") or True}
    ctx = make_ctx(tmp_path)
    reg = build_registry(ctx, action_backends=backends)
    assert run(reg.execute("lock_screen", {})).ok and calls == ["kilit"]
    no = ConfirmRecorder(False)
    assert not run(reg.execute("power_action", {"action": "shutdown", "delay_seconds": 60}, no)).ok
    assert calls == ["kilit"] and no.prompts[0].danger
    assert run(reg.execute("power_action", {"action": "restart", "delay_seconds": 30}, ConfirmRecorder(True))).ok
    assert calls[-1] == ("restart", 30)
    assert not run(reg.execute("power_action", {"action": "shutdown", "delay_seconds": 0}, ConfirmRecorder(True))).ok
    assert run(reg.execute("cancel_power_action", {})).ok and calls[-1] == "iptal"


def test_approved_commands_only(tmp_path, win):
    seen = []

    def runner(cmd, target):
        seen.append((cmd.exe, cmd.args, target))
        return 0, "Yanıt: 127.0.0.1"

    reg = build_registry(make_ctx(tmp_path), action_backends={"runner": runner})
    r = run(reg.execute("run_approved_command", {"command": "ping", "target": "google.com"}))
    assert r.ok and r.untrusted and seen == [("ping.exe", ("-n", "4"), "google.com")]
    for bad in ("google.com && calc", "x; rm -rf /", "$(whoami)", "", "a b"):
        assert not run(reg.execute("run_approved_command", {"command": "ping", "target": bad})).ok
    assert not run(reg.execute("run_approved_command", {"command": "ip_yapilandirmasi", "target": "x"})).ok
    assert not run(reg.execute("run_approved_command", {"command": "powershell -c whoami", "target": ""})).ok
    assert not run(reg.execute("run_approved_command", {"command": "format", "target": ""})).ok
    assert len(seen) == 1
    assert run(reg.execute("run_approved_command", {"command": "dns_onbellegini_temizle", "target": ""})).ok
    # Her komut System32'deki sabit bir exe; kabuk yorumlayıcısı yok
    for c in APPROVED_COMMANDS.values():
        assert c.exe.endswith(".exe") and c.exe not in ("cmd.exe", "powershell.exe", "pwsh.exe")


def test_registry_never_offers_free_shell(tmp_path):
    reg = build_registry(make_ctx(tmp_path))
    for spec in reg.specs():
        props = spec.input_schema.get("properties", {})
        assert not {"command_line", "shell", "script", "cmd"} & set(props), spec.name
        if "command" in props:
            assert "enum" in props["command"]


# ---------- zamanlayıcı ----------
def test_scheduler_runs_only_safe_actions(tmp_path, win):
    async def main():
        ctx = make_ctx(tmp_path)
        reg = build_registry(ctx)
        notes = []
        sched = ctx.scheduler
        sched.notify = notes.append
        sched.bind(asyncio.get_running_loop())
        r = await reg.execute("schedule_task", {"action": "hatirlatma", "minutes": 5, "text": "Su iç", "value": -1})
        assert r.ok and "JARVIS kapanırsa" in r.summary
        lst = await reg.execute("list_scheduled_tasks", {})
        assert len(lst.data) == 1
        assert (await reg.execute("cancel_scheduled_task", {"task_id": r.data["id"]})).data["cancelled"]
        assert (await reg.execute("list_scheduled_tasks", {})).data == []
        # Kısa gecikmeyle gerçek çalıştırma
        sched.schedule("ses_ayarla", 1, "", 25, delay_s=0.05)
        sched.schedule("hatirlatma", 1, "Toplantı", -1, delay_s=0.05)
        sched.schedule("uygulama_ac", 1, "powershell", -1, delay_s=0.05)  # onay ister -> çalışmamalı
        await asyncio.sleep(0.4)
        assert ctx.audio.volume == 25
        assert "Hatırlatma: Toplantı" in notes
        assert ctx.opener.opened == []
        results = [res for _id, res in sched.results if res is not None]
        assert any(not res.ok and res.decision == "reddedildi" for res in results)
        bad = await reg.execute("schedule_task", {"action": "ses_ayarla", "minutes": 1, "text": "", "value": -1})
        assert not bad.ok
        sched.cancel_all()

    run(main())


def test_scheduler_requires_loop(tmp_path):
    reg = build_registry(make_ctx(tmp_path))
    r = run(reg.execute("schedule_task", {"action": "hatirlatma", "minutes": 1, "text": "x", "value": -1}))
    assert not r.ok and "hazır değil" in r.error
    assert isinstance(TaskScheduler(reg).list(), list)


def test_default_backends_are_honest_off_windows(tmp_path, win):
    if sys.platform == "win32":
        pytest.skip("Windows'ta gerçek arka uçlar var")
    from jarvis.security.action_validator import PathSandbox
    from jarvis.tools.defaults import default_context

    (tmp_path / "k").mkdir()
    ctx = default_context(PathSandbox({"k": tmp_path / "k"}))
    reg = build_registry(ctx)
    for name, args in (("get_volume", {}), ("set_volume", {"level": 5}), ("switch_to_app", {"name": "x"}),
                       ("open_settings", {"page": "bluetooth"})):
        r = run(reg.execute(name, args))
        assert not r.ok and "desteklenmiyor" in r.error, name
    assert ctx.apps.entries() == []  # Windows dışında uydurma uygulama listesi yok
