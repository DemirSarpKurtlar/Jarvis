"""Ekran görüntüsü yakalama: çoklu monitör, DPI farkındalığı, küçültme ve PNG.

Yeniden kullanılabilir tek giriş noktası (ekran görüntüsü aracı, "ekranımda ne var?" ve
bilgisayar kullanımı aynı kodu kullanır):

    png, width, height, scale = capture("monitor", max_long_edge=1568)
    cap = grab("window")                       # tüm bilgiler: cap.region, cap.to_screen(x, y)

* Koordinatlar **fiziksel piksel**, sanal masaüstüne göredir (birincil monitörün sol üstü 0,0;
  solundaki monitörün x'i negatiftir). Süreç monitör başına DPI farkında (Per-Monitor v2) olmalı;
  Qt bunu zaten açar, `ensure_dpi_awareness()` yine de güvenceye alır.
* `scale` = görüntü pikseli / ekran pikseli (küçültme yoksa 1.0). Görüntüdeki bir noktanın ekrandaki
  yeri: `cap.to_screen(x, y)` = (region.left + x / scale, region.top + y / scale).
* Yakalama GDI BitBlt ile yapılır (saf ctypes, ek paket yok; paketlenmiş exe'de de çalışır).
  Küçültme ve PNG kodlama QImage ile (QImage her thread'de güvenle kullanılabilir, QApplication
  gerekmez).
* Windows dışında `ScreenUnsupported` fırlatılır; hiçbir şey başarılıymış gibi gösterilmez.
  Testler `ScreenBackend` protokolüne uyan sahte bir arka uç verir.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from jarvis.core.errors import JarvisError

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
DEFAULT_MAX_LONG_EDGE = 1568        # Claude görüntü girdisi için önerilen en uzun kenar
MAX_CAPTURE_PIXELS = 80_000_000     # 3 adet 4K monitör ~25 M piksel; bunun çok üstü saçma bir alan


class ScreenError(JarvisError):
    """Ekran yakalanamadı. `user_message` Türkçe."""


class ScreenUnsupported(ScreenError):
    def __init__(self) -> None:
        super().__init__("ekran yakalama desteklenmiyor",
                         "Ekran görüntüsü yalnızca Windows'ta alınabiliyor.")


# ---------- veri tipleri ----------
@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    @property
    def area(self) -> int:
        return max(0, self.width) * max(0, self.height)

    @property
    def center(self) -> tuple[int, int]:
        return self.left + self.width // 2, self.top + self.height // 2

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom

    def intersect(self, other: Rect) -> Rect | None:
        left, top = max(self.left, other.left), max(self.top, other.top)
        right, bottom = min(self.right, other.right), min(self.bottom, other.bottom)
        if right <= left or bottom <= top:
            return None
        return Rect(left, top, right - left, bottom - top)

    @staticmethod
    def union(rects: list[Rect]) -> Rect:
        left, top = min(r.left for r in rects), min(r.top for r in rects)
        right, bottom = max(r.right for r in rects), max(r.bottom for r in rects)
        return Rect(left, top, right - left, bottom - top)

    @staticmethod
    def from_ltrb(left: int, top: int, right: int, bottom: int) -> Rect:
        return Rect(int(left), int(top), int(right - left), int(bottom - top))


@dataclass(frozen=True)
class Monitor:
    index: int          # 1'den başlar; birincil monitör her zaman 1
    rect: Rect          # monitörün tamamı (fiziksel piksel)
    work: Rect          # görev çubuğu hariç çalışma alanı
    primary: bool
    scale: float = 1.0  # Windows ölçekleme (1.25 = %125)
    name: str = ""


@dataclass(frozen=True)
class Capture:
    png: bytes
    width: int          # PNG genişliği
    height: int
    scale: float        # görüntü pikseli / ekran pikseli
    region: Rect        # yakalanan ekran bölgesi (fiziksel piksel, sanal masaüstü koordinatı)
    monitor: int | None = None    # tek monitör yakalandıysa numarası
    title: str = ""     # pencere yakalandıysa başlığı

    def as_tuple(self) -> tuple[bytes, int, int, float]:
        return self.png, self.width, self.height, self.scale

    def to_screen(self, x: float, y: float) -> tuple[int, int]:
        """Görüntüdeki noktayı ekrandaki fiziksel piksele çevirir (fare tıklaması için)."""
        return (int(round(self.region.left + x / self.scale)), int(round(self.region.top + y / self.scale)))

    def from_screen(self, x: int, y: int) -> tuple[float, float]:
        return ((x - self.region.left) * self.scale, (y - self.region.top) * self.scale)


class ScreenBackend(Protocol):
    def monitors(self) -> list[Monitor]: ...
    def grab(self, rect: Rect) -> np.ndarray: ...            # (h, w, 4) uint8, BGRA
    def active_window(self) -> tuple[int, str] | None: ...    # (hwnd, başlık); JARVIS'in kendi penceresi atlanır
    def window_rect(self, hwnd: int) -> Rect | None: ...      # görünen çerçeve (gölge hariç)
    def monitor_of_window(self, hwnd: int) -> int | None: ...


# ---------- küçültme ve PNG ----------
def fit_size(width: int, height: int, max_long_edge: int | None) -> tuple[int, int, float]:
    """En uzun kenarı `max_long_edge`'i aşmayacak boyut ve ölçek (büyütme yapılmaz)."""
    if not max_long_edge or max(width, height) <= max_long_edge:
        return width, height, 1.0
    scale = max_long_edge / max(width, height)
    return max(1, round(width * scale)), max(1, round(height * scale)), scale


