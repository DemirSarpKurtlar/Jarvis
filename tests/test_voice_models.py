"""Model indirme/önbellek ve gerçek modellerle çalışan testler.

`models` işaretli testler gerçek model dosyası ister; yoksa atlanır (bkz. tests/model_assets.py).
"""

import hashlib
import io
import json
import socket
import threading
import urllib.error

import numpy as np
import pytest

from tests import model_assets
from jarvis.voice.audio_io import FRAME_SAMPLES, StreamResampler, to_int16
from jarvis.voice.models import (OWW_EMBEDDING, OWW_HEY_JARVIS, OWW_MELSPEC, WAKE_FILES, DownloadProgress, ModelError,
                                 ModelFile, ModelManager)


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes, length: int | None = None) -> None:
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data) if length is None else length)}


class FakeOpener:
    def __init__(self, files: dict[str, bytes] | None = None, error: Exception | None = None,
                 lengths: dict[str, int] | None = None) -> None:
        self.files, self.error, self.lengths = files or {}, error, lengths or {}
        self.urls: list[str] = []
        self.user_agents: list[str] = []

    def __call__(self, req, timeout=None):  # noqa: ANN001, ANN204
        self.urls.append(req.full_url)
        self.user_agents.append(req.get_header("User-agent"))
        if self.error is not None:
            raise self.error
        return FakeResponse(self.files[req.full_url], self.lengths.get(req.full_url))


def spec(data: bytes, **kw) -> ModelFile:  # noqa: ANN003
    return ModelFile("t", "deneme/model.onnx", "https://example.invalid/model.onnx", "deneme modeli", **kw)


def test_download_verifies_and_moves_atomically(tmp_path):
    data = b"x" * 700_000
    f = spec(data, size=len(data), sha256=hashlib.sha256(data).hexdigest())
    seen: list[DownloadProgress] = []
    opener = FakeOpener({f.url: data})
    m = ModelManager(tmp_path, opener=opener)
    assert not m.is_ready(f)
    path = m.ensure(f, seen.append)
    assert path.read_bytes() == data and m.is_ready(f)
    assert not list(tmp_path.rglob("*.part"))
    assert seen[-1].finished and seen[-1].done == len(data)
    assert "JARVIS/" in opener.user_agents[0]
    # İkinci kez ağa çıkmaz
    m.ensure(f)
    assert len(opener.urls) == 1


@pytest.mark.parametrize("kind", ["sha256", "md5", "size"])
def test_corrupt_download_is_rejected_and_removed(tmp_path, kind):
    data = b"gercek-olmayan-veri"
    kw = {"sha256": "0" * 64} if kind == "sha256" else {"md5": "0" * 32} if kind == "md5" else {"size": 999}
    f = spec(data, **kw)
    m = ModelManager(tmp_path, opener=FakeOpener({f.url: data}))
    with pytest.raises(ModelError) as ei:
        m.ensure(f)
    msg = ei.value.user_message
    assert ("bozuk" in msg) or ("eksik indi" in msg)
    assert not m.path(f).exists() and not list(tmp_path.rglob("*.part"))


def test_truncated_transfer_detected_by_content_length(tmp_path):
    f = spec(b"")
    m = ModelManager(tmp_path, opener=FakeOpener({f.url: b"abc"}, lengths={f.url: 10}))
    with pytest.raises(ModelError) as ei:
        m.ensure(f)
    assert "eksik" in ei.value.user_message


@pytest.mark.parametrize("error,expected", [
    (urllib.error.URLError("dns"), "internet bağlantısı yok"),
    (socket.timeout("yavaş"), "internet bağlantısı yok"),
    (urllib.error.HTTPError("u", 404, "yok", {}, None), "HTTP 404"),
    (OSError(28, "No space left on device"), "diskte yeterli yer yok"),
])
def test_network_errors_have_turkish_messages(tmp_path, error, expected):
    f = spec(b"")
    seen = []
    m = ModelManager(tmp_path, opener=FakeOpener(error=error))
    with pytest.raises(ModelError) as ei:
        m.ensure(f, seen.append)
    assert expected in ei.value.user_message
    assert seen and seen[-1].error and expected in seen[-1].text()


def test_cancel_stops_download(tmp_path):
    data = b"y" * 2_000_000
    f = spec(data)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ModelError, match="iptal"):
        ModelManager(tmp_path, opener=FakeOpener({f.url: data})).ensure(f, cancel=cancel)


