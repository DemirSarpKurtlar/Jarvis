"""Araç hizmeti ve arayüz: yerel komutlar Claude'suz çalışır, onay penceresi engellemez, Sistem ekranı."""

import asyncio
import subprocess
import sys
import time

import psutil
import pytest
import qasync
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from jarvis.app import configure_application
from jarvis.config.settings import SettingsStore
from jarvis.core.assistant import Assistant
from jarvis.core.events import Topics
from jarvis.core.state import AssistantState
from jarvis.interface import theme
from jarvis.interface.main_window import MainWindow
from jarvis.interface.system_view import SystemView, hardware_html
from jarvis.paths import AppPaths
from jarvis.security.permission_manager import ConfirmPrompt
from jarvis.tools import registry as regmod
from tests.fake_api import FakeAPI
from tests.test_tools_system import snapshot
from tests.tool_fakes import make_ctx


@pytest.fixture
def loop(qapp):
    configure_application(qapp)
    lp = qasync.QEventLoop(qapp, already_running=False)
    asyncio.set_event_loop(lp)
    yield lp
    lp.close()
    asyncio.set_event_loop(None)


def make_assistant(tmp_path, api=None):
    store = SettingsStore(AppPaths.resolve().settings_file)
    store.load()
    api = api or FakeAPI()
    ctx = make_ctx(tmp_path)
    a = Assistant(store, sample_interval_s=0.2, data_dir=tmp_path / "data", tool_context=ctx,
                  client_factory=lambda key, max_retries=0: api.client(key, max_retries=0))
    return a, ctx, api


async def wait_for(cond, timeout=5.0):
    t = time.perf_counter()
    while not cond():
        if time.perf_counter() - t > timeout:
            raise AssertionError("koşul zaman aşımına uğradı")
        await asyncio.sleep(0.02)


def test_assistant_exposes_registry(tmp_path):
    a, _ctx, _ = make_assistant(tmp_path)
    assert a.tools.get("open_app") is not None and a.audit is a.tool_service.audit
    assert all(t["strict"] for t in a.tools.to_claude_tools())
    a.db.close()


def test_local_intent_opens_notepad_without_claude(tmp_path, monkeypatch):
    """Kabul testi: "Jarvis, Notepad'i aç" API anahtarı olmadan, Claude'a istek atmadan çalışır."""
    monkeypatch.setattr(regmod, "CURRENT_PLATFORM", "win32")
    a, ctx, api = make_assistant(tmp_path)
    events = []
    for t in (Topics.CHAT_STARTED, Topics.CHAT_DONE, Topics.CHAT_FAILED, Topics.STATE_CHANGED):
        a.bus.subscribe(t, lambda p, t=t: events.append((t, p)))

    async def go():
        await a.start()
        await a.send_text("Jarvis, Notepad'i aç")
        await a.shutdown()

    asyncio.run(go())
    assert ctx.opener.opened == ["notepad.exe"]
    assert api.requests == []  # Claude'a hiç gidilmedi
    done = [p for t, p in events if t == Topics.CHAT_DONE]
    assert done and done[0].text == "Not Defteri açılıyor." and done[0].model_name == "Yerel komut"
    assert done[0].cost_usd == 0
    assert not [p for t, p in events if t == Topics.CHAT_FAILED]
    states = [p.new for t, p in events if t == Topics.STATE_CHANGED]
    assert AssistantState.EXECUTING in states and a.state.state is AssistantState.STANDBY
    rows = AuditLoggerRows(a)
    assert rows and rows[0].tool == "open_app" and rows[0].source == "yerel"


def AuditLoggerRows(a):  # noqa: N802
    from jarvis.security.audit_logger import AuditLogger
    from jarvis.storage.database import Database

    return AuditLogger(Database(a.db.path)).recent()


def test_local_intent_on_unsupported_os_is_honest(tmp_path, monkeypatch):
    monkeypatch.setattr(regmod, "CURRENT_PLATFORM", "linux")
    a, ctx, api = make_assistant(tmp_path)
    done = []
    a.bus.subscribe(Topics.CHAT_DONE, done.append)

    async def go():
        await a.start()
        await a.send_text("Notepad'i aç")
        await a.shutdown()

    asyncio.run(go())
    assert ctx.opener.opened == [] and api.requests == []
    assert "desteklenmiyor" in done[0].text


