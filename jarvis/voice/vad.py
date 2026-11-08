"""Konuşma algılama (VAD) ve söz sonu tespiti (endpointing).

* `SileroVad`: faster-whisper paketinin içinde gelen Silero VAD v6 ONNX modeli, akış
  halinde (512 örnek + 64 örnek bağlam, gizli durum korunur). Ayrı indirme gerekmez.
* `EnergyVad`: model yoksa kullanılan basit enerji tabanlı yedek. Ortam gürültüsüne göre
  ayarlanır (kalibrasyon).
* `Endpointer`: kare olasılıklarından "konuşma başladı / bitti / hiç konuşulmadı / çok uzun"
  kararlarını verir ve konuşmanın sesini (öncesindeki kısa payla birlikte) toplar.
"""

from __future__ import annotations

import importlib.util
import logging
import math
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol

import numpy as np

from jarvis.voice.audio_io import FRAME_S, SAMPLE_RATE, rms_dbfs

log = logging.getLogger(__name__)

SILERO_WINDOW = 512
SILERO_CONTEXT = 64


class VadModel(Protocol):
    def frame_prob(self, frame: np.ndarray) -> float: ...
    def reset(self) -> None: ...


def find_silero_model() -> Path | None:
    """faster-whisper'ı içe aktarmadan (ctranslate2 yüklemeden) model dosyasının yerini bulur."""
    spec = importlib.util.find_spec("faster_whisper")
    if spec is None or not spec.submodule_search_locations:
        return None
    for loc in spec.submodule_search_locations:
        for name in ("silero_vad_v6.onnx", "silero_vad.onnx"):
            p = Path(loc) / "assets" / name
            if p.is_file():
                return p
    return None


class SileroVad:
    """Silero VAD v6, akış halinde. `frame_prob` 80 ms'lik karedeki en yüksek konuşma olasılığını verir."""

    def __init__(self, path: Path | None = None) -> None:
        import onnxruntime as ort

        path = path or find_silero_model()
        if path is None:
            raise FileNotFoundError("Silero VAD modeli bulunamadı (faster-whisper kurulu değil)")
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 3
        self._sess = ort.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"])
        names = [i.name for i in self._sess.get_inputs()]
        if names[:3] != ["input", "h", "c"]:
            raise ValueError(f"Beklenmeyen Silero VAD girişleri: {names}")
        self.reset()

    def reset(self) -> None:
        self._h = np.zeros((1, 1, 128), np.float32)
        self._c = np.zeros((1, 1, 128), np.float32)
        self._context = np.zeros(SILERO_CONTEXT, np.float32)
        self._pending = np.zeros(0, np.float32)
        self.last_prob = 0.0

    def window_prob(self, window: np.ndarray) -> float:
        x = np.concatenate((self._context, window.astype(np.float32)))[None, :]
        out, self._h, self._c = self._sess.run(None, {"input": x, "h": self._h, "c": self._c})
        self._context = window[-SILERO_CONTEXT:].astype(np.float32)
        return float(np.asarray(out).reshape(-1)[0])

    def frame_prob(self, frame: np.ndarray) -> float:
        f = frame.astype(np.float32) / 32768.0 if frame.dtype == np.int16 else frame.astype(np.float32)
        buf = np.concatenate((self._pending, f))
        best = None
        while len(buf) >= SILERO_WINDOW:
            p = self.window_prob(buf[:SILERO_WINDOW])
            best = p if best is None else max(best, p)
            buf = buf[SILERO_WINDOW:]
        self._pending = buf
        if best is not None:
            self.last_prob = best
        return self.last_prob


class EnergyVad:
    """Model yoksa: gürültü tabanının `margin_db` üstündeki ses konuşma sayılır (yumuşak geçişli)."""

    def __init__(self, noise_floor_dbfs: float = -55.0, margin_db: float = 12.0, slope_db: float = 3.0) -> None:
        self.noise_floor_dbfs = noise_floor_dbfs
        self.margin_db = margin_db
        self.slope_db = slope_db

    def reset(self) -> None:
        pass

    def frame_prob(self, frame: np.ndarray) -> float:
        db = rms_dbfs(frame)
        z = (db - (self.noise_floor_dbfs + self.margin_db)) / self.slope_db
        return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


# ---------- ortam gürültüsü ----------
@dataclass(frozen=True)
class NoiseProfile:
    floor_dbfs: float     # karelerin ortanca seviyesi
    p90_dbfs: float       # gürültünün tepe seviyesi (yüzde 90)

    @property
    def description(self) -> str:
        if self.floor_dbfs < -55:
            word = "çok sessiz"
        elif self.floor_dbfs < -45:
            word = "sessiz"
        elif self.floor_dbfs < -35:
            word = "biraz gürültülü"
        else:
            word = "gürültülü"
        return f"Ortam {word} (gürültü seviyesi {self.floor_dbfs:.0f} dBFS)."


