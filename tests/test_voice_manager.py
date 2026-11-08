"""Sesli konuşma döngüsü uçtan uca: gerçek VoiceManager, gerçek ChatService (sahte API),
gerçek mikrofon/hoparlör sınıfları (sahte ses sürücüsü), sahte STT/TTS/uyandırma modeli.

Ağ koruması: bu dosyadaki her testte gerçek soket bağlantısı yasak. Claude'a giden tek şey
sahte API'nin kaydettiği JSON gövdesidir; içinde yalnızca tanınan metin olmalı.
"""

import asyncio
import json
import socket
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from tests.fake_api import FakeAPI, text_stream
from tests.voice_fakes import (FakeBackend, FakeSTT, FakeTTS, MarkerWakeModel, StaticLoader, silence, speech,
                               wake_word)
from jarvis.config.settings import SettingsStore
from jarvis.core.chat_service import ChatService
from jarvis.core.events import EventBus, Topics
from jarvis.core.state import AssistantState as S, StateMachine
from jarvis.security.credentials import CredentialStore
from jarvis.storage.database import Database
from jarvis.voice.audio_io import AudioPlayer, AudioUnavailable, MicrophoneStream, OutputHistory, to_int16
from jarvis.voice.components import RealComponentLoader, VoiceComponents
from jarvis.voice.models import ModelError, ModelManager
from jarvis.voice.vad import EnergyVad
from jarvis.voice.voice_manager import MicState, Mode, VoiceManager
from jarvis.voice.wake_word import WakeWordDetector

KEY = "sk-ant-api03-" + "C" * 40


LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _is_loopback(address) -> bool:  # noqa: ANN001
    host = address[0] if isinstance(address, tuple) and address else address
    return isinstance(host, str) and host in LOOPBACK


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Ses testlerinde bilgisayar dışına hiçbir bağlantı kurulamaz.

    Windows'ta asyncio olay döngüsü kendi iç uyandırma mekanizması için 127.0.0.1 üzerinden
    soket çifti (socketpair) açar; bu dışarıya çıkmadığı için izinlidir.
    """
    real_connect = socket.socket.connect

    def guarded_connect(self, address):  # noqa: ANN001, ANN202
        if _is_loopback(address):
            return real_connect(self, address)
        raise AssertionError("Ses testinde ağ bağlantısı denendi!")

    def refuse(*a, **kw):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("Ses testinde ağ bağlantısı denendi!")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "create_connection", refuse)


def test_network_guard_blocks_outside_but_allows_loopback():
    with pytest.raises(AssertionError):
        socket.socket().connect(("8.8.8.8", 53))
    a, b = socket.socketpair()  # Windows'ta 127.0.0.1 üzerinden kurulur
    a.close()
    b.close()


class Harness:
    def __init__(self, tmp_path, replies=(), stt_texts=(), wake=True, tts=True, speed=8.0, tts_seconds=0.3,
                 follow_up_s=1.0, with_key=True):  # noqa: ANN001
        self.store = SettingsStore(tmp_path / "settings.json")
        self.store.load()
        v = self.store.settings.voice
        v.follow_up_s = follow_up_s
        v.no_speech_timeout_s = 2.0
        v.wake_chime = False
        self.bus = EventBus()
        self.state = StateMachine()
        self.history = []
        self.state.add_listener(lambda ch: self.history.append(ch.new))
        self.state.add_listener(lambda ch: self.bus.publish(Topics.STATE_CHANGED, ch))
        self.events: list[tuple[str, object]] = []
        for t in (Topics.VOICE_STATUS, Topics.VOICE_TRANSCRIPT, Topics.VOICE_ERROR, Topics.NOTICE,
                  Topics.CHAT_DONE, Topics.CHAT_FAILED, Topics.CHAT_STARTED):
            self.bus.subscribe(t, lambda p, t=t: self.events.append((t, p)))
        if with_key:
            CredentialStore().set_anthropic_key(KEY)
        self.api = FakeAPI([text_stream([r]) for r in replies])
        self.db = Database(tmp_path / "j.db")
        self.chat = ChatService(self.store, self.bus, self.state, self.db, ThreadPoolExecutor(2),
                                client_factory=lambda key, max_retries=0: self.api.client(key, max_retries=0))
        self.backend = FakeBackend(speed=speed)
        self.history_out = OutputHistory()
        self.player = AudioPlayer(self.backend, history=self.history_out)
        self.stt = FakeSTT(list(stt_texts))
        self.tts = FakeTTS(tts_seconds) if tts else None
        self.mics: list[MicrophoneStream] = []

        def mic_factory():  # noqa: ANN202
            m = MicrophoneStream(self.backend)
            self.mics.append(m)
            return m

        self.wake_model = MarkerWakeModel()
        comps = VoiceComponents(
            vad=EnergyVad(-70), mic_factory=mic_factory, player=self.player,
            wake=WakeWordDetector(self.wake_model, 0.5, 0.35) if wake else None,
            stt=self.stt, tts=self.tts, stt_label="sahte")
        self.loader = StaticLoader(comps)
        self.vm = VoiceManager(self.store, self.bus, self.state, self.chat, self.loader, sleep=lambda s: None)

    def of(self, topic):  # noqa: ANN001, ANN201
        return [p for t, p in self.events if t == topic]

    async def start(self):  # noqa: ANN201
        self.bus.bind_loop(asyncio.get_running_loop())
        await self.chat.start()
        await self.vm.start()
        await until(lambda: self.vm.components is None or not self.vm.components.wake
                    or (self.vm.processor is not None and self.vm.processor.mode is not Mode.PAUSED))

    async def stop(self):  # noqa: ANN201
        await self.vm.shutdown()
        await self.chat.shutdown()

    def say(self, frames):  # noqa: ANN001, ANN201
        self.backend.feed(frames)


async def until(cond, timeout=5.0):  # noqa: ANN001, ANN201
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.01)
    raise AssertionError("koşul zamanında sağlanmadı")


def compact(states):  # noqa: ANN001, ANN201
    out = []
    for s in states:
        if not out or out[-1] is not s:
            out.append(s)
    return out


def test_full_conversation_wake_listen_answer_follow_up(tmp_path):
    h = Harness(tmp_path, replies=["Bugün İstanbul'da hava güneşli. Sıcaklık yirmi derece."],
                stt_texts=["Hey Jarvis bugün hava nasıl"])

    async def go():
        await h.start()
        assert h.vm.status.mic is MicState.WAKE and h.vm.status.ready
        h.say(silence(5) + wake_word(1))
        await until(lambda: h.state.state is S.LISTENING)
        h.say(speech(12) + silence(12))
        await until(lambda: S.SPEAKING in h.history)
        await until(lambda: h.state.state is S.LISTENING and h.history.count(S.LISTENING) >= 2)  # takip
        h.say(silence(20))                                  # takip süresinde (1 sn) konuşmadı
        await until(lambda: h.state.state is S.STANDBY)
        await until(lambda: h.vm.processor.mode is Mode.WAKE)
        await h.stop()

    asyncio.run(go())
    assert compact(h.history) == [S.WAKE_DETECTED, S.LISTENING, S.PROCESSING, S.STANDBY, S.PROCESSING,
                                  S.SPEAKING, S.LISTENING, S.STANDBY]
    # Claude'a giden tek şey temizlenmiş metin; ses verisi yok
    chat_reqs = [r for r in h.api.requests if r["path"] == "/v1/messages"]
    assert len(chat_reqs) == 1
    assert chat_reqs[0]["body"]["messages"] == [{"role": "user", "content": "bugün hava nasıl"}]
    assert len(json.dumps(chat_reqs[0]["body"])) < 20_000
    assert h.of(Topics.VOICE_TRANSCRIPT)[0].text == "bugün hava nasıl"
    assert len(h.stt.calls) == 1 and h.stt.calls[0].dtype == np.int16
    assert h.tts.sentences == ["Bugün İstanbul'da hava güneşli.", "Sıcaklık yirmi derece."]
    assert h.backend.played_nonzero_samples() > 0.5 * 22050
    assert not h.backend.active_inputs and not h.backend.active_outputs    # kapanışta akışlar kapandı


def test_follow_up_question_without_wake_word(tmp_path):
    h = Harness(tmp_path, replies=["Saat on dört otuz.", "Yarın yağmur bekleniyor."],
                stt_texts=["saat kaç", "peki yarın hava nasıl"], follow_up_s=3.0)

    async def go():
        await h.start()
        h.say(wake_word(1))
        await until(lambda: h.state.state is S.LISTENING)
        h.say(speech(8) + silence(12))
        await until(lambda: h.history.count(S.LISTENING) >= 2)     # konuşma modu
        h.say(speech(8) + silence(12))                             # uyandırma kelimesi yok
        await until(lambda: len(h.of(Topics.CHAT_DONE)) == 2)
        await until(lambda: h.history.count(S.LISTENING) >= 3)
        await h.stop()

    asyncio.run(go())
    bodies = [r["body"]["messages"][-1]["content"] for r in h.api.requests if r["path"] == "/v1/messages"]
    assert bodies == ["saat kaç", "peki yarın hava nasıl"]


def test_barge_in_stops_speech_immediately_and_listens(tmp_path):
    long_answer = " ".join(f"Bu uzun cevabın {i}. cümlesi." for i in range(8))
    h = Harness(tmp_path, replies=[long_answer, "Tamam, durdum."], stt_texts=["anlat bakalım", "dur"],
                speed=1.0, tts_seconds=1.0, follow_up_s=1.0)

    async def go():
        await h.start()
        h.say(wake_word(1))
        await until(lambda: h.state.state is S.LISTENING)
        h.say(speech(8) + silence(12))
        await until(lambda: h.state.state is S.SPEAKING and h.backend.played_nonzero_samples() > 4000)
        assert h.vm.processor.mode is Mode.BARGE
        played_before = h.backend.played_nonzero_samples()
        t0 = time.perf_counter()
        h.say(speech(6, amp=9000))                                 # kullanıcı araya girdi
        await until(lambda: h.state.state is S.LISTENING, timeout=2.0)
        reaction = time.perf_counter() - t0
        await until(lambda: not h.backend.active_outputs, timeout=1.0)
        stopped_at = h.backend.played_nonzero_samples()
        await asyncio.sleep(0.3)
        assert h.backend.played_nonzero_samples() == stopped_at    # durdu, tek örnek bile sızmadı
        h.say(silence(12))                                         # "dur" bitti
        await until(lambda: len(h.of(Topics.CHAT_DONE)) == 2)
        await until(lambda: h.state.state in (S.LISTENING, S.STANDBY) and len(h.tts.sentences) > 2)
        await h.stop()
        return reaction, played_before, stopped_at

    reaction, before, stopped = asyncio.run(go())
    assert reaction < 0.8            # 3 kare (240 ms) + işleme
    total = 8 * 22050                # 8 cümle x 1 sn
    assert stopped < total * 0.5     # cevabın çoğu hiç çalınmadı
    # sözü kesen konuşma, ön payıyla birlikte tanımaya gitti
    assert len(h.stt.calls) == 2 and len(h.stt.calls[1]) >= 6 * 1280
    assert h.api.requests[-1]["body"]["messages"][-1]["content"] == "dur"


def test_own_echo_does_not_interrupt(tmp_path):
    h = Harness(tmp_path, replies=["Birinci cümle. İkinci cümle. Üçüncü cümle."], stt_texts=["selam"],
                speed=1.0, tts_seconds=0.6, follow_up_s=0.5)

    async def go():
        await h.start()
        h.say(wake_word(1))
        await until(lambda: h.state.state is S.LISTENING)
        h.say(speech(8) + silence(12))
        await until(lambda: h.state.state is S.SPEAKING)
        # Hoparlörden çıkan ses %35 seviyesiyle, 100 ms gecikmeyle mikrofona dönüyor
        assert h.vm.processor.mode is Mode.BARGE       # sözü kesme dinlemesi açık
        while h.state.state is S.SPEAKING:
            now = time.monotonic()
            echo = 0.35 * h.history_out.segment(now - 0.1, 0.08)
            mic = to_int16(echo + np.random.default_rng().normal(0, 0.001, len(echo)))
            h.say([mic])
            await asyncio.sleep(0.08)
        coupling = h.vm.processor.echo.coupling_db
        await h.stop()
        return coupling

    coupling = asyncio.run(go())
    assert coupling == pytest.approx(-9.1, abs=2.0)      # gerçek yankı seviyesi öğrenildi (0,35 kat = -9,1 dB)
    assert len(h.tts.sentences) == 3                     # sonuna kadar konuştu
    assert len(h.stt.calls) == 1                          # yankı yeni bir istek başlatmadı
    assert S.LISTENING in h.history[h.history.index(S.SPEAKING):]   # bitince takip dinlemesi


def test_manual_talk_button_without_wake_word(tmp_path):
    h = Harness(tmp_path, replies=["Hesap makinesini açamıyorum, henüz bu yeteneğim yok."],
                stt_texts=["hesap makinesini aç"], wake=False, follow_up_s=0.0)

    async def go():
        await h.start()
        assert h.vm.status.mic is MicState.IDLE and "Konuş" in h.vm.status.detail
        h.say(wake_word(2))                                   # model yok: uyanmamalı
        await asyncio.sleep(0.2)
        assert h.state.state is S.STANDBY
        h.vm.activate()                                       # "Konuş" düğmesi
        assert h.state.state is S.LISTENING and h.vm.status.mic is MicState.LISTENING
        h.say(speech(8) + silence(12))
        await until(lambda: S.SPEAKING in h.history)
        await until(lambda: h.state.state is S.STANDBY)       # takip kapalı: beklemeye
        await h.stop()

    asyncio.run(go())
    assert h.api.requests[-1]["body"]["messages"][-1]["content"] == "hesap makinesini aç"


def test_talk_button_while_listening_sends_immediately(tmp_path):
    h = Harness(tmp_path, replies=["Tamam."], stt_texts=["kısa bir soru"], follow_up_s=0.0)

    async def go():
        await h.start()
        h.vm.activate()
        h.say(speech(10))
        await until(lambda: h.vm.processor.endpointer.speech_s > 0.5)
        h.vm.activate()                                       # sessizliği bekleme, şimdi gönder
        h.say(speech(1))
        await until(lambda: len(h.of(Topics.CHAT_DONE)) == 1)
        await h.stop()

    asyncio.run(go())
    assert h.stt.calls and len(h.stt.calls[0]) >= 8 * 1280


def test_mute_closes_input_stream_and_ignores_wake(tmp_path):
    h = Harness(tmp_path)

    async def go():
        await h.start()
        assert len(h.backend.active_inputs) == 1
        h.store.settings.voice.microphone_enabled = False     # Assistant.set_microphone_enabled gibi
        h.bus.publish(Topics.MICROPHONE_TOGGLED, False)
        await until(lambda: not h.backend.active_inputs)
        assert h.vm.mic is None and h.vm.status.mic is MicState.OFF
        h.say(wake_word(3) + speech(10))
        await asyncio.sleep(0.3)
        assert h.state.state is S.STANDBY and h.history == [] and h.backend.unheard == 13
        h.vm.activate()                                       # kapalıyken "Konuş"
        assert any("Mikrofon kapalı" in n for n in h.of(Topics.NOTICE))
        h.store.settings.voice.microphone_enabled = True
        h.bus.publish(Topics.MICROPHONE_TOGGLED, True)
        await until(lambda: len(h.backend.active_inputs) == 1 and h.vm.status.mic is MicState.WAKE)
        await h.stop()

    asyncio.run(go())
    assert h.backend.inputs_opened == 2 and len(h.mics) == 2


def test_mute_while_listening_returns_to_standby(tmp_path):
    h = Harness(tmp_path)

    async def go():
        await h.start()
        h.say(wake_word(1))
        await until(lambda: h.state.state is S.LISTENING)
        h.store.settings.voice.microphone_enabled = False
        h.bus.publish(Topics.MICROPHONE_TOGGLED, False)
        assert h.state.state is S.STANDBY
        await until(lambda: not h.backend.active_inputs)
        await h.stop()

    asyncio.run(go())
    assert h.stt.calls == []


def test_unrecognized_speech_asks_once_then_goes_quiet(tmp_path):
    h = Harness(tmp_path, stt_texts=["", ""], follow_up_s=2.0)

    async def go():
        await h.start()
        h.say(wake_word(1))
        await until(lambda: h.state.state is S.LISTENING)
        h.say(speech(6) + silence(12))
        await until(lambda: h.tts.sentences == ["Anlayamadım, tekrar söyler misin?"])
        await until(lambda: h.history.count(S.LISTENING) >= 2)
        h.say(speech(6) + silence(12))                         # yine anlaşılmadı: döngüye girme
        await until(lambda: len(h.stt.calls) == 2 and h.state.state is S.STANDBY)
        await h.stop()

    asyncio.run(go())
    assert h.api.requests == [] or all(r["path"] != "/v1/messages" for r in h.api.requests)
    assert h.tts.sentences == ["Anlayamadım, tekrar söyler misin?"]


def test_no_key_answer_is_spoken_and_text_chat_unaffected(tmp_path):
    h = Harness(tmp_path, stt_texts=["merhaba"], with_key=False, follow_up_s=0.0)

    async def go():
        await h.start()
        h.say(wake_word(1))
        await until(lambda: h.state.state is S.LISTENING)
        h.say(speech(6) + silence(12))
        await until(lambda: h.tts.sentences and h.state.state is S.STANDBY)
        await h.stop()

    asyncio.run(go())
    assert "anahtarı girilmedi" in " ".join(h.tts.sentences)


def test_wake_ignored_while_text_chat_is_busy(tmp_path):
    h = Harness(tmp_path)

    async def go():
        await h.start()
        h.chat.busy = True
        h.state.transition(S.PROCESSING, "yazılı istek")
        await until(lambda: h.vm.processor.mode is Mode.PAUSED)
        h.say(wake_word(1))
        await asyncio.sleep(0.2)
        assert h.state.state is S.PROCESSING
        h.chat.busy = False
        h.state.reset()
        await until(lambda: h.vm.processor.mode is Mode.WAKE)
        await h.stop()

    asyncio.run(go())
    assert S.WAKE_DETECTED not in h.history


def test_typed_answers_are_not_spoken(tmp_path):
    h = Harness(tmp_path, replies=["Yazılı cevap."])

    async def go():
        await h.start()
        await h.chat.send("yazıyla soruyorum")
        await asyncio.sleep(0.1)
        await h.stop()

    asyncio.run(go())
    assert h.tts.sentences == [] and S.SPEAKING not in h.history


def test_calibration_measures_noise_and_saves(tmp_path):
    h = Harness(tmp_path)

    async def go():
        await h.start()
        task = asyncio.ensure_future(h.vm.calibrate(seconds=0.8))
        await asyncio.sleep(0.05)
        h.say(silence(15, amp=200))
        profile = await task
        await h.stop()
        return profile

    profile = asyncio.run(go())
    assert -70 < profile.floor_dbfs < -40
    reread = SettingsStore(tmp_path / "settings.json").load()
    assert reread.voice.noise_floor_dbfs == pytest.approx(profile.floor_dbfs, abs=0.1)


def test_speak_test_and_speech_rate(tmp_path):
    h = Harness(tmp_path)
    h.store.settings.voice.speech_rate = 1.3

    async def go():
        await h.start()
        await h.vm.speak_test("Deneme bir iki.")
        await h.stop()

    asyncio.run(go())
    assert h.tts.sentences == ["Deneme bir iki."] and h.tts.rates == [1.3]
    assert compact(h.history) == [S.PROCESSING, S.SPEAKING, S.STANDBY]


# ---------- eksik parçalar: ses kapanır, yazılı sohbet sürer ----------
def test_no_audio_system_degrades_gracefully(tmp_path):
    h = Harness(tmp_path, replies=["Yazılı cevap geldi."])
    h.vm.loader = StaticLoader(error=AudioUnavailable("yok", user_message="Mikrofon bulunamadı."))

    async def go():
        await h.start()
        assert not h.vm.ready and h.vm.status.mic is MicState.IDLE
        assert h.vm.status.detail == "Mikrofon bulunamadı."
        h.vm.activate()
        done = await h.chat.send("yazıyla merhaba")
        await h.stop()
        return done

    done = asyncio.run(go())
    assert done.text == "Yazılı cevap geldi."
    assert "Mikrofon bulunamadı." in h.of(Topics.VOICE_ERROR)
    assert "Mikrofon bulunamadı." in h.of(Topics.NOTICE)       # Konuş'a basınca açıklama


def test_real_loader_with_missing_models_keeps_vad_and_reports_turkish(tmp_path, monkeypatch):
    """Gerçek yükleyici: aygıt var ama modeller indirilemiyor (çevrimdışı)."""
    import urllib.error

    def offline(req, timeout=None):  # noqa: ANN001, ANN202
        raise urllib.error.URLError("çevrimdışı")

    def no_whisper(self, name, progress=None, cancel=None):  # noqa: ANN001, ANN202
        raise ModelError("hf", user_message=f"Konuşma tanıma modeli ({name}) indirilemedi: internet bağlantısı yok.")

    monkeypatch.setattr(ModelManager, "ensure_whisper", no_whisper)
    monkeypatch.delenv("JARVIS_VOICE")
    backend = FakeBackend()
    loader = RealComponentLoader(ModelManager(tmp_path / "models", opener=offline), backend_loader=lambda: backend)
    h = Harness(tmp_path)
    h.vm.loader = loader

    async def go():
        await h.start()
        await h.stop()

    asyncio.run(go())
    notes = " | ".join(h.of(Topics.NOTICE))
    assert "Uyandırma kelimesi kullanılamıyor" in notes and "Konuş" in notes
    assert "Sesli yanıt kapalı" in notes
    assert "Sesli komut kapalı" in notes
    assert not h.vm.ready and "indirilemedi" in h.vm.status.detail
    assert not backend.active_inputs                          # hazır değilken mikrofon açılmadı


def test_real_loader_respects_voice_off_env(tmp_path):
    h = Harness(tmp_path)
    h.vm.loader = RealComponentLoader(backend_loader=lambda: pytest.fail("ses sistemine dokunulmamalı"))

    async def go():
        await h.start()
        await h.stop()

    asyncio.run(go())
    assert "JARVIS_VOICE=off" in h.vm.status.detail and h.of(Topics.VOICE_ERROR) == []


def test_real_loader_without_microphone(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_VOICE")
    loader = RealComponentLoader(ModelManager(tmp_path), backend_loader=lambda: FakeBackend(with_input=False))
    with pytest.raises(AudioUnavailable) as ei:
        loader.load(SettingsStore(tmp_path / "s.json").load().voice, lambda p: None, __import__("threading").Event(),
                    lambda: 1.0)
    assert "Mikrofon bulunamadı" in ei.value.user_message


def test_startup_does_not_block_and_shutdown_cancels_slow_loading(tmp_path):
    h = Harness(tmp_path)
    h.vm.loader = StaticLoader(h.loader.components, delay=0.5)

    async def go():
        h.bus.bind_loop(asyncio.get_running_loop())
        t0 = time.perf_counter()
        task = asyncio.ensure_future(h.vm.start())
        await asyncio.sleep(0.05)
        assert time.perf_counter() - t0 < 0.2 and h.vm.status.starting
        await h.vm.shutdown()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())
    assert not h.backend.active_inputs and h.vm.mic is None


# ---------- Assistant ile: sesli komut, yazılı metinle aynı yoldan ----------
def test_voice_local_command_runs_without_claude_and_is_spoken(tmp_path, monkeypatch):
    """"Jarvis, Notepad'i aç" sesle: yerel komut Claude'suz çalışır, sonucu sesli söylenir."""
    from jarvis.core.assistant import Assistant
    from jarvis.paths import AppPaths
    from jarvis.tools import registry as regmod
    from tests.tool_fakes import make_ctx

    monkeypatch.setattr(regmod, "CURRENT_PLATFORM", "win32")
    store = SettingsStore(AppPaths.resolve().settings_file)
    store.load()
    store.settings.voice.follow_up_s = 0.0
    store.settings.voice.wake_chime = False
    api = FakeAPI()
    backend = FakeBackend()
    stt, tts = FakeSTT(["Not defterini aç."]), FakeTTS(0.1)   # Whisper small'ın gerçek çıktısı
    comps = VoiceComponents(vad=EnergyVad(-70), mic_factory=lambda: MicrophoneStream(backend),
                            player=AudioPlayer(backend), wake=WakeWordDetector(MarkerWakeModel(), 0.5, 0.35),
                            stt=stt, tts=tts, stt_label="sahte")
    ctx = make_ctx(tmp_path)
    a = Assistant(store, sample_interval_s=0.5, data_dir=tmp_path / "data", tool_context=ctx,
                  client_factory=lambda key, max_retries=0: api.client(key, max_retries=0),
                  voice_loader=StaticLoader(comps))
    history = []
    a.state.add_listener(lambda ch: history.append(ch.new))

    async def go():
        await a.start()
        await until(lambda: a.voice.processor is not None and a.voice.processor.mode is Mode.WAKE)
        backend.feed(wake_word(1))
        await until(lambda: a.state.state is S.LISTENING)
        backend.feed(speech(8) + silence(12))
        await until(lambda: tts.sentences and a.state.state is S.STANDBY)
        await a.shutdown()

    asyncio.run(go())
    assert ctx.opener.opened == ["notepad.exe"]
    assert [r for r in api.requests if r["path"] == "/v1/messages"] == []    # Claude'a gidilmedi
    assert tts.sentences == ["Not Defteri açılıyor."]
    assert S.EXECUTING in history and S.SPEAKING in history