def encode_png(bgra: np.ndarray, max_long_edge: int | None = None) -> tuple[bytes, int, int, float]:
    """BGRA dizisini (gerekirse küçültüp) PNG'ye çevirir: (png, genişlik, yükseklik, ölçek)."""
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
    from PySide6.QtGui import QImage

    if bgra.ndim != 3 or bgra.shape[2] != 4 or bgra.dtype != np.uint8:
        raise ScreenError(f"beklenmeyen görüntü biçimi: {bgra.shape} {bgra.dtype}", "Ekran görüntüsü okunamadı.")
    arr = np.ascontiguousarray(bgra)
    h, w = arr.shape[:2]
    # Format_RGB32 bellekte B,G,R,X sırasıdır: GDI'nin 32 bit DIB'iyle birebir aynı
    img = QImage(arr.data, w, h, arr.strides[0], QImage.Format.Format_RGB32)
    tw, th, _ = fit_size(w, h, max_long_edge)
    if (tw, th) != (w, h):
        img = img.scaled(tw, th, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    ok = img.save(buf, "PNG")
    buf.close()
    del img          # QImage `arr` belleğini kullanıyor: önce o bırakılsın
    if not ok:
        raise ScreenError("PNG kodlanamadı", "Ekran görüntüsü PNG olarak kaydedilemedi.")
    return bytes(ba.data()), tw, th, (tw / w if w else 1.0)


# ---------- Windows arka ucu (saf ctypes) ----------
_DPI_LOCK = threading.Lock()
_dpi_done = False


def ensure_dpi_awareness() -> str:
    """Süreci monitör başına DPI farkında yapar (Per-Monitor v2). Zaten ayarlıysa dokunmaz.

    Qt 6 bunu uygulama açılışında zaten yapar; bu çağrı, Qt'siz kullanımda (betik, test) koordinatların
    fiziksel piksel olmasını güvenceye alır. Durumu metin olarak döndürür (log için).
    """
    global _dpi_done
    if not IS_WINDOWS:
        return "yok"
    with _DPI_LOCK:  # pragma: no cover - Windows
        import ctypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        try:
            user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
            user32.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            current = user32.GetAwarenessFromDpiAwarenessContext(user32.GetThreadDpiAwarenessContext())
        except (AttributeError, OSError):
            current = -1
        if current == 2:   # DPI_AWARENESS_PER_MONITOR_AWARE (v1 ya da v2)
            _dpi_done = True
            return "monitör başına"
        if not _dpi_done:
            _dpi_done = True
            try:
                user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
                if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):   # PER_MONITOR_AWARE_V2
                    return "monitör başına (v2, JARVIS ayarladı)"
            except (AttributeError, OSError):
                pass
            log.warning("Süreç DPI farkında değil (durum=%s); ekran koordinatları ölçeklenmiş olabilir", current)
        return "sistem" if current == 1 else "farkında değil"


