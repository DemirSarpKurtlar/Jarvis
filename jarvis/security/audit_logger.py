"""Denetim kaydı: çalıştırılan, reddedilen ya da onaylanan her araç çağrısı.

Kayıt SQLite `audit_log` tablosuna yazılır (veritabanı geçişi: "3: denetim kaydı").
Gizli bilgiler asla yazılmaz:

- adı parola/anahtar/token/secret çağrıştıran alanların değeri `***` olur,
- tüm metinler log maskelemesinden (`redact`) geçer,
- belge içeriği gibi uzun metinlerin yalnızca uzunluğu yazılır,
- her alan kısaltılır.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from jarvis.core.logging_setup import redact
from jarvis.storage.database import Database

log = logging.getLogger(__name__)

_SECRET_KEY = re.compile(r"(?i)(pass(word)?|parola|sifre|şifre|secret|token|api[_-]?key|credential|auth|cookie)")
_CONTENT_KEYS = {"content", "text", "body", "icerik", "metin"}
MAX_FIELD_CHARS = 300
MAX_ARGS_JSON = 2000


def sanitize(value: Any, key: str = "", depth: int = 0) -> Any:
    if depth > 4:
        return "…"
    if key and _SECRET_KEY.search(key):
        return "***"
    if isinstance(value, dict):
        return {str(k): sanitize(v, str(k), depth + 1) for k, v in list(value.items())[:40]}
    if isinstance(value, (list, tuple)):
        return [sanitize(v, "", depth + 1) for v in list(value)[:40]]
    if isinstance(value, str):
        if key.lower() in _CONTENT_KEYS and len(value) > 80:
            return f"<{len(value)} karakter>"
        v = redact(value)
        return v if len(v) <= MAX_FIELD_CHARS else v[:MAX_FIELD_CHARS] + "…"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return sanitize(str(value), key, depth + 1)


@dataclass(frozen=True)
class AuditEntry:
    id: int
    ts_utc: str
    source: str
    tool: str
    permission: str
    decision: str
    ok: bool
    args: str
    summary: str | None
    error: str | None
    duration_ms: int | None


class AuditLogger:
    # Karar değerleri
    ALLOWED = "izin"
    CONFIRMED = "onaylandi"
    DECLINED = "reddedildi"
    FORBIDDEN = "yasak"
    INVALID = "gecersiz"
    UNSUPPORTED = "desteklenmiyor"

    def __init__(self, db: Database | None) -> None:
        self.db = db

    def record(self, *, source: str, tool: str, permission: str, decision: str, ok: bool,
               args: Any = None, summary: str | None = None, error: str | None = None,
               duration_ms: int | None = None) -> int:
        args_json = json.dumps(sanitize(args or {}), ensure_ascii=False, default=str)
        if len(args_json) > MAX_ARGS_JSON:
            args_json = args_json[:MAX_ARGS_JSON] + "…"
        row = (datetime.now(timezone.utc).isoformat(timespec="seconds"), source[:40], tool[:80], permission,
               decision, int(bool(ok)), args_json,
               sanitize(summary or "")[:MAX_FIELD_CHARS] or None, sanitize(error or "")[:MAX_FIELD_CHARS] or None,
               duration_ms)
        log.info("Araç: %s kaynak=%s izin=%s karar=%s sonuç=%s", tool, source, permission, decision,
                 "tamam" if ok else "başarısız")
        if self.db is None:
            return 0
        try:
            return self.db.execute(
                "INSERT INTO audit_log (ts_utc, source, tool, permission, decision, ok, args_json, summary, error,"
                " duration_ms) VALUES (?,?,?,?,?,?,?,?,?,?)", row)
        except Exception:  # denetim kaydı yazılamazsa işlem yine loglanmış olur
            log.exception("Denetim kaydı yazılamadı")
            return 0

    def recent(self, limit: int = 100) -> list[AuditEntry]:
        if self.db is None:
            return []
        rows = self.db.query("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (int(limit),))
        return [AuditEntry(r["id"], r["ts_utc"], r["source"], r["tool"], r["permission"], r["decision"],
                           bool(r["ok"]), r["args_json"], r["summary"], r["error"], r["duration_ms"]) for r in rows]


class Stopwatch:
    def __init__(self) -> None:
        self.t0 = time.monotonic()

    @property
    def ms(self) -> int:
        return int((time.monotonic() - self.t0) * 1000)
