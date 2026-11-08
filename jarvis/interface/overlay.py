"""Ekranın üstünde dinleme göstergesi: taslak B, "Kenar ışığı".

Kaynak: `design/taslaklar/jarvis-dinleme-gostergesi-v1.html` (varyant B) ve uygulama notları.

* `ListeningOverlay`: ekranın üst kenarına yapışık çıkıntı (halka + metin) ve iki yana sarkan
  ışık. Tamamen QPainter ile çizilir. Odak almaz, tıklamaları geçirir; Windows'ta imleç çıkıntının
  üstünde durunca yalnızca o an tıklanabilir olur (mikrofon / gizle düğmeleri).
* `OverlayController`: olay veriyolunu dinler, göstergeyi yalnızca sesli akışlarda (uyandırma ya
  da Konuş) açar, beklemeye dönünce 2,5 sn sonra kenara kaydırıp gizler.

Performans: gizliyken hiçbir zamanlayıcı çalışmaz. Görünürken en fazla 30 fps çizilir; düşünme ve
yürütmede 15 fps, sabit anlarda Windows'ta yalnızca imleç yoklaması (10 Hz), diğerlerinde 0.
"""

from __future__ import annotations

import logging
import math
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from PySide6.QtCore import QByteArray, QLineF, QObject, QPoint, QPointF, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import (QColor, QCursor, QFont, QFontMetricsF, QGuiApplication, QImage, QLinearGradient,
                           QMouseEvent, QPainter, QPainterPath, QPaintEvent, QPen, QTextLayout, QTextOption)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QWidget

from jarvis.core.events import Topics
from jarvis.core.state import AssistantState, StateChange
from jarvis.interface.components import ICON_PATHS
from jarvis.interface.theme import STATE_LOOKS, C, Fonts, qcolor
from jarvis.interface.visualizer import _noise

log = logging.getLogger(__name__)
S = AssistantState
IS_WINDOWS = sys.platform == "win32"

# ---------- ölçüler (dip; 4K'da x1,5) ----------
WINDOW_H = 90          # pencere yüksekliği sabit; çıkıntı içinde büyür (yeniden boyutlandırma titretir)
TAB_H = 38             # tek satırlık çıkıntı yüksekliği
TAB_RADIUS = 17        # alt köşeler
SHOULDER = 14          # kenara kavisli bağlanan omuzlar
RING_BOX = 26
RING_R = 7.2
PAD_L, PAD_R, PAD_R_EMPTY = 8, 16, 8
PAD_TOP, PAD_BOTTOM = 2, 4
TEXT_GAP = 8
STATUS_MAX_W = 440     # durum metni bundan uzunsa kısaltılır
SPEECH_MAX_W = 400     # konuşma/söz metni genişliği
LINE_H = 19
SPEECH_PAD = 4
BTN = 24
BTN_GAP = 4
CTRL_MARGIN, CTRL_MARGIN_EMPTY = 12, 6
BIG_SCREEN_H = 1800    # mantıksal yüksekliği bunu aşan ekranda ölçüler x1,5
DIVIDER = "#2A3B58"
BTN_ICON = "#9DB0C8"
TAB_BG = QColor(7, 13, 26, round(0.97 * 255))
N_RING_BARS = 32
_RING_DIRS = [(math.cos(i / N_RING_BARS * math.tau - math.pi / 2), math.sin(i / N_RING_BARS * math.tau - math.pi / 2))
              for i in range(N_RING_BARS)]

# ---------- zamanlama (sn) ----------
STANDBY_HIDE_MS = 2500
ERROR_HIDE_MS = 6000
MIC_OFF_HIDE_MS = 1800
TRANSCRIPT_HOLD_MS = 1600
HOVER_DWELL_S = 0.3
DROP_S, LEAVE_S, RESIZE_S, COLOR_S, SHAKE_S = 0.45, 0.4, 0.34, 0.35, 0.42

FPS_ANIM = 30
FPS_SLOW = 15
POLL_MS = 100


@dataclass(frozen=True)
class OverlayView:
    """Göstergenin o anki içeriği.

    kind: wake | listening | thinking | executing | speaking | error | standby | mic_off
    style: status (Saira, durum renginde) | speech (iki satır gövde metni) | transcript (tek satır, sonu görünür)
    """
    kind: str
    color: str
    text: str = ""
    secondary: str = ""
    style: str = "status"


# ---------- saf yardımcılar (test edilebilir) ----------
def pick_screen(screens: Sequence[Any], cursor: QPoint, primary: Any | None) -> Any | None:
    """İmlecin bulunduğu ekran; bulunamazsa birincil ekran (o da yoksa ilk ekran)."""
    for scr in screens:
        if scr.geometry().contains(cursor):
            return scr
    if primary is not None:
        return primary
    return screens[0] if screens else None


def scale_for(screen_geometry: QRect) -> float:
    """4K'da %100 ölçekte gösterge çok küçük kalmasın (uygulama notları, "1080p ve 4K")."""
    return 1.5 if screen_geometry.height() > BIG_SCREEN_H else 1.0


# SHQueryUserNotificationState sonuçları
QUNS_NOT_PRESENT = 1
QUNS_BUSY = 2
QUNS_RUNNING_D3D_FULL_SCREEN = 3
QUNS_PRESENTATION_MODE = 4
QUNS_ACCEPTS_NOTIFICATIONS = 5


def _query_notification_state() -> int | None:
    import ctypes

    state = ctypes.c_int(0)
    hr = ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state))  # type: ignore[attr-defined]
    return state.value if hr == 0 else None


def exclusive_fullscreen_active(query: Callable[[], int | None] | None = None) -> bool:
    """Gerçek (exclusive) tam ekran D3D oyunu ya da sunum modu açık mı?

    Bu durumda hiçbir pencere oyunun üstüne çizilemez; göstermeye çalışmak oyunu masaüstüne
    düşürebilir. Kenarlıksız pencere modu (QUNS_BUSY) engel değildir.
    """
    if query is None:
        if not IS_WINDOWS:
            return False
        query = _query_notification_state
    try:
        state = query()
    except Exception:  # noqa: BLE001
        log.debug("Tam ekran durumu sorgulanamadı", exc_info=True)
        return False
    return state in (QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE)


def wrap_lines(text: str, font: QFont, width: float, max_lines: int = 2) -> list[str]:
    """Metni en fazla `max_lines` satıra böler; sığmayan son satır "…" ile kısaltılır."""
    text = " ".join(text.split())
    if not text:
        return []
    layout = QTextLayout(text, font)
    opt = QTextOption()
    opt.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    layout.setTextOption(opt)
    layout.beginLayout()
    spans: list[tuple[int, int]] = []
    while True:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(width)
        spans.append((line.textStart(), line.textLength()))
    layout.endLayout()
    lines = [text[a:a + n].strip() for a, n in spans[:max_lines]]
    if len(spans) > max_lines:
        rest = text[spans[max_lines - 1][0]:]
        lines[-1] = QFontMetricsF(font).elidedText(rest, Qt.TextElideMode.ElideRight, width)
    return lines


