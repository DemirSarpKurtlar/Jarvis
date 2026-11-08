"""Uyandırma kelimesini konuşma tanımayla doğrulama ("Carvis" telaffuzu için).

Hazır `hey_jarvis` modeli İngilizce "Jarvis" ile eğitilmiştir. Türkçe "Carvis/Cervis" çoğu zaman
eşiğin altında, ama sıfırdan belirgin biçimde yüksek bir skor üretir. Bu modül:

1. `WakeCandidateTracker`: beklemedeyken kısa bir konuşma parçası (en fazla ~4 sn) boyunca
   uyandırma skoru küçük bir tabanı (`floor`) aşmışsa parçayı aday olarak verir.
2. `match_wake`: adayın konuşma tanıma metninin başında Jarvis'e benzeyen bir kelime var mı
   bakar (Jarvis, Carvis, Cervis, Çarvis, Javis, "car vis"…). Varsa kalan metin doğrudan komut
   olarak kullanılabilir ("Carvis, Chrome'u aç").

Konuşma tanıma yalnızca bu adaylarda çalışır (her konuşmada değil): işlemci yükü küçük kalır.
Gizlilik: aday ses yalnızca yerelde çözülür; logda metin değil yalnızca skor ve sonuç yazar.
"""

from __future__ import annotations

import difflib
import re
from collections import deque
from dataclasses import dataclass

import numpy as np

from jarvis.tools.text import fold
from jarvis.voice.audio_io import FRAME_SAMPLES, SAMPLE_RATE

FRAME_S = FRAME_SAMPLES / SAMPLE_RATE
TARGET = "carvis"                     # katlanmış ve j->c yapılmış "Jarvis"
_LEAD = {"hey", "hi", "hay", "hei", "hej", "ey", "hay", "he", "ha"}
# Jarvis'e çok benzeyen ama gündelik Türkçe kelimeler: uyandırmasın
_NOT_WAKE = {"servis", "serbis", "kervan", "carsi", "garson", "harbi", "carpis", "carsamba"}


def _key(word: str) -> str:
    """Telaffuz anahtarı: Türkçe karakterler ASCII, j/g->c, z->s, w->v, yalnızca harfler."""
    w = fold(word).replace(" ", "")
    w = re.sub(r"[^a-z]", "", w)
    if not w:
        return ""
    w = w.replace("w", "v").replace("z", "s")
    if w[0] in "jgy":
        w = "c" + w[1:]
    w = w.replace("j", "c")
    return w


def _similar(key: str) -> bool:
    if not 5 <= len(key) <= 8 or key in _NOT_WAKE or key[0] not in "cks":
        return False
    if key == TARGET:
        return True
    return difflib.SequenceMatcher(None, key, TARGET).ratio() >= 0.75 and key.endswith(("s", "si", "is", "iz"))


@dataclass(frozen=True)
class WakeMatch:
    word: str      # tanınan biçim (ör. "Carvis")
    rest: str      # uyandırma kelimesinden sonraki metin ("Chrome'u aç"); yoksa ""


def match_wake(text: str) -> WakeMatch | None:
    """Metnin başında (ilk 3 kelime içinde, "hey" gibi ön ekten sonra) Jarvis benzeri kelime var mı?"""
    tokens = re.findall(r"\S+", text or "")
    i = 0
    while i < len(tokens) and i < 2 and _key(tokens[i]) in _LEAD:
        i += 1
    for j in range(i, min(i + 2, len(tokens))):
        candidates = [(tokens[j], j + 1)]
        if j + 1 < len(tokens):
            candidates.append((tokens[j] + tokens[j + 1], j + 2))   # "Car vis", "Jar vis"
        for word, end in candidates:
            if _similar(_key(word)):
                rest = " ".join(tokens[end:]).lstrip(" ,.;:-").strip()
                return WakeMatch(word.strip(" ,.!?;:-"), rest)
        if j > i:   # en fazla bir kelime atlanır ("Tamam Carvis ...")
            break
    return None


@dataclass(frozen=True)
class WakeCandidate:
    audio: np.ndarray       # int16, ön tampon dahil
    peak: float             # parça boyunca en yüksek uyandırma skoru


class WakeCandidateTracker:
    """Ses thread'inde her karede çağrılır (yalnızca uyandırma beklenirken). Kendi kilidi yoktur."""

    def __init__(self, floor: float = 0.03, speech_prob: float = 0.5, end_silence_frames: int = 4,
                 preroll_frames: int = 6, max_segment_s: float = 4.0, min_speech_frames: int = 3) -> None:
        self.floor = floor
        self.speech_prob = speech_prob
        self.end_silence_frames = end_silence_frames
        self.max_frames = int(max_segment_s / FRAME_S)
        self.min_speech_frames = min_speech_frames
        self._pre: deque[np.ndarray] = deque(maxlen=preroll_frames)
        self._seg: list[np.ndarray] = []
        self._speech = 0
        self._silence = 0
        self._peak = 0.0
        self._too_long = False

    def reset(self) -> None:
        self._pre.clear()
        self._end()

    def _end(self) -> None:
        self._seg = []
        self._speech = 0
        self._silence = 0
        self._peak = 0.0
        self._too_long = False

    def feed(self, samples: np.ndarray, prob: float, score: float) -> WakeCandidate | None:
        speech = prob >= self.speech_prob
        if not self._seg:
            if not speech:
                self._pre.append(samples)
                return None
            self._seg = list(self._pre)
            self._pre.clear()
        self._seg.append(samples)
        self._peak = max(self._peak, score)
        if speech:
            self._speech += 1
            self._silence = 0
        else:
            self._silence += 1
        if len(self._seg) > self.max_frames:
            self._too_long = True      # uzun konuşma (oda sohbeti, TV): aday değil, sonuna kadar yut
        if self._silence < self.end_silence_frames:
            return None
        cand = None
        if not self._too_long and self._speech >= self.min_speech_frames and self._peak >= self.floor:
            cand = WakeCandidate(np.concatenate(self._seg), self._peak)
        self._end()
        return cand
