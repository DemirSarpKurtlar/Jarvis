"""Bilgisayar kullanılırken ekranda duran küçük gösterge: "Bilgisayarı kullanıyorum — durdurmak için Esc Esc".

Birincil ekranın sol alt köşesinde, her zaman üstte, odak almaz ve tıklamaları geçirir (ajanın
tıklamaları altındaki uygulamaya gider). Pencere ilk etkinlikte oluşturulur; oturum bitince gizlenir.
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from jarvis.core.events import EventBus, Topics

log = logging.getLogger(__name__)

MARGIN = 14


class _IndicatorWindow(QWidget):
    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool | Qt.WindowType.WindowDoesNotAcceptFocus
                         | Qt.WindowType.WindowTransparentForInput)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setObjectName("agentIndicator")
        self.setStyleSheet("#agentIndicator { background: rgba(7, 13, 26, 235); border: 1px solid #F2B84B;"
                           " border-radius: 8px; } QLabel { color: #E6EDF7; }")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(2)
        self.title = QLabel()
        self.title.setStyleSheet("font-weight: 600; color: #F2B84B;")
        self.detail = QLabel()
        self.detail.setStyleSheet("font-size: 11px;")
        lay.addWidget(self.title)
        lay.addWidget(self.detail)

    def place(self) -> None:
        self.adjustSize()
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self.move(area.left() + MARGIN, area.bottom() - self.height() - MARGIN)

    def showEvent(self, event) -> None:  # noqa: ANN001
        super().showEvent(event)
        try:
            from jarvis.interface.overlay import _win

            w = _win()
            if w is not None:
                w.make_overlay(int(self.winId()), click_through=True)
                w.topmost(int(self.winId()))
        except Exception:  # noqa: BLE001
            log.debug("Gösterge pencere stili uygulanamadı", exc_info=True)


class AgentIndicator:
    def __init__(self, bus: EventBus) -> None:
        self.window: _IndicatorWindow | None = None
        self._unsub = bus.subscribe(Topics.AGENT_STATUS, self._on_status)

    @property
    def visible(self) -> bool:
        return self.window is not None and self.window.isVisible()

    def _on_status(self, st: Any) -> None:
        if getattr(st, "active", False):
            if self.window is None:
                self.window = _IndicatorWindow()
            self.window.title.setText(getattr(st, "indicator", "") or "Bilgisayarı kullanıyorum")
            self.window.detail.setText(str(getattr(st, "text", ""))[:90])
            self.window.place()
            if not self.window.isVisible():
                self.window.show()
        elif self.window is not None:
            self.window.hide()

    def shutdown(self) -> None:
        self._unsub()
        if self.window is not None:
            # deleteLater yok (overlay.py ile aynı gerekçe): gizli pencere referansla birlikte gider
            self.window.hide()
            self.window = None
