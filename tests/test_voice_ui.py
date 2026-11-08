"""Ses motorunun arayüz bağlantıları (ekransız Qt): Konuş düğmesi, mikrofon işareti, indirme bandı,
reaktör seviyesi ve Ayarlar > Ses bölümü."""

import asyncio
import time

import pytest
import qasync

from tests.voice_fakes import FakeBackend, FakeSTT, FakeTTS, MarkerWakeModel, StaticLoader, wake_word
from jarvis.app import configure_application
from jarvis.config.settings import SettingsStore
from jarvis.core.assistant import Assistant
from jarvis.core.events import Topics
from jarvis.core.state import AssistantState as S
from jarvis.interface import theme
from jarvis.interface.main_window import MainWindow
from jarvis.paths import AppPaths
from jarvis.voice.audio_io import AudioDevice, AudioPlayer, MicrophoneStream
from jarvis.voice.components import VoiceComponents
from jarvis.voice.models import DownloadProgress
from jarvis.voice.vad import EnergyVad
from jarvis.voice.voice_manager import MicState, VoiceLevel, VoiceStatus
from jarvis.voice.wake_word import WakeWordDetector


@pytest.fixture
def loop(qapp):
    configure_application(qapp)
    lp = qasync.QEventLoop(qapp, already_running=False)
    asyncio.set_event_loop(lp)
    yield lp
    lp.close()
    asyncio.set_event_loop(None)


def make(loop, loader=None):  # noqa: ANN001, ANN201
    store = SettingsStore(AppPaths.resolve().settings_file)
    store.load()
    assistant = Assistant(store, sample_interval_s=0.5, voice_loader=loader)
    window = MainWindow(assistant, theme.load_fonts())
    return assistant, window


def fake_components(backend: FakeBackend, wake: bool = True) -> VoiceComponents:
    return VoiceComponents(vad=EnergyVad(-70), mic_factory=lambda: MicrophoneStream(backend),
                           player=AudioPlayer(backend), stt=FakeSTT(["merhaba"]), tts=FakeTTS(0.1),
                           wake=WakeWordDetector(MarkerWakeModel(), 0.5, 0.35) if wake else None,
                           stt_label="small (CPU, int8)")


async def until(cond, timeout=5.0):  # noqa: ANN001, ANN201
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("koşul sağlanmadı")


def test_talk_button_and_pill_follow_real_voice_state(qapp, loop):
    backend = FakeBackend()
    a, w = make(loop, StaticLoader(fake_components(backend)))
    w.show()
    assert not w.panel.talk.isEnabled()                     # motor hazır olmadan kapalı

    async def go():
        await a.start()
        await until(lambda: a.voice.status.mic is MicState.WAKE)
        assert w.panel.talk.isEnabled()
        assert w.panel.mic_pill.text() == "Mikrofon: uyandırma bekleniyor"
        w.panel.talk.click()                                 # elle başlat
        assert a.state.state is S.LISTENING
        assert w.panel.mic_pill.text() == "Mikrofon: dinliyor"
        # Reaktör gerçek mikrofon seviyesini alıyor
        a.bus.publish(Topics.VOICE_LEVEL, VoiceLevel("mic", 0.7))
        assert w.panel.reactor._external_level == pytest.approx(0.7)
        a.bus.publish(Topics.VOICE_LEVEL, VoiceLevel("tts", 0.2))   # dinlerken TTS seviyesi yok sayılır
        assert w.panel.reactor._external_level == pytest.approx(0.7)
        w.panel.mic_button.click()                           # tam kapatma
        assert a.state.state is S.STANDBY
        await until(lambda: not backend.active_inputs)
        assert w.panel.mic_pill.text() == "Mikrofon: kapalı" and not w.panel.talk.isEnabled()
        assert w.panel.reactor._external_level is None
        w.panel.mic_button.click()
        await until(lambda: a.voice.status.mic is MicState.WAKE and backend.active_inputs)
        backend.feed(wake_word(1))
        await until(lambda: a.state.state in (S.WAKE_DETECTED, S.LISTENING))
        await a.shutdown()

    loop.run_until_complete(go())
    assert not backend.active_inputs and not backend.active_outputs


