"""Otomasyon ekranındaki "Sistem rutinleri" bölümü ve güç geri sayımlarının listede görünmesi."""

import asyncio
import time

import pytest
import qasync

from jarvis.app import configure_application
from jarvis.config.settings import SettingsStore
from jarvis.core.assistant import Assistant
from jarvis.interface import theme
from jarvis.interface.main_window import MainWindow
from jarvis.paths import AppPaths


@pytest.fixture
def loop(qapp):
    configure_application(qapp)
    lp = qasync.QEventLoop(qapp, already_running=False)
    asyncio.set_event_loop(lp)
    yield lp
    lp.close()
    asyncio.set_event_loop(None)


async def wait_for(cond, timeout=5.0):
    t = time.perf_counter()
    while not cond():
        if time.perf_counter() - t > timeout:
            raise AssertionError("zaman aşımı")
        await asyncio.sleep(0.02)


def test_sistem_panel_and_power_countdown(qapp, loop):
    store = SettingsStore(AppPaths.resolve().settings_file)
    store.load()
    a = Assistant(store, sample_interval_s=0.5)
    w = MainWindow(a, theme.load_fonts())

    async def go():
        await a.start()
        assert a.sistem.gpu is not None and a.sistem.routine is not None
        w.show()
        w.navigate("otomasyon")
        v = w.automation_view
        p = v.sistem_panel
        # GPU uyarısı ayarı hemen kaydedilir
        assert p.gpu.isChecked() and p.threshold.value() == 85
        p.threshold.setValue(80)
        p.gpu.setChecked(False)
        again = SettingsStore(AppPaths.resolve().settings_file)
        again.load()
        assert again.settings.sistem.gpu_alert_threshold_c == 80 and not again.settings.sistem.gpu_alert_enabled
        assert not p.threshold.isEnabled()
        # İndirilenler rutini: durum dosyasından okunur; kapatma onay istemez
        a.sistem.routine.set_enabled(True)
        p.sync()
        assert p.downloads.isChecked() and not p.undo.isEnabled()
        p.downloads.click()        # kapat
        await wait_for(lambda: not a.sistem.routine.enabled)
        await wait_for(lambda: "kapattım" in p.note.text())
        assert not p.downloads.isChecked()
        # Onaydan sonra kurulan uyku geri sayımı görev listesinde, iptal düğmesiyle
        a.tool_service.ctx.scheduler.schedule_internal("guc_uyku", "Uyku modu", 3600, lambda: None)
        await asyncio.sleep(0.05)
        v.refresh()
        label = v.list_box.itemAt(0).widget().findChildren(type(p.note))[0].text()
        assert "Güç — Uyku modu" in label and "dk sonra" in label
        a.tool_service.ctx.scheduler.cancel_all()
        await a.shutdown()

    loop.run_until_complete(go())