def test_json_config_is_validated(tmp_path):
    f = ModelFile("j", "x/ses.onnx.json", "https://example.invalid/a.json", "ses ayarı", kind="json")
    m = ModelManager(tmp_path, opener=FakeOpener({f.url: b"<html>hata</html>"}))
    with pytest.raises(ModelError) as ei:
        m.ensure(f)
    assert "bozuk" in ei.value.user_message
    m.opener = FakeOpener({f.url: json.dumps({"audio": {"sample_rate": 22050}}).encode()})
    assert json.loads(m.ensure(f).read_text())["audio"]["sample_rate"] == 22050


def test_progress_text_is_turkish():
    p = DownloadProgress("Türkçe ses (dfki, erkek)", 31_457_280, 62_914_560)
    assert p.fraction == pytest.approx(0.5)
    assert "%50" in p.text() and "30 / 60 MB" in p.text() and "ilk açılışta" in p.text()


def test_known_model_urls():
    assert all(f.url.startswith("https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/")
               for f in WAKE_FILES)
    assert all(f.sha256 and f.size for f in WAKE_FILES)
    onnx, cfg = ModelManager().piper_files("tr_TR-dfki-medium")
    assert onnx.url == ("https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/tr/tr_TR/dfki/medium/"
                        "tr_TR-dfki-medium.onnx?download=true")
    assert onnx.md5 and onnx.size == 63201294 and cfg.kind == "json"
    fah, _ = ModelManager().piper_files("tr_TR-fahrettin-medium")
    assert "/tr/tr_TR/fahrettin/medium/" in fah.url
    with pytest.raises(ModelError):
        ModelManager().piper_files("xx")


