"""Asistan çekirdeği.

Modülleri (ayarlar, durum makinesi, olay veriyolu, sistem izleme, ileride
ses/AI/hafıza) bir araya getirir; başlatma ve temiz kapatmadan sorumludur.
Arayüzü bilmez: arayüz yalnızca olay veriyoluna abone olur.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from jarvis.config.constants import SYSTEM_SAMPLE_INTERVAL_S
from jarvis.config.settings import Settings, SettingsStore
from jarvis.core.chat_service import ChatService, ClientFactory
from jarvis.core.errors import add_error_listener, remove_error_listener
from jarvis.core.events import EventBus, Topics
from jarvis.core.state import AssistantState, StateMachine
from jarvis.ai.claude_client import ClaudeClient
from jarvis.paths import AppPaths
from jarvis.security.credentials import CredentialStore
from jarvis.storage.database import Database
from jarvis.system.monitor import SystemMonitor

log = logging.getLogger(__name__)


class Assistant:
    def __init__(
        self,
        store: SettingsStore,
        bus: EventBus | None = None,
        monitor_factory: Callable[[], SystemMonitor] = SystemMonitor,
        sample_interval_s: float = SYSTEM_SAMPLE_INTERVAL_S,
        data_dir: Path | None = None,
        credentials: CredentialStore | None = None,
        client_factory: ClientFactory = ClaudeClient,
    ) -> None:
        self.store = store
        self.bus = bus or EventBus()
        self.state = StateMachine()
        self.state.add_listener(lambda ch: self.bus.publish(Topics.STATE_CHANGED, ch))
        self._monitor_factory = monitor_factory
        self._monitor: SystemMonitor | None = None
        self._sample_interval_s = sample_interval_s
        # Engelleyici işler (sensör okuma, ileride disk/ağ) için arka plan thread'leri
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="jarvis-io")
        self._tasks: list[asyncio.Task] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self.running = False
        self.db = Database((data_dir or AppPaths.resolve().data_dir) / "jarvis.db")
        self.chat = ChatService(store, self.bus, self.state, self.db, self._executor,
                                credentials, client_factory)

    @property
    def settings(self) -> Settings:
        return self.store.settings

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.bus.bind_loop(self._loop)
        add_error_listener(self._on_unhandled_error)
        # NVML başlatma sürücüye bağlı olarak yavaş olabilir: arayüzü bekletmesin
        self._monitor = await self._loop.run_in_executor(self._executor, self._monitor_factory)
        self._tasks.append(asyncio.create_task(self._monitor_loop(), name="system-monitor"))
        # Anahtar doğrulaması ağ beklerken arayüz açılışını geciktirmesin
        self._tasks.append(asyncio.create_task(self.chat.start(), name="claude-start"))
        self.running = True
        log.info("Asistan çekirdeği başlatıldı")

    def spawn(self, coro, name: str = "gorev") -> asyncio.Task:  # noqa: ANN001
        """Arayüzden başlatılan arka plan işi. Kapanışta iptal edilir, hatası loglanır."""
        task = asyncio.ensure_future(coro)
        task.set_name(name)
        self._tasks.append(task)

        def done(t: asyncio.Task) -> None:
            if t in self._tasks:
                self._tasks.remove(t)
            if not t.cancelled() and t.exception() is not None:
                log.error("Arka plan görevi başarısız (%s)", name, exc_info=t.exception())
        task.add_done_callback(done)
        return task

    def send_text(self, text: str) -> asyncio.Task:
        """Arayüzden gelen yazılı mesajı arka planda işler."""
        return self.spawn(self.chat.send(text), "chat-send")

    async def _monitor_loop(self) -> None:
        assert self._loop is not None and self._monitor is not None
        failures = 0
        while True:
            try:
                snap = await self._loop.run_in_executor(self._executor, self._monitor.sample)
                failures = 0
                self.bus.publish(Topics.SYSTEM_SNAPSHOT, snap)
            except asyncio.CancelledError:
                raise
            except Exception:
                failures += 1
                # Aynı hatayla log dosyasını doldurma: ilk 3 hatayı ayrıntılı yaz
                if failures <= 3:
                    log.exception("Sistem bilgisi okunamadı (%d. deneme)", failures)
            await asyncio.sleep(self._sample_interval_s)

    def set_microphone_enabled(self, enabled: bool) -> None:
        self.settings.voice.microphone_enabled = enabled
        self.store.save()
        log.info("Mikrofon %s", "açıldı" if enabled else "kapatıldı")
        self.bus.publish(Topics.MICROPHONE_TOGGLED, enabled)

    def set_volume(self, volume: int) -> None:
        self.settings.voice.volume = volume
        self.store.save()

    def acknowledge_error(self) -> None:
        if self.state.state is AssistantState.ERROR:
            self.state.reset()

    def _on_unhandled_error(self, user_message: str) -> None:
        # Başka bir thread'den gelebilir: işi ana döngüye devret
        if self._loop is None or self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(self._publish_error, user_message)

    def _publish_error(self, user_message: str) -> None:
        self.bus.publish(Topics.APP_ERROR, user_message)
        try:
            self.state.fail(user_message)
        except Exception:
            log.exception("Hata durumuna geçilemedi")

    async def shutdown(self) -> None:
        if not self.running:
            return
        self.running = False
        log.info("Asistan kapatılıyor")
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await self.chat.shutdown()
        if self._monitor is not None:
            self._monitor.close()
        self._executor.shutdown(wait=True, cancel_futures=True)
        try:
            self.store.save()
        except OSError:
            log.exception("Ayarlar kapanışta kaydedilemedi")
        self.db.close()
        remove_error_listener(self._on_unhandled_error)
        self.bus.publish(Topics.APP_SHUTDOWN)
        log.info("Asistan kapatıldı")
