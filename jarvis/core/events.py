"""Basit yayın/abone olay veriyolu.

Modüller birbirini doğrudan çağırmak yerine konu (topic) üzerinden haberleşir.
Bir dinleyicinin hatası diğerlerini etkilemez; loglanır.

Eşzamanlılık kuralı: `publish` ana (Qt/asyncio) thread'den çağrılır.
Yan thread'ler `publish_threadsafe` kullanır.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

Handler = Callable[[Any], None]


class Topics:
    STATE_CHANGED = "state.changed"            # payload: StateChange
    SYSTEM_SNAPSHOT = "system.snapshot"        # payload: SystemSnapshot
    SETTINGS_CHANGED = "settings.changed"      # payload: Settings
    MICROPHONE_TOGGLED = "voice.microphone"    # payload: bool (açık mı)
    APP_ERROR = "app.error"                    # payload: str (kullanıcı mesajı)
    CHAT_STARTED = "chat.started"              # payload: ChatStarted
    CHAT_DELTA = "chat.delta"                  # payload: str (yeni metin parçası)
    CHAT_DONE = "chat.done"                    # payload: ChatDone
    CHAT_FAILED = "chat.failed"                # payload: str (kullanıcı mesajı)
    NOTICE = "app.notice"                      # payload: str (bilgi/uyarı, hata değil)
    USAGE_UPDATED = "usage.updated"            # payload: BudgetStatus
    CLAUDE_STATUS = "claude.status"            # payload: ClaudeStatus
    CONFIRM_REQUEST = "app.confirm"            # payload: ConfirmRequest
    APP_SHUTDOWN = "app.shutdown"              # payload: None
    # --- Aşama 5: araçlar ---
    TOOL_CONFIRM = "tool.confirm"              # payload: ToolConfirmRequest (security/permission_manager.py)
    TOOL_EXECUTED = "tool.executed"            # payload: ToolResult
    # --- Aşama 5 sonu ---


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self, topic: str, handler: Handler) -> Callable[[], None]:
        self._handlers[topic].append(handler)

        def unsubscribe() -> None:
            if handler in self._handlers.get(topic, []):
                self._handlers[topic].remove(handler)

        return unsubscribe

    def has_subscribers(self, topic: str) -> bool:
        return bool(self._handlers.get(topic))

    def publish(self, topic: str, payload: Any = None) -> None:
        for handler in list(self._handlers.get(topic, [])):
            try:
                handler(payload)
            except Exception:
                log.exception("Olay dinleyicisi hata verdi (konu=%s, dinleyici=%r)", topic, handler)

    def publish_threadsafe(self, topic: str, payload: Any = None) -> None:
        if self._loop is None:
            raise RuntimeError("EventBus bir event loop'a bağlanmadı")
        self._loop.call_soon_threadsafe(self.publish, topic, payload)