def test_whisper_download_reports_byte_progress(tmp_path, monkeypatch):
    import huggingface_hub

    class Sib:
        def __init__(self, name, size):  # noqa: ANN001
            self.rfilename, self.size = name, size

    class Info:
        siblings = [Sib("config.json", 10), Sib("model.bin", 1000), Sib("tokenizer.json", 20),
                    Sib("vocabulary.txt", 5), Sib("README.md", 3), Sib(".gitattributes", 1)]

    class FakeApi:
        def model_info(self, repo, files_metadata=False):  # noqa: ANN001, ANN201
            assert repo == "Systran/faster-whisper-small" and files_metadata
            return Info()

    downloaded = []

    def fake_download(repo, filename, local_dir, tqdm_class):  # noqa: ANN001, ANN202
        downloaded.append(filename)
        size = {s.rfilename: s.size for s in Info.siblings}[filename]
        bar = tqdm_class(total=size, initial=0, unit="B", unit_scale=True, desc=filename)
        for _ in range(size // 5):
            bar.update(5)
        bar.close()
        (tmp_path / "whisper" / "small" / filename).write_bytes(b"0" * size)
        return str(tmp_path / "whisper" / "small" / filename)

    monkeypatch.setattr(huggingface_hub, "HfApi", FakeApi)
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", fake_download)
    seen: list[DownloadProgress] = []
    m = ModelManager(tmp_path)
    path = m.ensure_whisper("small", seen.append)
    assert path == tmp_path / "whisper" / "small" and m.whisper_ready("small")
    assert downloaded == ["config.json", "model.bin", "tokenizer.json", "vocabulary.txt"]  # README yok
    assert seen[-1].finished and seen[-1].total == 1035
    assert max(p.done for p in seen) == 1035


def test_whisper_offline_message(tmp_path, monkeypatch):
    import huggingface_hub

    class OfflineApi:
        def model_info(self, *a, **kw):  # noqa: ANN002, ANN003, ANN201
            raise ConnectionError("hf erişilemiyor")

    monkeypatch.setattr(huggingface_hub, "HfApi", OfflineApi)
    with pytest.raises(ModelError) as ei:
        ModelManager(tmp_path).ensure_whisper("small")
    assert "internet bağlantısı yok" in ei.value.user_message


# ---------- gerçek modeller ----------
def _speech_16k(tts, text: str) -> np.ndarray:  # noqa: ANN001
    a = tts.synthesize(text)
    pcm = to_int16(StreamResampler(tts.sample_rate, 16000).process(a) * 0.6)
    pad = np.zeros(16000, np.int16)
    return np.concatenate([pad, pcm, pad])


def _frames(pcm: np.ndarray):  # noqa: ANN202
    for i in range(0, len(pcm) - FRAME_SAMPLES + 1, FRAME_SAMPLES):
        yield pcm[i:i + FRAME_SAMPLES]


@pytest.mark.models
def test_real_openwakeword_files_download_and_load():
    from jarvis.voice.wake_word import OnnxWakeWordModel

    mel, emb, ww = model_assets.need_files(OWW_MELSPEC, OWW_EMBEDDING, OWW_HEY_JARVIS)
    model = OnnxWakeWordModel(mel, emb, {"hey_jarvis": ww})
    scores = [model.process(f)["hey_jarvis"] for f in _frames(np.zeros(16000 * 2, np.int16))]
    assert max(scores) < 0.1   # sessizlikte uyanmaz


@pytest.mark.models
def test_real_hey_jarvis_wakes_and_other_speech_does_not():
    """Piper'ın İngilizce erkek sesiyle gerçek "Hey Jarvis" üretilir ve gerçek modelle algılanır."""
    from jarvis.voice.tts import PiperTTS
    from jarvis.voice.wake_word import OnnxWakeWordModel, WakeWordDetector

    mel, emb, ww = model_assets.need_files(OWW_MELSPEC, OWW_EMBEDDING, OWW_HEY_JARVIS)
    tts = PiperTTS(model_assets.english_voice())
    det = WakeWordDetector(OnnxWakeWordModel(mel, emb, {"hey_jarvis": ww}), 0.5, 0.35)

    def detections(text):  # noqa: ANN001, ANN202
        det.reset()
        return [d for i, f in enumerate(_frames(_speech_16k(tts, text))) if (d := det.feed(f, i * 0.08))]

    hey = detections("Hey Jarvis.")
    assert len(hey) == 1 and hey[0].label == "hey_jarvis" and hey[0].score >= 0.5   # bekleme: tek uyanma
    assert detections("Jarvis.")                                                    # tek başına da
    assert not detections("What time is it in Istanbul right now?")
    assert not detections("Please open the calculator.")


@pytest.mark.models
def test_real_silero_vad_separates_speech_from_silence_and_noise():
    from jarvis.voice.tts import PiperTTS
    from jarvis.voice.vad import SileroVad, find_silero_model

    if find_silero_model() is None:
        pytest.skip("faster-whisper kurulu değil")
    vad = SileroVad()
    pcm = _speech_16k(PiperTTS(model_assets.english_voice()), "This is a test of the voice activity detector.")
    probs = [vad.frame_prob(f) for f in _frames(pcm)]
    assert max(probs[:10]) < 0.3                 # baştaki 1 sn sessizlik
    assert sum(p > 0.5 for p in probs) >= 10     # konuşma bölümü
    vad.reset()
    noise = (np.random.default_rng(0).normal(0, 300, 16000 * 2)).astype(np.int16)
    assert max(vad.frame_prob(f) for f in _frames(noise)) < 0.5   # düz gürültü konuşma değil


def test_silero_vad_ships_with_faster_whisper_and_is_quiet_on_silence():
    from jarvis.voice.vad import SileroVad, find_silero_model

    if find_silero_model() is None:
        pytest.skip("faster-whisper kurulu değil")
    vad = SileroVad()
    assert max(vad.frame_prob(np.zeros(FRAME_SAMPLES, np.int16)) for _ in range(10)) < 0.2


@pytest.mark.models
def test_real_piper_english_voice_sentence_api():
    from jarvis.voice.tts import PiperTTS

    tts = PiperTTS(model_assets.english_voice())
    normal = tts.synthesize("Hello there, this is a speed test.", 1.0)
    fast = tts.synthesize("Hello there, this is a speed test.", 1.5)
    assert tts.sample_rate == 22050 and normal.dtype == np.float32
    assert 0.5 < len(normal) / 22050 < 5
    assert len(fast) < len(normal) * 0.8          # hız ayarı length_scale'e yansıyor
    assert np.max(np.abs(normal)) <= 1.0


@pytest.mark.models
def test_real_turkish_voice_and_whisper_round_trip():
    """Türkçe erkek ses (dfki) cümleyi söyler, faster-whisper geri yazıya çevirir."""
    from jarvis.voice.stt import WhisperSTT, choose
    from jarvis.voice.tts import PiperTTS

    tts = PiperTTS(model_assets.turkish_voice())
    stt = WhisperSTT(model_assets.whisper(), choose(model_assets.whisper().name, "cpu"))
    stt.load()
    pcm = _speech_16k(tts, "Merhaba, bugün hava çok güzel.")
    tr = stt.transcribe(pcm)
    low = tr.text.lower()
    assert "merhaba" in low and ("hava" in low or "güzel" in low), tr.text
