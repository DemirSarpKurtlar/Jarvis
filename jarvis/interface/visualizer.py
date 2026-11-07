"""Ortadaki "reaktör" ses görselleştiricisi.

Her asistan durumunun kendine özgü rengi ve hareketi vardır. Çizim tamamen
QPainter ile yapılır. Pencere görünmezken zamanlayıcı durur (boşta CPU
harcamaz). `set_level()` ileride mikrofon/TTS ses seviyesini besleyecek;
o zamana kadar dinleme/konuşma hareketi prosedürel üretilir.
"""

from __future__ import annotations

import math
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QBrush, QFont, QPainter, QPaintEvent, QPen, QPixmap, QRadialGradient
from PySide6.QtWidgets import QSizePolicy, QWidget

from jarvis.core.state import AssistantState
from jarvis.interface.theme import STATE_LOOKS, C, Fonts, qcolor

S = AssistantState
N_BARS = 120


def _noise(i: int, t: float) -> float:
    """0..1 aralığında, yumuşak değişen sözde gürültü."""
    v = (math.sin(i * 0.37 + t * 3.1) + math.sin(i * 0.11 - t * 1.7) * 0.7
         + math.sin(i * 1.3 + t * 5.3) * 0.3) / 4 + 0.5
    return min(1.0, max(0.0, v))


