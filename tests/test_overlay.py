"""Ekranın üstündeki dinleme göstergesi (taslak B, "Kenar ışığı"): pencere, akış kuralları, çizim."""

import asyncio
import sys
from types import SimpleNamespace

import pytest
import qasync
from PySide6.QtCore import QPoint, QRect, Qt, QTimer
from PySide6.QtWidgets import QApplication

from jarvis.app import configure_application
from jarvis.config.settings import SettingsStore
from jarvis.core.assistant import Assistant
from jarvis.core.chat_service import ChatDone, ChatStarted
from jarvis.core.events import Topics
from jarvis.core.state import AssistantState as S
from jarvis.interface import overlay as ov
from jarvis.interface import theme
from jarvis.interface.overlay import (ListeningOverlay, OverlayController, OverlayView, exclusive_fullscreen_active,
                                      pick_screen, scale_for, wrap_lines)
from jarvis.interface.startup_settings import StartupSection
from jarvis.paths import AppPaths

SCREEN = QRect(0, 0, 1920, 1080)
LONG_REPLY = ("Not Defteri açık. En üste “Yarınki toplantı” başlığını yazdım, devamını söyleyebilirsin. "
              "Ayrıca yarın sabah dokuzda bir hatırlatma kurdum ve takvimine de ekledim, istersen değiştiririm.")


@pytest.fixture
def env(qapp):
    configure_application(qapp)
    lp = qasync.QEventLoop(qapp, already_running=False)
    asyncio.set_event_loop(lp)
    store = SettingsStore(AppPaths.resolve().settings_file)
    store.load()
    assistant = Assistant(store, sample_interval_s=0.5)
    flags = SimpleNamespace(main_active=False, fullscreen=False, geometry=SCREEN)
    ctl = OverlayController(assistant, theme.load_fonts(),
                            main_window_active=lambda: flags.main_active,
                            fullscreen_check=lambda: flags.fullscreen,
                            screen_geometry=lambda: flags.geometry)
    yield assistant, ctl, flags
    ctl.shutdown()
    assistant.db.close()
    lp.close()
    asyncio.set_event_loop(None)


def active_timers(ctl: OverlayController) -> list[QTimer]:
    timers = ctl.findChildren(QTimer) + ctl.overlay.findChildren(QTimer)
    return [t for t in timers if t.isActive()]


def wake_to_listening(assistant: Assistant) -> None:
    assistant.state.transition(S.WAKE_DETECTED, "Jarvis")
    assistant.state.transition(S.LISTENING, "Dinliyorum")


# ---------- pencere ----------
def test_window_flags_never_take_focus(env, qtbot):
    assistant, ctl, _ = env
    w = ctl.overlay
    flags = w.windowFlags()
    for f in (Qt.WindowType.Tool, Qt.WindowType.WindowStaysOnTopHint, Qt.WindowType.FramelessWindowHint,
              Qt.WindowType.WindowDoesNotAcceptFocus):
        assert flags & f, f
    assert w.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    assert w.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert w.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert w.parent() is None   # ana pencere simge durumuna inince birlikte gizlenmesin
    if sys.platform != "win32":
        assert flags & Qt.WindowType.WindowTransparentForInput   # Windows'ta WS_EX_TRANSPARENT ile
    active_before, focus_before = QApplication.activeWindow(), QApplication.focusWidget()
    assistant.state.transition(S.WAKE_DETECTED, "Jarvis")
    assert w.isVisible()
    assert QApplication.activeWindow() is active_before and QApplication.activeWindow() is not w
    assert QApplication.focusWidget() is focus_before     # gösterge odağı değiştirmedi
    assert w.height() == ov.WINDOW_H and w.width() == SCREEN.width()


