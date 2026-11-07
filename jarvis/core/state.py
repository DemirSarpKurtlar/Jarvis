"""Asistan durum makinesi.

Arayüzdeki 7 durumun tek doğruluk kaynağı. Yalnızca tanımlı geçişlere izin
verilir; geçersiz bir geçiş `InvalidStateTransition` fırlatır (hatalar
gizlenmez). ERROR durumuna her durumdan geçilebilir.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from jarvis.core.errors import InvalidStateTransition

log = logging.getLogger(__name__)


class AssistantState(str, Enum):
    STANDBY = "standby"            # Beklemede
    WAKE_DETECTED = "wake"         # Uyandı
    LISTENING = "listening"        # Dinliyor
    PROCESSING = "processing"      # Düşünüyor
    EXECUTING = "executing"        # Yürütüyor
    SPEAKING = "speaking"          # Konuşuyor
    ERROR = "error"                # Hata


S = AssistantState

ALLOWED_TRANSITIONS: dict[AssistantState, frozenset[AssistantState]] = {
    S.STANDBY: frozenset({S.WAKE_DETECTED, S.LISTENING, S.PROCESSING}),
    S.WAKE_DETECTED: frozenset({S.LISTENING, S.STANDBY}),
    S.LISTENING: frozenset({S.PROCESSING, S.STANDBY}),
    S.PROCESSING: frozenset({S.EXECUTING, S.SPEAKING, S.LISTENING, S.STANDBY}),
    S.EXECUTING: frozenset({S.PROCESSING, S.SPEAKING, S.STANDBY}),
    # Konuşurken söz kesilirse tekrar dinlemeye geçilir
    S.SPEAKING: frozenset({S.LISTENING, S.PROCESSING, S.STANDBY}),
    S.ERROR: frozenset({S.STANDBY}),
}


@dataclass(frozen=True)
class StateChange:
    old: AssistantState
    new: AssistantState
    detail: str
    at: float


Listener = Callable[[StateChange], None]


class StateMachine:
    def __init__(self) -> None:
        self._state = AssistantState.STANDBY
        self._detail = ""
        self._lock = threading.RLock()
        self._listeners: list[Listener] = []

    @property
    def state(self) -> AssistantState:
        return self._state

    @property
    def detail(self) -> str:
        return self._detail

    def can_transition(self, new: AssistantState) -> bool:
        return new is AssistantState.ERROR or new in ALLOWED_TRANSITIONS[self._state]

    def transition(self, new: AssistantState, detail: str = "") -> StateChange:
        with self._lock:
            old = self._state
            if new is old and new is not AssistantState.ERROR:
                # Aynı duruma geçiş: sadece ayrıntı metnini güncelle
                self._detail = detail
                change = StateChange(old, new, detail, time.monotonic())
            elif not self.can_transition(new):
                raise InvalidStateTransition(
                    f"Geçersiz durum geçişi: {old.value} -> {new.value}",
                    user_message="İç durum hatası oluştu, asistan beklemeye alınıyor.",
                )
            else:
                self._state = new
                self._detail = detail
                change = StateChange(old, new, detail, time.monotonic())
                log.debug("Durum: %s -> %s (%s)", old.value, new.value, detail)
        for fn in list(self._listeners):
            try:
                fn(change)
            except Exception:
                log.exception("Durum dinleyicisi hata verdi")
        return change

    def fail(self, detail: str) -> StateChange:
        return self.transition(AssistantState.ERROR, detail)

    def reset(self) -> StateChange:
        """Hata ya da herhangi bir durumdan güvenle beklemeye dön."""
        with self._lock:
            if self._state is AssistantState.STANDBY:
                return StateChange(self._state, self._state, "", time.monotonic())
            if AssistantState.STANDBY not in ALLOWED_TRANSITIONS[self._state]:
                # Örn. WAKE_DETECTED dışındaki her durumdan STANDBY'a izin var;
                # yine de güvenli yol: önce ERROR, sonra STANDBY.
                self.transition(AssistantState.ERROR, "sıfırlama")
        return self.transition(AssistantState.STANDBY)

    def add_listener(self, fn: Listener) -> None:
        self._listeners.append(fn)
