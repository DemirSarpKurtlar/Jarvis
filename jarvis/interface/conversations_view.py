"""Konuşmalar ekranı: geçmiş konuşmalar ve mesajları (yalnızca okuma)."""

from __future__ import annotations

import html
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QTextBrowser, QVBoxLayout,
                               QWidget)

from jarvis.core.assistant import Assistant
from jarvis.interface.format import usd
from jarvis.interface.theme import C


def _local(ts_utc: str) -> str:
    try:
        return datetime.fromisoformat(ts_utc).astimezone().strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return ts_utc


class ConversationsView(QWidget):
    def __init__(self, assistant: Assistant, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.assistant = assistant
        lay = QVBoxLayout(self)
        lay.setContentsMargins(40, 30, 40, 30)
        lay.addWidget(QLabel("Konuşmalar", objectName="BosBaslik"))
        row = QHBoxLayout()
        self.list = QListWidget()
        self.list.setFixedWidth(320)
        self.list.setStyleSheet(f"QListWidget {{ border:1px solid {C.CELIK}; border-radius:12px; padding:6px; }}"
                                f"QListWidget::item {{ padding:8px; border-radius:8px; }}"
                                f"QListWidget::item:selected {{ background: rgba(82,228,242,0.10); color:{C.BUZ}; }}")
        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(True)
        self.view.setStyleSheet(f"QTextBrowser {{ border:1px solid {C.CELIK}; border-radius:12px; padding:10px; }}")
        row.addWidget(self.list)
        row.addWidget(self.view, 1)
        lay.addLayout(row, 1)
        self.empty = QLabel("Henüz konuşma yok. Panel ekranından JARVIS'e bir şey yazarak başlayabilirsin.",
                            objectName="BosMetin")
        lay.addWidget(self.empty)
        self.list.currentItemChanged.connect(self._show)

    def refresh(self) -> None:
        convs = self.assistant.chat.conversation.list_conversations()
        self.list.clear()
        for c in convs:
            item = QListWidgetItem(f"{c['title'] or 'Adsız'}\n{_local(c['started_at'])}, {usd(c['cost'] or 0)}")
            item.setData(Qt.ItemDataRole.UserRole, c["id"])
            self.list.addItem(item)
        self.empty.setVisible(not convs)
        if convs:
            self.list.setCurrentRow(0)
        else:
            self.view.clear()

    def _show(self, item: QListWidgetItem | None) -> None:
        if item is None:
            return
        rows = self.assistant.chat.conversation.messages_of(item.data(Qt.ItemDataRole.UserRole))
        parts = []
        for m in rows:
            who = "Sen" if m["role"] == "user" else "JARVIS"
            color = C.SIS if m["role"] == "user" else C.BUZ
            meta = f" <span style='color:{C.SIS};font-size:11px'>{m['model'] or ''} {usd(m['cost_usd'])}</span>" \
                if m["role"] == "assistant" and m["cost_usd"] is not None else ""
            body = html.escape(m["text"]).replace("\n", "<br>")
            parts.append(f"<p><span style='color:{C.SIS};font-size:12px'>{who}, {_local(m['ts_utc'])}</span>{meta}"
                         f"<br><span style='color:{color}'>{body}</span></p>")
        self.view.setHtml("".join(parts))

    def showEvent(self, event) -> None:  # noqa: ANN001
        super().showEvent(event)
        self.refresh()