# ---------- akış ----------
def test_shows_on_wake_and_slides_away_after_standby(env, qtbot, monkeypatch):
    assistant, ctl, _ = env
    assert not ctl.visible
    assistant.state.transition(S.WAKE_DETECTED, "Jarvis")
    assert ctl.visible and ctl.overlay.view.kind == "wake"
    assert ctl.overlay.view.text == "Seni duydum"
    assistant.state.transition(S.LISTENING, "Dinliyorum")
    assert ctl.overlay.view.kind == "listening" and ctl.overlay.view.text == "Dinliyorum"
    assistant.state.reset()
    assert ctl.visible and ctl.overlay.view.kind == "standby"
    assert ctl._hide_timer.isActive() and ctl._hide_timer.interval() == 2500
    monkeypatch.setattr(ov, "LEAVE_S", 0.05)
    ctl._hide_timer.start(30)
    qtbot.waitUntil(lambda: not ctl.visible, timeout=3000)
    assert not ctl.session
    assert active_timers(ctl) == []


def test_talk_button_flow_also_shows(env):
    assistant, ctl, _ = env
    assistant.state.transition(S.LISTENING, "Dinliyorum")   # Konuş: beklemeden doğrudan dinleme
    assert ctl.visible and ctl.overlay.view.kind == "listening"


def test_typed_chat_does_not_pop_overlay(env):
    assistant, ctl, _ = env
    assistant.bus.publish(Topics.CHAT_STARTED, ChatStarted("merhaba", "Haiku 5.5", "kısa", False))
    assistant.state.transition(S.PROCESSING, "Haiku 5.5 ile düşünüyorum")
    assistant.state.transition(S.SPEAKING, "")
    assistant.state.reset()
    assert not ctl.visible and not ctl.session
    assert active_timers(ctl) == []


def test_not_shown_when_main_window_active(env):
    assistant, ctl, flags = env
    flags.main_active = True
    wake_to_listening(assistant)
    assert not ctl.visible
    assert active_timers(ctl) == []


def test_hidden_when_main_window_becomes_active(env):
    assistant, ctl, flags = env
    assistant.state.transition(S.WAKE_DETECTED, "Jarvis")
    assert ctl.visible
    flags.main_active = True
    assistant.state.transition(S.LISTENING, "Dinliyorum")
    assert not ctl.visible
    assert active_timers(ctl) == []


def test_not_shown_when_setting_disabled(env):
    assistant, ctl, _ = env
    assistant.settings.ui.overlay_enabled = False
    wake_to_listening(assistant)
    assert not ctl.visible


def test_disabling_setting_hides_visible_overlay(env):
    assistant, ctl, _ = env
    wake_to_listening(assistant)
    assert ctl.visible
    assistant.settings.ui.overlay_enabled = False
    assistant.bus.publish(Topics.SETTINGS_CHANGED, assistant.settings)
    assert not ctl.visible and active_timers(ctl) == []


def test_not_shown_over_exclusive_fullscreen(env):
    assistant, ctl, flags = env
    flags.fullscreen = True
    wake_to_listening(assistant)
    assert not ctl.visible


def test_texts_follow_the_voice_flow(env, monkeypatch):
    assistant, ctl, _ = env
    bus = assistant.bus
    wake_to_listening(assistant)
    assistant.state.transition(S.PROCESSING, "Söylediğini yazıya çeviriyorum")
    assert (ctl.overlay.view.kind, ctl.overlay.view.text, ctl.overlay.view.secondary) == ("thinking", "Düşünüyorum", "")

    # Söylenen cümle VOICE_TRANSCRIPT ile tek seferde gelir ve kısa bir süre görünür
    bus.publish(Topics.VOICE_TRANSCRIPT, SimpleNamespace(text="Not Defteri'ni  aç"))
    assert ctl.overlay.view.text == "Not Defteri'ni aç" and ctl.overlay.view.style == "transcript"
    assistant.state.transition(S.PROCESSING, "Haiku 5.5 ile düşünüyorum")
    bus.publish(Topics.CHAT_STARTED, ChatStarted("Not Defteri'ni aç", "Haiku 5.5", "kısa", False))
    assert ctl.overlay.view.style == "transcript"     # tutma süresi bitmedi
    ctl._hold_until = 0
    ctl._hold_timer.timeout.emit()
    v = ctl.overlay.view
    assert (v.kind, v.text, v.secondary) == ("thinking", "Düşünüyorum", "Haiku 5.5")

    assistant.state.transition(S.EXECUTING, "Not Defteri açılıyor")
    assert (ctl.overlay.view.kind, ctl.overlay.view.text) == ("executing", "Not Defteri açılıyor")
    assistant.state.transition(S.PROCESSING, "Haiku 5.5 ile düşünüyorum")

    # ChatService bitince önce beklemeye döner, ses yöneticisi aynı adımda konuşmaya geçer
    bus.publish(Topics.CHAT_DONE, ChatDone(LONG_REPLY, "Haiku 5.5", 0.001))
    assistant.state.reset()
    assistant.state.transition(S.PROCESSING, "Cevabı seslendiriyorum")
    assistant.state.transition(S.SPEAKING, "")
    assert ctl.visible and not ctl._hide_timer.isActive()
    v = ctl.overlay.view
    assert (v.kind, v.style, v.text) == ("speaking", "speech", LONG_REPLY)
    lines = ctl.overlay._layout.lines
    assert len(lines) == 2 and lines[1].endswith("…")     # ilk kısım, en fazla iki satır
    assert lines[0].startswith("Not Defteri açık.")
    assert ctl.overlay._layout.h > ov.TAB_H

    # Takip sorusu için yeniden dinleme
    assistant.state.transition(S.LISTENING, "Devam edebilirsin")
    assert ctl.session and ctl.overlay.view.kind == "listening"
    assert ctl.overlay.view.secondary == "Devam edebilirsin"


