"""Kısa süreli konuşma bağlamı ve konuşma kaydı.

- Claude'a yalnızca son birkaç mesaj gönderilir (maliyet); tüm geçmiş değil.
- Geçmiş yalnızca metindir (bkz. claude_client: düşünme blokları geri gönderilmez).
- Uzun süre (varsayılan 30 dk) sessizlikten sonra yeni konuşma başlar.
- Konuşmalar SQLite'a yazılır; "Konuşmalar" ekranı buradan okur.
  Uzun süreli hafıza (özetleme, önemli bilgiler) Aşama 4'te eklenecek.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone

from jarvis.storage.database import Database

MAX_CONTEXT_MESSAGES = 16      # son 8 soru-cevap
IDLE_RESET_S = 30 * 60


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Turn:
    role: str   # "user" | "assistant"
    text: str


class ConversationManager:
    def __init__(self, db: Database, max_messages: int = MAX_CONTEXT_MESSAGES,
                 idle_reset_s: float = IDLE_RESET_S, clock=time.monotonic) -> None:  # noqa: ANN001
        self.db = db
        self.max_messages = max_messages
        self.idle_reset_s = idle_reset_s
        self._clock = clock
        self.turns: list[Turn] = []
        self.conversation_id: int | None = None
        self._last = 0.0

    def new_conversation(self) -> None:
        self.turns = []
        self.conversation_id = None

    def _ensure(self, first_text: str) -> int:
        now = self._clock()
        if self.conversation_id is not None and now - self._last > self.idle_reset_s:
            self.new_conversation()
        self._last = now
        if self.conversation_id is None:
            title = first_text.strip().replace("\n", " ")
            title = title[:60] + ("…" if len(title) > 60 else "")
            self.conversation_id = self.db.execute(
                "INSERT INTO conversations (started_at, title) VALUES (?, ?)", (_utc(), title))
        return self.conversation_id

    def api_messages(self, user_text: str) -> list[dict]:
        """Gönderilecek mesajlar: son pencere + yeni kullanıcı mesajı. İlk mesaj daima 'user'."""
        window = self.turns[-(self.max_messages - 1):] if self.max_messages > 1 else []
        while window and window[0].role != "user":
            window = window[1:]
        msgs = [{"role": t.role, "content": t.text} for t in window]
        msgs.append({"role": "user", "content": user_text})
        return msgs

    def log_user(self, text: str) -> None:
        cid = self._ensure(text)
        self.db.execute("INSERT INTO messages (conversation_id, ts_utc, role, text) VALUES (?,?,?,?)",
                        (cid, _utc(), "user", text))

    def commit_exchange(self, user_text: str, assistant_text: str, model: str, cost: float) -> None:
        """Başarılı bir soru-cevabı bağlama ve kayda ekler."""
        cid = self._ensure(user_text)
        self.db.execute(
            "INSERT INTO messages (conversation_id, ts_utc, role, text, model, cost_usd) VALUES (?,?,?,?,?,?)",
            (cid, _utc(), "assistant", assistant_text, model, cost))
        self.turns += [Turn("user", user_text), Turn("assistant", assistant_text)]
        # Bellekte de sınırsız büyümesin
        if len(self.turns) > self.max_messages * 2:
            self.turns = self.turns[-self.max_messages:]

    # ---- "Konuşmalar" ekranı için ----
    def list_conversations(self, limit: int = 100) -> list[dict]:
        rows = self.db.query(
            "SELECT c.id, c.started_at, c.title, COUNT(m.id) n, COALESCE(SUM(m.cost_usd),0) cost"
            " FROM conversations c LEFT JOIN messages m ON m.conversation_id = c.id"
            " GROUP BY c.id ORDER BY c.id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def messages_of(self, conversation_id: int) -> list[dict]:
        rows = self.db.query("SELECT ts_utc, role, text, model, cost_usd FROM messages"
                             " WHERE conversation_id = ? ORDER BY id", (conversation_id,))
        return [dict(r) for r in rows]
