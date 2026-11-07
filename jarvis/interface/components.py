"""Küçük, yeniden kullanılabilir arayüz parçaları: ikonlar, çubuklar, sparkline."""

from __future__ import annotations

from collections import deque

from PySide6.QtCore import QByteArray, QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QIcon, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QFrame, QSizePolicy, QWidget

from jarvis.interface.theme import C, qcolor

# 24x24 çizgi ikonlar (taslaktakiyle aynı)
ICON_PATHS: dict[str, str] = {
    "panel": '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3"/>',
    "konusmalar": '<path d="M4 6h16M4 12h10M4 18h13"/>',
    "hafiza": '<path d="M12 3c-4 0-7 2.5-7 6 0 2 1 3.4 2 4.4V18l3-1.5h2c4 0 7-2.5 7-6s-3-7.5-7-7.5z"/>',
    "sistem": '<rect x="4" y="5" width="16" height="11" rx="2"/><path d="M9 20h6M12 16v4"/>',
    "otomasyon": '<path d="M5 12a7 7 0 0 1 12-5l2-2v6h-6l2-2a4 4 0 1 0 1 5"/>',
    "ayarlar": '<circle cx="12" cy="12" r="3"/><path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1"/>',
    "kapat": '<path d="M12 3v8M7 6.5a7 7 0 1 0 10 0"/>',
    "mikrofon": '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/>',
    "mikrofon_kapali": '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3M4 4l16 16"/>',
    "ses": '<path d="M4 9v6h4l5 4V5L8 9H4zM16 9a4 4 0 0 1 0 6"/>',
    "carpi": '<path d="M6 6l12 12M18 6L6 18"/>',
}


def _render(name: str, color: str, size: int, stroke: float) -> QPixmap:
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
        f'stroke="{color}" stroke-width="{stroke}" stroke-linecap="round" stroke-linejoin="round">'
        f"{ICON_PATHS[name]}</svg>"
    )
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    pm = QPixmap(size * 2, size * 2)  # yüksek DPI için 2x
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    renderer.render(p)
    p.end()
    pm.setDevicePixelRatio(2)
    return pm


def icon(name: str, color: str = C.SIS, active: str | None = None, size: int = 20, stroke: float = 1.6) -> QIcon:
    ic = QIcon()
    ic.addPixmap(_render(name, color, size, stroke), QIcon.Mode.Normal, QIcon.State.Off)
    if active:
        ic.addPixmap(_render(name, active, size, stroke), QIcon.Mode.Normal, QIcon.State.On)
        ic.addPixmap(_render(name, C.BUZ, size, stroke), QIcon.Mode.Active, QIcon.State.Off)
    return ic


class Divider(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Ayrac")


class ThinBar(QWidget):
    """İnce doluluk çubuğu. `hot=True` ise cyan→amber geçişli."""

    def __init__(self, hot: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._value: float | None = 0.0
        self._hot = hot
        self.setFixedHeight(3)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_value(self, fraction: float | None) -> None:
        self._value = None if fraction is None else max(0.0, min(1.0, fraction))
        self.update()

    def paintEvent(self, _e) -> None:  # noqa: ANN001
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qcolor(C.CELIK))
        p.drawRoundedRect(r, 1.5, 1.5)
        if self._value:
            fill = QRectF(r.x(), r.y(), r.width() * self._value, r.height())
            if self._hot:
                g = QLinearGradient(fill.topLeft(), fill.topRight())
                g.setColorAt(0, qcolor(C.CYAN))
                g.setColorAt(1, qcolor(C.AMBER))
                p.setBrush(g)
            else:
                p.setBrush(qcolor(C.CYAN))
            p.drawRoundedRect(fill, 1.5, 1.5)
        p.end()


class Sparkline(QWidget):
    def __init__(self, maxlen: int = 60, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._data: deque[float] = deque(maxlen=maxlen)
        self.setFixedHeight(26)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def push(self, value: float) -> None:
        self._data.append(value)
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(200, 26)

    def paintEvent(self, _e) -> None:  # noqa: ANN001
        if len(self._data) < 2:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height() - 2
        n = self._data.maxlen or len(self._data)
        step = w / max(1, n - 1)
        offset = (n - len(self._data)) * step
        path = QPainterPath()
        for i, v in enumerate(self._data):
            pt = QPointF(offset + i * step, 1 + h - (min(100.0, max(0.0, v)) / 100) * h)
            path.moveTo(pt) if i == 0 else path.lineTo(pt)
        p.setPen(QPen(qcolor(C.CYAN), 1.4))
        p.drawPath(path)
        p.end()