def test_voice_level_drives_bars_only_when_matching(env):
    assistant, ctl, _ = env
    wake_to_listening(assistant)
    assistant.bus.publish(Topics.VOICE_LEVEL, SimpleNamespace(source="tts", level=0.9))
    assert ctl.overlay._level_target == 0
    assistant.bus.publish(Topics.VOICE_LEVEL, SimpleNamespace(source="mic", level=0.7))
    assert ctl.overlay._level_target == pytest.approx(0.7)


def test_errors_show_and_stay_six_seconds(env):
    assistant, ctl, _ = env
    wake_to_listening(assistant)
    assistant.state.transition(S.PROCESSING, "Söylediğini yazıya çeviriyorum")
    assistant.bus.publish(Topics.VOICE_ERROR, "Söylediğini yazıya çeviremedim.")
    assistant.state.reset()            # ses yöneticisi hatadan sonra beklemeye döner
    v = ctl.overlay.view
    assert (v.kind, v.text, v.color) == ("error", "Söylediğini yazıya çeviremedim.", theme.C.KIZIL)
    assert ctl._hide_timer.interval() == 6000 and ctl._hide_timer.isActive()

    # Durum makinesi hatası (ör. Claude isteği başarısız)
    assistant.state.transition(S.WAKE_DETECTED, "Jarvis")
    assert ctl.overlay.view.kind == "wake"
    assistant.state.fail("İnternet bağlantısı yok.")
    assert (ctl.overlay.view.kind, ctl.overlay.view.text) == ("error", "İnternet bağlantısı yok.")
    assert ctl._hide_timer.interval() == 6000


def test_errors_outside_voice_flow_are_ignored(env):
    assistant, ctl, _ = env
    assistant.bus.publish(Topics.VOICE_ERROR, "Mikrofon açılamadı.")
    assistant.bus.publish(Topics.APP_ERROR, "Beklenmeyen hata")
    assert not ctl.visible


def test_hide_button_only_hides(env, qtbot):
    assistant, ctl, _ = env
    w = ctl.overlay
    wake_to_listening(assistant)
    assistant.state.transition(S.PROCESSING, "Haiku 5.5 ile düşünüyorum")
    w.set_hovered(True)
    rects = w.button_rects()
    assert set(rects) == {"mic", "hide"}
    assert w._layout.w > w._compute_layout(w.view, False).w     # düğmeler çıkıntıyı genişletir
    qtbot.mouseClick(w, Qt.MouseButton.LeftButton, pos=rects["hide"].center().toPoint())
    assert w.leaving or not w.isVisible()
    qtbot.waitUntil(lambda: not w.isVisible(), timeout=3000)
    # İstek iptal edilmedi; aynı konuşmanın devamı göstergeyi yeniden açmaz
    assert assistant.state.state is S.PROCESSING
    assistant.state.transition(S.SPEAKING, "")
    assistant.state.transition(S.LISTENING, "Devam edebilirsin")
    assert not ctl.visible
    assistant.state.reset()
    assert not ctl.session and active_timers(ctl) == []
    # Yeni bir uyandırma yeniden gösterir
    assistant.state.transition(S.WAKE_DETECTED, "Jarvis")
    assert ctl.visible


