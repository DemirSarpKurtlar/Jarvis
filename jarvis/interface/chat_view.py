"""Panel ekranındaki canlı sohbet alanı: son soru ve akışla gelen cevap."""

from __future__ import annotations

import html

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QFrame, QGridLayout, QLabel, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from jarvis.core.chat_service import ChatDone, ChatStarted
from jarvis.interface.format import usd
from jarvis.interface.theme import C

WELCOME = ("Merhaba! \"Hey Jarvis\" diyerek, Konuş düğmesine basarak ya da yazarak benimle konuşabilirsin. "
           "Bilgisayarını yönetme yeteneğim sonraki aşamalarda eklenecek.")


class ChatView(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMaximumWidth(760)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setMaximumHeight(230)
        inner = QWidget()
        grid = QGridLayout(inner)
        grid.setContentsMargins(0, 0, 8, 0)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(10)
        grid.setColumnMinimumWidth(0, 56)
        grid.setColumnStretch(1, 1)

        self.user_who = QLabel("Sen", objectName="Kim")
        self.user_text = QLabel(objectName="SozSen")
        self.jarvis_who = QLabel("JARVIS", objectName="Kim")
        self.jarvis_text = QLabel(WELCOME, objectName="SozJarvis")
        self.meta = QLabel(objectName="Meta")
        for w in (self.user_who, self.jarvis_who):
            w.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        for w in (self.user_text, self.jarvis_text, self.meta):
            w.setWordWrap(True)
            w.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                      | Qt.TextInteractionFlag.LinksAccessibleByMouse)
        self.meta.setOpenExternalLinks(True)
        self.meta.setTextFormat(Qt.TextFormat.RichText)
        grid.addWidget(self.user_who, 0, 0)
        grid.addWidget(self.user_text, 0, 1)
        grid.addWidget(self.jarvis_who, 1, 0)
        grid.addWidget(self.jarvis_text, 1, 1)
        grid.addWidget(self.meta, 2, 1)
        self.scroll.setWidget(inner)
        outer.addWidget(self.scroll)

        self.user_who.hide()
        self.user_text.hide()
        self.meta.hide()
        self._buffer = ""
        # Akış parçalarını kare başına bir kez çiz: her harfte yeniden yerleşim yapma
        self._flush = QTimer(self, interval=40, singleShot=True, timeout=self._render_buffer)

    # ---- olaylar ----
    def on_started(self, ev: ChatStarted) -> None:
        self.user_who.show()
        self.user_text.show()
        self.user_text.setText(ev.user_text)
        self._buffer = ""
        self.jarvis_text.setStyleSheet("")
        self.jarvis_text.setText("…")
        self._set_meta(ev.model_name + (", web'de arıyor" if ev.web_search else ""), note=ev.note)

    def on_delta(self, text: str) -> None:
        self._buffer += text
        if not self._flush.isActive():
            self._flush.start()

    def _render_buffer(self) -> None:
        self.jarvis_text.setText(self._buffer or "…")
        self._scroll_to_end()

    def on_done(self, ev: ChatDone) -> None:
        self._flush.stop()
        self.jarvis_text.setText(ev.text)
        self.jarvis_text.setStyleSheet(f"color:{C.AMBER};" if ev.refused else "")
        self._set_meta(f"{ev.model_name}, {usd(ev.cost_usd)}", ev.citations, ev.note)
        self._scroll_to_end()

    def on_failed(self, message: str) -> None:
        self._flush.stop()
        self.jarvis_text.setStyleSheet(f"color:{C.AMBER};")
        self.jarvis_text.setText(message)
        self.meta.hide()

    # ---- yardımcılar ----
    def _set_meta(self, head: str, citations: list[tuple[str, str]] | None = None, note: str | None = None) -> None:
        parts = [html.escape(head)]
        if citations:
            links = ", ".join(f'<a style="color:{C.CYAN};text-decoration:none" href="{html.escape(u)}">'
                              f"{html.escape(t)}</a>" for t, u in citations[:6])
            parts.append(f"Kaynaklar: {links}")
        if note:
            parts.append(f'<span style="color:{C.AMBER}">{html.escape(note)}</span>')
        self.meta.setText("<br>".join(parts))
        self.meta.show()

    def _scroll_to_end(self) -> None:
        bar = self.scroll.verticalScrollBar()
        # Yeni metin yerleşimi bir sonraki olay turunda hesaplanır; ondan sonra kaydır
        bar.rangeChanged.connect(self._on_range, Qt.ConnectionType.UniqueConnection)
        QTimer.singleShot(0, lambda: bar.setValue(bar.maximum()))

    def _on_range(self, _min: int, maximum: int) -> None:
        self.scroll.verticalScrollBar().setValue(maximum)
