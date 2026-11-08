"""Türkçe telaffuz ("Carvis", "Cervis"): uyandırma modeli eşiğin altında kaldığında kısa konuşma
parçası yerelde yazıya çevrilip doğrulanır. Komut aynı cümledeyse doğrudan işlenir."""

import asyncio

import numpy as np
import pytest

from tests.test_voice_manager import Harness, compact, no_network, until  # noqa: F401  (fixture)
from tests.voice_fakes import silence, speech
from jarvis.core.events import Topics
from jarvis.core.state import AssistantState as S
from jarvis.voice.voice_manager import Mode
from jarvis.voice.wake_verify import WakeCandidateTracker, match_wake


class NearMissModel:
    """Konuşma karelerinde eşiğin altında ama sıfırdan büyük skor (Türkçe "Carvis" gibi)."""

    labels = ["hey_jarvis"]

    def __init__(self, score: float = 0.12) -> None:
        self.score = score

    def process(self, frame: np.ndarray) -> dict[str, float]:
        return {"hey_jarvis": self.score if np.abs(frame.astype(np.int32)).mean() > 500 else 0.0}

    def reset(self) -> None:
        pass


@pytest.mark.parametrize("text, rest", [
    ("Jarvis", ""), ("Carvis, Chrome'u aç.", "Chrome'u aç."), ("Cervis not defterini aç", "not defterini aç"),
    ("Çarvis", ""), ("Hey Jarvis müziği durdur", "müziği durdur"), ("Javis", ""), ("Car vis saat kaç", "saat kaç"),
    ("Jarvıs.", ""), ("Tamam Carvis sesi kıs", "sesi kıs"), ("Carviz", ""), ("Jervis", ""),
])
def test_wake_variants_match(text, rest):
    m = match_wake(text)
    assert m is not None and m.rest == rest


@pytest.mark.parametrize("text", ["servis geldi mi", "Bugün hava nasıl", "Kervan", "çarşı", "Cars", "Ceviz",
                                  "", "Harvey", "Bir şey sorabilir miyim Jarvis"])
def test_ordinary_words_do_not_wake(text):
    assert match_wake(text) is None


def test_tracker_only_short_segments_with_some_wake_score():
    t = WakeCandidateTracker(floor=0.03)
    loud, quiet = speech(1)[0], silence(1)[0]
    out = [t.feed(loud, 0.9, 0.1) for _ in range(8)] + [t.feed(quiet, 0.0, 0.0) for _ in range(4)]
    cands = [c for c in out if c is not None]
    assert len(cands) == 1 and cands[0].peak == pytest.approx(0.1) and len(cands[0].audio) >= 8 * 1280
    # Skor tabanın altında: aday yok
    out = [t.feed(loud, 0.9, 0.001) for _ in range(8)] + [t.feed(quiet, 0.0, 0.0) for _ in range(4)]
    assert not any(out)
    # Uzun konuşma (oda sohbeti, TV): aday yok
    out = [t.feed(loud, 0.9, 0.2) for _ in range(80)] + [t.feed(quiet, 0.0, 0.0) for _ in range(4)]
    assert not any(out)


def near_miss(h: Harness) -> None:
    h.vm.components.wake.model = NearMissModel()


def test_turkish_wake_with_command_in_same_sentence(tmp_path):
    h = Harness(tmp_path, replies=["Saat on iki."], stt_texts=["Carvis, saat kaç?"])

    async def go():
        await h.start()
        near_miss(h)
        h.say(silence(3) + speech(10) + silence(8))
        await until(lambda: h.of(Topics.CHAT_DONE))
        await until(lambda: h.state.state in (S.LISTENING, S.STANDBY) and S.SPEAKING in h.history)
        await h.stop()

    asyncio.run(go())
    assert h.of(Topics.VOICE_TRANSCRIPT)[0].text == "saat kaç?"
    chat = [r for r in h.api.requests if r["path"] == "/v1/messages"]
    assert chat[0]["body"]["messages"] == [{"role": "user", "content": "saat kaç?"}]
    assert compact(h.history)[:3] == [S.WAKE_DETECTED, S.LISTENING, S.PROCESSING]
    assert len(h.stt.calls) == 1                      # yeniden dinleme yok: tek çözümleme


def test_turkish_wake_alone_then_listens(tmp_path):
    h = Harness(tmp_path, replies=["Tamam."], stt_texts=["Cervis.", "müziği durdur"])

    async def go():
        await h.start()
        near_miss(h)
        h.say(silence(3) + speech(6) + silence(8))
        await until(lambda: h.state.state is S.LISTENING)
        h.vm.components.wake.model = NearMissModel(0.0)     # dinlerken skor önemsiz
        h.say(speech(10) + silence(12))
        await until(lambda: h.of(Topics.CHAT_DONE))
        await h.stop()

    asyncio.run(go())
    assert [t.text for t in h.of(Topics.VOICE_TRANSCRIPT)] == ["müziği durdur"]
    assert S.WAKE_DETECTED in h.history


def test_ordinary_talk_does_not_wake(tmp_path):
    h = Harness(tmp_path, stt_texts=["servis ne zaman gelecek"])

    async def go():
        await h.start()
        near_miss(h)
        h.say(silence(3) + speech(10) + silence(8))
        await until(lambda: len(h.stt.calls) == 1)
        await asyncio.sleep(0.3)
        await h.stop()

    asyncio.run(go())
    assert S.WAKE_DETECTED not in h.history and not h.of(Topics.VOICE_TRANSCRIPT)
    assert not [r for r in h.api.requests if r["path"] == "/v1/messages"]


def test_setting_off_disables_verification_live(tmp_path):
    h = Harness(tmp_path, stt_texts=["Carvis"])

    async def go():
        await h.start()
        near_miss(h)
        h.store.settings.voice.wake_verify = False
        h.vm.apply_settings()
        assert h.vm.processor.wake_verify is None
        h.say(silence(3) + speech(10) + silence(8))
        await asyncio.sleep(1.0)
        assert h.vm.processor.mode is Mode.WAKE
        h.store.settings.voice.wake_verify = True
        h.vm.apply_settings()
        assert h.vm.processor.wake_verify is not None
        await h.stop()

    asyncio.run(go())
    assert h.stt.calls == [] and S.WAKE_DETECTED not in h.history