def test_mic_button_toggles_microphone(env, qtbot):
    assistant, ctl, _ = env
    w = ctl.overlay
    wake_to_listening(assistant)
    assert assistant.settings.voice.microphone_enabled
    w.set_hovered(True)
    qtbot.mouseClick(w, Qt.MouseButton.LeftButton, pos=w.button_rects()["mic"].center().toPoint())
    assert assistant.settings.voice.microphone_enabled is False
    assert (w.view.kind, w.view.text) == ("mic_off", "Mikrofon kapalı")
    assert ctl._hide_timer.interval() == 1800
    assistant.set_microphone_enabled(True)


def test_hovered_overlay_waits_before_fading(env):
    assistant, ctl, _ = env
    wake_to_listening(assistant)
    assistant.state.reset()
    ctl.overlay.set_hovered(True)
    ctl._hide_timeout()
    assert ctl.visible and ctl.session and ctl._hide_timer.isActive()


# ---------- ekran seçimi ----------
class FakeScreen:
    def __init__(self, name: str, rect: QRect) -> None:
        self.name = name
        self._rect = rect

    def geometry(self) -> QRect:
        return self._rect


def test_pick_screen_follows_cursor_and_falls_back_to_primary():
    left = FakeScreen("sol", QRect(-1920, 0, 1920, 1080))
    main = FakeScreen("ana", QRect(0, 0, 2560, 1440))
    right = FakeScreen("sag", QRect(2560, 0, 3840, 2160))
    screens = [left, main, right]
    assert pick_screen(screens, QPoint(-100, 500), main) is left
    assert pick_screen(screens, QPoint(3000, 10), main) is right
    assert pick_screen(screens, QPoint(10, 10), main) is main
    assert pick_screen(screens, QPoint(99999, 99999), main) is main     # imleç hiçbirinde değil
    assert pick_screen(screens, QPoint(99999, 99999), None) is left
    assert pick_screen([], QPoint(0, 0), None) is None


def test_overlay_placed_on_chosen_screen_and_scaled_on_4k(env):
    assistant, ctl, flags = env
    flags.geometry = QRect(1920, 0, 3840, 2160)
    assistant.state.transition(S.WAKE_DETECTED, "Jarvis")
    g = ctl.overlay.geometry()
    assert (g.x(), g.y(), g.width()) == (1920, 0, 3840)
    assert g.height() == round(ov.WINDOW_H * 1.5)
    assert scale_for(QRect(0, 0, 1920, 1080)) == 1.0 and scale_for(QRect(0, 0, 3840, 2160)) == 1.5


def test_screen_is_kept_during_the_conversation(env):
    assistant, ctl, flags = env
    assistant.state.transition(S.WAKE_DETECTED, "Jarvis")
    flags.geometry = QRect(1920, 0, 1920, 1080)
    assistant.state.transition(S.LISTENING, "Dinliyorum")
    assert ctl.overlay.geometry().x() == 0   # gösterge ekranlar arasında zıplamaz


# ---------- tam ekran algılama ----------
@pytest.mark.parametrize(("state", "expected"), [
    (ov.QUNS_RUNNING_D3D_FULL_SCREEN, True),
    (ov.QUNS_PRESENTATION_MODE, True),
    (ov.QUNS_BUSY, False),              # kenarlıksız pencere: üstüne çizilebilir
    (ov.QUNS_ACCEPTS_NOTIFICATIONS, False),
    (ov.QUNS_NOT_PRESENT, False),
    (None, False),                      # sorgu başarısız
])
def test_exclusive_fullscreen_detection(state, expected):
    assert exclusive_fullscreen_active(lambda: state) is expected


def test_exclusive_fullscreen_query_errors_are_safe():
    def boom():
        raise OSError("shell32 yok")
    assert exclusive_fullscreen_active(boom) is False


@pytest.mark.skipif(sys.platform == "win32", reason="Windows dışı davranış")
def test_exclusive_fullscreen_false_off_windows():
    assert exclusive_fullscreen_active() is False


