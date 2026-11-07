"""Arayüz testleri (ekransız Qt). Gerçek Windows penceresi testi ayrıca yapılacak."""

import asyncio
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import qasync

from jarvis.app import configure_application
from jarvis.config.settings import SettingsStore
from jarvis.core.assistant import Assistant
from jarvis.core.errors import install_exception_hooks
from jarvis.core.state import AssistantState as S
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


@pytest.fixture
def ui(qapp, loop):
    fonts = theme.load_fonts()
    store = SettingsStore(AppPaths.resolve().settings_file)
    store.load()
    assistant = Assistant(store, sample_interval_s=0.2)
    window = MainWindow(assistant, fonts, developer=True)
    yield assistant, window, loop
    if assistant.running:
        loop.run_until_complete(assistant.shutdown())
    window.deleteLater()


def run(loop, coro):
    return loop.run_until_complete(coro)


def test_fonts_loaded(qapp):
    f = theme.load_fonts()
    assert f.display == "Saira"
    assert f.body == "IBM Plex Sans"


def test_every_state_renders(ui):
    assistant, window, loop = ui
    window.show()
    sm = assistant.state
    for st in (S.WAKE_DETECTED, S.LISTENING, S.PROCESSING, S.EXECUTING, S.SPEAKING, S.STANDBY):
        sm.transition(st, "ayrıntı")
        assert window.panel.reactor.state is st
        img = window.grab().toImage()
        assert not img.isNull() and img.width() > 0
    sm.fail("deneme")
    assert window.panel.reactor.state is S.ERROR
    assert window.panel.banner.isVisible()
    assert "deneme" in window.panel.banner.text.text()


def test_banner_close_acknowledges_error(ui):
    assistant, window, _ = ui
    window.show()
    assistant.state.fail("x")
    window.panel._close_banner()
    assert assistant.state.state is S.STANDBY
    assert not window.panel.banner.isVisible()


def test_microphone_toggle_persists(ui):
    assistant, window, _ = ui
    window.show()
    assert assistant.settings.voice.microphone_enabled
    window.panel.mic_button.click()
    assert assistant.settings.voice.microphone_enabled is False
    assert "kapalı" in window.panel.mic_pill.text()
    reread = SettingsStore(assistant.store.path).load()
    assert reread.voice.microphone_enabled is False
    window.panel.mic_button.click()
    assert assistant.settings.voice.microphone_enabled is True


def test_live_system_data_reaches_panel(ui):
    assistant, window, loop = ui
    window.show()

    async def go():
        await assistant.start()
        await asyncio.sleep(0.8)

    run(loop, go())
    assert window.telemetry.cpu.value.text() != "–"
    assert "GB" in window.telemetry.ram.value.text()


def test_ui_stays_responsive_while_monitoring(ui):
    """Arka planda sensör okunurken olay döngüsü gecikmesi küçük kalmalı."""
    assistant, window, loop = ui
    window.show()
    lags = []

    async def probe():
        await assistant.start()
        for _ in range(40):  # ~2 saniye
            t = time.perf_counter()
            await asyncio.sleep(0.05)
            lags.append(time.perf_counter() - t - 0.05)

    run(loop, probe())
    assert max(lags) < 0.15, f"en kötü gecikme {max(lags)*1000:.0f} ms"


def test_reactor_timer_stops_when_hidden(ui):
    _, window, _ = ui
    window.show()
    assert window.panel.reactor.animating
    window.hide()
    assert not window.panel.reactor.animating


def test_reduced_motion_disables_animation(ui):
    _, window, _ = ui
    window.show()
    window.panel.reactor.set_reduced_motion(True)
    assert not window.panel.reactor.animating


def test_error_from_background_thread_is_shown(ui):
    assistant, window, loop = ui
    window.show()
    install_exception_hooks()

    async def go():
        await assistant.start()
        t = threading.Thread(target=lambda: (_ for _ in ()).throw(RuntimeError("yan thread çöktü")), name="deneme")
        t.start()
        t.join()
        await asyncio.sleep(0.1)

    run(loop, go())
    assert assistant.state.state is S.ERROR
    assert window.panel.banner.isVisible()


def test_close_requested_then_clean_shutdown(ui):
    assistant, window, loop = ui
    window.show()
    fired = []
    window.close_requested.connect(lambda: fired.append(True))

    async def go():
        await assistant.start()
        window.sidebar.quit_button.click()
        await assistant.shutdown()

    run(loop, go())
    assert fired == [True]
    assert not assistant.running


def test_corrupt_settings_warning_shown(qapp, loop):
    paths = AppPaths.resolve()
    paths.settings_file.write_text("bozuk", encoding="utf-8")
    store = SettingsStore(paths.settings_file)
    store.load()
    w = MainWindow(Assistant(store), theme.load_fonts())
    w.show()
    assert w.panel.banner.isVisible()
    assert "yedeklendi" in w.panel.banner.text.text()


def test_navigation_shows_honest_coming_soon_pages(ui):
    _, window, _ = ui
    window.show()
    window.navigate("hafiza")
    assert window.stack.currentWidget() is window.pages["hafiza"]
    assert window.sidebar.buttons["hafiza"].isChecked()


def test_full_app_start_and_shutdown(jarvis_home):
    """Gerçek uygulamayı ayrı süreçte başlatır, 2 sn sonra kendini kapatmasını bekler."""
    env = dict(os.environ, JARVIS_HOME=str(jarvis_home), QT_QPA_PLATFORM="offscreen")
    t0 = time.perf_counter()
    proc = subprocess.run([sys.executable, "-m", "jarvis", "--smoke-test", "2"],
                          env=env, capture_output=True, text=True, timeout=60,
                          cwd=Path(__file__).resolve().parents[1])
    elapsed = time.perf_counter() - t0
    log = (jarvis_home / "logs" / "jarvis.log").read_text(encoding="utf-8")
    assert proc.returncode == 0, proc.stderr + log
    assert "Arayüz hazır" in log
    assert "Asistan kapatıldı" in log and "JARVIS kapatıldı" in log
    assert "Traceback" not in log and " ERROR " not in log and "CRITICAL" not in log
    assert elapsed < 20
