"""Ses testleri için sahte parçalar: PortAudio benzeri arka uç, sentetik ses, sahte STT/TTS/uyandırma.

Gerçek kodun (MicrophoneStream, AudioPlayer, FrameProcessor, VoiceManager) olduğu gibi
çalıştırılabilmesi için yalnızca en alttaki ses sürücüsü ve ağır modeller taklit edilir.
"""

from __future__ import annotations

import queue
import threading
import time

import numpy as np

from jarvis.voice.audio_io import FRAME_SAMPLES, SAMPLE_RATE
from jarvis.voice.stt import Transcript

RNG = np.random.default_rng(1234)
WAKE_VALUE = 7777        # sahte uyandırma modelinin tanıdığı "kelime"


# ---------- sentetik ses ----------
def silence(n_frames: int = 1, amp: int = 20) -> list[np.ndarray]:
    return [RNG.integers(-amp, amp, FRAME_SAMPLES).astype(np.int16) for _ in range(n_frames)]


def speech(n_frames: int = 1, amp: int = 4000) -> list[np.ndarray]:
    """Konuşma benzeri: genliği 4 Hz'de dalgalanan geniş bantlı gürültü (~-22 dBFS)."""
    out = []
    for _ in range(n_frames):
        t = np.arange(FRAME_SAMPLES) / SAMPLE_RATE
        env = 0.6 + 0.4 * np.sin(2 * np.pi * 4 * t + RNG.uniform(0, 6.28))
        out.append((RNG.normal(0, amp, FRAME_SAMPLES) * env).clip(-32768, 32767).astype(np.int16))
    return out


def wake_word(n_frames: int = 1) -> list[np.ndarray]:
    return [np.full(FRAME_SAMPLES, WAKE_VALUE, np.int16) for _ in range(n_frames)]


def sine(freq: float, seconds: float, sr: int = SAMPLE_RATE, amp: float = 0.5) -> np.ndarray:
    t = np.arange(int(sr * seconds)) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


# ---------- sahte PortAudio ----------
class FakePortAudioError(Exception):
    pass


class _Default:
    def __init__(self) -> None:
        self.device = [0, 1]


class FakeInputStream:
    def __init__(self, backend: "FakeBackend", samplerate, channels, dtype, blocksize, device, callback) -> None:  # noqa: ANN001
        if samplerate not in backend.input_rates:
            raise FakePortAudioError(f"Invalid sample rate {samplerate}")
        self.backend, self.samplerate, self.blocksize, self.callback = backend, samplerate, blocksize, callback
        self.device = device
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.closed = False
        backend.inputs_opened += 1

    def start(self) -> None:
        self.backend.active_inputs.add(self)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        buf = np.zeros(0, np.int16)
        while not self._stop.is_set():
            try:
                block = self.backend.mic_source.get(timeout=0.02)
            except queue.Empty:
                continue
            buf = np.concatenate((buf, block))
            while len(buf) >= self.blocksize:
                part, buf = buf[:self.blocksize], buf[self.blocksize:]
                self.callback(part.reshape(-1, 1), self.blocksize, None, None)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(1.0)
        self.backend.active_inputs.discard(self)

    def close(self) -> None:
        self.closed = True
        self.backend.active_inputs.discard(self)


