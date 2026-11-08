"""Ses motorunun karar mantığı: uyandırma eşikleri, söz sonu, metin hazırlama, kare işleyici."""

import numpy as np
import pytest

from tests.voice_fakes import MarkerWakeModel, silence, speech, wake_word
from jarvis.config.settings import VoiceSettings
from jarvis.voice.audio_io import FRAME_S, Frame, OutputHistory
from jarvis.voice.echo import EchoGuard
from jarvis.voice.stt import choose, clean_transcript
from jarvis.voice.tts import MORE_ON_SCREEN, length_scale_for, speakable_text, split_sentences
from jarvis.voice.vad import (EndpointConfig, Endpointer, EndpointKind, EnergyVad, measure_noise,
                              speech_threshold_for_noise)
from jarvis.voice.voice_manager import (BargeInEvent, FrameProcessor, Mode, NoSpeechEvent, SpeechStartEvent,
                                        UtteranceEvent, WakeEvent)
from jarvis.voice.wake_word import WakeWordDetector, hey_threshold_from_sensitivity


class ScoreModel:
    """Sıradaki skoru döndüren sahte uyandırma modeli."""

    labels = ["hey_jarvis"]

    def __init__(self, scores):  # noqa: ANN001
        self.scores = list(scores)
        self.resets = 0

    def process(self, frame):  # noqa: ANN001, ANN201
        return {"hey_jarvis": self.scores.pop(0) if self.scores else 0.0}

    def reset(self) -> None:
        self.resets += 1


def run_detector(det, n):  # noqa: ANN001, ANN201
    frame = np.zeros(1280, np.int16)
    return [(i, d) for i in range(n) if (d := det.feed(frame, i * FRAME_S))]


# ---------- uyandırma ----------
def test_sensitivity_maps_to_threshold():
    assert hey_threshold_from_sensitivity(0.5) == pytest.approx(0.5)
    assert hey_threshold_from_sensitivity(1.0) == pytest.approx(0.1)
    assert hey_threshold_from_sensitivity(0.0) == pytest.approx(0.9)
    assert VoiceSettings().wake_words == ["hey_jarvis", "jarvis"]


def test_hey_jarvis_fires_on_single_frame_then_cools_down():
    model = ScoreModel([0.1, 0.7, 0.9, 0.8, 0.0] + [0.0] * 10 + [0.9] * 30)
    det = WakeWordDetector(model, hey_threshold=0.5, jarvis_threshold=None, cooldown_s=1.5)
    hits = run_detector(det, 45)
    # 1. karede uyandı; 1,5 sn (~19 kare) bekleme boyunca skor yüksek olsa da yeniden uyanmadı
    assert [i for i, _ in hits] == [1, 20, 39]
    assert hits[0][1].label == "hey_jarvis" and model.resets == 3


def test_standalone_jarvis_needs_two_frames_over_low_threshold():
    det = WakeWordDetector(ScoreModel([0.4, 0.0, 0.4, 0.42]), hey_threshold=0.5, jarvis_threshold=0.35)
    hits = run_detector(det, 4)
    assert [(i, d.label) for i, d in hits] == [(3, "jarvis")]   # tek kare yetmez, art arda iki kare


def test_jarvis_disabled_when_not_in_wake_words():
    det = WakeWordDetector(ScoreModel([0.4] * 5), hey_threshold=0.5, jarvis_threshold=None)
    assert run_detector(det, 5) == []


def test_custom_model_label_detected():
    class Two(ScoreModel):
        labels = ["hey_jarvis", "benim_jarvisim"]

        def process(self, frame):  # noqa: ANN001, ANN201
            return {"hey_jarvis": 0.0, "benim_jarvisim": self.scores.pop(0) if self.scores else 0.0}

    det = WakeWordDetector(Two([0.0, 0.6]), 0.5, 0.35, custom_labels=("benim_jarvisim",))
    hits = run_detector(det, 2)
    assert hits[0][1].label == "benim_jarvisim"


