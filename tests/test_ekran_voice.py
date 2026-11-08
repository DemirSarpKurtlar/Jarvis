"""Ses motoruyla birlikte: dikte modu, seçili metni sesli okuma, İngilizce söyleme.

Gerçek Assistant + VoiceManager + ToolService; sahte ses sürücüsü, STT, TTS, klavye ve pano.
Ağ yok: Claude'a giden istekler sahte API'ye, İngilizce ses indirmesi sahte sunucuya gider.
"""

import asyncio
import hashlib
import io
import time
from types import SimpleNamespace

import pytest

from jarvis.config.settings import SettingsStore
from jarvis.core.events import Topics
from jarvis.core.state import AssistantState as S
from jarvis.paths import AppPaths
from jarvis.security.credentials import CredentialStore
from jarvis.tools import registry as regmod
from jarvis.voice import bridge as bridge_mod
from jarvis.voice.audio_io import AudioPlayer, MicrophoneStream
from jarvis.voice.components import VoiceComponents
from jarvis.voice.languages import _voice
from jarvis.voice.models import ModelManager
from jarvis.voice.vad import EnergyVad
from jarvis.voice.voice_manager import MicState, Mode
from jarvis.voice.wake_word import WakeWordDetector
from tests.ekran_fakes import FakeClipboard, FakeKeyboard, ekran_ctx
from tests.fake_api import FakeAPI, text_stream, tool_use_stream
from tests.voice_fakes import FakeBackend, FakeSTT, FakeTTS, MarkerWakeModel, StaticLoader, silence, speech, wake_word

KEY = "sk-ant-api03-" + "V" * 40


async def until(cond, timeout=6.0):  # noqa: ANN001, ANN201
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.01)
    raise AssertionError("koşul zamanında sağlanmadı")


def make(tmp_path, monkeypatch, stt_texts, responses=(), follow_up_s=3.0, with_key=False, **extra):  # noqa: ANN001, ANN003, ANN201
    from jarvis.core.assistant import Assistant

    monkeypatch.setattr(regmod, "CURRENT_PLATFORM", "win32")
    store = SettingsStore(AppPaths.resolve().settings_file)
    store.load()
    v = store.settings.voice
    v.follow_up_s, v.wake_chime, v.no_speech_timeout_s = follow_up_s, False, 2.0
    if with_key:
        CredentialStore().set_anthropic_key(KEY)
    api = FakeAPI(list(responses))
    backend = FakeBackend()
    stt, tts = FakeSTT(list(stt_texts)), FakeTTS(0.1)
    comps = VoiceComponents(vad=EnergyVad(-70), mic_factory=lambda: MicrophoneStream(backend),
                            player=AudioPlayer(backend), wake=WakeWordDetector(MarkerWakeModel(), 0.5, 0.35),
                            stt=stt, tts=tts, stt_label="sahte")
    ctx = ekran_ctx(tmp_path, **extra)
    a = Assistant(store, sample_interval_s=0.5, data_dir=tmp_path / "data", tool_context=ctx,
                  client_factory=lambda key, max_retries=0: api.client(key, max_retries=0),
                  voice_loader=StaticLoader(comps))
    h = SimpleNamespace(a=a, api=api, backend=backend, stt=stt, tts=tts, ctx=ctx, states=[], events=[])
    a.state.add_listener(lambda ch: h.states.append(ch.new))
    for t in (Topics.NOTICE, Topics.VOICE_ERROR, Topics.CHAT_DONE):
        a.bus.subscribe(t, lambda p, t=t: h.events.append((t, p)))
    return h


def claude_requests(h):  # noqa: ANN001, ANN201
    return [r for r in h.api.requests if r["path"] == "/v1/messages"]


async def listening(h):  # noqa: ANN001
    """Ses motoru dinlemede ve yeni söz bekliyor (bir önceki istek bitti)."""
    vm = h.a.voice
    await until(lambda: h.a.state.state is S.LISTENING and vm.processor.mode is Mode.LISTEN and not vm._busy)


