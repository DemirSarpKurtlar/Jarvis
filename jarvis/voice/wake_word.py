"""Uyandırma kelimesi: openWakeWord ONNX modelleri ile "Hey Jarvis" ve tek başına "Jarvis".

openWakeWord'ün akış (streaming) hattının ONNX ile birebir yeniden yazımıdır
(`openwakeword.utils.AudioFeatures` + `Model.predict`): 80 ms'lik her karede
melspektrogram -> 96 boyutlu gömme -> son 16 gömme ile kelime modeli skoru.

Neden paketin kendisi değil? `openwakeword` 0.6.0 içe aktarılırken scipy ve
scikit-learn'ü (yalnızca eğitim için) yükler, Linux'ta Python 3.12+ için kurulamayan
tflite-runtime'a bağlıdır ve PyInstaller paketine ~150 MB ekler. Çıkarım için gereken
tek şey onnxruntime ve aynı model dosyalarıdır. Eşdeğerlik `tests/test_voice_models.py`
içinde paketle karşılaştırılarak sınanır (paket kuruluysa).

Tek başına "Jarvis": hazır `hey_jarvis` modeli "Jarvis" kelimesinde de (çoğu zaman daha
düşük) skor üretir. Bu yüzden ikinci, daha düşük bir eşik (`jarvis_threshold`) art arda
en az 2 karede aşılırsa da uyanılır. Kullanıcı özel model eğitirse (`custom_wake_model_path`)
o model ek olarak yüklenir.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from jarvis.voice.audio_io import FRAME_SAMPLES

log = logging.getLogger(__name__)

MEL_CONTEXT = 160 * 3          # melspektrogram modelinin kenar payı (örnek)
EMBED_WINDOW = 76              # gömme modeli 76 mel karesi ister
FEATURE_HISTORY = 120          # ~10 sn gömme geçmişi
MEL_HISTORY = 10 * 97          # ~10 sn mel geçmişi
WARMUP_FRAMES = 5              # openWakeWord gibi ilk 5 karede skor 0


class WakeModel(Protocol):
    labels: list[str]

    def process(self, frame: np.ndarray) -> dict[str, float]: ...
    def reset(self) -> None: ...


def _session(path: Path, threads: int = 1):  # noqa: ANN202
    import onnxruntime as ort

    opts = ort.SessionOptions()
    opts.inter_op_num_threads = threads
    opts.intra_op_num_threads = threads
    opts.log_severity_level = 3
    return ort.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"])


class OnnxWakeWordModel:
    """openWakeWord akış hattı. `process()` tam olarak 1280 örneklik (80 ms) int16 kare bekler;
    farklı uzunluklar içeride biriktirilir."""

    def __init__(self, melspec_path: Path, embedding_path: Path, models: Mapping[str, Path],
                 threads: int = 1) -> None:
        self._mel = _session(melspec_path, threads)
        self._emb = _session(embedding_path, threads)
        self._models = {}
        for label, path in models.items():
            sess = _session(path, threads)
            inp = sess.get_inputs()[0]
            n_frames = int(inp.shape[1]) if isinstance(inp.shape[1], int) else 16
            self._models[label] = (sess, inp.name, n_frames)
        self.labels = list(self._models)
        self.reset()

    def reset(self) -> None:
        self._raw = np.zeros(0, np.float32)
        self._pending = np.zeros(0, np.int16)
        self._melbuf = np.ones((EMBED_WINDOW, 32), np.float32)
        # openWakeWord gibi: gömme tamponunu rastgele düşük seviyeli sesle doldur
        rnd = np.random.default_rng(0).integers(-1000, 1000, 16000 * 4).astype(np.float32)
        spec = self._melspec(rnd)
        wins = [spec[i:i + EMBED_WINDOW] for i in range(0, spec.shape[0], 8)
                if spec[i:i + EMBED_WINDOW].shape[0] == EMBED_WINDOW]
        self._features = self._embed(np.array(wins, np.float32))
        self._frames_seen = 0
        self._last = {label: 0.0 for label in self._models}

    def _melspec(self, x: np.ndarray) -> np.ndarray:
        out = self._mel.run(None, {"input": x[None, :].astype(np.float32)})[0]
        return np.squeeze(out) / 10.0 + 2.0

    def _embed(self, windows: np.ndarray) -> np.ndarray:
        out = self._emb.run(None, {"input_1": windows[..., None].astype(np.float32)})[0]
        return out.reshape(out.shape[0], -1)

    def process(self, frame: np.ndarray) -> dict[str, float]:
        if frame.dtype != np.int16:
            raise ValueError("Uyandırma modeli 16 bit PCM (int16) bekler")
        buf = np.concatenate((self._pending, frame))
        n_chunks = len(buf) // FRAME_SAMPLES
        self._pending = buf[n_chunks * FRAME_SAMPLES:]
        scores = dict(self._last)
        for c in range(n_chunks):
            chunk = buf[c * FRAME_SAMPLES:(c + 1) * FRAME_SAMPLES].astype(np.float32)
            self._raw = np.concatenate((self._raw, chunk))[-(FRAME_SAMPLES + MEL_CONTEXT) * 4:]
            mel = self._melspec(self._raw[-(FRAME_SAMPLES + MEL_CONTEXT):])
            self._melbuf = np.vstack((self._melbuf, mel))[-MEL_HISTORY:]
            emb = self._embed(self._melbuf[-EMBED_WINDOW:][None])
            self._features = np.vstack((self._features, emb))[-FEATURE_HISTORY:]
            self._frames_seen += 1
            for label, (sess, name, n) in self._models.items():
                s = float(sess.run(None, {name: self._features[-n:][None].astype(np.float32)})[0].reshape(-1)[0])
                scores[label] = 0.0 if self._frames_seen <= WARMUP_FRAMES else s
        self._last = scores
        return scores


@dataclass(frozen=True)
class WakeDetection:
    label: str        # "hey_jarvis" | "jarvis" | özel modelin adı
    score: float
    model: str        # skoru veren model


def hey_threshold_from_sensitivity(sensitivity: float) -> float:
    """Hassasiyet 0..1 -> eşik 0,9..0,1 (0,5 hassasiyet = openWakeWord'ün varsayılan 0,5 eşiği)."""
    s = max(0.0, min(1.0, sensitivity))
    return round(0.9 - 0.8 * s, 3)


class WakeWordDetector:
    """Model skorlarını eşik, sabır (art arda kare) ve bekleme süresiyle uyandırmaya çevirir.

    * Ana eşik (Hey Jarvis): tek karede aşılması yeter.
    * Düşük eşik (tek başına Jarvis): art arda `low_patience` karede aşılmalı.
    * Uyandıktan sonra `cooldown_s` boyunca yeni uyandırma yok ve modelin tamponu sıfırlanır,
      aynı sözün kuyruğu tekrar tetiklemesin.
    """

    def __init__(self, model: WakeModel, hey_threshold: float = 0.5, jarvis_threshold: float | None = 0.35,
                 hey_enabled: bool = True, custom_labels: tuple[str, ...] = (), low_patience: int = 2,
                 cooldown_s: float = 1.5, base_label: str = "hey_jarvis") -> None:
        self.model = model
        self.hey_threshold = hey_threshold
        self.jarvis_threshold = jarvis_threshold
        self.hey_enabled = hey_enabled
        self.custom_labels = custom_labels
        self.low_patience = max(1, low_patience)
        self.cooldown_s = cooldown_s
        self.base_label = base_label
        self._low_run: dict[str, int] = {}
        self._cooldown_until = 0.0
        self.last_scores: dict[str, float] = {}

    def feed(self, frame: np.ndarray, now: float) -> WakeDetection | None:
        scores = self.model.process(frame)
        self.last_scores = scores
        if now < self._cooldown_until:
            return None
        det = self._decide(scores)
        if det is not None:
            self._cooldown_until = now + self.cooldown_s
            self._low_run.clear()
            self.model.reset()
            log.info("Uyandırma kelimesi algılandı: %s (skor %.2f)", det.label, det.score)
        return det

    def _decide(self, scores: dict[str, float]) -> WakeDetection | None:
        for label in self.custom_labels:
            s = scores.get(label, 0.0)
            if s >= self.hey_threshold:
                return WakeDetection(label, s, label)
            if self.jarvis_threshold is not None and s >= self.jarvis_threshold:
                if self._run(label):
                    return WakeDetection(label, s, label)
            else:
                self._low_run[label] = 0
        s = scores.get(self.base_label, 0.0)
        if self.hey_enabled and s >= self.hey_threshold:
            return WakeDetection("hey_jarvis", s, self.base_label)
        if self.jarvis_threshold is not None and s >= self.jarvis_threshold:
            if self._run(self.base_label):
                return WakeDetection("jarvis", s, self.base_label)
        else:
            self._low_run[self.base_label] = 0
        return None

    def _run(self, label: str) -> bool:
        self._low_run[label] = self._low_run.get(label, 0) + 1
        return self._low_run[label] >= self.low_patience

    def reset(self) -> None:
        self._low_run.clear()
        self._cooldown_until = 0.0
        self.model.reset()
