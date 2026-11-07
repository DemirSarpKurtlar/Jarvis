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


# ---------------- Aşama 2: sohbet arayüzü ----------------
from tests.fake_api import FakeAPI, text_stream  # noqa: E402
from jarvis.core.chat_service import ClaudeStatus  # noqa: E402
from jarvis.security.credentials import CredentialStore  # noqa: E402

KEY = "sk-ant-api03-" + "U" * 40


def make_chat_ui(loop, api, with_key=True):
    fonts = theme.load_fonts()
    store = SettingsStore(AppPaths.resolve().settings_file)
    store.load()
    if with_key:
        CredentialStore().set_anthropic_key(KEY)
    a = Assistant(store, sample_interval_s=0.5,
                  client_factory=lambda key, max_retries=0: api.client(key, max_retries=0))
    w = MainWindow(a, fonts)
    w.show()
    return a, w


async def wait_for(cond, timeout=5.0):
    t = time.perf_counter()
    while not cond():
        if time.perf_counter() - t > timeout:
            raise AssertionError("koşul zaman aşımına uğradı")
        await asyncio.sleep(0.02)


def test_typing_sends_and_streams_answer(qapp, loop):
    api = FakeAPI([text_stream(["İstanbul'un ", "nüfusu yaklaşık 16 milyon."])])
    a, w = make_chat_ui(loop, api)

    async def go():
        await a.start()
        await wait_for(lambda: a.chat.status is ClaudeStatus.CONNECTED)
        w.panel.input.setText("İstanbul'un nüfusu kaç")
        w.panel.input.returnPressed.emit()
        await asyncio.sleep(0)
        assert not w.panel.input.isEnabled()        # cevap gelirken kilitli
        await wait_for(lambda: w.panel.input.isEnabled())
        await a.shutdown()

    loop.run_until_complete(go())
    assert w.panel.chat.user_text.text() == "İstanbul'un nüfusu kaç"
    assert w.panel.chat.jarvis_text.text() == "İstanbul'un nüfusu yaklaşık 16 milyon."
    assert "Haiku 5.5" in w.panel.chat.meta.text() and "$0," in w.panel.chat.meta.text()
    assert w.telemetry.month.value.text().startswith("$0,")
    assert "Haiku" in w.telemetry.models.legend.text()
    assert w.panel.claude_pill.text() == "Claude: bağlı"
    assert w.panel.input.text() == ""


def test_no_key_banner_offers_key_entry(qapp, loop):
    a, w = make_chat_ui(loop, FakeAPI(), with_key=False)

    async def go():
        await a.start()
        await wait_for(lambda: a.chat.status is ClaudeStatus.NO_KEY)
        await asyncio.sleep(0.05)
        await a.shutdown()

    loop.run_until_complete(go())
    assert w.panel.banner.isVisible()
    assert w.panel.banner.action.text() == "Anahtarı gir"
    assert w.telemetry.claude_status.value.text().startswith("Anahtar girilmedi")


def test_key_dialog_saves_verified_key(qapp, loop, memory_keyring):
    a, w = make_chat_ui(loop, FakeAPI(), with_key=False)

    async def go():
        await a.start()
        await wait_for(lambda: a.chat.status is ClaudeStatus.NO_KEY)
        w.open_key_dialog()
        dlg = w.settings_view._dialog
        assert dlg.edit.echoMode() == dlg.edit.EchoMode.Password   # anahtar ekranda gizli
        dlg.edit.setText(KEY)
        dlg.ok.click()
        await wait_for(lambda: a.chat.status is ClaudeStatus.CONNECTED)
        await a.shutdown()

    loop.run_until_complete(go())
    assert list(memory_keyring.data.values()) == [KEY]
    assert w.settings_view.key_btn.text() == "Anahtarı değiştir"


def test_settings_view_changes_persist(qapp, loop):
    a, w = make_chat_ui(loop, FakeAPI())
    sv = w.settings_view
    sv.model.setCurrentIndex(3)       # Opus
    sv.monthly.setValue(12.5)
    sv.policy.setCurrentIndex(2)      # durdur
    sv.web.setChecked(False)
    reread = SettingsStore(a.store.path).load()
    assert reread.ai.preferred_model == "advanced"
    assert reread.budget.monthly_limit_usd == 12.5
    assert reread.budget.on_limit == "block"
    assert reread.ai.web_search_enabled is False
    loop.run_until_complete(a.shutdown()) if a.running else a.db.close()


def test_conversations_page_lists_real_history(qapp, loop):
    api = FakeAPI([text_stream(["Selam Demir!"])])
    a, w = make_chat_ui(loop, api)

    async def go():
        await a.start()
        await wait_for(lambda: a.chat.status is ClaudeStatus.CONNECTED)
        await a.chat.send("Selam, ben Demir")
        w.navigate("konusmalar")
        await asyncio.sleep(0.05)
        await a.shutdown()

    loop.run_until_complete(go())
    cv = w.conversations
    assert cv.list.count() == 1 and "Selam, ben Demir" in cv.list.item(0).text()
    assert "Selam Demir!" in cv.view.toPlainText()


def test_budget_ask_dialog_answers_future(qapp, loop):
    from PySide6.QtWidgets import QMessageBox
    api = FakeAPI([text_stream(["tamam"])])
    a, w = make_chat_ui(loop, api)
    a.settings.budget.on_limit = "ask"
    a.settings.budget.daily_limit_usd = 0.000001

    async def go():
        await a.start()
        await wait_for(lambda: a.chat.status is ClaudeStatus.CONNECTED)
        a.db.execute("INSERT INTO usage (ts_utc, local_day, local_month, model, tier, input_tokens, output_tokens,"
                     " cache_creation_tokens, cache_read_tokens, web_search_requests, cost_usd)"
                     " VALUES ('x', strftime('%Y-%m-%d','now','localtime'), strftime('%Y-%m','now','localtime'),"
                     " 'claude-haiku-5-5','light',0,0,0,0,0,1.0)")
        task = a.send_text("selam")
        await wait_for(lambda: getattr(w, "_confirm_box", None) is not None and w._confirm_box.isVisible())
        box = w._confirm_box
        yes = [b for b in box.buttons() if box.buttonRole(b) == QMessageBox.ButtonRole.AcceptRole][0]
        yes.click()
        await task
        await a.shutdown()

    loop.run_until_complete(go())
    assert w.panel.chat.jarvis_text.text() == "tamam"
