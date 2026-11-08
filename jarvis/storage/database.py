"""Yerel SQLite veritabanı.

Tek bir bağlantı, kilitle korunur. Şema sürümlüdür: her geçiş (migration) bir
kez ve sırayla uygulanır. Aşama 2: kullanım (maliyet) ve konuşma kayıtları.
Aşama 4: hafıza tabloları (2. geçiş).

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
    # 2: hafıza (Aşama 4) — uzun süreli hafıza kayıtları, tam metin arama dizini, özet işaretleri
    """
    CREATE TABLE memories (
        id INTEGER PRIMARY KEY,
        category TEXT NOT NULL,
        content TEXT NOT NULL,
        norm TEXT NOT NULL,               -- arama ve tekrar tespiti için sadeleştirilmiş metin
        importance INTEGER NOT NULL CHECK (importance BETWEEN 1 AND 5),
        source TEXT NOT NULL,             -- 'konuşma:<id>' | 'kullanıcı' | 'çıkarım'
        is_inference INTEGER NOT NULL DEFAULT 0,
        sensitive INTEGER NOT NULL DEFAULT 0,
        sensitive_kind TEXT,
        status TEXT NOT NULL CHECK (status IN ('active', 'pending_confirmation')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        expires_at TEXT,
        last_used_at TEXT,
        use_count INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX idx_memories_status ON memories(status);
    CREATE INDEX idx_memories_expires ON memories(expires_at);
    CREATE INDEX idx_memories_source ON memories(source);
    CREATE VIRTUAL TABLE memories_fts USING fts5(
        norm, content='memories', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
    );
    CREATE TRIGGER memories_ai AFTER INSERT ON memories BEGIN
        INSERT INTO memories_fts(rowid, norm) VALUES (new.id, new.norm);
    END;
    CREATE TRIGGER memories_ad AFTER DELETE ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, norm) VALUES ('delete', old.id, old.norm);
    END;
    CREATE TRIGGER memories_au AFTER UPDATE OF norm ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, norm) VALUES ('delete', old.id, old.norm);
        INSERT INTO memories_fts(rowid, norm) VALUES (new.id, new.norm);
    END;
    CREATE TABLE memory_summarized (
        conversation_id INTEGER PRIMARY KEY,
        ts_utc TEXT NOT NULL,
        result TEXT NOT NULL              -- 'özet' | 'kısa' | 'gereksiz'
    );
    """,
    # --- Aşama 5 başlangıcı (araçlar ve güvenlik) ---
    # 3: denetim kaydı (birleştirmede hafıza geçişinden sonra gelecek; IF NOT EXISTS sıra değişimine dayanıklı)
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY,
        ts_utc TEXT NOT NULL,
        source TEXT NOT NULL,             -- claude / yerel / arayuz / zamanlayici
        tool TEXT NOT NULL,
        permission TEXT NOT NULL,         -- okuma / güvenli / onaylı / yasak
        decision TEXT NOT NULL,           -- izin / onaylandi / reddedildi / yasak / gecersiz / desteklenmiyor
        ok INTEGER NOT NULL,
        args_json TEXT NOT NULL,          -- gizli alanlar maskelenmiş, kısaltılmış
        summary TEXT,
        error TEXT,
        duration_ms INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts_utc);
    """,
    # --- Aşama 5 sonu ---
    # --- RUTIN ---
    # rutinler: kullanıcı tanımlı çok adımlı komutlar (jarvis/tools/routines.py). Örnekler kapalı gelir.
    # IF NOT EXISTS ve "yoksa ekle": birleştirmede geçiş sırası değişse de güvenli.
    """
    CREATE TABLE IF NOT EXISTS routines (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        phrases_json TEXT NOT NULL DEFAULT '[]',   -- tetikleyici sözler
        steps_json TEXT NOT NULL,                  -- [{"tool": ..., "args": {...}}]
        enabled INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_run_at TEXT
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_routines_name ON routines(name);
    INSERT INTO routines (name, phrases_json, steps_json, enabled, created_at, updated_at)
    SELECT 'Çalışma modu', '["çalışma modu", "çalışma zamanı"]',
           '[{"tool": "open_app", "args": {"name": "Visual Studio Code"}},
             {"tool": "open_app", "args": {"name": "Spotify"}},
             {"tool": "close_app", "args": {"name": "Discord", "force": false}},
             {"tool": "set_volume", "args": {"level": 30}},
             {"tool": "say", "args": {"text": "Çalışma modu hazır. Kolay gelsin."}}]',
           0, datetime('now'), datetime('now')
    WHERE NOT EXISTS (SELECT 1 FROM routines WHERE name = 'Çalışma modu');
    INSERT INTO routines (name, phrases_json, steps_json, enabled, created_at, updated_at)
    SELECT 'Oyun modu', '["oyun modu", "oyun zamanı"]',
           '[{"tool": "close_app", "args": {"name": "Visual Studio Code", "force": false}},
             {"tool": "open_app", "args": {"name": "Steam"}},
             {"tool": "open_app", "args": {"name": "Discord"}},
             {"tool": "set_volume", "args": {"level": 60}},
             {"tool": "say", "args": {"text": "Oyun modu hazır. İyi eğlenceler."}}]',
           0, datetime('now'), datetime('now')
    WHERE NOT EXISTS (SELECT 1 FROM routines WHERE name = 'Oyun modu');
    INSERT INTO routines (name, phrases_json, steps_json, enabled, created_at, updated_at)
    SELECT 'İyi geceler', '["iyi geceler", "yatıyorum"]',
           '[{"tool": "close_app", "args": {"name": "Spotify", "force": false}},
             {"tool": "set_volume", "args": {"level": 15}},
             {"tool": "say", "args": {"text": "İyi geceler. Ekranı kilitledim."}},
             {"tool": "wait", "args": {"seconds": 2}},
             {"tool": "lock_screen", "args": {}}]',
           0, datetime('now'), datetime('now')
    WHERE NOT EXISTS (SELECT 1 FROM routines WHERE name = 'İyi geceler');
    """,
    # --- RUTIN sonu ---
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
