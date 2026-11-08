"""Konuşma tanıma: faster-whisper (yerel, ücretsiz), Türkçe.

Varsayılan seçim ("auto"):
* NVIDIA GPU (CUDA) varsa `large-v3-turbo`, float16.
* Yoksa CPU'da `small`, int8.
CUDA kütüphaneleri eksikse (Windows'ta sık: cuBLAS/cuDNN DLL'leri) model yüklenir ama ilk
çözümlemede hata verir; bu yüzden yüklemeden hemen sonra 1 saniyelik sessizlikle ısınma
yapılır, hata olursa CPU'ya düşülür ve kullanıcıya söylenir.

Bu sınıfın yöntemleri engelleyicidir: her zaman ses thread'inde ya da executor'da çağrılır.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from jarvis.ai.router import tr_lower
from jarvis.core.errors import JarvisError

log = logging.getLogger(__name__)

# Whisper'ın sessizlikte "duyduğu" bilinen Türkçe/İngilizce uydurmalar (altyazı eğitim verisinden)
HALLUCINATIONS = (
    "altyazı", "abone ol", "izlediğiniz için teşekkür", "izlediğiniz için teşekkürler",
    "beğenmeyi unutmayın", "thank you for watching", "subtitles by", "amara.org",
)
WAKE_PREFIX = re.compile(r"^\s*(hey|hi|hay|hei|hej)?[\s,]*(jarvis|cervis|çervis|carvis|jarvıs)[\s,.!?:]*",
                         re.IGNORECASE)


class STTError(JarvisError):
    pass


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str = "tr"
    duration_s: float = 0.0
    seconds: float = 0.0          # çözümleme süresi
    no_speech_prob: float = 0.0
    avg_logprob: float = 0.0


class SpeechToText(Protocol):
    label: str

    def transcribe(self, audio: np.ndarray) -> Transcript: ...


@dataclass(frozen=True)
class SttChoice:
    model: str
    device: str
    compute_type: str

    @property
    def label(self) -> str:
        return f"{self.model} ({'GPU' if self.device == 'cuda' else 'CPU'}, {self.compute_type})"


def cuda_available() -> bool:
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:  # noqa: BLE001
        return False


def choose(model_setting: str = "auto", device_setting: str = "auto", has_cuda: bool | None = None) -> SttChoice:
    cuda = cuda_available() if has_cuda is None else has_cuda
    device = "cuda" if device_setting == "cuda" or (device_setting == "auto" and cuda) else "cpu"
    if model_setting == "auto":
        model = "large-v3-turbo" if device == "cuda" else "small"
    else:
        model = model_setting
    return SttChoice(model, device, "float16" if device == "cuda" else "int8")


def clean_transcript(text: str) -> str:
    """Boşlukları düzeltir, baştaki uyandırma kelimesini ve bilinen uydurmaları atar."""
    t = " ".join(text.split())
    t = WAKE_PREFIX.sub("", t, count=1).strip()
    low = tr_lower(t)
    if any(h in low or h in t.lower() for h in HALLUCINATIONS):
        return ""
    if len(re.sub(r"[\W_]+", "", t)) < 2:   # yalnızca noktalama ya da tek harf
        return ""
    return t


class WhisperSTT:
    def __init__(self, model_dir: Path, choice: SttChoice, beam_size: int = 5, cpu_threads: int = 0) -> None:
        self.model_dir = model_dir
        self.choice = choice
        self.beam_size = beam_size
        self.cpu_threads = cpu_threads
        self._model = None
        self._lock = threading.Lock()

    @property
    def label(self) -> str:
        return self.choice.label

    def load(self) -> None:
        """Modeli yükler ve 1 sn sessizlikle ısındırır (CUDA DLL hataları ancak ilk çözümlemede çıkar)."""
        from faster_whisper import WhisperModel

        try:
            self._model = WhisperModel(str(self.model_dir), device=self.choice.device,
                                       compute_type=self.choice.compute_type, cpu_threads=self.cpu_threads)
            self._warmup()
        except Exception as e:  # noqa: BLE001 - CUDA/cuDNN hataları RuntimeError/OSError olarak gelir
            self._model = None
            raise STTError(f"Whisper yüklenemedi ({self.label}): {e}",
                           user_message="Konuşma tanıma modeli yüklenemedi. Model dosyası bozuk olabilir; "
                                        "Ayarlar > Ses bölümünden modelleri yeniden indir.") from e
        log.info("Konuşma tanıma hazır: %s", self.label)

    def _warmup(self) -> None:
        segments, _ = self._model.transcribe(np.zeros(16000, np.float32), language="tr", beam_size=1,  # type: ignore[union-attr]
                                             without_timestamps=True, condition_on_previous_text=False)
        list(segments)

    def transcribe(self, audio: np.ndarray) -> Transcript:
        if self._model is None:
            raise STTError("model yüklenmedi", user_message="Konuşma tanıma henüz hazır değil.")
        f = audio.astype(np.float32) / 32768.0 if audio.dtype == np.int16 else audio.astype(np.float32)
        t0 = time.perf_counter()
        with self._lock:
            segments, info = self._model.transcribe(
                f, language="tr", task="transcribe", beam_size=self.beam_size, vad_filter=False,
                condition_on_previous_text=False, without_timestamps=True, temperature=(0.0, 0.2, 0.4),
            )
            segs = list(segments)
        text = clean_transcript(" ".join(s.text for s in segs))
        nsp = max((s.no_speech_prob for s in segs), default=1.0)
        alp = float(np.mean([s.avg_logprob for s in segs])) if segs else -10.0
        if segs and nsp > 0.6 and alp < -1.0:
            text = ""   # Whisper: büyük olasılıkla konuşma yok
        return Transcript(text, info.language, len(f) / 16000, time.perf_counter() - t0, nsp, alp)