def test_dictation_by_voice_types_into_focused_window_without_claude(tmp_path, monkeypatch):
    kb = FakeKeyboard()
    h = make(tmp_path, monkeypatch, ["Dikte modu.", "Merhaba virgül nasılsın soru işareti.", "Dikte bitti."],
             keyboard=kb)
    vm = h.a.voice
    seen_status = []

    async def go():
        await h.a.start()
        await until(lambda: vm.processor is not None and vm.processor.mode is Mode.WAKE)
        h.backend.feed(wake_word(1))
        await until(lambda: h.a.state.state is S.LISTENING)
        h.backend.feed(speech(8) + silence(12))                   # "Dikte modu."
        await until(lambda: vm.dictation is not None)
        await listening(h)
        seen_status.append(vm.status)
        h.backend.feed(speech(8) + silence(12))                   # uyandırma kelimesi gerekmez
        await until(lambda: kb.typed)
        await listening(h)
        h.backend.feed(speech(8) + silence(12))                   # "Dikte bitti."
        await until(lambda: vm.dictation is None and h.a.state.state is S.STANDBY)
        await until(lambda: vm.processor.mode is Mode.WAKE)       # yine uyandırma kelimesi bekleniyor
        h.rows = h.a.db.query("SELECT tool, source, decision, ok FROM audit_log")
        await h.a.shutdown()

    asyncio.run(go())
    assert kb.typed == ["Merhaba, nasılsın?"]
    assert claude_requests(h) == []                                # dikte metni Claude'a gitmedi
    assert seen_status[0].mic is MicState.DICTATION and "dikte bitti" in seen_status[0].detail
    assert h.tts.sentences[0].startswith("Dikte modu açık") and h.tts.sentences[-1] == "Dikte modu kapandı."
    assert [(r["tool"], r["source"], r["ok"]) for r in h.rows] == [("dictation", "ses", 1)]   # onay penceresi yok
    assert len(h.stt.calls) == 3


def test_typed_dictation_start_requires_confirmation(tmp_path, monkeypatch):
    kb = FakeKeyboard()
    h = make(tmp_path, monkeypatch, ["Toplantı saat üçte nokta"], keyboard=kb)
    vm = h.a.voice
    answers = []

    async def go():
        await h.a.start()
        await until(lambda: vm.processor is not None and vm.processor.mode is Mode.WAKE)
        await h.a._handle_text("dikte modu")                      # onay penceresi yok (arayüz yok) -> hayır
        assert vm.dictation is None
        h.a.bus.subscribe(Topics.TOOL_CONFIRM, lambda req: (answers.append(req.prompt), req.future.set_result(True)))
        await h.a._handle_text("dikte modunu aç")
        assert vm.dictation is not None
        await listening(h)                                        # yazılı başlatıldı: uyandırma beklemeden dinler
        h.backend.feed(speech(8) + silence(12))
        await until(lambda: kb.typed)
        await listening(h)
        await h.a._handle_text("dikte bitti")
        await until(lambda: vm.dictation is None and h.a.state.state is S.STANDBY)
        await until(lambda: vm.processor.mode is Mode.WAKE)
        await h.a.shutdown()

    asyncio.run(go())
    assert len(answers) == 1 and "odaktaki pencereye" in answers[0].message
    assert kb.typed == ["Toplantı saat üçte."]
    done = [p.text for t, p in h.events if t == Topics.CHAT_DONE]
    assert done[0] == "Tamam, yapmadım." and done[-1] == "Dikte modu kapandı."
    assert claude_requests(h) == []