def test_unmatched_text_goes_to_claude(tmp_path):
    a, ctx, api = make_assistant(tmp_path)
    failed = []
    a.bus.subscribe(Topics.CHAT_FAILED, failed.append)

    async def go():
        await a.start()
        await a.send_text("Bana bir şiir yazar mısın")
        await a.shutdown()

    asyncio.run(go())
    assert failed and "anahtarı girilmedi" in failed[0]  # Claude yoluna gitti (anahtar yok)
    assert ctx.opener.opened == []


def test_confirmation_through_bus(tmp_path):
    a, ctx, _ = make_assistant(tmp_path)
    (tmp_path / "Desktop" / "sil.txt").write_text("x")

    async def go():
        a.bus.bind_loop(asyncio.get_running_loop())
        # Arayüz yokken onay gerektiren işlem reddedilir
        r = await a.tool_service.run("delete_path", {"path": "Masaüstü/sil.txt", "permanent": True})
        assert not r.ok and (tmp_path / "Desktop" / "sil.txt").exists()
        answers = []

        def ui(req):
            answers.append(req.prompt)
            req.future.set_result(True)

        a.bus.subscribe(Topics.TOOL_CONFIRM, ui)
        r = await a.tool_service.run("delete_path", {"path": "Masaüstü/sil.txt", "permanent": True})
        assert r.ok and not (tmp_path / "Desktop" / "sil.txt").exists()
        assert answers[0].danger and "KALICI" in answers[0].message

    asyncio.run(go())
    a.db.close()


def test_confirmation_timeout(tmp_path):
    a, _ctx, _ = make_assistant(tmp_path)
    notices = []
    a.bus.subscribe(Topics.NOTICE, notices.append)
    a.bus.subscribe(Topics.TOOL_CONFIRM, lambda req: None)  # hiç yanıt vermeyen arayüz

    async def go():
        a.store.settings.tools.__dict__["confirm_timeout_s"] = 0.05  # doğrulamayı atla: test hızlı bitsin
        return await a.tool_service.confirm(ConfirmPrompt("x", "X", "?"))

    assert asyncio.run(go()) is False
    assert "süresi doldu" in notices[0]
    a.db.close()


def test_read_only_setting(tmp_path):
    store = SettingsStore(AppPaths.resolve().settings_file)
    store.load()
    store.settings.tools.read_only = True
    a = Assistant(store, data_dir=tmp_path / "d", tool_context=make_ctx(tmp_path))

    async def go():
        assert (await a.tool_service.run("cpu_status", {})).ok
        r = await a.tool_service.run("create_folder", {"path": "Masaüstü/yeni"})
        assert not r.ok and "Salt okunur" in r.error

    asyncio.run(go())
    assert not (tmp_path / "Desktop" / "yeni").exists()
    a.db.close()


# ---------------- arayüz ----------------
def test_hardware_html_is_honest():
    html = hardware_html(snapshot(), {"name": "Intel Core i7", "physical_cores": 8, "logical_cores": 16,
                                      "frequency_mhz": 3600, "temperature_c": None,
                                      "temperature_unavailable": "Güvenilir sensör yok"},
                         [{"mount": "C:\\", "free_gb": 200.0, "total_gb": 1000.0, "percent": 80.0}],
                         [{"interface": "Ethernet", "ipv4": ["192.168.1.5"], "ipv6": []}],
                         {"os": "Windows 11", "uptime_text": "5 saat 0 dakika", "battery": {"present": False}})
    assert "RTX 4070" in html and "61 °C" in html and "bu kartta okunamıyor" in html
    assert "desteklenmiyor: Güvenilir sensör yok" in html and "192.168.1.5" in html and "Windows 11" in html
    assert "yükleniyor" in hardware_html(None, None, None, None, None)
    no_gpu = hardware_html(snapshot(gpu=False), None, None, None, None)
    assert "NVIDIA ekran kartı bulunamadı" in no_gpu


