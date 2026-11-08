"""Uzun süren araç işleri için iptal belirteçleri.

Araç işleyicileri arka plan thread'inde çalışır ve asyncio iptaliyle durdurulamaz. Uzun işler
(disk taraması, hız testi, geçici dosya temizliği) döngülerinde `token.check()` çağırır;
"taramayı durdur" komutu (`cancel_long_operation`) ya da süre sınırı belirteci tetikler.
"""

from __future__ import annotations

import itertools
import threading
import time
from contextlib import contextmanager


class OperationCancelled(Exception):
    """İş kullanıcı isteğiyle durduruldu."""


class CancelToken:
    def __init__(self, label: str, deadline_s: float | None = None, clock=time.monotonic) -> None:  # noqa: ANN001
        self.label = label
        self._event = threading.Event()
        self._clock = clock
        self._deadline = clock() + deadline_s if deadline_s is not None else None

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def expired(self) -> bool:
        return self._deadline is not None and self._clock() >= self._deadline

    def stop_requested(self) -> bool:
        """İptal edildi ya da süre doldu mu (döngüyü bitirme sinyali)."""
        return self.cancelled or self.expired

    def check(self) -> None:
        if self.cancelled:
            raise OperationCancelled(self.label)


class Operations:
    """Çalışan uzun işlerin kaydı (thread güvenli)."""

    def __init__(self) -> None:
        self._ops: dict[int, CancelToken] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    @contextmanager
    def track(self, label: str, deadline_s: float | None = None):  # noqa: ANN201
        token = CancelToken(label, deadline_s)
        with self._lock:
            op_id = next(self._ids)
            self._ops[op_id] = token
        try:
            yield token
        finally:
            with self._lock:
                self._ops.pop(op_id, None)

    def running(self) -> list[str]:
        with self._lock:
            return [t.label for t in self._ops.values()]

    def cancel_all(self) -> list[str]:
        with self._lock:
            tokens = list(self._ops.values())
        for t in tokens:
            t.cancel()
        return [t.label for t in tokens]


OPERATIONS = Operations()