class FakeOutputStream:
    """Gerçek zamanın `speed` katı hızında çalan sahte hoparlör. Çalınan her şeyi kaydeder."""

    def __init__(self, backend: "FakeBackend", samplerate, channels, dtype, blocksize, device, callback) -> None:  # noqa: ANN001
        if samplerate not in backend.output_rates:
            raise FakePortAudioError(f"Invalid sample rate {samplerate}")
        self.backend, self.samplerate, self.blocksize, self.callback = backend, samplerate, blocksize, callback
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.aborted = False
        backend.outputs_opened += 1

    def start(self) -> None:
        self.backend.active_outputs.add(self)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        period = self.blocksize / self.samplerate / self.backend.speed
        while not self._stop.is_set():
            out = np.zeros((self.blocksize, 1), np.float32)
            self.callback(out, self.blocksize, None, None)
            if self._stop.is_set():
                break
            with self.backend.lock:
                self.backend.played.append(out[:, 0].copy())
                self.backend.played_rate = self.samplerate
            time.sleep(period)

    def _end(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(1.0)
        self.backend.active_outputs.discard(self)

    def stop(self) -> None:
        self._end()

    def abort(self) -> None:
        self.aborted = True
        self.backend.aborts += 1
        self._end()

    def close(self) -> None:
        self._end()


class FakeBackend:
    """`sounddevice` modülünün kullandığımız kısmı."""

    PortAudioError = FakePortAudioError

    def __init__(self, input_rates=(16000, 48000), output_rates=(16000, 22050, 48000), speed: float = 8.0,
                 with_input: bool = True, with_output: bool = True) -> None:  # noqa: ANN001
        self.input_rates, self.output_rates, self.speed = input_rates, output_rates, speed
        self.default = _Default()
        self.mic_source: queue.Queue[np.ndarray] = queue.Queue()
        self.played: list[np.ndarray] = []
        self.played_rate = None
        self.lock = threading.Lock()
        self.active_inputs: set = set()
        self.active_outputs: set = set()
        self.inputs_opened = self.outputs_opened = self.aborts = 0
        self.unheard = 0
        self.devices = []
        if with_input:
            self.devices.append({"name": "Test Mikrofonu", "hostapi": 0, "max_input_channels": 1,
                                 "max_output_channels": 0, "default_samplerate": 48000.0})
        if with_output:
            self.devices.append({"name": "Test Hoparlörü", "hostapi": 0, "max_input_channels": 0,
                                 "max_output_channels": 2, "default_samplerate": 48000.0})
        self.devices.append({"name": "WASAPI kopyası", "hostapi": 1, "max_input_channels": 2,
                             "max_output_channels": 2, "default_samplerate": 48000.0})
        if not with_input:
            self.default.device = [-1, 0]

    def query_devices(self, device=None, kind=None):  # noqa: ANN001, ANN201
        if device is None and kind is None:
            return list(self.devices)
        if device is None:
            idx = self.default.device[0 if kind == "input" else 1]
            return self.devices[idx]
        return self.devices[device]

    def query_hostapis(self):  # noqa: ANN201
        return [{"name": "MME"}, {"name": "Windows WASAPI"}]

    def InputStream(self, **kw):  # noqa: ANN003, ANN201, N802
        return FakeInputStream(self, **kw)

    def OutputStream(self, **kw):  # noqa: ANN003, ANN201, N802
        return FakeOutputStream(self, **kw)

    # test yardımcıları
    def feed(self, frames: list[np.ndarray]) -> None:
        """Mikrofona ses ver. Gerçek aygıt gibi: akış kapalıyken söylenen hiçbir şey yakalanmaz."""
        if not self.active_inputs:
            self.unheard += len(frames)
            return
        for f in frames:
            self.mic_source.put(f)

    def played_audio(self) -> np.ndarray:
        with self.lock:
            return np.concatenate(self.played) if self.played else np.zeros(0, np.float32)

    def played_nonzero_samples(self) -> int:
        return int(np.count_nonzero(np.abs(self.played_audio()) > 1e-4))


# ---------- sahte modeller ----------
class MarkerWakeModel:
    """Karede WAKE_VALUE varsa yüksek skor verir. Gerçek WakeWordDetector bununla sınanır."""

    labels = ["hey_jarvis"]

    def __init__(self, score: float = 0.95) -> None:
        self.score = score
        self.resets = 0

    def process(self, frame: np.ndarray) -> dict[str, float]:
        return {"hey_jarvis": self.score if np.all(frame == WAKE_VALUE) else 0.0}

    def reset(self) -> None:
        self.resets += 1


class FakeSTT:
    label = "sahte (test)"

    def __init__(self, texts: list[str]) -> None:
        self.texts = list(texts)
        self.calls: list[np.ndarray] = []

    def transcribe(self, audio: np.ndarray) -> Transcript:
        self.calls.append(audio)
        text = self.texts.pop(0) if self.texts else ""
        return Transcript(text, "tr", len(audio) / SAMPLE_RATE, 0.01)


class FakeTTS:
    sample_rate = 22050

    def __init__(self, seconds_per_sentence: float = 0.3, freq: float = 300.0) -> None:
        self.seconds = seconds_per_sentence
        self.freq = freq
        self.sentences: list[str] = []
        self.rates: list[float] = []

    def synthesize(self, sentence: str, speech_rate: float = 1.0) -> np.ndarray:
        self.sentences.append(sentence)
        self.rates.append(speech_rate)
        # Konuşma benzeri: tonlar + gürültü (yankı testinde korelasyon anlamlı olsun)
        n = int(self.sample_rate * self.seconds)
        t = np.arange(n) / self.sample_rate
        sig = 0.4 * np.sin(2 * np.pi * self.freq * t) + 0.2 * RNG.normal(0, 1, n)
        return (0.5 * sig).astype(np.float32)


class StaticLoader:
    """Hazır parçaları döndüren yükleyici (indirme yok)."""

    def __init__(self, components=None, error: Exception | None = None, delay: float = 0.0) -> None:  # noqa: ANN001
        self.components, self.error, self.delay = components, error, delay
        self.calls = 0

    def load(self, settings, progress, cancel, gain):  # noqa: ANN001, ANN201
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.components
