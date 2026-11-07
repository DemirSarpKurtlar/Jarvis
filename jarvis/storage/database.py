"""Yerel SQLite veritabanı.

Tek bir bağlantı, kilitle korunur. Şema sürümlüdür: her geçiş (migration) bir
kez ve sırayla uygulanır. Aşama 2: kullanım (maliyet) ve konuşma kayıtları.
Aşama 4 hafıza tablolarını yeni bir geçişle ekleyecek.

Çağrılar kısa ve hızlıdır; yine de asistan bunları arka plan
executor'ünde çalıştırır (arayüz thread'i hiç beklemez).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MIGRATIONS: list[str] = [
    # 1: kullanım ve konuşmalar
    """
    CREATE TABLE usage (
        id INTEGER PRIMARY KEY,
        ts_utc TEXT NOT NULL,
        local_day TEXT NOT NULL,          -- YYYY-MM-DD (kullanıcının yerel saati)
        local_month TEXT NOT NULL,        -- YYYY-MM
        model TEXT NOT NULL,
        tier TEXT NOT NULL,
        input_tokens INTEGER NOT NULL,
        output_tokens INTEGER NOT NULL,
        cache_creation_tokens INTEGER NOT NULL,
        cache_read_tokens INTEGER NOT NULL,
        web_search_requests INTEGER NOT NULL,
        cost_usd REAL NOT NULL,
        pricing_assumed INTEGER NOT NULL DEFAULT 0,
        request_id TEXT,
        route_reason TEXT
    );
    CREATE INDEX idx_usage_day ON usage(local_day);
    CREATE INDEX idx_usage_month ON usage(local_month);

    CREATE TABLE conversations (
        id INTEGER PRIMARY KEY,
        started_at TEXT NOT NULL,
        title TEXT
    );
    CREATE TABLE messages (
        id INTEGER PRIMARY KEY,
        conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        ts_utc TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
        text TEXT NOT NULL,
        model TEXT,
        cost_usd REAL
    );
    CREATE INDEX idx_messages_conv ON messages(conversation_id);
    """,
]


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    @property
    def schema_version(self) -> int:
        return self._conn.execute("PRAGMA user_version").fetchone()[0]

    def _migrate(self) -> None:
        with self._lock:
            current = self.schema_version
            for version, sql in enumerate(MIGRATIONS[current:], start=current + 1):
                log.info("Veritabanı şeması %d sürümüne yükseltiliyor", version)
                self._conn.executescript(f"BEGIN; {sql}; PRAGMA user_version = {version}; COMMIT;")

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Yazma işlemi; eklenen satırın kimliğini döndürür."""
        with self._lock:
            cur = self._conn.execute(sql, params)
            return cur.lastrowid or 0

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.executemany(sql, rows)
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()