def _ease_out_cubic(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def _ease_out_back(t: float, overshoot: float = 0.9) -> float:
    """Taslaktaki cubic-bezier(.25,1.2,.45,1) iniş: hafifçe aşıp oturur."""
    t = max(0.0, min(1.0, t)) - 1
    return 1 + t * t * ((overshoot + 1) * t + overshoot)


def _lerp_color(a: QColor, b: QColor, t: float) -> QColor:
    t = max(0.0, min(1.0, t))
    return QColor.fromRgbF(a.redF() + (b.redF() - a.redF()) * t, a.greenF() + (b.greenF() - a.greenF()) * t,
                           a.blueF() + (b.blueF() - a.blueF()) * t, 1.0)


def _with_alpha(c: QColor, alpha: float) -> QColor:
    out = QColor(c)
    out.setAlphaF(max(0.0, min(1.0, alpha)))
    return out


# ---------- Windows: tıklama geçirme ve odak almama ----------
class _Win32:
    GWL_EXSTYLE = -20
    WS_EX_TOPMOST = 0x00000008
    WS_EX_TRANSPARENT = 0x00000020
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_LAYERED = 0x00080000
    WS_EX_NOACTIVATE = 0x08000000
    HWND_TOPMOST = -1
    SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_NOOWNERZORDER = 0x0001, 0x0002, 0x0010, 0x0200

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        u = ctypes.windll.user32  # type: ignore[attr-defined]
        self._get = u.GetWindowLongPtrW
        self._get.restype = ctypes.c_ssize_t
        self._get.argtypes = [wintypes.HWND, ctypes.c_int]
        self._set = u.SetWindowLongPtrW
        self._set.restype = ctypes.c_ssize_t
        self._set.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        self._pos = u.SetWindowPos
        self._pos.restype = wintypes.BOOL
        self._pos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.c_uint]

    def make_overlay(self, hwnd: int, click_through: bool) -> None:
        ex = self._get(hwnd, self.GWL_EXSTYLE)
        ex |= self.WS_EX_LAYERED | self.WS_EX_NOACTIVATE | self.WS_EX_TOOLWINDOW | self.WS_EX_TOPMOST
        ex = (ex | self.WS_EX_TRANSPARENT) if click_through else (ex & ~self.WS_EX_TRANSPARENT)
        self._set(hwnd, self.GWL_EXSTYLE, ex)

    def set_click_through(self, hwnd: int, on: bool) -> None:
        # Qt bayrağını setWindowFlag ile değiştirmek pencereyi yeniden oluşturur; doğrudan SetWindowLongPtr
        ex = self._get(hwnd, self.GWL_EXSTYLE)
        new = (ex | self.WS_EX_TRANSPARENT) if on else (ex & ~self.WS_EX_TRANSPARENT)
        if new != ex:
            self._set(hwnd, self.GWL_EXSTYLE, new)

    def topmost(self, hwnd: int) -> None:
        """Bazı oyunlar en üstte kalma sırasını ezer: her gösterimde yeniden uygulanır (odak çalmadan)."""
        self._pos(hwnd, self.HWND_TOPMOST, 0, 0, 0, 0,
                  self.SWP_NOMOVE | self.SWP_NOSIZE | self.SWP_NOACTIVATE | self.SWP_NOOWNERZORDER)


_win32: _Win32 | None = None


def _win() -> _Win32 | None:
    global _win32
    if not IS_WINDOWS:
        return None
    if _win32 is None:
        try:
            _win32 = _Win32()
        except Exception:  # noqa: BLE001
            log.warning("Gösterge için Windows pencere işlevleri yüklenemedi", exc_info=True)
            return None
    return _win32


# ---------- pencere ----------
@dataclass
class _Layout:
    w: float
    h: float
    lines: list[str]
    text_w: float          # metin bloğunun genişliği (ikincil dahil)
    primary_w: float
    secondary: str
    elided_primary: str