def test_voice_disabled_keeps_text_ui_and_explains(qapp, loop):
    a, w = make(loop)    # testlerde JARVIS_VOICE=off

    async def go():
        await a.start()
        await until(lambda: not a.voice.status.starting and a.voice.status.detail.startswith("Ses motoru kapalı"))
        await a.shutdown()

    loop.run_until_complete(go())
    assert not w.panel.talk.isEnabled() and "JARVIS_VOICE" in w.panel.talk.toolTip()
    assert w.panel.mic_pill.text() == "Mikrofon: dinlemiyor"
    assert w.panel.input.isEnabled()


def test_download_progress_banner(qapp, loop):
    a, w = make(loop)
    w.show()
    bus = a.bus
    bus.publish(Topics.VOICE_DOWNLOAD, DownloadProgress("Türkçe ses (dfki, erkek)", 10 * 2**20, 60 * 2**20))
    assert w.panel.banner.isVisible() and "%17" in w.panel.banner.text.text()
    assert w.panel.banner.property("seviye") == "bilgi"
    bus.publish(Topics.VOICE_DOWNLOAD, DownloadProgress("Türkçe ses (dfki, erkek)", 60 * 2**20, 60 * 2**20,
                                                        finished=True))
    assert not w.panel.banner.isVisible()
    # Başka bir hata gösterilirken indirme bandı onun üstüne yazmaz
    w.panel.banner.show_message("Önemli hata", "hata")
    bus.publish(Topics.VOICE_DOWNLOAD, DownloadProgress("konuşma tanıma (small)", 1, 100))
    assert w.panel.banner.text.text() == "Önemli hata"
    w.panel.banner.hide()
    bus.publish(Topics.VOICE_DOWNLOAD, DownloadProgress("konuşma tanıma (small)", 0, None,
                                                        error="internet bağlantısı yok"))
    assert "indirilemedi" in w.panel.banner.text.text() and w.panel.banner.action.text() == "Tekrar dene"


def test_voice_settings_panel(qapp, loop):
    a, w = make(loop)
    panel = w.settings_view.voice_panel
    panel.fill_devices([AudioDevice(0, "USB Mikrofon", "MME", 1, 0, 48000, True, False)],
                       [AudioDevice(1, "Hoparlör", "MME", 0, 2, 48000, False, True)])
    assert panel.input_combo.count() == 2 and panel.input_combo.isEnabled()
    panel.input_combo.setCurrentIndex(1)
    assert a.settings.voice.input_device == "USB Mikrofon"
    assert panel.restart_hint.isVisibleTo(panel)
    panel.rate.setValue(130)
    assert a.settings.voice.speech_rate == pytest.approx(1.3) and panel.rate_label.text() == "1,30 kat"
    panel.solo.setChecked(False)
    assert a.settings.voice.wake_words == ["hey_jarvis"]
    panel.hey.setChecked(False)                     # ikisi birden kapanamaz
    assert a.settings.voice.wake_words == ["hey_jarvis"] and panel.hey.isChecked()
    panel.follow.setValue(0)
    assert a.settings.voice.follow_up_s == 0
    reread = SettingsStore(a.store.path).load().voice
    assert reread.input_device == "USB Mikrofon" and reread.speech_rate == pytest.approx(1.3)
    # Kayıtlı aygıt artık takılı değil: listede "bağlı değil" olarak kalır
    panel.fill_devices([], [], None)
    assert "bağlı değil" in panel.input_combo.currentText()
    panel.show_status(VoiceStatus(MicState.WAKE, True, True, False, "\"Hey Jarvis\" de.", "small (CPU, int8)"))
    assert "small (CPU, int8)" in panel.status.text() and not panel.test_btn.isEnabled()
    a.bus.publish(Topics.VOICE_LEVEL, VoiceLevel("mic", 0.42))
    assert panel.meter.value() == 42