class ReactorWidget(QWidget):
    FPS_ACTIVE = 30
    FPS_IDLE = 8   # beklemede hareket çok yavaş: az kare, düşük CPU

    def __init__(self, fonts: Fonts, reduced_motion: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(280, 280)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        self._fonts = fonts
        self._reduced = reduced_motion
        self._state = S.STANDBY
        self._state_t = time.monotonic()
        self._subtitle_override: str | None = None
        self._levels = [0.0] * N_BARS
        self._external_level: float | None = None
        self._cache: dict[str, tuple[tuple, QPixmap]] = {}
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.update)
        self._apply_fps()

    # ---- dışarıya açık API ----
    @property
    def state(self) -> AssistantState:
        return self._state

    def set_state(self, state: AssistantState, detail: str = "") -> None:
        if state is not self._state:
            self._state_t = time.monotonic()
        self._state = state
        self._subtitle_override = detail or None
        self._apply_fps()
        self.update()

    def set_level(self, level: float | None) -> None:
        """0..1 arası gerçek ses seviyesi (Aşama 3). None: prosedürel hareket."""
        self._external_level = None if level is None else max(0.0, min(1.0, level))

    def set_reduced_motion(self, reduced: bool) -> None:
        self._reduced = reduced
        self._apply_fps()
        self.update()

    # ---- zamanlayıcı yönetimi ----
    def _apply_fps(self) -> None:
        if self._reduced:
            self._timer.stop()
            return
        fps = self.FPS_IDLE if self._state in (S.STANDBY, S.ERROR) else self.FPS_ACTIVE
        self._timer.setInterval(int(1000 / fps))
        if self.isVisible() and not self._timer.isActive():
            self._timer.start()

    def showEvent(self, event) -> None:  # noqa: ANN001
        super().showEvent(event)
        self._apply_fps()

    def hideEvent(self, event) -> None:  # noqa: ANN001
        super().hideEvent(event)
        self._timer.stop()

    @property
    def animating(self) -> bool:
        return self._timer.isActive()

    # ---- çizim ----
    def _target_level(self, i: int, z: float, s: float) -> float:
        st = self._state
        if self._reduced:
            return 0.05
        if st is S.STANDBY:
            return 0.04 + 0.03 * math.sin(z * 1.2)
        if st is S.WAKE_DETECTED:
            return 0.45 * max(0.0, 1 - s * 1.6) + 0.08
        if st is S.LISTENING:
            amp = self._external_level if self._external_level is not None else (0.6 + 0.4 * math.sin(z * 2.3))
            return 0.08 + 0.55 * _noise(i, z) ** 2 * amp
        if st is S.SPEAKING:
            amp = self._external_level if self._external_level is not None else (math.sin(z * 4.4) * 0.3 + 0.7)
            return 0.1 + 0.32 * (math.sin(i / N_BARS * math.pi * 8 + z * 6) * 0.5 + 0.5) * amp
        if st is S.ERROR:
            return 0.05
        return 0.05

    # ---- katman önbelleği ----
    def _layer(self, kind: str, draw) -> QPixmap:  # noqa: ANN001
        """Değişmeyen katmanları (ölçek, halka, yazı) bir kez çizip saklar."""
        dpr = self.devicePixelRatioF()
        look = STATE_LOOKS[self._state]
        key = (kind, self.width(), self.height(), dpr, self._state, self._subtitle_override)
        cached = self._cache.get(kind)
        if cached is not None and cached[0] == key:
            return cached[1]
        pm = QPixmap(int(self.width() * dpr), int(self.height() * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.translate(self.width() / 2, self.height() / 2)
        draw(p, look)
        p.end()
        self._cache[kind] = (key, pm)
        return pm

    def _geometry(self) -> tuple[float, float]:
        side = min(self.width(), self.height())
        return side, side * 0.30

    def _draw_background(self, p: QPainter, look) -> None:  # noqa: ANN001
        side, R = self._geometry()
        color = look.color
        # Dış ölçek halkası
        p.setPen(QPen(qcolor(C.SIS, 0.22), 1))
        for i in range(72):
            a = i / 72 * math.tau
            r1, r2 = R * 1.52, R * (1.6 if i % 6 == 0 else 1.56)
            p.drawLine(QPointF(math.cos(a) * r1, math.sin(a) * r1), QPointF(math.cos(a) * r2, math.sin(a) * r2))
        # İç dolgu
        grad = QRadialGradient(QPointF(0, 0), R)
        grad.setColorAt(0, qcolor(C.GECE, 0))
        grad.setColorAt(1, qcolor(color, 0.08))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(grad))
        p.drawEllipse(QPointF(0, 0), R * 0.98, R * 0.98)
        # Ana halka + parıltı (yarı saydam katmanlar)
        p.setBrush(Qt.BrushStyle.NoBrush)
        for width, alpha in ((14, 0.05), (8, 0.09), (4, 0.18)):
            p.setPen(QPen(qcolor(color, alpha), width))
            p.drawEllipse(QPointF(0, 0), R, R)
        p.setPen(QPen(qcolor(color), 2))
        p.drawEllipse(QPointF(0, 0), R, R)

    def _draw_text(self, p: QPainter, look) -> None:  # noqa: ANN001
        side, R = self._geometry()
        title_font = QFont(self._fonts.display)
        title_font.setPixelSize(max(20, int(side * 0.075)))
        title_font.setWeight(QFont.Weight.Normal)
        p.setFont(title_font)
        p.setPen(qcolor(look.color))
        p.drawText(QRectF(-R * 0.95, -side * 0.075, R * 1.9, side * 0.09), Qt.AlignmentFlag.AlignCenter, look.title)
        sub_font = QFont(self._fonts.body)
        sub_font.setPixelSize(max(11, int(side * 0.024)))
        p.setFont(sub_font)
        p.setPen(qcolor(C.SIS))
        p.drawText(QRectF(-R * 0.72, side * 0.025, R * 1.44, side * 0.14),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap,
                   self._subtitle_override or look.subtitle)

    def resizeEvent(self, event) -> None:  # noqa: ANN001
        super().resizeEvent(event)
        self._cache.clear()

    # ---- çizim ----
    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: ARG002
        look = STATE_LOOKS[self._state]
        now = time.monotonic()
        z, s = now, now - self._state_t
        w, h = self.width(), self.height()
        _, R = self._geometry()
        color = look.color

        p = QPainter(self)
        p.drawPixmap(0, 0, self._layer("background", self._draw_background))
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.translate(w / 2, h / 2)

        # Radyal çubuklar (her karede değişen tek ana katman)
        smoothing = 1.0 if self._reduced else 0.18
        bar_pen = QPen(qcolor(color), 2)
        bar_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        for i in range(N_BARS):
            self._levels[i] += (self._target_level(i, z, s) - self._levels[i]) * smoothing
            if self._state is S.ERROR and i % 7 < 2:
                continue  # kırık halka
            lv = self._levels[i]
            a = i / N_BARS * math.tau - math.pi / 2
            r1, r2 = R * 1.06, R * (1.06 + lv * 0.9)
            bar_pen.setColor(qcolor(color, 0.35 + lv))
            p.setPen(bar_pen)
            p.drawLine(QPointF(math.cos(a) * r1, math.sin(a) * r1), QPointF(math.cos(a) * r2, math.sin(a) * r2))

        # Düşünme: dönen yaylar
        if self._state is S.PROCESSING:
            for k in range(3):
                pen = QPen(qcolor(color, 0.9 - k * 0.25), 3 - k * 0.6)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                p.setPen(pen)
                rk = R * (0.86 - k * 0.08)
                start = z * (1.6 + k * 0.7) * (-1 if k % 2 else 1)
                span = math.pi * (0.5 + k * 0.2)
                p.drawArc(QRectF(-rk, -rk, 2 * rk, 2 * rk), int(-math.degrees(start) * 16), int(-math.degrees(span) * 16))

        # Yürütme: dolan segmentli halka
        if self._state is S.EXECUTING:
            segs = 36
            filled = int((s * 14) % (segs + 8))
            rk = R * 0.86
            for k in range(segs):
                p.setPen(QPen(qcolor(color, 0.95 if k < filled else 0.15), 4))
                a0 = k / segs * 360 - 90 + 1.5
                p.drawArc(QRectF(-rk, -rk, 2 * rk, 2 * rk), int(-a0 * 16), int(-(360 / segs - 3) * 16))

        # Uyandı: dışa yayılan dalga
        if self._state is S.WAKE_DETECTED and s < 1.2:
            p.setPen(QPen(qcolor(color, max(0.0, 1 - s / 1.2)), 2))
            rw = R * (1 + s * 0.9)
            p.drawEllipse(QPointF(0, 0), rw, rw)

        p.resetTransform()
        p.drawPixmap(0, 0, self._layer("text", self._draw_text))
        p.end()
