"""Hafıza kayıtları: ekleme, tekrar birleştirme, düzenleme, silme, süre dolumu.

Kurallar (kod düzeyinde zorlanır, modelin kararına bırakılmaz):
- Gizli bilgi (parola, API anahtarı, kart numarası ...) hiçbir zaman kaydedilmez.
- Çıkarım (`is_inference`) asla doğrulanmış bilgi olarak kaydedilmez: onay bekler.
- Hassas bilgi (sağlık, para, kesin konum ...) kullanıcı onaylayana kadar onay bekler.
- Neredeyse aynı bir kayıt varsa yenisi eklenmez; mevcut kayıt güncellenir.

Bütün yöntemler engelleyicidir (SQLite); asistan bunları executor'de çağırır.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from jarvis.memory.text import (SENSITIVE_KINDS, is_near_duplicate, is_secret_memory, local_sensitive_kind,
                                normalize, stem, tokens)
from jarvis.storage.database import Database

log = logging.getLogger(__name__)

ACTIVE = "active"
PENDING = "pending_confirmation"

SOURCE_USER = "kullanıcı"
SOURCE_INFERENCE = "çıkarım"
SOURCE_CONVERSATION = "konuşma:"     # + konuşma kimliği

# kategori -> (tekil etiket, filtre etiketi)
CATEGORIES: dict[str, tuple[str, str]] = {
    "preference": ("Tercih", "Tercihler"),
    "personal": ("Kişisel", "Kişisel"),
    "app": ("Uygulama", "Uygulamalar"),
    "command": ("Komut", "Komutlar"),
    "routine": ("Rutin", "Rutinler"),
    "task": ("Görev", "Görevler"),
    "decision": ("Karar", "Kararlar"),
    "technical": ("Teknik sorun", "Teknik sorunlar"),
    "conversation_summary": ("Konuşma özeti", "Konuşma özetleri"),
}

MAX_CONTENT_CHARS = 500


class SecretRejected(ValueError):
    """Gizli bilgi içeren kayıt reddedildi."""

    user_message = "Şifre, API anahtarı ya da kart numarası gibi gizli bilgileri hafızaya kaydetmiyorum."


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


@dataclass(frozen=True)
class Memory:
    id: int
    category: str
    content: str
    importance: int
    source: str
    is_inference: bool
    sensitive: bool
    sensitive_kind: str | None
    status: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    use_count: int

    @property
    def confirmed(self) -> bool:
        return self.status == ACTIVE

    @property
    def category_label(self) -> str:
        return CATEGORIES.get(self.category, (self.category, ""))[0]

    @property
    def conversation_id(self) -> int | None:
        if self.source.startswith(SOURCE_CONVERSATION):
            try:
                return int(self.source[len(SOURCE_CONVERSATION):])
            except ValueError:
                return None
        return None


@dataclass(frozen=True)
class Candidate:
    """Kaydedilmek istenen yeni bilgi (çıkarıcıdan, özetleyiciden ya da kullanıcıdan)."""

    content: str
    category: str
    importance: int = 3
    source: str = SOURCE_USER
    is_inference: bool = False
    sensitive: bool = False
    sensitive_kind: str | None = None
    expires_at: datetime | None = None
    confirmed_by_user: bool = False   # kullanıcı arayüzden kendisi ekledi/onayladı


@dataclass(frozen=True)
class AddResult:
    action: str            # "eklendi" | "birleştirildi" | "güncellendi" | "reddedildi"
    memory: Memory | None
    reason: str = ""


def _row_to_memory(r) -> Memory:  # noqa: ANN001
    return Memory(
        id=r["id"], category=r["category"], content=r["content"], importance=r["importance"],
        source=r["source"], is_inference=bool(r["is_inference"]), sensitive=bool(r["sensitive"]),
        sensitive_kind=r["sensitive_kind"], status=r["status"],
        created_at=_parse(r["created_at"]) or utc_now(), updated_at=_parse(r["updated_at"]) or utc_now(),
        expires_at=_parse(r["expires_at"]), last_used_at=_parse(r["last_used_at"]), use_count=r["use_count"],
    )


def _clean(text: str) -> str:
    return " ".join(text.split())[:MAX_CONTENT_CHARS]


class MemoryStore:
    def __init__(self, db: Database, clock: Callable[[], datetime] = utc_now) -> None:
        self.db = db
        self._clock = clock

    def now(self) -> datetime:
        return self._clock()

    # ---------- okuma ----------
    def get(self, memory_id: int) -> Memory | None:
        rows = self.db.query("SELECT * FROM memories WHERE id = ?", (memory_id,))
        return _row_to_memory(rows[0]) if rows else None

    def list(self, category: str | None = None, status: str | None = None,
             include_expired: bool = False) -> list[Memory]:
        sql = "SELECT * FROM memories WHERE 1=1"
        params: list = []
        if category:
            sql += " AND category = ?"
            params.append(category)
        if status:
            sql += " AND status = ?"
            params.append(status)
        if not include_expired:
            sql += " AND (expires_at IS NULL OR expires_at > ?)"
            params.append(_iso(self.now()))
        sql += " ORDER BY importance DESC, updated_at DESC, id DESC"
        return [_row_to_memory(r) for r in self.db.query(sql, params)]

    def pending(self) -> list[Memory]:
        return self.list(status=PENDING)

    def count(self) -> int:
        return self.db.query("SELECT COUNT(*) FROM memories")[0][0]

    # ---------- yazma ----------
    def add(self, cand: Candidate) -> AddResult:
        """Yeni bilgiyi ekler ya da neredeyse aynı kaydı günceller. Gizli bilgi reddedilir."""
        content = _clean(cand.content)
        if not content:
            return AddResult("reddedildi", None, "boş")
        if is_secret_memory(content):
            log.info("Hafıza: gizli bilgi içeren aday reddedildi (kategori=%s)", cand.category)
            return AddResult("reddedildi", None, "gizli bilgi")
        if cand.category not in CATEGORIES:
            cand = replace(cand, category="personal")
        importance = max(1, min(5, int(cand.importance)))

        kind = cand.sensitive_kind if cand.sensitive_kind in SENSITIVE_KINDS else None
        local_kind = local_sensitive_kind(content)
        sensitive = cand.sensitive or kind is not None or local_kind is not None
        if sensitive and kind is None:
            kind = local_kind or "other"
        # Durum: kullanıcı kendisi eklemediyse çıkarım ve hassas bilgi onay bekler
        if cand.confirmed_by_user:
            status = ACTIVE
        elif cand.is_inference or sensitive:
            status = PENDING
        else:
            status = ACTIVE

        now = self.now()
        dup = None if cand.category == "conversation_summary" else self._find_duplicate(content)
        if dup is not None:
            return self._merge(dup, content, cand, importance, sensitive, kind, now)

        mid = self.db.execute(
            "INSERT INTO memories (category, content, norm, importance, source, is_inference, sensitive,"
            " sensitive_kind, status, created_at, updated_at, expires_at, use_count)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)",
            (cand.category, content, normalize(content), importance, cand.source, int(cand.is_inference),
             int(sensitive), kind if sensitive else None, status, _iso(now), _iso(now), _iso(cand.expires_at)))
        log.info("Hafıza: kayıt eklendi (no=%d, kategori=%s, durum=%s, çıkarım=%s, hassas=%s)",
                 mid, cand.category, status, cand.is_inference, sensitive)
        return AddResult("eklendi", self.get(mid))

    def _find_duplicate(self, content: str) -> Memory | None:
        for m in self.list():
            if m.category == "conversation_summary":
                continue
            if is_near_duplicate(m.content, content):
                return m
        return None

    def _merge(self, old: Memory, content: str, cand: Candidate, importance: int, sensitive: bool,
               kind: str | None, now: datetime) -> AddResult:
        if cand.is_inference:
            # Çıkarım mevcut kaydın metnini ve durumunu değiştirmez; yalnızca önemini/tazeliğini artırır
            new_content, sens, new_kind, is_inf = old.content, old.sensitive, old.sensitive_kind, old.is_inference
            new_status = ACTIVE if cand.confirmed_by_user else old.status
            source, category = old.source, old.category
        else:
            # Kullanıcının kendi söylediği en yeni ifade geçerlidir (ör. düzeltme)
            new_content, sens, new_kind, is_inf = content, sensitive, (kind if sensitive else None), False
            already_approved = old.status == ACTIVE and old.sensitive
            new_status = ACTIVE if (cand.confirmed_by_user or not sensitive or already_approved) else PENDING
            source, category = cand.source, cand.category
        # Biri kalıcıysa kayıt kalıcı olur, ikisi de geçiciyse en geç olan
        if old.expires_at is None or cand.expires_at is None:
            expires = None
        else:
            expires = max(old.expires_at, cand.expires_at)
        self.db.execute(
            "UPDATE memories SET content=?, norm=?, importance=?, is_inference=?, sensitive=?, sensitive_kind=?,"
            " status=?, updated_at=?, expires_at=?, source=?, category=? WHERE id=?",
            (new_content, normalize(new_content), max(old.importance, importance), int(is_inf),
             int(sens), new_kind, new_status, _iso(now), _iso(expires), source, category, old.id))
        log.info("Hafıza: benzer kayıt birleştirildi (no=%d)", old.id)
        return AddResult("birleştirildi", self.get(old.id))

    def replace_content(self, memory_id: int, cand: Candidate) -> AddResult:
        """Yanlış bir kaydı yeni bilgiyle düzeltir (kullanıcı "aslında ..." dediğinde)."""
        old = self.get(memory_id)
        if old is None or cand.is_inference:
            return self.add(cand)
        content = _clean(cand.content)
        if not content or is_secret_memory(content):
            return AddResult("reddedildi", None, "gizli bilgi" if content else "boş")
        local_kind = local_sensitive_kind(content)
        sensitive = cand.sensitive or local_kind is not None or bool(cand.sensitive_kind)
        # Daha önce onaylanmış kayıt düzeltiliyorsa ve yeni metin de hassas değilse etkin kalır
        status = ACTIVE if (cand.confirmed_by_user or not sensitive or (old.status == ACTIVE and old.sensitive)) \
            else PENDING
        kind = (cand.sensitive_kind if cand.sensitive_kind in SENSITIVE_KINDS else None) or local_kind
        self.db.execute(
            "UPDATE memories SET content=?, norm=?, importance=?, is_inference=0, sensitive=?, sensitive_kind=?,"
            " status=?, updated_at=?, expires_at=?, source=?, category=? WHERE id=?",
            (content, normalize(content), max(1, min(5, cand.importance)), int(sensitive),
             (kind or "other") if sensitive else None, status, _iso(self.now()), _iso(cand.expires_at),
             cand.source, cand.category if cand.category in CATEGORIES else old.category, memory_id))
        log.info("Hafıza: kayıt düzeltildi (no=%d)", memory_id)
        return AddResult("güncellendi", self.get(memory_id))

    def update(self, memory_id: int, *, content: str | None = None, category: str | None = None,
               importance: int | None = None, expires_at: datetime | None | str = "değişmedi") -> Memory | None:
        """Kullanıcının arayüzden yaptığı düzenleme: kayıt artık kullanıcı onaylı bilgidir."""
        old = self.get(memory_id)
        if old is None:
            return None
        new_content = _clean(content) if content is not None else old.content
        if not new_content:
            raise ValueError("Hafıza kaydı boş olamaz.")
        if is_secret_memory(new_content):
            raise SecretRejected("gizli bilgi")
        new_cat = category if category in CATEGORIES else old.category
        new_imp = max(1, min(5, importance)) if importance is not None else old.importance
        new_exp = old.expires_at if expires_at == "değişmedi" else expires_at
        self.db.execute(
            "UPDATE memories SET content=?, norm=?, category=?, importance=?, expires_at=?, updated_at=?,"
            " status=?, is_inference=0, source=CASE WHEN source LIKE 'konuşma:%' THEN source ELSE ? END"
            " WHERE id=?",
            (new_content, normalize(new_content), new_cat, new_imp, _iso(new_exp), _iso(self.now()), ACTIVE,
             SOURCE_USER, memory_id))
        log.info("Hafıza: kayıt kullanıcı tarafından düzenlendi (no=%d)", memory_id)
        return self.get(memory_id)

    def confirm(self, memory_id: int) -> Memory | None:
        """Onay bekleyen kaydı kullanıcı onayladı."""
        self.db.execute("UPDATE memories SET status=?, updated_at=? WHERE id=?",
                        (ACTIVE, _iso(self.now()), memory_id))
        log.info("Hafıza: kayıt kullanıcı tarafından onaylandı (no=%d)", memory_id)
        return self.get(memory_id)

    def delete(self, memory_id: int) -> bool:
        existed = self.get(memory_id) is not None
        self.db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        if existed:
            log.info("Hafıza: kayıt silindi (no=%d)", memory_id)
        return existed

    def delete_many(self, ids: list[int]) -> int:
        return sum(1 for i in ids if self.delete(i))

    def purge_expired(self) -> int:
        """Süresi dolan geçici kayıtları kalıcı olarak siler."""
        now = _iso(self.now())
        n = self.db.query("SELECT COUNT(*) FROM memories WHERE expires_at IS NOT NULL AND expires_at <= ?",
                          (now,))[0][0]
        if n:
            self.db.execute("DELETE FROM memories WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,))
            log.info("Hafıza: süresi dolan %d kayıt silindi", n)
        return n

    def mark_used(self, ids: list[int]) -> None:
        if not ids:
            return
        now = _iso(self.now())
        self.db.executemany("UPDATE memories SET use_count = use_count + 1, last_used_at = ? WHERE id = ?",
                            [(now, i) for i in ids])

    def find_matching(self, query_tokens: list[str]) -> list[Memory]:
        """Sorgudaki her kelimenin (kökünün) geçtiği kayıtlar. "unut ..." komutu için: yanlış kaydı silmemek
        adına bütün kelimeler eşleşmeli (VE), tek kelime yetmez."""
        qs = [stem(t) for t in query_tokens if t]
        if not qs:
            return []
        out = []
        for m in self.list(include_expired=True):
            mt = tokens(m.content)
            if all(any(tok.startswith(s) for tok in mt) for s in qs):
                out.append(m)
        return out

    def latest_from_conversation(self, conversation_id: int) -> Memory | None:
        rows = self.db.query("SELECT * FROM memories WHERE source = ? AND category != 'conversation_summary'"
                             " ORDER BY updated_at DESC, id DESC LIMIT 1",
                             (f"{SOURCE_CONVERSATION}{conversation_id}",))
        return _row_to_memory(rows[0]) if rows else None

    # ---------- konuşma özetleri ----------
    def mark_summarized(self, conversation_id: int, result: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO memory_summarized (conversation_id, ts_utc, result) VALUES (?,?,?)",
                        (conversation_id, _iso(self.now()), result))

    def unsummarized_conversations(self, exclude_id: int | None, since: datetime, limit: int = 3) -> list[int]:
        rows = self.db.query(
            "SELECT c.id FROM conversations c WHERE c.started_at >= ? AND c.id != ?"
            " AND c.id NOT IN (SELECT conversation_id FROM memory_summarized)"
            " ORDER BY c.id DESC LIMIT ?", (_iso(since), exclude_id if exclude_id is not None else -1, limit))
        return [r[0] for r in rows]


def expiry_in_days(days: int | None, now: datetime) -> datetime | None:
    if days is None:
        return None
    return now + timedelta(days=max(1, min(365, int(days))))