def test_mute_and_typing_failure_end_dictation(tmp_path, monkeypatch):
    kb = FakeKeyboard()
    h = make(tmp_path, monkeypatch, ["Dikte modu.", "bir şey"], keyboard=kb)
    vm = h.a.voice

    async def go():
        await h.a.start()
        await until(lambda: vm.processor is not None and vm.processor.mode is Mode.WAKE)
        h.backend.feed(wake_word(1))
        await until(lambda: h.a.state.state is S.LISTENING)
        h.backend.feed(speech(8) + silence(12))
        await until(lambda: vm.dictation is not None)
        await listening(h)
        kb.fail_typing = regmod.ToolError("x", "Odaktaki pencereye yazamadım.")
        h.backend.feed(speech(8) + silence(12))
        await until(lambda: vm.dictation is None)
        await until(lambda: h.a.state.state is S.STANDBY)
        # yeniden aç, sonra mikrofonu kapat
        h.a.bus.subscribe(Topics.TOOL_CONFIRM, lambda req: req.future.set_result(True))
        await h.a._handle_text("dikte modu")
        assert vm.dictation is not None
        h.a.set_microphone_enabled(False)
        await until(lambda: vm.dictation is None and vm.mic is None)
        await h.a.shutdown()

    asyncio.run(go())
    errors = [p for t, p in h.events if t == Topics.VOICE_ERROR]
    notices = [p for t, p in h.events if t == Topics.NOTICE]
    assert any("yazamadım" in e and "Dikte modu kapandı" in e for e in errors)
    assert any("Mikrofon kapandığı için dikte modu kapandı" in n for n in notices)


def test_dictation_does_not_start_without_microphone(tmp_path, monkeypatch):
    h = make(tmp_path, monkeypatch, [], keyboard=FakeKeyboard())

    async def go():
        await h.a.start()
        await until(lambda: h.a.voice.processor is not None)
        h.a.set_microphone_enabled(False)
        await until(lambda: h.a.voice.mic is None)
        h.a.bus.subscribe(Topics.TOOL_CONFIRM, lambda req: req.future.set_result(True))
        await h.a._handle_text("dikte modu")
        await h.a.shutdown()

    asyncio.run(go())
    done = [p.text for t, p in h.events if t == Topics.CHAT_DONE]
    assert "mikrofonun açık olması" in done[-1] and h.a.voice.dictation is None


def test_read_selection_by_voice_is_spoken_once(tmp_path, monkeypatch):
    clip = FakeClipboard()
    kb = FakeKeyboard(clipboard=clip, selection="Bu cümle seçili. İkinci cümle de.")
    h = make(tmp_path, monkeypatch, ["Seçili metni oku."], follow_up_s=0.0, keyboard=kb, clipboard=clip)

    async def go():
        await h.a.start()
        await until(lambda: h.a.voice.processor is not None and h.a.voice.processor.mode is Mode.WAKE)
        h.backend.feed(wake_word(1))
        await until(lambda: h.a.state.state is S.LISTENING)
        h.backend.feed(speech(8) + silence(12))
        await until(lambda: h.tts.sentences and h.a.state.state is S.STANDBY and not h.a.voice._busy)
        await h.a.shutdown()

    asyncio.run(go())
    # Araç seçili metni okudu; "Seçili metni okudum" özeti ikinci kez seslendirilmedi
    assert h.tts.sentences == ["Bu cümle seçili.", "İkinci cümle de."]
    assert clip.text == "eski pano" and kb.copies == [False]
    assert claude_requests(h) == []
    assert any(t == Topics.CHAT_DONE and "okudum" in p.text for t, p in h.events)


class _Resp:
    def __init__(self, data: bytes) -> None:
        self._b, self.headers = io.BytesIO(data), {"Content-Length": str(len(data))}

    def read(self, n: int = -1) -> bytes:
        return self._b.read(n)

    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, *a) -> None:  # noqa: ANN002
        pass


