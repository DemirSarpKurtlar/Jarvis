"""Araçların ses motoruna erişimi (seslendirme, başka dilde seslendirme, dikte modu).

Araçlar arka plan thread'lerinde çalışır; `VoiceManager` ise asyncio (Qt) thread'inde yaşar.
Bu köprü iki dünyayı güvenle bağlar:

* `speak(text, language)`: engelleyici; çağıran araç thread'inde sentezler ve çalar. Sesli bir
  isteğin içinde konuşulduysa VoiceManager aynı cevabı ikinci kez seslendirmez.
* `start_dictation(session)` / `stop_dictation()`: VoiceManager'ın döngü thread'inde çalıştırılır.

VoiceManager kendini oluştururken köprüyü araç bağlamına (`ToolContext.extra["voice"]`) koyar.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvis.core.errors import JarvisError
from jarvis.core.events import Topics
from jarvis.voice.languages import LANGUAGE_NAMES, LANGUAGE_VOICES
from jarvis.voice.models import ModelManager

if TYPE_CHECKING:
    from jarvis.voice.voice_manager import VoiceManager

log = logging.getLogger(__name__)


class VoiceUnavailable(JarvisError):
    pass


def _piper(path: Path) -> Any:  # noqa: ANN401
    from jarvis.voice.tts import PiperTTS

    return PiperTTS(path)


class VoiceBridge:
    def __init__(self, vm: VoiceManager, manager_factory: Callable[[], ModelManager] = ModelManager,
                 tts_factory: Callable[[Path], Any] = _piper) -> None:
        self.vm = vm
        self.manager_factory = manager_factory
        self.tts_factory = tts_factory
        self._voices: dict[str, Any] = {}
        self._lock = threading.Lock()

    # ---------- durum ----------
    @property
    def can_speak(self) -> bool:
        comps = self.vm.components
        return comps is not None and comps.player is not None and comps.tts is not None

    @property
    def unavailable_reason(self) -> str:
        comps = self.vm.components
        if comps is None:
            return self.vm.status.detail or "Ses motoru hazır değil."
        if comps.player is None:
            return "Hoparlör bulunamadı."
        return "Konuşma sentezi hazır değil."

    @property
    def dictation_active(self) -> bool:
        return self.vm.dictation is not None

    # ---------- seslendirme (araç thread'inden) ----------
    def voice_for(self, language: str) -> Any:  # noqa: ANN401
        """Dilin Piper sesini (gerekirse indirip) yükler. Türkçe için None: ana ses kullanılır."""
        if language == "tr":
            return None
        info = LANGUAGE_VOICES.get(language)
        if info is None:
            raise VoiceUnavailable(f"dil sesi yok: {language}",
                                   f"{LANGUAGE_NAMES.get(language, language)} için bir sesim yok.")
        with self._lock:
            if language in self._voices:
                return self._voices[language]
            manager = self.manager_factory()
            if not all(manager.is_ready(f) for f in info.files):
                log.info("%s ilk kez indiriliyor (%d MB)", info.title, info.size_mb)
                try:
                    self.vm.bus.publish_threadsafe(Topics.NOTICE, f"{info.title} ilk kez indiriliyor "
                                                   f"(~{info.size_mb} MB), bir dakika sürebilir.")
                except RuntimeError:   # veriyolu döngüye bağlı değil (testler)
                    pass
            # ModelError (Türkçe user_message) olduğu gibi yukarı gider
            path = manager.ensure_all(list(info.files), self.vm._progress_threadsafe, self.vm._cancel,
                                      label=info.title)[0]
            try:
                tts = self.tts_factory(path)
            except JarvisError:
                raise
            except Exception as e:  # noqa: BLE001
                raise VoiceUnavailable(f"ses yüklenemedi: {e}", f"{info.title} yüklenemedi.") from e
            self._voices[language] = tts
            return tts

    def speak(self, text: str, language: str = "tr", max_chars: int = 1500) -> bool:
        """Metni seslendirir; bitene kadar bekler. Ses motoru/hoparlör yoksa False (hata değil)."""
        if not self.can_speak:
            return False
        tts = self.voice_for(language)
        return self.vm.speak_external(text, tts, max_chars=max_chars)

    # ---------- dikte (döngü thread'inde) ----------
    def _on_loop(self, fn: Callable[..., Any], *args: Any, timeout: float = 5.0) -> Any:  # noqa: ANN401
        loop = self.vm._loop
        if loop is None or loop.is_closed():
            raise VoiceUnavailable("ses döngüsü yok", self.unavailable_reason)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            return fn(*args)
        fut: concurrent.futures.Future = concurrent.futures.Future()

        def run() -> None:
            try:
                fut.set_result(fn(*args))
            except BaseException as e:  # noqa: BLE001 - sonucu çağıran thread'e taşı
                fut.set_exception(e)

        loop.call_soon_threadsafe(run)
        return fut.result(timeout)

    def start_dictation(self, session: Any) -> None:  # noqa: ANN401
        self._on_loop(self.vm.start_dictation, session)

    def stop_dictation(self) -> bool:
        return bool(self._on_loop(self.vm.stop_dictation))
