"""Otomasyon ekranı: arayüzden görev zamanlama, listeleme, zamanı gelince bildirim, iptal."""

import asyncio
import time
from datetime import datetime, timedelta

import pytest
import qasync

from jarvis.app import configure_application
from jarvis.config.settings import SettingsStore
from jarvis.core.assistant import Assistant
from jarvis.core.events import Topics
from jarvis.interface import theme
from jarvis.interface.automation_view import remaining
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


def test_schedule_list_fire_and_cancel(qapp, loop):
    store = SettingsStore(AppPaths.resolve().settings_file)
    store.load()
    a = Assistant(store, sample_interval_s=0.5)
    w = MainWindow(a, theme.load_fonts())
    notices = []
    a.bus.subscribe(Topics.NOTICE, notices.append)

    async def go():
        await a.start()
        w.show()
        w.navigate("otomasyon")
        v = w.automation_view
        v.action.setCurrentIndex(0)          # hatırlatma
        v.minutes.setValue(30)
        v.text.setText("Su iç")
        v.add_btn.click()
        await wait_for(lambda: v.list_box.count() == 1)
        assert "Su iç" in v.list_box.itemAt(0).widget().findChild(type(v.result)).text()
        # İkinci görev: hemen çalışsın (zamanlayıcıyı doğrudan kısa gecikmeyle kur)
        a.tool_service.ctx.scheduler.schedule("hatirlatma", 1, "Toplantı", -1, delay_s=0.05)
        await wait_for(lambda: any("Toplantı" in n for n in notices))
        # İptal
        from PySide6.QtWidgets import QPushButton
        v.refresh()
        btn = v.list_box.itemAt(0).widget().findChild(QPushButton)
        btn.click()
        await wait_for(lambda: a.tool_service.ctx.scheduler.list() == [])
        v.refresh()
        assert v.empty.isVisible()
        await a.shutdown()

    loop.run_until_complete(go())
    assert "otomasyon" in w.pages


def test_remaining_text():
    now = datetime(2026, 11, 8, 12, 0)
    assert remaining(now + timedelta(minutes=90), now) == "1 sa 30 dk sonra"
    assert remaining(now + timedelta(minutes=5, seconds=10), now) == "5 dk sonra"
    assert remaining(now + timedelta(seconds=20), now) == "20 sn sonra"
    assert remaining(now - timedelta(seconds=1), now) == "şimdi"
