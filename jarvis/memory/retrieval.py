"""İlgili hafıza kayıtlarını bulma.

Puan = ilgi (FTS5 bm25) + önem + yakınlık + kullanım sıklığı. Türkçe için:
- Hem dizin hem sorgu `text.normalize` ile sadeleştirilir (ı/İ/ş/ğ/ü/ö/ç farkı yok).
- Ekler yüzünden ("kahveyi", "kahvemi") kelimenin ilk 5 harfiyle önek araması yapılır.

Sorguyla eşleşen kayıt az ise kullanıcı hakkındaki temel bilgiler (yüksek önemli
tercih ve kişisel bilgiler) boş yerlere eklenir; adını ya da cevap tarzı tercihini
her soruda bilmek işe yarar.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from jarvis.memory.store import ACTIVE, Memory, MemoryStore, _iso, _row_to_memory
from jarvis.memory.text import content_tokens, stem

DEFAULT_K = 5
MAX_CORE = 3
MIN_RELATIVE_RELEVANCE = 0.15

W_RELEVANCE, W_IMPORTANCE, W_RECENCY, W_USE = 0.6, 0.2, 0.1, 0.1
RECENCY_HALF_LIFE_DAYS = 60.0


@dataclass(frozen=True)
class ScoredMemory:
    memory: Memory
    score: float
    matched: bool          # sorguyla eşleşti mi (yoksa temel bilgi olarak mı eklendi)


def fts_query(text: str, max_terms: int = 12) -> str | None:
    seen: list[str] = []
    for t in content_tokens(text):
        s = stem(t)
        if len(s) < 2 or s in seen:
            continue
        seen.append(s)
        if len(seen) >= max_terms:
            break
    if not seen:
        return None
    # Sadeleştirilmiş kelimeler yalnızca [a-z0-9]: tırnak içinde güvenli
    return " OR ".join(f'"{s}"*' for s in seen)


def _recency(m: Memory, now: datetime) -> float:
    ref = max(x for x in (m.updated_at, m.last_used_at, m.created_at) if x is not None)
    days = max(0.0, (now - ref).total_seconds() / 86400)
    return 0.5 ** (days / RECENCY_HALF_LIFE_DAYS)


def _use(m: Memory) -> float:
    return min(1.0, math.log1p(m.use_count) / math.log(11))


def _visible_sql() -> str:
    # Onay bekleyen hassas bilgi asla kullanılmaz (kullanıcı henüz izin vermedi)
    return ("(m.expires_at IS NULL OR m.expires_at > ?)"
            " AND NOT (m.status = 'pending_confirmation' AND m.sensitive = 1)")


class MemoryRetriever:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def search(self, query: str, k: int = DEFAULT_K, fill_core: bool = True) -> list[ScoredMemory]:
        now = self.store.now()
        db = self.store.db
        results: list[ScoredMemory] = []
        q = fts_query(query)
        if q:
            rows = db.query(
                "SELECT m.*, bm25(memories_fts) AS rank FROM memories_fts"
                " JOIN memories m ON m.id = memories_fts.rowid"
                f" WHERE memories_fts MATCH ? AND {_visible_sql()} ORDER BY rank LIMIT 50",
                (q, _iso(now)))
            if rows:
                rel = [-r["rank"] for r in rows]
                top = max(rel) or 1.0
                for r, rv in zip(rows, rel):
                    relevance = max(0.0, rv / top) if top > 0 else 0.0
                    if relevance < MIN_RELATIVE_RELEVANCE:
                        continue
                    m = _row_to_memory(r)
                    score = (W_RELEVANCE * relevance + W_IMPORTANCE * m.importance / 5
                             + W_RECENCY * _recency(m, now) + W_USE * _use(m))
                    if not m.confirmed:
                        score *= 0.8   # doğrulanmamış çıkarımlar biraz geride
                    results.append(ScoredMemory(m, score, True))
        results.sort(key=lambda s: s.score, reverse=True)
        results = results[:k]

        if fill_core and len(results) < k:
            taken = {s.memory.id for s in results}
            rows = db.query(
                "SELECT m.* FROM memories m WHERE m.status = ? AND m.importance >= 4"
                " AND m.category IN ('preference', 'personal')"
                f" AND {_visible_sql()} ORDER BY m.importance DESC, m.updated_at DESC LIMIT ?",
                (ACTIVE, _iso(now), MAX_CORE + len(taken)))
            for r in rows:
                if len(results) >= k or sum(1 for s in results if not s.matched) >= MAX_CORE:
                    break
                if r["id"] in taken:
                    continue
                m = _row_to_memory(r)
                results.append(ScoredMemory(m, W_IMPORTANCE * m.importance / 5, False))
        return results
