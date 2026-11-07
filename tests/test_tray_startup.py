"""Bildirim alanı, pencere kapatma davranışı, Ayarlar > Başlangıç ve pencere, uygulama simgesi."""

import asyncio

import pytest
import qasync
from PySide6.QtCore import Qt

from jarvis.app import configure_application
from jarvis.config.settings import SettingsStore
from jarvis.core.assistant import Assistant
from jarvis.integrations.autostart import RUN_KEY, UNSUPPORTED_MESSAGE, Autostart
from jarvis.interface import theme
from jarvis.interface.app_icon import app_icon
from jarvis.interface.main_window import MainWindow
from jarvis.interface.startup_settings import NO_TRAY_NOTE, StartupSection
from jarvis.interface.tray import TrayController
from jarvis.paths import AppPaths
from tests.test_autostart import CMD, FakeWinreg


@pytest.fixture
def ui(qapp):
    configure_application(qapp)
    lp = qasync.QEventLoop(qapp, already_running=False)
    asyncio.set_event_loop(lp)
    store = SettingsStore(AppPaths.resolve().settings_file)
    store.load()
    assistant = Assistant(store, sample_interval_s=0.5)
    window = MainWindow(assistant, theme.load_fonts())
    closed = []
    window.close_requested.connect(lambda: closed.append(True))
    yield assistant, window, closed
    if assistant.running:
        lp.run_until_complete(assistant.shutdown())
    else:
        assistant.db.close()
    window.deleteLater()
    lp.close()
    asyncio.set_event_loop(None)


def test_app_icon_and_window_icon(qapp):
    configure_application(qapp)
    icon = app_icon()
    assert not icon.isNull()
    assert not qapp.windowIcon().isNull()
    assert not icon.pixmap(16, 16).isNull() and not icon.pixmap(256, 256).isNull()


def test_close_quits_by_default(ui):
    _, window, closed = ui
    window.show()
    window.close()
    assert closed == [True]


def test_close_without_tray_quits_even_if_setting_on(ui):
    assistant, window, closed = ui
    assistant.settings.ui.close_to_tray = True
    window.show()
    window.close()  # bildirim alanı yok: kapatmak = çıkmak
    assert closed == [True]


def test_close_to_tray_hides_and_quit_app_really_quits(ui):
    assistant, window, closed = ui
    tray = TrayController(window, assistant)
    assistant.settings.ui.close_to_tray = True
    window.show()
    window.close()
    assert not window.isVisible() and closed == []
    assert tray._hint_shown
    window.quit_app()  # gizli pencereden de "tamamen kapat" çalışmalı
    assert closed == [True]
    assert not tray.icon.isVisible()


def test_tray_menu_actions(ui):
    assistant, window, closed = ui
    tray = TrayController(window, assistant)
    window.show()
    tray.refresh()
    assert tray.toggle_action.text() == "Pencereyi gizle"
    tray.toggle_action.trigger()
    assert not window.isVisible()
    tray.refresh()
    assert tray.toggle_action.text() == "Pencereyi göster"
    tray.toggle_action.trigger()
    assert window.isVisible()

    assert not tray.mic_action.isChecked()
    tray.mic_action.trigger()  # mikrofonu kapat
    assert assistant.settings.voice.microphone_enabled is False
    assert "kapalı" in window.panel.mic_pill.text()
    assert SettingsStore(assistant.store.path).load().voice.microphone_enabled is False
    window.panel.mic_button.click()  # arayüzden açılınca tepsi menüsü de güncellenir
    assert assistant.settings.voice.microphone_enabled is True
    assert not tray.mic_action.isChecked()

    tray.quit_action.trigger()
    assert closed == [True]


def test_bring_to_front_restores_minimized_window(ui):
    _, window, _ = ui
    window.showMinimized()
    window.bring_to_front()
    assert window.isVisible()
    assert not (window.windowState() & Qt.WindowState.WindowMinimized)


def test_sidebar_quit_button_ignores_close_to_tray(ui):
    assistant, window, closed = ui
    TrayController(window, assistant)
    assistant.settings.ui.close_to_tray = True
    window.show()
    window.sidebar.quit_button.click()
    assert closed == [True]


def test_settings_autostart_toggle_writes_registry_immediately(ui):
    assistant, _, _ = ui
    reg = FakeWinreg()
    sec = StartupSection(assistant, autostart=Autostart(reg, command=CMD), has_tray=True)
    assert sec.autostart_box.isEnabled() and not sec.autostart_box.isChecked()
    sec.autostart_box.setChecked(True)
    assert reg.keys[RUN_KEY]["JARVIS"][0] == CMD
    assert SettingsStore(assistant.store.path).load().general.autostart is True
    sec.autostart_box.setChecked(False)
    assert "JARVIS" not in reg.keys[RUN_KEY]
    assert SettingsStore(assistant.store.path).load().general.autostart is False


def test_settings_autostart_failure_reverts_checkbox(ui):
    assistant, _, _ = ui
    reg = FakeWinreg()
    reg.fail_writes = True
    sec = StartupSection(assistant, autostart=Autostart(reg, command=CMD), has_tray=True)
    sec.autostart_box.setChecked(True)
    assert not sec.autostart_box.isChecked()
    assert "yazılamadı" in sec.autostart_note.text()
    assert assistant.settings.general.autostart is False


def test_settings_autostart_unsupported(ui, monkeypatch):
    from jarvis.integrations import autostart as au

    monkeypatch.setattr(au, "is_supported", lambda: False)
    assistant, _, _ = ui
    sec = StartupSection(assistant, autostart=Autostart(), has_tray=False)
    assert not sec.autostart_box.isEnabled()
    assert sec.autostart_note.text() == UNSUPPORTED_MESSAGE
    assert not sec.tray_box.isEnabled()
    assert any(lbl.text() == NO_TRAY_NOTE for lbl in sec.findChildren(type(sec.autostart_note)))


def test_settings_close_to_tray_toggle_persists(ui):
    assistant, _, _ = ui
    sec = StartupSection(assistant, autostart=Autostart(FakeWinreg(), command=CMD), has_tray=True)
    sec.tray_box.setChecked(True)
    assert SettingsStore(assistant.store.path).load().ui.close_to_tray is True


def test_settings_view_contains_startup_section(ui):
    _, window, _ = ui
    sec = window.settings_view.startup
    assert sec.autostart_box.text() == "Windows açılınca başlat"
