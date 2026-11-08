"""Ekran kartı sıcaklık uyarısı.

`Topics.SYSTEM_SNAPSHOT` ölçümlerini dinler (asistanın izleme döngüsü yayınlar). Sıcaklık eşiğin
(varsayılan 85 °C) üstünde belirli bir süre (varsayılan 30 sn) kalırsa bir kez uyarır; sonraki uyarı
için bekleme süresi (varsayılan 10 dk) dolmalıdır. Uyarı `Topics.NOTICE` ile ekranda görünür; ayar
açıksa ses motoruyla da söylenir (yalnızca JARVIS o an boştaysa; meşgulse yalnızca ekranda kalır).
Sıcaklık okunamıyorsa (NVIDIA kartı/sürücüsü yok) hiçbir şey uydurulmaz, uyarı da verilmez.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from jarvis.config.settings import SistemSettings
from jarvis.core.events import EventBus, Topics

log = logging.getLogger(__name__)

HYSTERESIS_C = 3   # eşiğin bu kadar altına inmeden "soğudu" sayılmaz (sınırda gidip gelme)


class GpuTempWatcher:
    def __init__(self, bus: EventBus, settings: Callable[[], SistemSettings],
                 speak: Callable[[str], Awaitable[object]] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.bus, self.settings, self.speak, self.clock = bus, settings, speak, clock
        self._above_since: float | None = None
        self._last_alert: float | None = None
        self.alerts: list[str] = []
        self._unsub = bus.subscribe(Topics.SYSTEM_SNAPSHOT, self.on_snapshot)

    def close(self) -> None:
        self._unsub()

    def on_snapshot(self, snap) -> None:  # noqa: ANN001
        s = self.settings()
        gpu = getattr(snap, "gpu", None)
        temp = getattr(gpu, "temperature_c", None) if gpu is not None else None
        if not s.gpu_alert_enabled or temp is None:
            self._above_since = None
            return
        now = self.clock()
        if temp < s.gpu_alert_threshold_c:
            if temp <= s.gpu_alert_threshold_c - HYSTERESIS_C:
                self._above_since = None
            return
        if self._above_since is None:
            self._above_since = now
        if now - self._above_since < s.gpu_alert_duration_s:
            return
        if self._last_alert is not None and now - self._last_alert < s.gpu_alert_cooldown_min * 60:
            return
        self._last_alert = now
        self._alert(int(temp), s, getattr(gpu, "name", "Ekran kartı"))

    def _alert(self, temp: int, s: SistemSettings, name: str) -> None:
        dur = s.gpu_alert_duration_s
        since = f" {dur} saniyedir" if dur >= 5 else ""
        msg = (f"Dikkat: ekran kartı{since} {temp} °C ({name}; eşik {s.gpu_alert_threshold_c} °C). Oyuna kısa "
               "bir ara vermeyi, fan ve hava akışını kontrol etmeyi düşünebilirsin.")
        self.alerts.append(msg)
        log.warning("GPU sıcaklık uyarısı: %s °C", temp)
        self.bus.publish(Topics.NOTICE, msg)
        if s.gpu_alert_speak and self.speak is not None:
            spoken = f"Dikkat, ekran kartı {temp} derece. Kısa bir ara vermek iyi olabilir."
            try:
                fut = asyncio.ensure_future(self.speak(spoken))
            except RuntimeError:   # döngü yok (test/kapanış): ekrandaki uyarı yeterli
                return
            fut.add_done_callback(lambda f: f.cancelled() or f.exception() is None or
                                  log.info("GPU uyarısı seslendirilemedi: %s", f.exception()))