# ---------- VAD ve söz sonu ----------
def test_energy_vad_and_noise_calibration():
    vad = EnergyVad(noise_floor_dbfs=-60)
    assert vad.frame_prob(silence(1)[0]) < 0.05
    assert vad.frame_prob(speech(1)[0]) > 0.95
    profile = measure_noise(silence(25))
    assert profile.floor_dbfs < -55 and "sessiz" in profile.description
    loud = measure_noise(speech(25, amp=800))
    assert loud.floor_dbfs > -40
    assert speech_threshold_for_noise(0.5, -70) == 0.5
    assert speech_threshold_for_noise(0.5, loud.floor_dbfs) > 0.5


def feed(ep, frames, prob):  # noqa: ANN001, ANN201
    return [e for f in frames if (e := ep.feed(f, prob)) is not None]


def test_endpointer_full_utterance_with_preroll():
    ep = Endpointer(EndpointConfig(silence_timeout_s=0.8, pre_roll_s=0.4))
    assert feed(ep, silence(10), 0.0) == []
    evs = feed(ep, speech(3), 1.0)                    # 0,24 sn konuşma -> başladı
    assert [e.kind for e in evs] == [EndpointKind.SPEECH_START]
    feed(ep, speech(10), 1.0)
    evs = feed(ep, silence(10), 0.0)
    assert [e.kind for e in evs] == [EndpointKind.UTTERANCE]
    dur = evs[0].duration_s
    # 0,4 sn ön pay + 1,04 sn konuşma + 2 karelik sessizlik payı
    assert 1.3 <= dur <= 1.8, dur
    assert ep.done and ep.feed(silence(1)[0], 1.0) is None


def test_endpointer_ignores_short_click_and_times_out():
    ep = Endpointer(EndpointConfig(no_speech_timeout_s=1.0))
    evs = feed(ep, speech(2), 1.0) + feed(ep, silence(20), 0.0)    # 0,16 sn tık
    assert [e.kind for e in evs] == [EndpointKind.NO_SPEECH]


def test_endpointer_max_length():
    ep = Endpointer(EndpointConfig(max_utterance_s=3.0))
    evs = feed(ep, speech(60), 1.0)
    assert [e.kind for e in evs] == [EndpointKind.SPEECH_START, EndpointKind.MAX_LENGTH]
    assert evs[1].duration_s == pytest.approx(3.0, abs=0.5)


def test_endpointer_short_pause_does_not_end_utterance():
    ep = Endpointer(EndpointConfig(silence_timeout_s=0.8))
    evs = feed(ep, speech(5), 1.0) + feed(ep, silence(6), 0.0) + feed(ep, speech(5), 1.0)  # 0,48 sn duraklama
    assert [e.kind for e in evs] == [EndpointKind.SPEECH_START]


# ---------- metin ----------
def test_transcript_cleanup():
    assert clean_transcript("  Hey Jarvis, saat kaç?  ") == "saat kaç?"
    assert clean_transcript("Jarvis bugün hava nasıl") == "bugün hava nasıl"
    assert clean_transcript("Altyazı M.K.") == ""
    assert clean_transcript("İzlediğiniz için teşekkürler.") == ""
    assert clean_transcript(" . ") == ""
    assert clean_transcript("Not Defteri'ni aç") == "Not Defteri'ni aç"


def test_stt_device_choice():
    assert choose("auto", "auto", has_cuda=True).label == "large-v3-turbo (GPU, float16)"
    assert choose("auto", "auto", has_cuda=False).label == "small (CPU, int8)"
    assert choose("auto", "cpu", has_cuda=True).model == "small"
    assert choose("medium", "auto", has_cuda=False).model == "medium"


def test_speakable_text_strips_markdown_and_links():
    md = ("# Başlık\n**Kalın** ve *eğik* metin. [Kaynak](https://ornek.com) burada.\n"
          "- birinci madde\n- ikinci madde\n```python\nprint('x')\n```\nSon: https://a.b/c")
    t = speakable_text(md)
    assert "#" not in t and "*" not in t and "```" not in t and "http" not in t
    assert "Kalın ve eğik metin." in t and "Kaynak burada." in t
    assert "birinci madde." in t and "Kodu ekranda gösterdim." in t and "bağlantı" in t