class ListeningOverlay(QWidget):
    """Ekranın üst kenarına yapışık gösterge penceresi (ebeveynsiz, odak almaz)."""

    def __init__(self, fonts: Fonts, reduced_motion: bool = False,
                 on_mic: Callable[[], None] | None = None, on_hide: Callable[[], None] | None = None,
                 mic_enabled: Callable[[], bool] | None = None) -> None:
        super().__init__(None)
        flags = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                 | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.NoDropShadowWindowHint)
        if not IS_WINDOWS:
            # Windows dışında imleç yoklaması yok: pencere her zaman tıklamaları geçirir, düğme yok
            flags |= Qt.WindowType.WindowTransparentForInput
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)
        self.setWindowTitle("JARVIS dinleme göstergesi")
        self.setAccessibleName("JARVIS dinleme göstergesi")

        # Zaman kaynağı: ekran görüntüsü betiği animasyonu belirli bir anda dondurmak için değiştirir
        self.clock: Callable[[], float] = time.monotonic
        self._fonts = fonts
        self.reduced_motion = reduced_motion
        self._on_mic = on_mic
        self._on_hide = on_hide
        self._mic_enabled = mic_enabled or (lambda: True)
        self._k = 1.0
        self._dpi = 96.0
        self._font_cache: dict[tuple, QFont] = {}
        self._svg_cache: dict[tuple[str, str], QSvgRenderer] = {}
        self._shadow: tuple[tuple[int, int], QImage, QRectF] | None = None
        self._alpha_cache: dict[tuple[int, int], QColor] = {}
        self._tab_cache: tuple[tuple, QImage, QRectF] | None = None
        self._ring_cache: tuple[tuple, QImage, QRectF] | None = None

        now = self.clock()
        self.view = OverlayView("standby", C.CYAN)
        self._view_t = now
        self._color = QColor(C.CYAN)
        self._color_from = QColor(C.CYAN)
        self._color_t = now - COLOR_S
        self._layout = self._compute_layout(self.view, False)
        self._tab_w, self._tab_h = self._layout.w, self._layout.h
        self._tab_from = (self._tab_w, self._tab_h)
        self._tab_t = now - RESIZE_S
        self._shown_t = now - DROP_S
        self._leave_t: float | None = None
        self._shake_t = now - SHAKE_S
        self._level = 0.0
        self._level_target = 0.0
        self._ring = [0.0] * N_RING_BARS
        self.hovered = False
        self._hover_since: float | None = None
        self._hover_btn: str | None = None
        self._click_through = True

        self._frame = QTimer(self)
        self._frame.setTimerType(Qt.TimerType.PreciseTimer)
        self._frame.timeout.connect(self._tick)

    # ---------- dışarıya açık API ----------
    @property
    def leaving(self) -> bool:
        return self._leave_t is not None

    @property
    def animating(self) -> bool:
        return self._frame.isActive()

    def present(self, geometry: QRect) -> None:
        """Verilen ekranın üst kenarında göster (odak çalmadan)."""
        self._k = scale_for(geometry)
        self.setGeometry(geometry.x(), geometry.y(), geometry.width(), math.ceil(WINDOW_H * self._k))
        now = self.clock()
        was_visible = self.isVisible() and not self.leaving
        self._leave_t = None
        self._layout = self._compute_layout(self.view, self.hovered)
        if not was_visible:
            self._tab_w, self._tab_h = self._layout.w, self._layout.h
            self._tab_t = now - RESIZE_S
            self._shown_t = now - DROP_S if self.reduced_motion else now
            self._color = QColor(self.view.color)
            self._color_t = now - COLOR_S
        if IS_WINDOWS:
            self.winId()  # HWND gösterimden önce oluşsun: stiller ilk karede doğru olsun
            self._apply_win_styles()
        if not self.isVisible():
            self.show()   # activateWindow()/raise_() asla çağrılmaz
        if IS_WINDOWS:
            self._apply_win_styles()
            w = _win()
            if w is not None:
                try:
                    w.topmost(int(self.winId()))
                except Exception:  # noqa: BLE001
                    log.debug("Gösterge en üste alınamadı", exc_info=True)
        self._schedule()
        self.update()

    def set_view(self, view: OverlayView) -> None:
        if view == self.view:
            return
        now = self.clock()
        old = self.view
        self.view = view
        if view.kind != old.kind:
            self._view_t = now
            if view.kind == "error":
                self._shake_t = now
        if view.color != old.color:
            self._color_from = self._current_color(now)
            self._color = QColor(view.color)
            self._color_t = now - (COLOR_S if self.reduced_motion else 0)
        self._relayout(now)
        self._schedule()
        self.update()

    def set_level(self, level: float | None) -> None:
        self._level_target = 0.0 if level is None else max(0.0, min(1.0, float(level)))

    def set_reduced_motion(self, reduced: bool) -> None:
        self.reduced_motion = reduced
        self._schedule()
        self.update()

    def set_hovered(self, hovered: bool) -> None:
        """İmleç çıkıntının üstünde: düğmeler açılır ve pencere tıklanabilir olur."""
        if hovered == self.hovered:
            return
        self.hovered = hovered
        self._hover_btn = None
        self._set_click_through(not hovered)
        self._relayout(self.clock())
        self._schedule()
        self.update()

    def slide_out(self) -> None:
        """Kenara kayarak kaybol, sonra `hide()` (zamanlayıcılar durur)."""
        if not self.isVisible():
            return
        if self.reduced_motion:
            self.dismiss()
            return
        if self._leave_t is None:
            self._leave_t = self.clock()
        self._schedule()

    def dismiss(self) -> None:
        """Hemen gizle."""
        self._leave_t = None
        self._frame.stop()
        if self.hovered:
            self.set_hovered(False)
        self._hover_since = None
        self.hide()

    def tab_rect(self) -> QRectF:
        """Çıkıntının pencere içindeki dikdörtgeni (piksel değil dip)."""
        k = self._k
        w, h = self._tab_w, self._tab_h
        return QRectF((self.width() / k - w) / 2 * k, 0, w * k, h * k)

    def button_rects(self) -> dict[str, QRectF]:
        """Fare üstündeyken görünen düğmeler (pencere koordinatı)."""
        if not self.hovered:
            return {}
        k = self._k
        tab = self.tab_rect()
        right = tab.right() / k - (PAD_R if self._layout.lines else PAD_R_EMPTY)
        cy = self._tab_h / 2 - (PAD_BOTTOM - PAD_TOP) / 2
        hide = QRectF(right - BTN, cy - BTN / 2, BTN, BTN)
        mic = QRectF(hide.left() - BTN_GAP - BTN, cy - BTN / 2, BTN, BTN)
        return {name: QRectF(r.x() * k, r.y() * k, r.width() * k, r.height() * k)
                for name, r in (("mic", mic), ("hide", hide))}

    def _ca(self, color: QColor, alpha: float) -> QColor:
        """Saydamlığı 1/64 adımlarla önbellekli renk (her karede yüzlerce çubuk çizilir)."""
        key = (color.rgb(), max(0, min(64, round(alpha * 64))))
        c = self._alpha_cache.get(key)
        if c is None:
            if len(self._alpha_cache) > 2048:
                self._alpha_cache.clear()
            c = _with_alpha(color, key[1] / 64)
            self._alpha_cache[key] = c
        return c

    def _dirty_rect(self) -> QRect:
        """Bir sonraki karede değişebilecek alan: çıkıntı ve ışık (uyanırken tüm genişlik)."""
        if self.view.kind == "wake" or self.leaving:
            return self.rect()
        hw = max(self._tab_w, self._tab_from[0]) / 2 + SHOULDER
        reach = (hw + 420) * self._k
        mid = self.width() / 2
        return QRect(math.floor(mid - reach), 0, math.ceil(2 * reach), self.height()).intersected(self.rect())

    # ---------- zamanlama ----------
    def _frame_interval(self, now: float) -> int | None:
        if not self.isVisible():
            return None
        if self.leaving:
            return int(1000 / FPS_ANIM)
        rm = self.reduced_motion
        transient = (now - self._shown_t < DROP_S or now - self._tab_t < RESIZE_S
                     or now - self._color_t < COLOR_S)
        kind = self.view.kind
        if not rm:
            transient = (transient or (kind == "error" and now - self._shake_t < SHAKE_S)
                         or (kind == "wake" and now - self._view_t < 1.3)
                         or (kind == "standby" and now - self._view_t < 0.6))
            if transient or kind in ("listening", "speaking"):
                return int(1000 / FPS_ANIM)
            if kind in ("thinking", "executing"):
                return int(1000 / FPS_SLOW)
        elif transient:
            return int(1000 / FPS_ANIM)
        if IS_WINDOWS:
            return POLL_MS   # yalnızca imleç yoklaması
        return None

    def _schedule(self) -> None:
        interval = self._frame_interval(self.clock())
        if interval is None:
            self._frame.stop()
            return
        if self._frame.interval() != interval:
            self._frame.setInterval(interval)
        if not self._frame.isActive():
            self._frame.start()

    def _tick(self) -> None:
        now = self.clock()
        if self._leave_t is not None and now - self._leave_t >= LEAVE_S:
            self.dismiss()
            return
        if IS_WINDOWS:
            self._poll_hover(now)
        smooth = 1.0 if self.reduced_motion else 0.35
        self._level += (self._level_target - self._level) * smooth
        self.update(self._dirty_rect())
        self._schedule()

    def _poll_hover(self, now: float) -> None:
        if self.leaving:
            return
        inside = self.tab_rect().adjusted(-SHOULDER * self._k, 0, SHOULDER * self._k, 0).contains(
            QPointF(self.mapFromGlobal(QCursor.pos())))
        if inside:
            if self._hover_since is None:
                self._hover_since = now
            if not self.hovered and now - self._hover_since >= HOVER_DWELL_S:
                self.set_hovered(True)
        else:
            self._hover_since = None
            if self.hovered:
                self.set_hovered(False)

    # ---------- Windows ----------
    def _apply_win_styles(self) -> None:
        w = _win()
        if w is None:
            return
        try:
            w.make_overlay(int(self.winId()), self._click_through)
        except Exception:  # noqa: BLE001
            log.debug("Gösterge pencere stilleri uygulanamadı", exc_info=True)

    def _set_click_through(self, on: bool) -> None:
        self._click_through = on
        if not IS_WINDOWS or not self.isVisible():
            return
        w = _win()
        if w is not None:
            try:
                w.set_click_through(int(self.winId()), on)
            except Exception:  # noqa: BLE001
                log.debug("Tıklama geçirme değiştirilemedi", exc_info=True)

    def showEvent(self, event) -> None:  # noqa: ANN001
        super().showEvent(event)
        self._schedule()

    def hideEvent(self, event) -> None:  # noqa: ANN001
        super().hideEvent(event)
        self._frame.stop()

    # ---------- fare (yalnızca tıklanabilirken gelir) ----------
    def _button_at(self, pos: QPointF) -> str | None:
        for name, r in self.button_rects().items():
            if r.contains(pos):
                return name
        return None

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        btn = self._button_at(event.position())
        if btn != self._hover_btn:
            self._hover_btn = btn
            self.setCursor(Qt.CursorShape.PointingHandCursor if btn else Qt.CursorShape.ArrowCursor)
            self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() is not Qt.MouseButton.LeftButton:
            return
        btn = self._button_at(event.position())
        if btn == "mic" and self._on_mic is not None:
            self._on_mic()
            self.update()
        elif btn == "hide" and self._on_hide is not None:
            self._on_hide()
        event.accept()

    # ---------- yerleşim ----------
    def _font(self, kind: str) -> QFont:
        dpi = float(self.logicalDpiY() or 96)
        key = (kind, dpi)
        f = self._font_cache.get(key)
        if f is None:
            family, px, spacing = {
                "status": (self._fonts.display, 14.0, 101.5),
                "speech": (self._fonts.body, 13.5, None),
                "secondary": (self._fonts.body, 12.5, None),
            }[kind]
            f = QFont(family)
            f.setPointSizeF(px * 72.0 / dpi)  # CSS piksel ölçüsü (dip)
            f.setWeight(QFont.Weight.Normal)
            f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
            if spacing:
                f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, spacing)
            self._font_cache[key] = f
        return f

    def _compute_layout(self, view: OverlayView, hovered: bool) -> _Layout:
        lines: list[str] = []
        text_w = primary_w = 0.0
        secondary = ""
        elided = ""
        h = float(TAB_H)
        if view.text and view.style == "speech":
            f = self._font("speech")
            lines = wrap_lines(view.text, f, SPEECH_MAX_W)
            fm = QFontMetricsF(f)
            # Taslakta blok öğe: kaydırılan metin tam 400 dip genişlikte durur
            text_w = SPEECH_MAX_W if len(lines) > 1 else min(SPEECH_MAX_W, fm.horizontalAdvance(lines[0]))
            h = max(float(TAB_H), len(lines) * LINE_H + 2 * SPEECH_PAD + PAD_TOP + PAD_BOTTOM)
        elif view.text and view.style == "transcript":
            fm = QFontMetricsF(self._font("speech"))
            lines = [" ".join(view.text.split())]
            text_w = min(SPEECH_MAX_W, fm.horizontalAdvance(lines[0]))
        elif view.text:
            fm = QFontMetricsF(self._font("status"))
            elided = fm.elidedText(view.text, Qt.TextElideMode.ElideRight, STATUS_MAX_W)
            primary_w = fm.horizontalAdvance(elided)
            lines = [elided]
            text_w = primary_w
            if view.secondary:
                fs = QFontMetricsF(self._font("secondary"))
                secondary = fs.elidedText(view.secondary, Qt.TextElideMode.ElideRight, 220)
                text_w += 10 + 1 + 10 + fs.horizontalAdvance(secondary)
        w = PAD_L + RING_BOX
        if lines:
            w += TEXT_GAP + text_w
        if hovered:
            w += (CTRL_MARGIN if lines else CTRL_MARGIN_EMPTY) + 2 * BTN + BTN_GAP
        w += PAD_R if lines else PAD_R_EMPTY
        return _Layout(math.ceil(w), h, lines, text_w, primary_w, secondary, elided)

    def _relayout(self, now: float) -> None:
        cur = self._current_tab(now)
        self._layout = self._compute_layout(self.view, self.hovered)
        target = (self._layout.w, self._layout.h)
        if not self.isVisible() or self.reduced_motion:
            self._tab_w, self._tab_h = target
            self._tab_t = now - RESIZE_S
        elif target != (self._tab_w, self._tab_h):
            self._tab_from = cur
            self._tab_w, self._tab_h = target
            self._tab_t = now

    def _current_tab(self, now: float) -> tuple[float, float]:
        t = (now - self._tab_t) / RESIZE_S
        if t >= 1:
            return self._tab_w, self._tab_h
        e = _ease_out_cubic(t)
        fw, fh = self._tab_from
        return fw + (self._tab_w - fw) * e, fh + (self._tab_h - fh) * e

    def _current_color(self, now: float) -> QColor:
        t = (now - self._color_t) / COLOR_S
        return QColor(self._color) if t >= 1 else _lerp_color(self._color_from, self._color, t)

    # ---------- çizim ----------
    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: ARG002
        now = self.clock()
        k = self._k
        W = self.width() / k
        M = W / 2
        tab_w, tab_h = self._current_tab(now)
        color = self._current_color(now)
        rm = self.reduced_motion
        s = now - self._view_t
        level = 0.35 if rm and self.view.kind in ("listening", "speaking") else self._level

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        p.fillRect(self.rect(), Qt.GlobalColor.transparent)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        p.scale(k, k)

        # Çıkıntının dikey kayması: iniş (yukarıdan düşer) ya da çıkış (kenara kayar)
        dy = 0.0
        if self._leave_t is not None:
            dy = -(tab_h + SHOULDER) * 1.1 * _ease_out_cubic((now - self._leave_t) / LEAVE_S)
        elif not rm and now - self._shown_t < DROP_S:
            dy = -tab_h * (1 - _ease_out_back((now - self._shown_t) / DROP_S))
        if self._leave_t is None:
            self._paint_light(p, M, tab_w, color, s, now, level)

        shake = 0.0
        if not rm and self.view.kind == "error" and now - self._shake_t < SHAKE_S:
            shake = self._shake((now - self._shake_t) / SHAKE_S)
        p.translate(M + shake, dy)
        img, rect = self._tab_image(tab_w, tab_h, color)
        p.drawImage(rect, img)
        cy = tab_h / 2 - (PAD_BOTTOM - PAD_TOP) / 2
        self._paint_ring(p, QPointF(-tab_w / 2 + PAD_L + RING_BOX / 2, cy), color, s, now, level)
        p.end()

    def _tab_image(self, tab_w: float, tab_h: float, color: QColor) -> tuple[QImage, QRectF]:
        """Çıkıntının değişmeyen katmanı (gölge, zemin, kontur, metin, düğmeler).

        Yalnızca boyut, metin, renk ya da fare durumu değişince yeniden çizilir; her karede
        yalnızca halka ve ışık çizilir.
        """
        dpr = self.devicePixelRatioF()
        key = (round(tab_w * 4), round(tab_h * 4), self.view, self.hovered, self._hover_btn, color.rgba(),
               self._mic_enabled() if self.hovered else True, self._k, dpr, self._layout.lines)
        cached = self._tab_cache
        if cached is not None and cached[0] == key:
            return cached[1], cached[2]
        pad = 26.0
        rect = QRectF(-tab_w / 2 - SHOULDER - pad, -1, tab_w + 2 * (SHOULDER + pad), tab_h + 1 + pad + 8)
        scale = self._k * dpr
        img = QImage(math.ceil(rect.width() * scale), math.ceil(rect.height() * scale),
                     QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        ip = QPainter(img)
        ip.setRenderHint(QPainter.RenderHint.Antialiasing)
        ip.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        ip.scale(scale, scale)
        ip.translate(-rect.left(), -rect.top())
        path = self._tab_path(tab_w, tab_h)
        self._paint_tab_shadow(ip, path, tab_w, tab_h)
        ip.fillPath(path, TAB_BG)
        ip.setClipPath(path)
        self._paint_content(ip, tab_w, tab_h, color)
        ip.end()
        self._tab_cache = (key, img, rect)
        return img, rect

    @staticmethod
    def _shake(t: float) -> float:
        keys = [(0, 0), (0.2, -4), (0.4, 4), (0.6, -4), (0.8, 4), (1, 0)]
        for (t0, v0), (t1, v1) in zip(keys, keys[1:], strict=False):
            if t <= t1:
                return v0 + (v1 - v0) * (t - t0) / (t1 - t0)
        return 0.0

    @staticmethod
    def _tab_path(w: float, h: float) -> QPainterPath:
        """Kenara yapışık çıkıntı: iç bükey omuzlar, yuvarlak alt köşeler (merkez x=0)."""
        L, R, r, sh = -w / 2, w / 2, min(TAB_RADIUS, h / 2), SHOULDER
        path = QPainterPath(QPointF(L - sh, 0))
        path.arcTo(QRectF(L - 2 * sh, 0, 2 * sh, 2 * sh), 90, -90)
        path.lineTo(L, h - r)
        path.arcTo(QRectF(L, h - 2 * r, 2 * r, 2 * r), 180, 90)
        path.lineTo(R - r, h)
        path.arcTo(QRectF(R - 2 * r, h - 2 * r, 2 * r, 2 * r), 270, 90)
        path.lineTo(R, sh)
        path.arcTo(QRectF(R, 0, 2 * sh, 2 * sh), 180, -90)
        path.closeSubpath()
        return path

    def _paint_tab_shadow(self, p: QPainter, path: QPainterPath, w: float, h: float) -> None:
        """drop-shadow(0 8px 16px rgba(0,0,0,.4)) ve 0,6 px siyah kontur.

        Gölge küçük bir görüntüye çizilip büyütülerek bulanıklaştırılır (ucuz Gauss yaklaşığı) ve
        çıkıntı boyutu değişmedikçe önbellekten gelir.
        """
        key = (round(w * 2), round(h * 2))
        cached = self._shadow
        if cached is None or cached[0] != key:
            pad = 24.0
            rect = QRectF(-w / 2 - SHOULDER - pad, -pad, w + 2 * (SHOULDER + pad), h + 2 * pad)
            small = 1 / 4
            img = QImage(math.ceil(rect.width() * small), math.ceil(rect.height() * small),
                         QImage.Format.Format_ARGB32_Premultiplied)
            img.fill(Qt.GlobalColor.transparent)
            ip = QPainter(img)
            ip.setRenderHint(QPainter.RenderHint.Antialiasing)
            ip.scale(small, small)
            ip.translate(-rect.left(), -rect.top())
            ip.fillPath(path, QColor(0, 0, 0, round(0.4 * 255)))
            ip.end()
            # Önce alan ortalamasıyla küçült, sonra yumuşak büyüt: yaklaşık 16 dip bulanıklık
            tiny = img.scaled(max(1, img.width() // 4), max(1, img.height() // 4),
                              Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
            blurred = tiny.scaled(img.width(), img.height(), Qt.AspectRatioMode.IgnoreAspectRatio,
                                  Qt.TransformationMode.SmoothTransformation)
            self._shadow = cached = (key, blurred, rect)
        _, img, rect = cached
        p.save()
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.drawImage(rect.translated(0, 8), img)
        p.restore()
        p.strokePath(path, QPen(QColor(0, 0, 0, round(0.6 * 255)), 1.0))

    def _paint_light(self, p: QPainter, M: float, tab_w: float, color: QColor, s: float, now: float,
                     level: float) -> None:
        """Üst kenardan iki yana sarkan ışık (taslaktaki isikCiz)."""
        kind = self.view.kind
        rm = self.reduced_motion
        hw = tab_w / 2 + SHOULDER
        z = now

        if kind in ("listening", "speaking", "error", "thinking", "executing"):
            # Açık arka planda ışık kaybolmasın: kenarlara doğru sönen hafif koyu bant
            L = hw + 340
            grad = QLinearGradient(M - L, 0, M + L, 0)
            dark = QColor(6, 12, 24)
            grad.setColorAt(0, _with_alpha(dark, 0))
            grad.setColorAt(0.3, _with_alpha(dark, 0.32))
            grad.setColorAt(0.7, _with_alpha(dark, 0.32))
            grad.setColorAt(1, _with_alpha(dark, 0))
            for y in range(14):
                p.setOpacity((1 - y / 14) ** 1.6)
                p.fillRect(QRectF(M - L, y, 2 * L, 1), grad)
            p.setOpacity(1)

        def both(x0: float, x1: float, fn: Callable[[float, float, float], None]) -> None:
            x = x0
            while x <= x1:
                fn(M + x, x - x0, x1 - x0)
                fn(M - x, x - x0, x1 - x0)
                x += 3

        def glow_rect(x: float, w: float, h: float, alpha: float) -> None:
            p.fillRect(QRectF(x - 1.5, 0, w + 3, h + 1.5), self._ca(color, alpha * 0.22))
            p.fillRect(QRectF(x, 0, w, h), self._ca(color, alpha))

        if kind in ("listening", "speaking"):
            self._paint_level_bars(p, M, hw, color, 0.0 if rm else z, level)
        elif kind == "wake":
            prog = 1.0 if rm else min(1.0, s / 0.55)
            uz = (M - hw) * (1 - (1 - prog) ** 3)
            alpha = 1.0 if rm else max(0.0, 1 - s / 1.3)
            if alpha > 0:
                x0, x1 = M - hw - uz, M + hw + uz
                grad = QLinearGradient(x0, 0, x1, 0)
                grad.setColorAt(0, self._ca(color, 0))
                grad.setColorAt(0.5, self._ca(color, alpha))
                grad.setColorAt(1, self._ca(color, 0))
                glow = QLinearGradient(x0, 0, x1, 0)
                glow.setColorAt(0, self._ca(color, 0))
                glow.setColorAt(0.5, self._ca(color, alpha * 0.25))
                glow.setColorAt(1, self._ca(color, 0))
                p.fillRect(QRectF(x0, 0, x1 - x0, 5), glow)
                p.fillRect(QRectF(x0, 0, x1 - x0, 2.5), grad)
        elif kind == "thinking":
            p.fillRect(QRectF(M - hw - 150, 0, 2 * (hw + 150), 1.2), self._ca(color, 0.3))
            prog = 0.5 if rm else (z * 0.8) % 1
            x = hw + prog * 150
            a = 1 - prog * 0.6
            glow_rect(M + x - 22, 44, 2.4, a)
            glow_rect(M - x - 22, 44, 2.4, a)
        elif kind == "executing":
            p.fillRect(QRectF(M - hw - 210, 0, 2 * (hw + 210), 1.4), self._ca(color, 0.22))
            uz = 120.0 if rm else ((s * 0.55) % 1) * 210
            glow_rect(M - hw - uz, 2 * (hw + uz), 2.4, 1.0)
        elif kind == "error":
            def dash(x: float, d: float, _length: float) -> None:
                if int(d // 9) % 2 == 0:
                    glow_rect(x - 1.5, 3, 2.4, 0.95)
            both(hw, hw + 130, dash)
        elif kind == "mic_off":
            p.fillRect(QRectF(M - hw - 60, 0, 2 * (hw + 60), 1.4), self._ca(color, 0.6))
        elif kind == "standby":
            prog = 0.0 if rm else max(0.0, 1 - s / 0.6)
            if prog > 0:
                p.fillRect(QRectF(M - hw - 100 * prog, 0, 2 * (hw + 100 * prog), 1.4), self._ca(color, prog))

    def _paint_level_bars(self, p: QPainter, M: float, hw: float, color: QColor, z: float, level: float) -> None:
        """Ses seviyesini izleyen çubuklar: çıkıntının iki yanından kenara sarkar, uzaklaştıkça söner.

        Her karede yüzlerce çubuk çizildiği için yükseklikler numpy ile hesaplanır ve her yanın
        çubukları tek fırçayla (uzaklıkla sönen yatay gradyan) tek çağrıda çizilir.
        """
        span = 70 + level * 330
        d = np.arange(0.0, span + 1e-6, 3.0)
        x = hw + d
        f = 1 - d / span
        i = np.rint(x / 3)
        noise = np.clip((np.sin(i * 0.37 + z * 3.1) + np.sin(i * 0.11 - z * 1.7) * 0.7
                         + np.sin(i * 1.3 + z * 5.3) * 0.3) / 4 + 0.5, 0.0, 1.0)
        heights = (1.5 + noise ** 2 * level * 11 * f).tolist()
        xs = x.tolist()
        p.save()
        p.setPen(Qt.PenStyle.NoPen)
        for side in (1, -1):
            x0, x1 = M + side * hw, M + side * (hw + span)
            for alpha, grow in ((0.22, 1.5), (1.0, 0.0)):   # önce yumuşak parıltı, sonra çubuk
                grad = QLinearGradient(x0, 0, x1, 0)
                grad.setColorAt(0, self._ca(color, alpha))
                grad.setColorAt(1, self._ca(color, 0.2 * alpha))
                p.setBrush(grad)
                half = 0.9 + grow
                p.drawRects([QRectF(M + side * xx - half, 0, 2 * half, hh + grow) for xx, hh in zip(xs, heights,
                                                                                                    strict=True)])
        p.restore()

    def _paint_content(self, p: QPainter, tab_w: float, tab_h: float, color: QColor) -> None:
        lay = self._layout
        left = -tab_w / 2
        cy = tab_h / 2 - (PAD_BOTTOM - PAD_TOP) / 2
        x = left + PAD_L + RING_BOX + TEXT_GAP
        view = self.view
        if lay.lines:
            if view.style == "speech":
                f = self._font("speech")
                fm = QFontMetricsF(f)
                p.setFont(f)
                p.setPen(color if view.kind == "error" else QColor(C.BUZ))
                top = cy - len(lay.lines) * LINE_H / 2
                for i, line in enumerate(lay.lines):
                    base = top + i * LINE_H + (LINE_H + fm.ascent() - fm.descent()) / 2
                    p.drawText(QPointF(x, base), line)
            elif view.style == "transcript":
                self._paint_transcript(p, x, cy, lay)
            else:
                f = self._font("status")
                fm = QFontMetricsF(f)
                base = cy + (fm.ascent() - fm.descent()) / 2
                p.setFont(f)
                p.setPen(color)
                p.drawText(QPointF(x, base), lay.elided_primary)
                if lay.secondary:
                    dx = x + lay.primary_w + 10
                    p.fillRect(QRectF(dx, base + 2 - 13, 1, 13), QColor(DIVIDER))
                    p.setFont(self._font("secondary"))
                    p.setPen(QColor(C.SIS))
                    p.drawText(QPointF(dx + 11, base), lay.secondary)
        if self.hovered:
            self._paint_buttons(p, tab_w, tab_h)

    def _paint_transcript(self, p: QPainter, x: float, cy: float, lay: _Layout) -> None:
        """Tek satır söz: sığmazsa sonu görünür, sol kenar 32 dip boyunca solar."""
        f = self._font("speech")
        fm = QFontMetricsF(f)
        text = lay.lines[0]
        full = fm.horizontalAdvance(text)
        base = cy + (fm.ascent() - fm.descent()) / 2
        if full <= lay.text_w + 0.5:
            p.setFont(f)
            p.setPen(QColor(C.BUZ))
            p.drawText(QPointF(x, base), text)
            return
        scale = self._k * self.devicePixelRatioF()
        h = LINE_H + 8
        img = QImage(math.ceil(lay.text_w * scale), math.ceil(h * scale), QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        ip = QPainter(img)
        ip.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        ip.setRenderHint(QPainter.RenderHint.Antialiasing)
        ip.scale(scale, scale)
        ip.setFont(f)
        ip.setPen(QColor(C.BUZ))
        ip.drawText(QPointF(lay.text_w - full, h / 2 + (fm.ascent() - fm.descent()) / 2), text)
        mask = QLinearGradient(0, 0, 32, 0)
        mask.setColorAt(0, QColor(0, 0, 0, 0))
        mask.setColorAt(1, QColor(0, 0, 0, 255))
        ip.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        ip.fillRect(QRectF(0, 0, lay.text_w, h), mask)
        ip.end()
        p.drawImage(QRectF(x, cy - h / 2, lay.text_w, h), img)

    def _icon(self, name: str, color: str) -> QSvgRenderer:
        key = (name, color)
        r = self._svg_cache.get(key)
        if r is None:
            svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
                   f'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">{ICON_PATHS[name]}</svg>')
            r = QSvgRenderer(QByteArray(svg.encode()))
            self._svg_cache[key] = r
        return r

    def _paint_buttons(self, p: QPainter, tab_w: float, tab_h: float) -> None:
        k = self._k
        mid = self.width() / k / 2
        for name, r in self.button_rects().items():
            rect = QRectF(r.x() / k - mid, r.y() / k, r.width() / k, r.height() / k)
            hot = self._hover_btn == name
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(214, 233, 243, round((0.14 if hot else 0.07) * 255)))
            p.drawEllipse(rect)
            if name == "mic":
                icon = "mikrofon" if self._mic_enabled() else "mikrofon_kapali"
                col = C.KIZIL if hot else BTN_ICON
            else:
                icon, col = "carpi", (C.BUZ if hot else BTN_ICON)
            c = rect.center()
            self._icon(icon, col).render(p, QRectF(c.x() - 6.5, c.y() - 6.5, 13, 13))

    def _paint_ring(self, p: QPainter, c: QPointF, color: QColor, s: float, now: float, level: float) -> None:
        """Küçük reaktör halkası (taslaktaki halkaCiz, B ölçüleri)."""
        kind = self.view.kind
        rm = self.reduced_motion
        z = now
        R = RING_R
        p.save()
        p.translate(c)
        # radyal çubuklar: ses seviyesi
        smooth = 1.0 if rm else 0.25
        buckets: dict[int, list[QLineF]] = {}
        r1 = R + 2.6
        for i, (ca, sa) in enumerate(_RING_DIRS):
            h = 0.0
            if kind in ("listening", "speaking"):
                h = level * (0.35 + 0.65 * _noise(i * 3, 0.0 if rm else z))
            elif kind == "wake":
                h = 0.9 * max(0.0, 1 - s * 1.4) if not rm else 0.5
            self._ring[i] += (h - self._ring[i]) * smooth
            v = self._ring[i]
            if v < 0.02:
                continue
            r2 = r1 + v * 6.5
            # Saydamlık 12 kademeye yuvarlanır: her kademe tek çağrıda çizilir
            buckets.setdefault(round((0.45 + v * 0.55) * 12), []).append(
                QLineF(ca * r1, sa * r1, ca * r2, sa * r2))
        for step, lines in buckets.items():
            pen = QPen(self._ca(color, step / 12), 1.4)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawLines(lines)

        # ana halka (+ parıltı); hata: kırık halka. Renk değişmedikçe önbellekten.
        img, rect = self._ring_image(color, kind == "error")
        p.drawImage(rect, img)

        # çekirdek
        if kind == "thinking":
            for k in range(2):
                b = (0.6 if rm else z * (2.4 + k * 1.1)) * (-1 if k else 1)
                r = R * (0.58 - k * 0.2)
                pen = QPen(self._ca(color, 0.95 - k * 0.3), 1.6 - k * 0.3)
                pen.setCapStyle(Qt.PenCapStyle.FlatCap)
                p.setPen(pen)
                self._arc(p, QRectF(-r, -r, 2 * r, 2 * r), b, math.pi * (0.9 - k * 0.2))
        elif kind == "executing":
            seg = 10
            filled = 6 if rm else int((s * 7) % (seg + 3))
            r = R * 0.55
            for k in range(seg):
                a = k / seg * math.tau - math.pi / 2
                pen = QPen(self._ca(color, 1.0 if k < filled else 0.2), 2)
                pen.setCapStyle(Qt.PenCapStyle.FlatCap)
                p.setPen(pen)
                self._arc(p, QRectF(-r, -r, 2 * r, 2 * r), a + 0.12, math.tau / seg - 0.24)
        else:
            r = R * (0.3 + (level * 0.22 if kind == "listening" else 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(self._ca(color, 0.22))
            p.drawEllipse(QPointF(0, 0), r + 1.6, r + 1.6)
            p.setBrush(color)
            p.drawEllipse(QPointF(0, 0), r, r)
        p.restore()

    def _ring_image(self, color: QColor, broken: bool) -> tuple[QImage, QRectF]:
        dpr = self.devicePixelRatioF()
        key = (color.rgba(), broken, self._k, dpr)
        cached = self._ring_cache
        if cached is not None and cached[0] == key:
            return cached[1], cached[2]
        R = RING_R
        rect = QRectF(-R - 4, -R - 4, 2 * R + 8, 2 * R + 8)
        scale = self._k * dpr
        img = QImage(math.ceil(rect.width() * scale), math.ceil(rect.height() * scale),
                     QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        ip = QPainter(img)
        ip.setRenderHint(QPainter.RenderHint.Antialiasing)
        ip.scale(scale, scale)
        ip.translate(-rect.left(), -rect.top())
        ring = QRectF(-R, -R, 2 * R, 2 * R)
        for width, alpha in ((5.5, 0.12), (3.2, 0.25), (1.7, 1.0)):
            pen = QPen(_with_alpha(color, alpha), width)
            pen.setCapStyle(Qt.PenCapStyle.FlatCap)
            ip.setPen(pen)
            ip.setBrush(Qt.BrushStyle.NoBrush)
            if broken:
                for k in range(5):
                    a = k / 5 * math.tau - math.pi / 2
                    self._arc(ip, ring, a + 0.28, math.tau / 5 - 0.56)
            else:
                ip.drawEllipse(ring)
        ip.end()
        self._ring_cache = (key, img, rect)
        return img, rect

    @staticmethod
    def _arc(p: QPainter, rect: QRectF, start: float, span: float) -> None:
        """Tuval açısıyla (saat yönü, radyan) yay çizer."""
        p.drawArc(rect, round(-math.degrees(start) * 16), round(-math.degrees(span) * 16))


# ---------- olaylar -> gösterge ----------
class OverlayController(QObject):
    """Sesli akışı izler, göstergeyi açar/günceller/gizler.

    Kurallar (uygulama notları):
    * Yalnızca sesli akışta açılır: uyandırma kelimesi ya da beklemeden doğrudan dinlemeye geçiş
      (Konuş). Ana pencerede yazılan sohbet göstergeyi açmaz.
    * JARVIS ana penceresi öndeyse açılmaz; ayar kapalıysa ya da gerçek tam ekran oyun varsa da.
    * Ekran, gösterim anında imlecin olduğu ekrandır ve konuşma bitene kadar değişmez.
    * Beklemeye dönünce 2,5 sn, hatada 6 sn sonra kenara kayıp gizlenir. İmleç üstündeyse beklenir.
    """

    def __init__(self, assistant: Any, fonts: Fonts, main_window_active: Callable[[], bool] | None = None,
                 overlay: ListeningOverlay | None = None,
                 fullscreen_check: Callable[[], bool] = exclusive_fullscreen_active,
                 screen_geometry: Callable[[], QRect | None] | None = None,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.assistant = assistant
        self._main_active = main_window_active or (lambda: False)
        self._fullscreen = fullscreen_check
        self._screen_geometry = screen_geometry or self._cursor_screen_geometry
        settings = assistant.settings
        self.overlay = overlay or ListeningOverlay(
            fonts, settings.ui.reduced_motion, on_mic=self._toggle_mic, on_hide=self._hide_clicked,
            mic_enabled=lambda: bool(self.assistant.settings.voice.microphone_enabled))
        self.session = False
        self._suppressed = False
        self._closed = False
        self._state = S.STANDBY
        self._detail = ""
        self._model = ""
        self._reply = ""
        self._transcript = ""
        self._error_text = ""
        self._error_until = 0.0
        self._hold_until = 0.0

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._hide_timeout)
        self._hold_timer = QTimer(self)
        self._hold_timer.setSingleShot(True)
        self._hold_timer.timeout.connect(self._refresh)

        bus = assistant.bus
        self._unsubs = [
            bus.subscribe(Topics.STATE_CHANGED, self._on_state),
            bus.subscribe(Topics.VOICE_LEVEL, self._on_level),
            bus.subscribe(Topics.VOICE_TRANSCRIPT, self._on_transcript),
            bus.subscribe(Topics.CHAT_STARTED, self._on_chat_started),
            bus.subscribe(Topics.CHAT_DONE, self._on_chat_done),
            bus.subscribe(Topics.CHAT_FAILED, self._on_chat_failed),
            bus.subscribe(Topics.VOICE_ERROR, self._on_error),
            bus.subscribe(Topics.APP_ERROR, self._on_error),
            bus.subscribe(Topics.MICROPHONE_TOGGLED, self._on_mic_toggled),
            bus.subscribe(Topics.SETTINGS_CHANGED, self._on_settings),
            bus.subscribe(Topics.APP_SHUTDOWN, lambda _p: self.shutdown()),
        ]

    # ---------- yardımcılar ----------
    @property
    def enabled(self) -> bool:
        return bool(self.assistant.settings.ui.overlay_enabled)

    @property
    def visible(self) -> bool:
        return self.overlay.isVisible()

    @staticmethod
    def _cursor_screen_geometry() -> QRect | None:
        app = QGuiApplication.instance()
        if app is None:
            return None
        scr = pick_screen(QGuiApplication.screens(), QCursor.pos(), QGuiApplication.primaryScreen())
        return scr.geometry() if scr is not None else None

    def _may_show(self) -> bool:
        if self._closed or not self.enabled or self._suppressed:
            return False
        if self._main_active():
            return False
        if self._fullscreen():
            log.info("Tam ekran oyun/sunum açık: dinleme göstergesi gösterilmiyor")
            return False
        return True

    def _show(self) -> None:
        if self.visible and not self.overlay.leaving:
            return
        if not self._may_show():
            return
        geo = self._screen_geometry()
        if geo is None:
            return
        self.overlay.set_reduced_motion(bool(self.assistant.settings.ui.reduced_motion))
        self.overlay.present(geo)
        log.debug("Dinleme göstergesi açıldı (%dx%d)", geo.width(), geo.height())

    def _end_session(self) -> None:
        self.session = False
        self._suppressed = False
        self._hide_timer.stop()
        self._hold_timer.stop()

    # ---------- görünüm ----------
    def _view(self) -> OverlayView:
        st, detail = self._state, self._detail
        look = STATE_LOOKS[st]
        now = time.monotonic()
        if st is S.WAKE_DETECTED:
            return OverlayView("wake", look.color, look.title)
        if st is S.LISTENING:
            extra = detail if detail and detail not in ("Dinliyorum", look.title) else ""
            return OverlayView("listening", look.color, "Dinliyorum", extra)
        if st is S.PROCESSING:
            if self._transcript and now < self._hold_until:
                return OverlayView("thinking", look.color, self._transcript, style="transcript")
            return OverlayView("thinking", look.color, look.title, self._model)
        if st is S.EXECUTING:
            return OverlayView("executing", look.color, detail or look.title)
        if st is S.SPEAKING:
            if self._reply:
                return OverlayView("speaking", look.color, self._reply, style="speech")
            return OverlayView("speaking", look.color, look.title)
        if st is S.ERROR:
            return self._error_view(detail or self._error_text or look.title)
        return OverlayView("standby", C.CYAN)

    def _error_view(self, text: str) -> OverlayView:
        fm = QFontMetricsF(self.overlay._font("status"))
        style = "speech" if fm.horizontalAdvance(text) > SPEECH_MAX_W else "status"
        return OverlayView("error", C.KIZIL, text, style=style)

    def _refresh(self) -> None:
        if self.session and self.visible:
            self.overlay.set_view(self._view())

    # ---------- olaylar ----------
    def _on_state(self, change: StateChange) -> None:
        if self._closed:
            return
        self._state, self._detail = change.new, change.detail
        new, old = change.new, change.old
        # Yeni sesli konuşma: uyandırma ya da beklemeden doğrudan dinleme (Konuş). Yazılı sohbet
        # beklemeden doğrudan düşünmeye geçer ve göstergeyi açmaz.
        starts = (new is S.WAKE_DETECTED and old is not S.WAKE_DETECTED) or (
            new is S.LISTENING and old in (S.STANDBY, S.ERROR))
        if starts:
            self.session = True
            self._suppressed = False
            self._model = self._reply = self._transcript = self._error_text = ""
            self._error_until = self._hold_until = 0.0
            self._hold_timer.stop()
        if not self.session:
            return
        if self.visible and self._main_active():
            # Kişi JARVIS penceresine geçti: aynı bilgi orada
            self.overlay.dismiss()
            self._hide_timer.stop()
            self._hold_timer.stop()
        if new is S.STANDBY:
            if self._suppressed or not self.visible or self.overlay.leaving:
                self._end_session()
                return
            if time.monotonic() < self._error_until:
                return   # hata 6 sn görünür kalır
            self.overlay.set_view(self._view())
            self.overlay.set_level(None)
            self._hide_timer.start(STANDBY_HIDE_MS)
            return
        self._hide_timer.stop()
        if new is not S.ERROR:
            self._error_until = 0.0
        if new not in (S.LISTENING, S.SPEAKING):
            self.overlay.set_level(None)
        self.overlay.set_view(self._view())
        self._show()
        if new is S.ERROR:
            self._error_until = time.monotonic() + ERROR_HIDE_MS / 1000
            if self.visible:
                self._hide_timer.start(ERROR_HIDE_MS)

    def _on_level(self, lv: Any) -> None:
        if not self.session or not self.visible:
            return
        st = self._state
        if (st is S.LISTENING and lv.source == "mic") or (st is S.SPEAKING and lv.source == "tts"):
            self.overlay.set_level(lv.level)

    def _on_transcript(self, tr: Any) -> None:
        if not self.session:
            return
        self._reply = ""
        self._transcript = " ".join((getattr(tr, "text", "") or "").split())
        if self._transcript:
            self._hold_until = time.monotonic() + TRANSCRIPT_HOLD_MS / 1000
            if self.visible:
                self._hold_timer.start(TRANSCRIPT_HOLD_MS)
            self._refresh()

    def _on_chat_started(self, ev: Any) -> None:
        if self.session:
            self._model = getattr(ev, "model_name", "") or ""
            self._refresh()

    def _on_chat_done(self, ev: Any) -> None:
        if self.session:
            self._reply = getattr(ev, "text", "") or ""
            self._refresh()

    def _on_chat_failed(self, msg: str) -> None:
        if self.session:
            self._reply = msg or ""

    def _on_error(self, msg: str) -> None:
        if not self.session or not msg or self._closed:
            return
        self._error_text = msg
        self._error_until = time.monotonic() + ERROR_HIDE_MS / 1000
        self._hold_timer.stop()
        self.overlay.set_level(None)
        self.overlay.set_view(self._error_view(msg))
        self._show()
        if self.visible:
            self._hide_timer.start(ERROR_HIDE_MS)

    def _on_mic_toggled(self, enabled: bool) -> None:
        if not self.session or not self.visible:
            return
        if enabled:
            self._refresh()
            return
        self.overlay.set_level(None)
        self.overlay.set_view(OverlayView("mic_off", C.SIS, "Mikrofon kapalı", "JARVIS penceresinden açabilirsin"))
        self._hide_timer.start(MIC_OFF_HIDE_MS)

    def _on_settings(self, settings: Any) -> None:
        self.overlay.set_reduced_motion(bool(settings.ui.reduced_motion))
        if not settings.ui.overlay_enabled and self.visible:
            self.overlay.dismiss()
            self._end_session()

    # ---------- düğmeler ve zamanlayıcılar ----------
    def _toggle_mic(self) -> None:
        enabled = bool(self.assistant.settings.voice.microphone_enabled)
        self.assistant.set_microphone_enabled(not enabled)

    def _hide_clicked(self) -> None:
        """Yalnızca göstergeyi bu konuşma için kapatır; isteği iptal etmez."""
        self._suppressed = True
        self._hide_timer.stop()
        self._hold_timer.stop()
        self.overlay.slide_out()
        if self._state in (S.STANDBY, S.ERROR):
            self._end_session()

    def _hide_timeout(self) -> None:
        if self.overlay.hovered:
            self._hide_timer.start(1000)   # imleç üstündeyken solmaz
            return
        self.overlay.slide_out()
        self._end_session()

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        for u in self._unsubs:
            u()
        self._unsubs.clear()
        self._end_session()
        self._hide_timer.stop()
        self._hold_timer.stop()
        self.overlay.dismiss()
        # deleteLater yok: pencere Python'a ait (ebeveynsiz). Ertelenmiş silme + Python'un kendi
        # silmesi yarışıp Linux CI'da Qt çökmesine yol açabiliyordu; gizli pencere referansla birlikte gider.