def measure_noise(frames: list[np.ndarray]) -> NoiseProfile:
    levels = np.array([rms_dbfs(f) for f in frames], np.float32) if frames else np.array([-120.0])
    return NoiseProfile(float(np.median(levels)), float(np.percentile(levels, 90)))


def speech_threshold_for_noise(base: float, noise_floor_dbfs: float | None) -> float:
    """Gürültülü ortamda konuşma eşiğini biraz yükselt (yanlış başlangıçları azaltır)."""
    if noise_floor_dbfs is None or noise_floor_dbfs < -45:
        return base
    return min(0.8, base + (0.1 if noise_floor_dbfs < -35 else 0.2))


# ---------- söz sonu tespiti ----------
class EndpointKind(str, Enum):
    SPEECH_START = "speech_start"
    UTTERANCE = "utterance"        # konuşma bitti (sessizlik)
    MAX_LENGTH = "max_length"      # en uzun süreye ulaşıldı, olduğu kadarı gönderilir
    NO_SPEECH = "no_speech"        # süre doldu, hiç konuşulmadı


@dataclass(frozen=True)
class EndpointEvent:
    kind: EndpointKind
    audio: np.ndarray | None = None   # int16, 16 kHz (UTTERANCE / MAX_LENGTH)

    @property
    def duration_s(self) -> float:
        return 0.0 if self.audio is None else len(self.audio) / SAMPLE_RATE


@dataclass
class EndpointConfig:
    threshold: float = 0.5           # konuşma olasılığı eşiği
    silence_timeout_s: float = 0.8   # bu kadar sessizlik = söz bitti
    max_utterance_s: float = 20.0
    no_speech_timeout_s: float = 6.0
    min_speech_s: float = 0.24       # bundan kısa ses (tık, öksürük) konuşma sayılmaz
    pre_roll_s: float = 0.4          # konuşma başlamadan önceki kısa pay (ilk hece kesilmesin)


@dataclass
class Endpointer:
    config: EndpointConfig = field(default_factory=EndpointConfig)

    def __post_init__(self) -> None:
        self.reset()

    def reset(self, no_speech_timeout_s: float | None = None, preroll: list[np.ndarray] | None = None,
              speech_started: bool = False) -> None:
        self._no_speech_timeout = (no_speech_timeout_s if no_speech_timeout_s is not None
                                   else self.config.no_speech_timeout_s)
        self._elapsed = 0.0
        self._pre: list[np.ndarray] = list(preroll or [])
        self._audio: list[np.ndarray] = []
        self._speech_run = 0.0
        self._silence = 0.0
        self.in_speech = False
        self.done = False
        if speech_started:
            # Sözü kesme: kullanıcı zaten konuşuyor, ön paydaki ses konuşmanın başı
            self.in_speech = True
            self._audio = list(self._pre)
            self._pre = []

    def collected(self) -> np.ndarray | None:
        """Şimdiye kadar toplanan konuşma sesi (konuşma başlamadıysa None)."""
        return np.concatenate(self._audio) if self.in_speech and self._audio else None

    @property
    def speech_s(self) -> float:
        return sum(len(a) for a in self._audio) / SAMPLE_RATE

    def feed(self, frame: np.ndarray, prob: float) -> EndpointEvent | None:
        if self.done:
            return None
        cfg = self.config
        self._elapsed += FRAME_S
        speech = prob >= cfg.threshold
        if not self.in_speech:
            self._pre.append(frame)
            max_pre = max(1, int(round((cfg.pre_roll_s + cfg.min_speech_s) / FRAME_S)))
            if len(self._pre) > max_pre:
                self._pre = self._pre[-max_pre:]
            self._speech_run = self._speech_run + FRAME_S if speech else 0.0
            if self._speech_run >= cfg.min_speech_s - 1e-9:
                self.in_speech = True
                self._audio = list(self._pre)
                self._pre = []
                self._silence = 0.0
                return EndpointEvent(EndpointKind.SPEECH_START)
            if self._elapsed >= self._no_speech_timeout:
                self.done = True
                return EndpointEvent(EndpointKind.NO_SPEECH)
            return None
        self._audio.append(frame)
        self._silence = 0.0 if speech else self._silence + FRAME_S
        if self._silence >= cfg.silence_timeout_s - 1e-9:
            self.done = True
            # Sondaki sessizliğin çoğunu at, kısa bir pay bırak
            keep = len(self._audio) - max(0, int(round(self._silence / FRAME_S)) - 2)
            return EndpointEvent(EndpointKind.UTTERANCE, np.concatenate(self._audio[:keep]))
        if self.speech_s >= cfg.max_utterance_s:
            self.done = True
            return EndpointEvent(EndpointKind.MAX_LENGTH, np.concatenate(self._audio))
        return None