def test_long_answer_is_cut_at_sentence():
    text = " ".join(f"Bu {i}. cümle oldukça uzun bir açıklama içeriyor." for i in range(40))
    t = speakable_text(text, max_chars=300)
    assert t.endswith(MORE_ON_SCREEN) and len(t) < 360
    assert t.replace(MORE_ON_SCREEN, "").strip().endswith(".")


def test_sentence_split_and_rate():
    s = split_sentences("Merhaba! Nasılsın? Bugün hava güzel. " + "çok, " * 80 + "son.")
    assert s[:3] == ["Merhaba!", "Nasılsın?", "Bugün hava güzel."]
    assert all(len(x) <= 221 for x in s)
    assert length_scale_for(1.0) == 1.0 and length_scale_for(2.0) == 0.5 and length_scale_for(0.5) == 2.0
    assert length_scale_for(5) == 0.5


# ---------- kare işleyici ----------
def make_processor(echo=None, **kw):  # noqa: ANN001, ANN003, ANN201
    events = []
    proc = FrameProcessor(EnergyVad(-70), WakeWordDetector(MarkerWakeModel(), 0.5, 0.35), echo,
                          EndpointConfig(silence_timeout_s=0.8, no_speech_timeout_s=2.0), events.append, **kw)
    return proc, events


def push(proc, frames, t0=0.0):  # noqa: ANN001, ANN201
    for i, f in enumerate(frames):
        proc.process(Frame(f, t0 + i * FRAME_S))


def test_processor_wake_then_utterance():
    proc, events = make_processor()
    proc.set_mode(Mode.WAKE)
    push(proc, silence(5) + speech(5))                # uyanmadan konuşma: yok sayılır
    assert events == []
    push(proc, wake_word(1))
    assert isinstance(events[-1], WakeEvent) and proc.mode is Mode.LISTEN
    push(proc, speech(8) + silence(11))
    kinds = [type(e) for e in events]
    assert kinds == [WakeEvent, SpeechStartEvent, UtteranceEvent]
    assert proc.mode is Mode.PAUSED and events[-1].epoch == proc.epoch
    push(proc, wake_word(1) + speech(10))             # düşünürken mikrofon karar vermez
    assert len(events) == 3


def test_processor_no_speech_and_request_end():
    proc, events = make_processor()
    proc.set_mode(Mode.LISTEN)
    push(proc, silence(30))
    assert isinstance(events[-1], NoSpeechEvent)
    proc.set_mode(Mode.LISTEN)
    push(proc, speech(10))
    proc.request_end()                                # "Konuş"a tekrar basıldı: bekleme, gönder
    push(proc, silence(1))
    assert isinstance(events[-1], UtteranceEvent) and len(events[-1].audio) >= 10 * 1280


def test_processor_barge_in_needs_sustained_loud_speech():
    proc, events = make_processor(barge_frames=3)
    proc.set_mode(Mode.BARGE)
    push(proc, speech(2) + silence(1) + speech(2))     # kesik kesik: kesme yok
    assert events == []
    push(proc, speech(3))
    assert isinstance(events[-1], BargeInEvent) and events[-1].reason == "speech"
    assert proc.mode is Mode.LISTEN and proc.endpointer.in_speech     # sözün başı korunmuş
    assert proc.endpointer.speech_s >= 0.4


def test_processor_does_not_barge_on_own_echo():
    rng = np.random.default_rng(3)
    out = (0.3 * rng.normal(0, 1, 16000 * 3)).astype(np.float32)
    h = OutputHistory()
    for i in range(0, len(out), 1024):
        h.push(out[i:i + 1024], 16000, 50.0 + (i + 1024) / 16000)
    proc, events = make_processor(echo=EchoGuard(h))
    proc.set_mode(Mode.BARGE)
    delay = 1600
    for k in range(5, 30):
        end = k * 1280
        echo = 0.3 * out[end - 1280 - delay:end - delay]
        proc.process(Frame((echo * 32767).astype(np.int16), 50.0 + end / 16000))
    assert events == []                                # yüksek sesli yankı olsa da kesme yok
