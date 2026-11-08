"""Arka plan sistem rutinleri: GPU sıcaklık uyarısı ve İndirilenler'in günlük düzenlenmesi.

Asistan açılışta `SistemServices(assistant).start()` çağırır (core/assistant.py, SISTEM bloğu).
- GPU uyarısı: `jarvis/system/gpu_watch.py` (SYSTEM_SNAPSHOT dinleyicisi).
- İndirilenler: rutin açıksa ve bugün henüz çalışmadıysa düzenler. JARVIS kapalıyken kaçırılan
  gün, bir sonraki açılıştan kısa süre sonra telafi edilir. Rutin kullanıcı onayıyla açılmıştır
  (onay penceresinde önizleme gösterilir); her çalıştırma geri alınabilir ve asla dosya silmez.
"""

from __future__ import annotations

import asyncio
import logging

from jarvis.core.errors import JarvisError
from jarvis.core.events import Topics
from jarvis.system.gpu_watch import GpuTempWatcher

log = logging.getLogger(__name__)

FIRST_CHECK_S = 90.0
CHECK_EVERY_S = 30 * 60.0


class SistemServices:
    def __init__(self, assistant, first_check_s: float = FIRST_CHECK_S,  # noqa: ANN001
                 check_every_s: float = CHECK_EVERY_S) -> None:
        self.a = assistant
        self.first_check_s, self.check_every_s = first_check_s, check_every_s
        self.gpu: GpuTempWatcher | None = None

    @property
    def routine(self):  # noqa: ANN201
        return self.a.tool_service.ctx.extra.get("sistem_routine")

    async def _speak(self, text: str) -> None:
        voice = getattr(self.a, "voice", None)
        if voice is None or not getattr(voice, "ready", False):
            return
        try:
            await voice.speak_test(text)   # ses motorunun dışa açık tek konuşma yolu; boşta değilse hata verir
        except JarvisError:
            pass

    def start(self) -> None:
        self.gpu = GpuTempWatcher(self.a.bus, lambda: self.a.settings.sistem, speak=self._speak)
        r = self.routine
        if r is not None:
            r.notify = lambda msg: self.a.bus.publish(Topics.NOTICE, msg)
            self.a.spawn(self._downloads_loop(), "indirilenler-rutini")

    async def run_downloads_if_due(self) -> dict | None:
        r = self.routine
        loop = asyncio.get_running_loop()
        if r is None or not await loop.run_in_executor(self.a.tool_service.executor, r.due):
            return None
        res = await loop.run_in_executor(self.a.tool_service.executor, r.run, "otomatik")
        log.info("İndirilenler rutini: %d dosya taşındı", res["moved"])
        if res["moved"]:
            self.a.bus.publish(Topics.NOTICE, f"İndirilenler düzenlendi: {res['moved']} dosya türlerine göre alt "
                                              "klasörlere taşındı. Geri almak için 'düzenlemeyi geri al' de.")
        return res

    async def _downloads_loop(self) -> None:
        await asyncio.sleep(self.first_check_s)
        while True:
            try:
                await self.run_downloads_if_due()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("İndirilenler rutini çalışamadı")
            await asyncio.sleep(self.check_every_s)

    def close(self) -> None:
        if self.gpu is not None:
            self.gpu.close()