# ---------- performans ----------
def test_no_timers_while_hidden(env, qtbot):
    assistant, ctl, _ = env
    assert active_timers(ctl) == [] and not ctl.overlay.animating
    assistant.bus.publish(Topics.VOICE_LEVEL, SimpleNamespace(source="mic", level=0.5))
    assistant.state.transition(S.PROCESSING, "yazı")      # yazılı sohbet
    assistant.state.reset()
    assert active_timers(ctl) == []
    wake_to_listening(assistant)
    assert ctl.overlay.animating
    assert ctl.overlay._frame.interval() >= 1000 // 30       # en fazla 30 fps
    assistant.state.transition(S.PROCESSING, "Haiku 5.5 ile düşünüyorum")
    ctl.overlay._shown_t = ctl.overlay._tab_t = ctl.overlay._color_t = 0   # geçişler bitti
    ctl.overlay._schedule()
    assert ctl.overlay._frame.interval() == 1000 // 15       # düşünürken 15 fps
    ctl.overlay.dismiss()
    ctl._end_session()
    assert active_timers(ctl) == [] and not ctl.overlay.isVisible()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows'ta imleç yoklaması için 10 Hz çalışır")
def test_reduced_motion_is_static(env):
    assistant, ctl, _ = env
    assistant.settings.ui.reduced_motion = True
    wake_to_listening(assistant)
    assert ctl.visible and ctl.overlay.reduced_motion
    assert not ctl.overlay.animating            # halka sabit; yalnızca renk ve metin değişir
    assistant.state.reset()
    ctl._hide_timeout()
    assert not ctl.visible                      # kayma animasyonu yok, hemen gizlenir


# ---------- çizim ----------
def test_wrap_lines_truncates_to_two_lines(qapp):
    configure_application(qapp)
    f = ListeningOverlay(theme.load_fonts())._font("speech")
    assert wrap_lines("Kısa cevap.", f, 400) == ["Kısa cevap."]
    lines = wrap_lines(LONG_REPLY, f, 400)
    assert len(lines) == 2 and lines[1].endswith("…")
    assert wrap_lines("   ", f, 400) == []


VIEWS = [
    OverlayView("wake", "#9FF3FA", "Seni duydum"),
    OverlayView("listening", theme.C.CYAN, "Dinliyorum"),
    OverlayView("thinking", theme.C.MENEKSE, "Not Defteri'ni aç ve yarınki toplantı için bir not başlat ve "
                "başlığı kalın yaz", style="transcript"),
    OverlayView("thinking", theme.C.MENEKSE, "Düşünüyorum", "Haiku 5.5"),
    OverlayView("executing", theme.C.AMBER, "Not Defteri açılıyor"),
    OverlayView("speaking", theme.C.CYAN, LONG_REPLY, style="speech"),
    OverlayView("error", theme.C.KIZIL, "Mikrofona ulaşılamıyor"),
    OverlayView("mic_off", theme.C.SIS, "Mikrofon kapalı", "JARVIS penceresinden açabilirsin"),
    OverlayView("standby", theme.C.CYAN),
]


@pytest.mark.parametrize("view", VIEWS, ids=[f"{v.kind}-{v.style}" for v in VIEWS])
def test_every_state_renders(qapp, view):
    configure_application(qapp)
    w = ListeningOverlay(theme.load_fonts())
    w.set_view(view)
    w.set_hovered(view.kind == "speaking")
    w.present(SCREEN)
    w._shown_t = 0   # iniş animasyonu bitti
    img = w.grab().toImage()
    assert img.width() >= SCREEN.width() and not img.isNull()
    dpr = img.devicePixelRatio()
    tab = w.tab_rect()
    center = img.pixelColor(round(tab.center().x() * dpr), round(min(tab.height() - 4, 30) * dpr))
    assert center.alpha() > 200                         # çıkıntı opak
    assert img.pixelColor(5, round(80 * dpr)).alpha() == 0   # çevresi saydam (tıklama geçer)
    w.dismiss()
    w.deleteLater()