class GdiBackend:  # pragma: no cover - yalnızca Windows
    """Win32 GDI ile yakalama. Tüm çağrılar kendi WinDLL örnekleriyle (argtypes başka modüllerle çakışmaz)."""

    def __init__(self) -> None:
        if not IS_WINDOWS:
            raise ScreenUnsupported()
        import ctypes
        from ctypes import wintypes as wt

        self._ct, self._wt = ctypes, wt
        self.dpi_mode = ensure_dpi_awareness()
        u = self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        g = self.gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        try:
            self.dwm = ctypes.WinDLL("dwmapi")
        except OSError:
            self.dwm = None
        try:
            self.shcore = ctypes.WinDLL("shcore")
        except OSError:
            self.shcore = None

        class RECT(ctypes.Structure):
            _fields_ = [("left", wt.LONG), ("top", wt.LONG), ("right", wt.LONG), ("bottom", wt.LONG)]

        class MONITORINFOEXW(ctypes.Structure):
            _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", wt.DWORD),
                        ("szDevice", wt.WCHAR * 32)]

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                        ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                        ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                        ("biClrImportant", wt.DWORD)]

        class BITMAPINFO(ctypes.Structure):
            _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]

        self.RECT, self.MONITORINFOEXW, self.BITMAPINFO = RECT, MONITORINFOEXW, BITMAPINFO
        self.MONITORENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC, ctypes.POINTER(RECT), wt.LPARAM)

        u.GetDC.argtypes, u.GetDC.restype = [wt.HWND], wt.HDC
        u.ReleaseDC.argtypes, u.ReleaseDC.restype = [wt.HWND, wt.HDC], ctypes.c_int
        u.EnumDisplayMonitors.argtypes = [wt.HDC, ctypes.c_void_p, self.MONITORENUMPROC, wt.LPARAM]
        u.EnumDisplayMonitors.restype = wt.BOOL
        u.GetMonitorInfoW.argtypes, u.GetMonitorInfoW.restype = [wt.HMONITOR, ctypes.POINTER(MONITORINFOEXW)], wt.BOOL
        u.MonitorFromWindow.argtypes, u.MonitorFromWindow.restype = [wt.HWND, wt.DWORD], wt.HMONITOR
        u.GetForegroundWindow.argtypes, u.GetForegroundWindow.restype = [], wt.HWND
        u.GetWindow.argtypes, u.GetWindow.restype = [wt.HWND, wt.UINT], wt.HWND
        u.GetWindowRect.argtypes, u.GetWindowRect.restype = [wt.HWND, ctypes.POINTER(RECT)], wt.BOOL
        u.IsWindowVisible.argtypes, u.IsWindowVisible.restype = [wt.HWND], wt.BOOL
        u.IsIconic.argtypes, u.IsIconic.restype = [wt.HWND], wt.BOOL
        u.GetWindowTextLengthW.argtypes, u.GetWindowTextLengthW.restype = [wt.HWND], ctypes.c_int
        u.GetWindowTextW.argtypes, u.GetWindowTextW.restype = [wt.HWND, wt.LPWSTR, ctypes.c_int], ctypes.c_int
        u.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
        u.GetWindowThreadProcessId.restype = wt.DWORD
        g.CreateCompatibleDC.argtypes, g.CreateCompatibleDC.restype = [wt.HDC], wt.HDC
        g.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
        g.CreateCompatibleBitmap.restype = wt.HBITMAP
        g.SelectObject.argtypes, g.SelectObject.restype = [wt.HDC, wt.HGDIOBJ], wt.HGDIOBJ
        g.BitBlt.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HDC, ctypes.c_int,
                             ctypes.c_int, wt.DWORD]
        g.BitBlt.restype = wt.BOOL
        g.GetDIBits.argtypes = [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p, ctypes.POINTER(BITMAPINFO),
                                wt.UINT]
        g.GetDIBits.restype = ctypes.c_int
        g.DeleteObject.argtypes, g.DeleteObject.restype = [wt.HGDIOBJ], wt.BOOL
        g.DeleteDC.argtypes, g.DeleteDC.restype = [wt.HDC], wt.BOOL
        if self.dwm is not None:
            self.dwm.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]
            self.dwm.DwmGetWindowAttribute.restype = ctypes.c_long
        if self.shcore is not None:
            self.shcore.GetDpiForMonitor.argtypes = [wt.HMONITOR, ctypes.c_int, ctypes.POINTER(wt.UINT),
                                                     ctypes.POINTER(wt.UINT)]
            self.shcore.GetDpiForMonitor.restype = ctypes.c_long

    # -- monitörler --
    def _monitor_handles(self) -> list[int]:
        handles: list[int] = []

        def cb(hmon, hdc, rect, lparam):  # noqa: ANN001, ANN202
            handles.append(hmon)
            return True

        proc = self.MONITORENUMPROC(cb)
        if not self.user32.EnumDisplayMonitors(None, None, proc, 0):
            raise ScreenError("EnumDisplayMonitors başarısız", "Monitörler okunamadı.")
        return handles

    def _info(self, hmon: int) -> Monitor | None:
        ct = self._ct
        mi = self.MONITORINFOEXW()
        mi.cbSize = ct.sizeof(self.MONITORINFOEXW)
        if not self.user32.GetMonitorInfoW(hmon, ct.byref(mi)):
            return None
        r, w = mi.rcMonitor, mi.rcWork
        scale = 1.0
        if self.shcore is not None:
            dx, dy = self._wt.UINT(), self._wt.UINT()
            if self.shcore.GetDpiForMonitor(hmon, 0, ct.byref(dx), ct.byref(dy)) == 0 and dx.value:
                scale = dx.value / 96.0
        return Monitor(0, Rect.from_ltrb(r.left, r.top, r.right, r.bottom), Rect.from_ltrb(w.left, w.top, w.right,
                       w.bottom), bool(mi.dwFlags & 1), round(scale, 3), mi.szDevice)

    def _handles_with_info(self) -> list[tuple[int, Monitor]]:
        pairs = [(h, m) for h in self._monitor_handles() if (m := self._info(h)) is not None]
        # Birincil önce, sonra soldan sağa / yukarıdan aşağı: "ikinci monitör" kararlı bir anlam taşısın
        pairs.sort(key=lambda p: (not p[1].primary, p[1].rect.left, p[1].rect.top))
        return [(h, Monitor(i, m.rect, m.work, m.primary, m.scale, m.name)) for i, (h, m) in enumerate(pairs, 1)]

    def monitors(self) -> list[Monitor]:
        return [m for _, m in self._handles_with_info()]

    def monitor_of_window(self, hwnd: int) -> int | None:
        hmon = self.user32.MonitorFromWindow(hwnd, 2)   # MONITOR_DEFAULTTONEAREST
        for h, m in self._handles_with_info():
            if h == hmon:
                return m.index
        return None

    # -- pencereler --
    def _title(self, hwnd: int) -> str:
        n = self.user32.GetWindowTextLengthW(hwnd)
        buf = self._ct.create_unicode_buffer(n + 1)
        self.user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value

    def _pid(self, hwnd: int) -> int:
        pid = self._wt.DWORD()
        self.user32.GetWindowThreadProcessId(hwnd, self._ct.byref(pid))
        return int(pid.value)

    def _usable(self, hwnd: int) -> bool:
        return bool(self.user32.IsWindowVisible(hwnd)) and not self.user32.IsIconic(hwnd) \
            and not self.user32.GetWindow(hwnd, 4) and bool(self._title(hwnd))   # GW_OWNER

    def active_window(self) -> tuple[int, str] | None:
        """Öndeki pencere. JARVIS'in kendisiyse Z sırasında altındaki ilk kullanıcı penceresi
        (yazılı komutta odak JARVIS'tedir; kullanıcı az önceki pencereyi kastediyor)."""
        hwnd = self.user32.GetForegroundWindow()
        own = os.getpid()
        steps = 0
        while hwnd and steps < 500:
            if self._pid(hwnd) != own and self._usable(hwnd):
                return int(hwnd), self._title(hwnd)
            hwnd = self.user32.GetWindow(hwnd, 2)   # GW_HWNDNEXT
            steps += 1
        return None

    def window_rect(self, hwnd: int) -> Rect | None:
        ct = self._ct
        r = self.RECT()
        if self.dwm is not None:
            # DWMWA_EXTENDED_FRAME_BOUNDS: görünmez yeniden boyutlandırma kenarları hariç gerçek çerçeve
            if self.dwm.DwmGetWindowAttribute(hwnd, 9, ct.byref(r), ct.sizeof(r)) == 0 and r.right > r.left:
                return Rect.from_ltrb(r.left, r.top, r.right, r.bottom)
        if self.user32.GetWindowRect(hwnd, ct.byref(r)) and r.right > r.left:
            return Rect.from_ltrb(r.left, r.top, r.right, r.bottom)
        return None

    # -- yakalama --
    def grab(self, rect: Rect) -> np.ndarray:
        ct, u, g = self._ct, self.user32, self.gdi32
        w, h = rect.width, rect.height
        screen = u.GetDC(None)
        if not screen:
            raise ScreenError("GetDC başarısız", "Ekran okunamadı.")
        mem = bmp = old = None
        try:
            mem = g.CreateCompatibleDC(screen)
            bmp = g.CreateCompatibleBitmap(screen, w, h)
            if not mem or not bmp:
                raise ScreenError("GDI nesnesi oluşturulamadı", "Ekran görüntüsü için bellek ayrılamadı.")
            old = g.SelectObject(mem, bmp)
            # SRCCOPY | CAPTUREBLT (katmanlı pencereler de görünsün)
            if not g.BitBlt(mem, 0, 0, w, h, screen, rect.left, rect.top, 0x00CC0020 | 0x40000000):
                raise ScreenError(f"BitBlt başarısız ({ct.get_last_error()})",
                                  "Ekran okunamadı (güvenli masaüstü ya da kilit ekranı açık olabilir).")
            bmi = self.BITMAPINFO()
            hdr = bmi.bmiHeader
            hdr.biSize = ct.sizeof(hdr)
            hdr.biWidth, hdr.biHeight = w, -h          # negatif: satırlar yukarıdan aşağı
            hdr.biPlanes, hdr.biBitCount, hdr.biCompression = 1, 32, 0   # BI_RGB
            out = np.empty((h, w, 4), np.uint8)
            if g.GetDIBits(mem, bmp, 0, h, out.ctypes.data, ct.byref(bmi), 0) != h:
                raise ScreenError("GetDIBits başarısız", "Ekran görüntüsü okunamadı.")
            return out
        finally:
            if mem and old:
                g.SelectObject(mem, old)
            if bmp:
                g.DeleteObject(bmp)
            if mem:
                g.DeleteDC(mem)
            u.ReleaseDC(None, screen)