def test_claude_speaks_translation_with_english_voice(tmp_path, monkeypatch):
    """"nasılsın İngilizce nasıl söylenir" (sesle): Claude çevirir, speak_in_language ile İngilizce ses okur;
    Claude'un yazılı cevabı Türkçe sesle tekrar okunmaz."""
    onnx, cfg = b"N" * 4096, b'{"audio": {"sample_rate": 22050}}'
    v = _voice("en", "en_US-test-medium", "İngilizce ses (test)", "test", len(onnx), hashlib.md5(onnx).hexdigest(),
               len(cfg), hashlib.md5(cfg).hexdigest())
    monkeypatch.setattr(bridge_mod, "LANGUAGE_VOICES", {"en": v})
    blobs = {v.onnx.url: onnx, v.config.url: cfg}
    english = FakeTTS(0.1, freq=500.0)
    h = make(tmp_path, monkeypatch, ["Nasılsın İngilizce nasıl söylenir?"], follow_up_s=0.0, with_key=True,
             responses=[tool_use_stream("speak_in_language", {"text": "How are you? I am fine.", "language": "en"}),
                        text_stream(["İngilizcesi: \"How are you?\""])])
    br = h.ctx.extra["voice"]
    br.manager_factory = lambda: ModelManager(tmp_path / "models", opener=lambda req, timeout: _Resp(blobs[req.full_url]))
    br.tts_factory = lambda path: english

    async def go():
        await h.a.start()
        await until(lambda: h.a.voice.processor is not None and h.a.voice.processor.mode is Mode.WAKE)
        h.backend.feed(wake_word(1))
        await until(lambda: h.a.state.state is S.LISTENING)
        h.backend.feed(speech(8) + silence(12))
        await until(lambda: any(t == Topics.CHAT_DONE for t, _ in h.events) and h.a.state.state is S.STANDBY
                    and not h.a.voice._busy)
        await h.a.shutdown()

    asyncio.run(go())
    assert english.sentences == ["How are you?", "I am fine."]
    assert h.tts.sentences == []                     # Türkçe ses İngilizce okumadı, cevap tekrar okunmadı
    reqs = claude_requests(h)
    assert reqs[0]["body"]["messages"][-1]["content"] == "Nasılsın İngilizce nasıl söylenir?"
    assert "speak_in_language" in [t["name"] for t in reqs[0]["body"]["tools"]]
    assert (tmp_path / "models" / "piper" / "en_US-test-medium.onnx").is_file()


def test_speak_external_outside_voice_turn(tmp_path, monkeypatch):
    h = make(tmp_path, monkeypatch, [])
    other = FakeTTS(0.1)

    async def go():
        await h.a.start()
        await until(lambda: h.a.voice.components is not None)
        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(None, h.a.voice.speak_external, "Hello there. How are you?", other)
        empty = await loop.run_in_executor(None, h.a.voice.speak_external, "   ", other)
        await h.a.shutdown()
        return ok, empty

    ok, empty = asyncio.run(go())
    assert ok is True and empty is False
    assert other.sentences == ["Hello there.", "How are you?"] and h.tts.sentences == []
    assert h.a.voice._spoke_in_turn is False         # sesli istek yokken bayrak kalkmaz
    assert h.backend.played_nonzero_samples() > 0


@pytest.mark.parametrize("text", ["ekranımda ne var", "ekrana bak", "bu hatayı açıkla", "nasılsın İngilizcesi ne",
                                  "bunu İngilizce söyle"])
def test_screen_questions_and_translation_go_to_claude(text):
    from jarvis.tools.intents import parse

    assert parse(text) is None


def test_dictation_survives_short_silence_and_closes_after_long_one(tmp_path, monkeypatch):
    from jarvis.voice import voice_manager as vm_mod

    h = make(tmp_path, monkeypatch, [], keyboard=FakeKeyboard())
    vm = h.a.voice

    async def go():
        await h.a.start()
        await until(lambda: vm.processor is not None and vm.processor.mode is Mode.WAKE)
        h.a.bus.subscribe(Topics.TOOL_CONFIRM, lambda req: req.future.set_result(True))
        await h.a._handle_text("yazmaya başla")
        await listening(h)
        epoch = vm.processor.epoch
        h.backend.feed(silence(40))                       # 3,2 sn sessizlik > 2 sn söz bekleme süresi
        await until(lambda: vm.processor.epoch > epoch)   # dinleme yeniden kuruldu
        await listening(h)
        assert vm.dictation is not None and vm.status.mic is MicState.DICTATION
        monkeypatch.setattr(vm_mod, "DICTATION_IDLE_LIMIT_S", 0.0)
        h.backend.feed(silence(40))
        await until(lambda: vm.dictation is None and h.a.state.state is S.STANDBY)
        await until(lambda: vm.processor.mode is Mode.WAKE)
        await h.a.shutdown()

    asyncio.run(go())
    assert any(t == Topics.NOTICE and "dikte modunu kapattım" in p for t, p in h.events)
