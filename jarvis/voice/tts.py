"""Konuşma sentezi: Piper (yerel, ücretsiz), Türkçe erkek ses.

Cevap cümle cümle sentezlenir: ilk cümle hazır olur olmaz çalmaya başlanır, gerisi
çalarken üretilir. `speech_rate` (0,5-2,0) Piper'ın `length_scale` değerine çevrilir
(hız 1,25 -> süre 0,8 kat).

Ekrana yazılan cevap Markdown içerebilir; sesli okumadan önce temizlenir (kod blokları
okunmaz, bağlantılar "bağlantı" olarak söylenir). Çok uzun cevapların yalnızca başı okunur.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

import numpy as np

from jarvis.core.errors import JarvisError

log = logging.getLogger(__name__)

MAX_SPOKEN_CHARS = 700
MORE_ON_SCREEN = "Devamını ekranda görebilirsin."


class TTSError(JarvisError):
    pass


class TextToSpeech(Protocol):
    sample_rate: int

    def synthesize(self, sentence: str, speech_rate: float = 1.0) -> np.ndarray: ...


def length_scale_for(speech_rate: float, base: float = 1.0) -> float:
    r = max(0.5, min(2.0, float(speech_rate)))
    return round(base / r, 4)


_CODE_BLOCK = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_MD_LINK = re.compile(r"\[([^\]]+)\]\((?:[^)]+)\)")
_URL = re.compile(r"https?://\S+|www\.\S+")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+", re.MULTILINE)
_EMPHASIS = re.compile(r"(\*\*|__|\*|_|~~)(?=\S)(.+?)(?<=\S)\1")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}.*$", re.MULTILINE)
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+|\n+")


def speakable_text(text: str, max_chars: int = MAX_SPOKEN_CHARS) -> str:
    """Markdown'ı sesli okunacak düz metne çevirir ve gerekirse kısaltır."""
    t = _CODE_BLOCK.sub(" Kodu ekranda gösterdim. ", text)
    t = _INLINE_CODE.sub(r"\1", t)
    t = _MD_LINK.sub(r"\1", t)
    t = _URL.sub("bağlantı", t)
    t = _TABLE_RULE.sub("", t)
    t = _HEADING.sub("", t)
    t = _BULLET.sub("", t)
    for _ in range(2):
        t = _EMPHASIS.sub(r"\2", t)
    t = t.replace("|", ", ")
    # Satır sonları cümle sonu gibi davransın
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    t = " ".join(ln if ln[-1] in ".!?…:;," else ln + "." for ln in lines)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > max_chars:
        cut = t[:max_chars]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        t = (cut[:end + 1] if end > max_chars // 3 else cut.rsplit(" ", 1)[0] + ".") + " " + MORE_ON_SCREEN
    return t


def split_sentences(text: str, max_len: int = 220) -> list[str]:
    """Cümlelere böler; çok uzun cümleleri virgüllerden ikiye ayırır (ilk ses daha erken başlasın)."""
    out: list[str] = []
    for part in _SENTENCE_END.split(text):
        part = part.strip()
        if not part:
            continue
        while len(part) > max_len:
            cut = part.rfind(", ", 0, max_len)
            if cut < max_len // 3:
                cut = part.rfind(" ", 0, max_len)
            if cut <= 0:
                break
            out.append(part[:cut + 1].strip())
            part = part[cut + 1:].strip()
        if part:
            out.append(part)
    return out


class PiperTTS:
    def __init__(self, model_path: Path, use_cuda: bool = False) -> None:
        try:
            from piper import PiperVoice
        except ImportError as e:
            raise TTSError(f"piper-tts yok: {e}", user_message="Konuşma sentezi (Piper) kurulu değil.") from e
        try:
            self.voice = PiperVoice.load(str(model_path), use_cuda=use_cuda)
        except Exception as e:  # noqa: BLE001
            raise TTSError(f"Piper sesi yüklenemedi: {e}",
                           user_message="Türkçe ses modeli yüklenemedi. Ayarlar > Ses bölümünden yeniden indir.") from e
        self.sample_rate = int(self.voice.config.sample_rate)
        self._base_length = float(getattr(self.voice.config, "length_scale", 1.0) or 1.0)
        self._lock = threading.Lock()
        self.model_path = model_path

    def synthesize(self, sentence: str, speech_rate: float = 1.0) -> np.ndarray:
        from piper import SynthesisConfig

        cfg = SynthesisConfig(length_scale=length_scale_for(speech_rate, self._base_length),
                              normalize_audio=True, volume=0.9)
        with self._lock:  # ONNX oturumu aynı anda tek cümle
            parts = [c.audio_float_array for c in self.voice.synthesize(sentence, syn_config=cfg)]
        if not parts:
            return np.zeros(0, np.float32)
        return np.concatenate(parts).astype(np.float32)

    def stream(self, text: str, speech_rate: float = 1.0) -> Iterator[np.ndarray]:
        for s in split_sentences(speakable_text(text)):
            yield self.synthesize(s, speech_rate)
