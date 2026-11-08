"""Konuşma tanıma: faster-whisper (yerel, ücretsiz), Türkçe.

Varsayılan seçim ("auto"):
* NVIDIA GPU (CUDA) varsa `large-v3-turbo`, float16.
* Yoksa CPU'da `small`, int8.
CUDA kütüphaneleri eksikse (Windows'ta sık: cuBLAS/cuDNN DLL'leri) model yüklenir ama ilk
çözümlemede hata verir; bu yüzden yüklemeden hemen sonra 1 saniyelik sessizlikle ısınma
yapılır, hata olursa CPU'ya düşülür ve kullanıcıya söylenir.

Doğruluk: her çözümlemeye Türkçe bir ipucu (`initial_prompt`) verilir: komut örnekleri ve bu
bilgisayardaki uygulama/oyun adları (bkz. `jarvis.voice.vocabulary`). İpucu yankılanırsa
(Whisper ipucunu aynen tekrarlarsa) aynı ses ipucusuz bir kez daha çözülür.
`JARVIS_STT_PROMPT=off` ipucunu kapatır (karşılaştırma ve sorun giderme için).

Bu sınıfın yöntemleri engelleyicidir: her zaman ses thread'inde ya da executor'da çağrılır.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from jarvis.ai.router import tr_lower
from jarvis.core.errors import JarvisError
from jarvis.voice.vocabulary import PromptCache, VocabularyFn, estimate_tokens, is_prompt_echo

log = logging.getLogger(__name__)

# Whisper'ın sessizlikte "duyduğu" bilinen Türkçe/İngilizce uydurmalar (altyazı eğitim verisinden).
# Yalnızca bu kalıplar: tek başına "altyazı" ya da "abone" geçen geçerli komutlar ("YouTube'da
# altyazıyı aç", "abonelikleri aç") atılmasın.
HALLUCINATIONS = (
    "abone olmayı unutmayın", "abone olun", "kanalıma abone ol", "kanala abone ol",
    "izlediğiniz için teşekkür", "beğenmeyi unutmayın", "thank you for watching", "thanks for watching",
    "subtitles by", "amara.org",
)
# Tek başına "Altyazı", "Altyazı M.K.", "Altyazılar: ..." ya da yalnızca "Abone ol."
_HALLUCINATION_RE = re.compile(r"^\W*(?:alt\s?yaz[ıi](?:lar)?\s*(?:m\.?\s*k\b|by\b|:|\W*$)|abone\s+ol\W*$)",
                               re.IGNORECASE)
WAKE_PREFIX = re.compile(r"^\s*(hey|hi|hay|hei|hej)?[\s,]*(jarvis|jarvıs|cervis|çervis|carvis|carvıs|çarvis|javis|carviz|jervis|garvis|car\s?vis|jar\s?vis)[\s,.!?:]*",
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
    if any(h in low or h in t.lower() for h in HALLUCINATIONS) or _HALLUCINATION_RE.search(low):
        return ""
    if len(re.sub(r"[\W_]+", "", t)) < 2:   # yalnızca noktalama ya da tek harf
        return ""
    return t


SUSPECT_NO_SPEECH = 0.5   # ipuculu çıktıda bunun üstü: ipucusuz doğrulama


def _no_speech_prob(segs: list) -> float:
    return max((s.no_speech_prob for s in segs), default=1.0)


def _looks_silent(segs: list) -> bool:
    return _no_speech_prob(segs) > 0.6 and float(np.mean([s.avg_logprob for s in segs])) < -1.0


def prompt_enabled() -> bool:
    return os.environ.get("JARVIS_STT_PROMPT", "").strip().lower() not in ("off", "0", "kapali", "kapalı")


class WhisperSTT:
    def __init__(self, model_dir: Path, choice: SttChoice, beam_size: int = 5, cpu_threads: int = 0,
                 vocabulary: VocabularyFn | None = None, use_prompt: bool | None = None) -> None:
        self.model_dir = model_dir
        self.choice = choice
        self.beam_size = beam_size
        self.cpu_threads = cpu_threads
        # Ses thread'inde çağrılır: engellememeli (uygulama dizininin önbelleğini okur, taramaz)
        self.vocabulary: VocabularyFn | None = vocabulary
        self.use_prompt = prompt_enabled() if use_prompt is None else use_prompt
        self._prompts = PromptCache()
        self._model = None
        self._lock = threading.Lock()

    def set_vocabulary(self, vocabulary: VocabularyFn | None) -> None:
        """Uygulama/oyun adlarını veren çağrılabilir (ipucuna eklenir). Ses motoru yüklendikten sonra bağlanır."""
        self.vocabulary = vocabulary

    def _count_tokens(self, text: str) -> int:
        tok = getattr(self._model, "hf_tokenizer", None)
        if tok is None:
            return estimate_tokens(text)
        return len(tok.encode(text, add_special_tokens=False).ids)

    def initial_prompt(self) -> str | None:
        if not self.use_prompt:
            return None
        return self._prompts.get(self.vocabulary, self._count_tokens) or None

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
        prompt = self.initial_prompt()
        with self._lock:
            segs, info = self._decode(f, prompt)
            if prompt and segs:
                raw = " ".join(s.text for s in segs)
                if is_prompt_echo(raw, prompt):
                    log.info("Konuşma tanıma ipucunu tekrarladı; ipucusuz yeniden çözülüyor")
                    segs, info = self._decode(f, None)
                elif _no_speech_prob(segs) > SUSPECT_NO_SPEECH:
                    # İpucu, sessizlikte/gürültüde ipucundaki bir komutu "duyurabilir" ("Hesap'ı kapat.").
                    # Konuşma olduğundan şüpheliyse ipucusuz doğrula: orada da konuşma yoksa boş say.
                    check, _ = self._decode(f, None)
                    if not check or _looks_silent(check) or not clean_transcript(" ".join(s.text for s in check)):
                        log.info("İpuculu çözümleme sessizlikte metin üretti; yok sayıldı")
                        segs = []
        text = clean_transcript(" ".join(s.text for s in segs))
        nsp = _no_speech_prob(segs)
        alp = float(np.mean([s.avg_logprob for s in segs])) if segs else -10.0
        if segs and _looks_silent(segs):
            text = ""   # Whisper: büyük olasılıkla konuşma yok
        return Transcript(text, info.language, len(f) / 16000, time.perf_counter() - t0, nsp, alp)

    def transcribe_plain(self, audio: np.ndarray) -> Transcript:
        """İpucusuz çözümleme (uyandırma doğrulaması): ipucundaki "Jarvis" sessizlikte uydurulmasın."""
        if self._model is None:
            raise STTError("model yüklenmedi", user_message="Konuşma tanıma henüz hazır değil.")
        f = audio.astype(np.float32) / 32768.0 if audio.dtype == np.int16 else audio.astype(np.float32)
        t0 = time.perf_counter()
        with self._lock:
            segs, info = self._decode(f, None)
        if segs and _looks_silent(segs):
            segs = []
        text = " ".join(" ".join(s.text for s in segs).split())
        return Transcript(text, info.language, len(f) / 16000, time.perf_counter() - t0,
                          _no_speech_prob(segs) if segs else 1.0)

    def _decode(self, f: np.ndarray, prompt: str | None) -> tuple[list, object]:
        segments, info = self._model.transcribe(  # type: ignore[union-attr]
            f, language="tr", task="transcribe", beam_size=self.beam_size, vad_filter=False,
            condition_on_previous_text=False, without_timestamps=True, temperature=(0.0, 0.2, 0.4),
            initial_prompt=prompt,
        )
        return list(segments), info
