"""Ses motorunun parçalarını hazırlar: aygıtlar, modellerin indirilmesi ve yüklenmesi.

Her parça ayrı ayrı bozulabilir; motor elindekiyle çalışır ve eksikleri Türkçe not eder:
* Uyandırma kelimesi yoksa: "Konuş" düğmesi çalışır.
* Konuşma sentezi yoksa: cevaplar yalnızca ekranda görünür.
* Konuşma tanıma ya da mikrofon yoksa: sesli komut verilemez, yazılı sohbet sürer.

`load()` engelleyicidir (indirme, model yükleme); her zaman arka plan thread'inde çağrılır.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from jarvis.config.settings import VoiceSettings
from jarvis.core.errors import JarvisError
from jarvis.voice.audio_io import (AudioPlayer, AudioUnavailable, MicrophoneStream, OutputHistory, find_device,
                                   list_devices, load_backend)
from jarvis.voice.models import WAKE_FILES, ModelError, ModelManager, OWW_EMBEDDING, OWW_HEY_JARVIS, OWW_MELSPEC, ProgressFn
from jarvis.voice.stt import SpeechToText, STTError, WhisperSTT, choose
from jarvis.voice.tts import PiperTTS, TextToSpeech
from jarvis.voice.vad import EnergyVad, SileroVad, VadModel
from jarvis.voice.wake_word import OnnxWakeWordModel, WakeWordDetector, hey_threshold_from_sensitivity

log = logging.getLogger(__name__)


class VoiceDisabled(JarvisError):
    pass


@dataclass
class VoiceComponents:
    vad: VadModel
    mic_factory: Callable[[], MicrophoneStream]
    player: AudioPlayer | None
    wake: WakeWordDetector | None = None
    stt: SpeechToText | None = None
    tts: TextToSpeech | None = None
    notes: list[str] = field(default_factory=list)
    stt_label: str = ""


class ComponentLoader(Protocol):
    def load(self, settings: VoiceSettings, progress: ProgressFn, cancel: threading.Event,
             gain: Callable[[], float]) -> VoiceComponents: ...


def build_wake_detector(settings: VoiceSettings, manager: ModelManager, progress: ProgressFn | None = None,
                        cancel: threading.Event | None = None) -> WakeWordDetector:
    words = set(settings.wake_words)
    manager.ensure_all(list(WAKE_FILES), progress, cancel, label="uyandırma kelimesi")
    models = {"hey_jarvis": manager.path(OWW_HEY_JARVIS)}
    custom: tuple[str, ...] = ()
    if settings.custom_wake_model_path:
        p = Path(settings.custom_wake_model_path).expanduser()
        if not p.is_file() or p.suffix.lower() != ".onnx":
            raise ModelError(f"özel model yok: {p}",
                             user_message=f"Özel uyandırma modeli bulunamadı ya da .onnx değil: {p}")
        label = p.stem
        models[label] = p
        custom = (label,)
    model = OnnxWakeWordModel(manager.path(OWW_MELSPEC), manager.path(OWW_EMBEDDING), models)
    return WakeWordDetector(
        model,
        hey_threshold=hey_threshold_from_sensitivity(settings.wake_sensitivity),
        jarvis_threshold=settings.jarvis_threshold if ("jarvis" in words or custom) else None,
        hey_enabled="hey_jarvis" in words or not words,
        custom_labels=custom,
    )


class RealComponentLoader:
    def __init__(self, manager: ModelManager | None = None, backend_loader: Callable[[], Any] = load_backend) -> None:
        self.manager = manager
        self.backend_loader = backend_loader

    def load(self, settings: VoiceSettings, progress: ProgressFn, cancel: threading.Event,
             gain: Callable[[], float]) -> VoiceComponents:
        if os.environ.get("JARVIS_VOICE", "").lower() in ("off", "0", "kapali", "kapalı"):
            raise VoiceDisabled("JARVIS_VOICE=off", user_message="Ses motoru kapalı (JARVIS_VOICE=off).")
        manager = self.manager or ModelManager()
        backend = self.backend_loader()
        inputs = list_devices(backend, "input")
        if not inputs:
            raise AudioUnavailable("giriş aygıtı yok", user_message="Mikrofon bulunamadı. Bir mikrofon takıp "
                                   "Ayarlar > Ses bölümünden 'Yeniden başlat'a bas. Yazılı sohbet çalışıyor.")
        notes: list[str] = []
        in_dev = find_device(inputs, settings.input_device)
        if settings.input_device and in_dev is None:
            notes.append(f"Seçili mikrofon ({settings.input_device}) bulunamadı, varsayılan kullanılıyor.")
        outputs = list_devices(backend, "output")
        out_dev = find_device(outputs, settings.output_device)
        if settings.output_device and out_dev is None:
            notes.append(f"Seçili hoparlör ({settings.output_device}) bulunamadı, varsayılan kullanılıyor.")
        history = OutputHistory()
        player = AudioPlayer(backend, out_dev, gain=gain, history=history) if outputs else None
        if player is None:
            notes.append("Hoparlör bulunamadı; cevaplar yalnızca ekranda.")

        # 1) Konuşma algılama: faster-whisper ile gelir, indirme yok
        try:
            vad: VadModel = SileroVad()
        except Exception as e:  # noqa: BLE001
            log.warning("Silero VAD yüklenemedi, enerji tabanlı yedek kullanılıyor: %s", e)
            vad = EnergyVad(settings.noise_floor_dbfs if settings.noise_floor_dbfs is not None else -55.0)
            notes.append("Gelişmiş konuşma algılama yüklenemedi; basit ses seviyesi algılama kullanılıyor.")

        # 2) Uyandırma kelimesi (küçük, ~3,7 MB)
        wake = None
        try:
            wake = build_wake_detector(settings, manager, progress, cancel)
        except Exception as e:  # noqa: BLE001
            msg = e.user_message if isinstance(e, JarvisError) else f"{type(e).__name__}: {e}"
            log.warning("Uyandırma kelimesi hazırlanamadı: %s", e)
            notes.append(f"Uyandırma kelimesi kullanılamıyor ({msg}). 'Konuş' düğmesini kullanabilirsin.")
        self._check_cancel(cancel)

        # 3) Konuşma sentezi (~63 MB)
        tts = None
        if player is not None:
            try:
                tts = PiperTTS(manager.ensure_piper(settings.tts_voice, progress, cancel))
            except Exception as e:  # noqa: BLE001
                msg = e.user_message if isinstance(e, JarvisError) else f"{type(e).__name__}: {e}"
                log.warning("Konuşma sentezi hazırlanamadı: %s", e)
                notes.append(f"Sesli yanıt kapalı: {msg}")
        self._check_cancel(cancel)

        # 4) Konuşma tanıma (small ~460 MB, large-v3-turbo ~1,6 GB)
        stt, label = self._load_stt(settings, manager, progress, cancel, notes)
        mic_factory = lambda: MicrophoneStream(backend, in_dev)  # noqa: E731
        return VoiceComponents(vad, mic_factory, player, wake, stt, tts, notes, label)

    @staticmethod
    def _check_cancel(cancel: threading.Event) -> None:
        if cancel.is_set():
            raise VoiceDisabled("iptal", user_message="Ses motoru başlatılması iptal edildi.")

    def _load_stt(self, settings: VoiceSettings, manager: ModelManager, progress: ProgressFn,
                  cancel: threading.Event, notes: list[str]) -> tuple[SpeechToText | None, str]:
        choice = choose(settings.stt_model, settings.stt_device)
        try:
            stt = WhisperSTT(manager.ensure_whisper(choice.model, progress, cancel), choice)
            stt.load()
            return stt, stt.label
        except (ModelError, STTError) as e:
            if choice.device != "cuda":
                notes.append(f"Sesli komut kapalı: {e.user_message}")
                return None, ""
            log.warning("GPU konuşma tanıma başarısız, CPU deneniyor: %s", e)
        cpu = choose("small" if settings.stt_model == "auto" else settings.stt_model, "cpu")
        try:
            stt = WhisperSTT(manager.ensure_whisper(cpu.model, progress, cancel), cpu)
            stt.load()
            notes.append("Ekran kartında konuşma tanıma çalışmadı (CUDA kütüphaneleri eksik olabilir); "
                         "işlemci kullanılıyor.")
            return stt, stt.label
        except (ModelError, STTError) as e:
            notes.append(f"Sesli komut kapalı: {e.user_message}")
            return None, ""