class UnsupportedScreen:
    def monitors(self) -> list[Monitor]:
        raise ScreenUnsupported()

    def grab(self, rect: Rect) -> np.ndarray:
        raise ScreenUnsupported()

    def active_window(self) -> tuple[int, str] | None:
        raise ScreenUnsupported()

    def window_rect(self, hwnd: int) -> Rect | None:
        raise ScreenUnsupported()

    def monitor_of_window(self, hwnd: int) -> int | None:
        raise ScreenUnsupported()


_backend: ScreenBackend | None = None
_backend_lock = threading.Lock()


def default_backend() -> ScreenBackend:
    global _backend
    with _backend_lock:
        if _backend is None:
            _backend = GdiBackend() if IS_WINDOWS else UnsupportedScreen()
        return _backend


# ---------- yüksek seviye API ----------
def list_monitors(backend: ScreenBackend | None = None) -> list[Monitor]:
    return (backend or default_backend()).monitors()


def _pick_monitor(mons: list[Monitor], b: ScreenBackend, monitor: int | None) -> Monitor:
    if not mons:
        raise ScreenError("monitör yok", "Bağlı bir monitör bulunamadı.")
    if monitor is not None and monitor > 0:
        for m in mons:
            if m.index == monitor:
                return m
        raise ScreenError(f"monitör {monitor} yok",
                          f"{monitor}. monitör yok; {len(mons)} monitör bağlı.")
    # Belirtilmemiş: kullanıcının çalıştığı (öndeki pencerenin bulunduğu) monitör, yoksa birincil
    active = b.active_window()
    if active is not None:
        idx = b.monitor_of_window(active[0])
        for m in mons:
            if m.index == idx:
                return m
    return next((m for m in mons if m.primary), mons[0])


