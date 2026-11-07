"""Hata türleri ve küresel hata yakalama.

Yakalanmamış hiçbir hata sessizce kaybolmaz: ana thread, yan thread'ler ve
asyncio görevlerindeki hatalar loglanır ve kayıtlı dinleyicilere (arayüz)
iletilir.
"""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Callable
from types import TracebackType

log = logging.getLogger("jarvis.errors")


class JarvisError(Exception):
    """Tüm JARVIS hatalarının tabanı. `user_message` kullanıcıya gösterilir."""

    def __init__(self, message: str, user_message: str | None = None) -> None:
        super().__init__(message)
        self.user_message = user_message or message


class InvalidStateTransition(JarvisError):
    pass


ErrorListener = Callable[[str], None]
_listeners: list[ErrorListener] = []


def add_error_listener(fn: ErrorListener) -> None:
    _listeners.append(fn)


def remove_error_listener(fn: ErrorListener) -> None:
    if fn in _listeners:
        _listeners.remove(fn)


def report_error(user_message: str) -> None:
    for fn in list(_listeners):
        try:
            fn(user_message)
        except Exception:  # dinleyici hatası zincirleme çökme yaratmasın
            log.exception("Hata dinleyicisi başarısız oldu")


def _user_message(exc: BaseException) -> str:
    if isinstance(exc, JarvisError):
        return exc.user_message
    return f"Beklenmeyen bir hata oluştu ({exc.__class__.__name__}). Ayrıntılar log dosyasında."


def _excepthook(
    exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None
) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    log.critical("Yakalanmamış hata", exc_info=(exc_type, exc, tb))
    report_error(_user_message(exc))


def _thread_excepthook(args: threading.ExceptHookArgs) -> None:
    if args.exc_value is None:
        return
    log.critical(
        "Thread içinde yakalanmamış hata (%s)",
        args.thread.name if args.thread else "?",
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )
    report_error(_user_message(args.exc_value))


def asyncio_exception_handler(loop, context: dict) -> None:  # noqa: ANN001
    exc = context.get("exception")
    msg = context.get("message", "asyncio hatası")
    if exc is not None:
        log.error("asyncio: %s", msg, exc_info=exc)
        report_error(_user_message(exc))
    else:
        log.error("asyncio: %s", msg)


def install_exception_hooks() -> None:
    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
