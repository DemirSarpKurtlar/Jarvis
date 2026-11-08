"""Yankı koruması: JARVIS konuşurken hoparlörden mikrofona dönen kendi sesini "kullanıcı konuştu"
sanıp sözünü kesmesin.

Bu tam bir yankı giderici (AEC) değildir; dürüst bir sezgisel yöntemdir:

1. **Enerji kapısı.** Hoparlöre son giden sesin seviyesinden, mikrofona ne kadar yankı
   gelmesi beklendiği tahmin edilir (`coupling_db`, konuşma sırasında kendini ayarlar).
   Mikrofon bunun en az `margin_db` üstündeyse kullanıcı konuşuyor olabilir.
2. **Benzerlik (korelasyon).** Mikrofon karesi, son 0-400 ms içinde çalınan sesle
   normalize çapraz korelasyonla karşılaştırılır. Çok benziyorsa kare yankı sayılır.
3. Sözü kesme ayrıca VAD'ın yükseltilmiş eşiğini art arda birkaç karede istemeye bağlıdır
   (bkz. `voice_manager.FrameProcessor`).

Sınırlar: Oda yankısı ve hoparlörün bozulması korelasyonu düşürür; çok yüksek sesle ve
mikrofona yakın hoparlörle yanlış kesme olabilir. Kulaklık ya da Windows'un yankı engelleme
özelliği olan mikrofon (ör. "iletişim" aygıtı) en iyi sonucu verir.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from jarvis.voice.audio_io import SAMPLE_RATE, OutputHistory, rms_dbfs, to_float


@dataclass(frozen=True)
class EchoAssessment:
    mic_db: float
    output_db: float
    expected_echo_db: float
    correlation: float
    user_likely: bool


def max_normalized_xcorr(mic: np.ndarray, ref: np.ndarray) -> float:
    """`mic`'in `ref` içindeki her konumla normalize çapraz korelasyonunun en büyüğü (|r|)."""
    n, m = len(mic), len(ref)
    if n == 0 or m < n:
        return 0.0
    size = 1 << int(np.ceil(np.log2(n + m)))
    corr = np.fft.irfft(np.fft.rfft(ref, size) * np.conj(np.fft.rfft(mic, size)), size)[: m - n + 1]
    c2 = np.concatenate(([0.0], np.cumsum(ref.astype(np.float64) ** 2)))
    ref_energy = c2[n:] - c2[:-n]
    mic_energy = float(np.sum(mic.astype(np.float64) ** 2))
    denom = np.sqrt(np.maximum(ref_energy, 1e-12) * max(mic_energy, 1e-12))
    valid = ref_energy > 1e-9
    if not np.any(valid):
        return 0.0
    return float(np.max(np.abs(corr[valid]) / denom[valid]))


class EchoGuard:
    def __init__(self, history: OutputHistory, margin_db: float = 6.0, corr_limit: float = 0.5,
                 max_lag_s: float = 0.4, coupling_db: float = -10.0) -> None:
        self.history = history
        self.margin_db = margin_db
        self.corr_limit = corr_limit
        self.max_lag_s = max_lag_s
        self.initial_coupling_db = coupling_db
        self.coupling_db = coupling_db

    def reset(self) -> None:
        self.coupling_db = self.initial_coupling_db

    def assess(self, frame: np.ndarray, t: float) -> EchoAssessment:
        mic = to_float(frame)
        dur = len(mic) / SAMPLE_RATE
        ref = self.history.segment(t, dur + self.max_lag_s)
        mic_db = rms_dbfs(mic)
        out_db = rms_dbfs(ref)
        if out_db < -70.0:
            # Yakında çalınan bir şey yok: yankı olamaz
            return EchoAssessment(mic_db, out_db, -120.0, 0.0, True)
        corr = max_normalized_xcorr(mic, ref)
        observed = mic_db - out_db
        if corr >= self.corr_limit:
            # Kesin yankı: bağlaşımı gözleme doğru hızlıca çek
            self.coupling_db = 0.8 * self.coupling_db + 0.2 * observed
        elif observed < self.coupling_db:
            # Beklenenden sessiz: yankı sandığımızdan zayıf, yavaşça aşağı çek
            self.coupling_db = 0.9 * self.coupling_db + 0.1 * observed
        self.coupling_db = max(-45.0, min(6.0, self.coupling_db))
        expected = out_db + self.coupling_db
        likely = mic_db >= expected + self.margin_db and corr < self.corr_limit
        return EchoAssessment(mic_db, out_db, expected, corr, likely)
