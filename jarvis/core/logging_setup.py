"""Loglama.

Dönen (rotating) dosya logu + isteğe bağlı konsol. Her çıktı, API anahtarı
ve benzeri gizli bilgileri maskeleyen `RedactingFormatter`'dan geçer;
hata izleri (traceback) dahil.
"""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Anthropic anahtarları: sk-ant-api03-..., sk-ant-admin01-...
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"), "sk-ant-***"),
    # Authorization: Bearer xxx
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}"), r"\1***"),
    # api_key=..., "x-api-key": "...", token: ..., password=...
    (
        re.compile(
            r"(?i)([\"']?(?:x-api-key|api[_-]?key|access[_-]?token|token|password|secret)"
            r"[\"']?\s*[:=]\s*[\"']?)([^\s\"',}]{4,})"
        ),
        r"\1***",
    ),
]

LOG_FORMAT = "%(asctime)s %(levelname)-8s [%(threadName)s] %(name)s: %(message)s"


def redact(text: str) -> str:
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(log_file: Path, level: str = "INFO", console: bool = False) -> None:
    """Kök logger'ı yapılandırır. Tekrar çağrılırsa eski handler'ları kaldırır."""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()

    root.setLevel(logging.DEBUG)
    formatter = RedactingFormatter(LOG_FORMAT)

    log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_file, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler()
        stream.setLevel(logging.DEBUG)
        stream.setFormatter(formatter)
        root.addHandler(stream)

    # Gürültülü kütüphaneler
    for noisy in ("asyncio", "PIL", "urllib3", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