def test_system_page_replaces_placeholder_and_kills_with_confirmation(qapp, loop, tmp_path):
    a, ctx, _ = make_assistant(tmp_path)
    ctx.snapshot = snapshot
    w = MainWindow(a, theme.load_fonts())
    w.show()
    assert isinstance(w.pages["sistem"], SystemView)
    view = w.system_view
    proc = subprocess.Popen(["ping", "-n", "60", "127.0.0.1"] if sys.platform == "win32" else ["sleep", "60"],
                            stdout=subprocess.DEVNULL)

    def find_row():
        t = view.proc_table
        for i in range(t.rowCount()):
            if t.item(i, 0) and t.item(i, 0).text() == str(proc.pid):
                return i
        return None

    def boxes():
        return [b for b in w.findChildren(QMessageBox) if b.isVisible()]

    async def go():
        await a.start()
        w.navigate("sistem")
        await wait_for(lambda: find_row() is not None)
        # Sıralama sayısal olmalı (PID sütunu)
        view.proc_table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        pids = [int(view.proc_table.item(i, 0).text()) for i in range(view.proc_table.rowCount())]
        assert pids == sorted(pids)
        view.proc_table.selectRow(find_row())
        view.kill_selected()
        await wait_for(lambda: boxes())
        box = boxes()[0]
        assert "sonlandırılacak" in box.text()
        cancel = [b for b in box.buttons() if b.text() == "Vazgeç"][0]
        assert box.defaultButton() is cancel  # varsayılan seçenek güvenli olan
        cancel.click()
        await wait_for(lambda: "yapmadım" in view.status.text())
        assert proc.poll() is None
        view.proc_table.selectRow(find_row())
        view.kill_selected()
        await wait_for(lambda: boxes())
        [b for b in boxes()[0].buttons() if b.text() == "Sonlandır"][0].click()
        await wait_for(lambda: proc.poll() is not None)
        await wait_for(lambda: "sonlandırıldı" in view.status.text())
        # Donanım sekmesi
        view.tabs.setCurrentIndex(1)
        await wait_for(lambda: "Çekirdek" in view.hw_label.text())
        assert "Ekran kartı" in view.hw_label.text()
        view._on_snapshot(snapshot())  # sahte GPU ölçümü ekrana hemen yansır
        assert "RTX 4070" in view.hw_label.text()
        # Uygulamalar sekmesi (sahte dizin)
        view.tabs.setCurrentIndex(2)
        await wait_for(lambda: view.app_list.count() > 0)
        view.app_search.setText("not defteri")
        view.search_apps()
        await wait_for(lambda: view.app_list.count() > 0 and view.app_list.item(0).text() == "Not Defteri")
        # Denetim kaydı sekmesi
        view.tabs.setCurrentIndex(3)
        await wait_for(lambda: view.audit_table.rowCount() >= 2)
        assert view.audit_table.item(0, 2).text() == "kill_process"
        await a.shutdown()

    try:
        loop.run_until_complete(go())
    finally:
        if proc.poll() is None:
            proc.kill()
        w.deleteLater()
    assert not psutil.pid_exists(proc.pid) or psutil.Process(proc.pid).status() == psutil.STATUS_ZOMBIE


def test_local_command_from_panel_input(qapp, loop, tmp_path, monkeypatch):
    monkeypatch.setattr(regmod, "CURRENT_PLATFORM", "win32")
    a, ctx, api = make_assistant(tmp_path)
    w = MainWindow(a, theme.load_fonts())
    w.show()

    async def go():
        await a.start()
        w.panel.input.setText("sesi yüzde 30 yap")
        w.panel.input.returnPressed.emit()
        await wait_for(lambda: w.panel.input.isEnabled() and ctx.audio.volume == 30)
        await a.shutdown()

    loop.run_until_complete(go())
    assert w.panel.chat.jarvis_text.text() == "Sesi %30 yaptım."
    assert "Yerel komut" in w.panel.chat.meta.text()
    assert api.requests == []
    w.deleteLater()
    QApplication.processEvents()