def grab(target: str = "monitor", *, monitor: int | None = None, hwnd: int | None = None,
         region: Rect | None = None, max_long_edge: int | None = None,
         backend: ScreenBackend | None = None) -> Capture:
    """Ekranın bir bölümünü yakalar.

    target:
      * "all"     : tüm monitörler (sanal masaüstü)
      * "monitor" : `monitor` numaralı monitör (None/0: öndeki pencerenin monitörü, yoksa birincil)
      * "window"  : `hwnd` penceresi (None: öndeki pencere; JARVIS'in kendisi atlanır)
      * "region"  : `region` dikdörtgeni
    max_long_edge: verilirse en uzun kenar bu boyuta küçültülür (büyütülmez).
    """
    b = backend or default_backend()
    mons = b.monitors()
    if not mons:
        raise ScreenError("monitör yok", "Bağlı bir monitör bulunamadı.")
    desktop = Rect.union([m.rect for m in mons])
    mon_index: int | None = None
    title = ""
    if target == "all":
        rect = desktop
    elif target == "monitor":
        m = _pick_monitor(mons, b, monitor)
        rect, mon_index = m.rect, m.index
    elif target == "window":
        if hwnd is None:
            active = b.active_window()
            if active is None:
                raise ScreenError("pencere yok", "Ekranda görüntüsü alınabilecek bir pencere bulamadım.")
            hwnd, title = active
        wr = b.window_rect(hwnd)
        if wr is None:
            raise ScreenError("pencere alanı yok", "Pencerenin yeri okunamadı (kapanmış ya da simge durumunda olabilir).")
        rect = wr.intersect(desktop)   # ekran dışına taşan kısım kırpılır
        if rect is None:
            raise ScreenError("pencere ekran dışında", "Pencere ekranda görünmüyor.")
        mon_index = b.monitor_of_window(hwnd)
    elif target == "region":
        if region is None:
            raise ScreenError("bölge yok", "Yakalanacak bölge verilmedi.")
        rect = region.intersect(desktop)
        if rect is None:
            raise ScreenError("bölge ekran dışında", "İstenen bölge ekranın dışında.")
    else:
        raise ScreenError(f"bilinmeyen hedef: {target}", "Bilinmeyen ekran görüntüsü türü.")
    if rect.area <= 0 or rect.area > MAX_CAPTURE_PIXELS:
        raise ScreenError(f"geçersiz alan: {rect}", "Yakalanacak alan geçersiz.")
    pixels = b.grab(rect)
    if pixels.shape[:2] != (rect.height, rect.width):
        raise ScreenError(f"boyut uyuşmuyor: {pixels.shape} != {rect}", "Ekran görüntüsü eksik okundu.")
    png, w, h, scale = encode_png(pixels, max_long_edge)
    return Capture(png, w, h, scale, rect, mon_index, title)


def capture(target: str = "monitor", *, monitor: int | None = None, hwnd: int | None = None,
            region: Rect | None = None, max_long_edge: int | None = DEFAULT_MAX_LONG_EDGE,
            backend: ScreenBackend | None = None) -> tuple[bytes, int, int, float]:
    """Kısa yol: (png_bayt, genişlik, yükseklik, ölçek). Ayrıntı (bölge, koordinat çevirme) için `grab()`."""
    return grab(target, monitor=monitor, hwnd=hwnd, region=region, max_long_edge=max_long_edge,
                backend=backend).as_tuple()