# ---------- ayarlar ve ana pencere ----------
def test_overlay_setting_checkbox_persists(env):
    assistant, _ctl, _ = env
    assert assistant.settings.ui.overlay_enabled is True
    section = StartupSection(assistant, has_tray=False)
    assert section.overlay_box.isChecked()
    section.overlay_box.setChecked(False)
    assert assistant.settings.ui.overlay_enabled is False
    store = SettingsStore(AppPaths.resolve().settings_file)
    assert store.load().ui.overlay_enabled is False


def test_main_window_wires_overlay_and_shuts_it_down(qapp):
    from jarvis.interface.main_window import MainWindow

    configure_application(qapp)
    lp = qasync.QEventLoop(qapp, already_running=False)
    asyncio.set_event_loop(lp)
    store = SettingsStore(AppPaths.resolve().settings_file)
    store.load()
    assistant = Assistant(store, sample_interval_s=0.5)
    window = MainWindow(assistant, theme.load_fonts())
    ctl = window.overlay
    try:
        ctl._fullscreen = lambda: False
        ctl._screen_geometry = lambda: SCREEN
        window.show()
        window.isActiveWindow = lambda: True            # JARVIS penceresi önde
        assistant.state.transition(S.WAKE_DETECTED, "Jarvis")
        assert not ctl.visible
        assistant.state.reset()
        window.isActiveWindow = lambda: False           # başka uygulama önde
        assistant.state.transition(S.WAKE_DETECTED, "Jarvis")
        assert ctl.visible
        assistant.bus.publish(Topics.APP_SHUTDOWN)
        assert ctl._closed and not ctl.visible
        assert not assistant.bus.has_subscribers(Topics.VOICE_TRANSCRIPT)
    finally:
        ctl.shutdown()
        assistant.db.close()
        window.deleteLater()
        lp.close()
        asyncio.set_event_loop(None)


# ---------- uçtan uca: gerçek ses yöneticisi (sahte ses sürücüsü, sahte Claude) ----------
def test_real_voice_conversation_drives_overlay(qapp, tmp_path, monkeypatch):
    import socket

    from tests.test_voice_manager import Harness, until
    from tests.voice_fakes import silence, speech, wake_word

    def refuse(*a, **kw):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("Testte ağ bağlantısı denendi!")
    monkeypatch.setattr(socket, "create_connection", refuse)

    configure_application(qapp)
    reply = "Bugün İstanbul'da hava güneşli. Sıcaklık yirmi derece."
    h = Harness(tmp_path, replies=[reply], stt_texts=["Hey Jarvis bugün hava nasıl"])
    assistant = SimpleNamespace(bus=h.bus, settings=h.store.settings,
                                set_microphone_enabled=lambda on: None)
    ctl = OverlayController(assistant, theme.load_fonts(), fullscreen_check=lambda: False,
                            screen_geometry=lambda: SCREEN)
    seen: list[tuple[str, str]] = []
    original = ctl.overlay.set_view
    ctl.overlay.set_view = lambda v: (seen.append((v.kind, v.text)), original(v))

    async def go():
        await h.start()
        h.say(silence(5) + wake_word(1))
        await until(lambda: h.state.state is S.LISTENING)
        assert ctl.visible
        h.say(speech(12) + silence(12))
        await until(lambda: S.SPEAKING in h.history)
        assert ctl.visible and ctl.overlay.view.kind == "speaking"
        await until(lambda: h.state.state is S.LISTENING and h.history.count(S.LISTENING) >= 2)  # takip
        assert ctl.visible and ctl.overlay.view.kind == "listening"
        h.say(silence(20))                                   # takipte konuşmadı: beklemeye döner
        await until(lambda: h.state.state is S.STANDBY)
        await h.stop()

    try:
        asyncio.run(go())
        kinds = [k for k, _ in seen]
        assert kinds[0] == "wake" and "listening" in kinds and "thinking" in kinds
        assert ("thinking", "bugün hava nasıl") in seen          # söylenen cümle
        assert ("speaking", reply) in seen                       # cevabın kendisi
        assert ctl.overlay.view.kind == "standby" and ctl._hide_timer.isActive()
        assert ctl._hide_timer.interval() == 2500
    finally:
        ctl.shutdown()
        h.db.close()
