"""Windows arka uçları (saf ctypes): ekran yakalama, fare/klavye girdisi, ön plan penceresi ve
Esc Esc acil durdurma.

- Koordinatlar fiziksel pikseldir: her çalışan thread "Per Monitor v2" DPI farkındalığına alınır,
  böylece ekran dikdörtgenleri, yakalama ve imleç konumu ölçeklenmemiş piksellerle çalışır.
- Yakalama GDI BitBlt ile yapılır (sanal masaüstü DC'si). Özel (exclusive) tam ekran oyunlarda kare
  siyah gelebilir; bu durum ajana bildirilir (pencereli tam ekran önerilir).
- Fare: önce SetCursorPos, ardından aynı noktaya SendInput (MOUSEEVENTF_ABSOLUTE|VIRTUALDESK). Böylece
  hem imleç konumunu okuyan hem ham girdi (raw input) dinleyen uygulamalar hareketi görür.
- Klavye: adlandırılmış tuşlar sanal tuş koduyla (tarama kodu da doldurulur), metin
  KEYEVENTF_UNICODE ile gönderilir (Türkçe karakterler klavye düzeninden bağımsız yazılır).
- Yükseltilmiş (yönetici) pencerelere Windows (UIPI) girdi iletmez; bu bilerek aşılmaz.
- Hile önleyiciler (ör. Riot Vanguard) yazılımsal girdiyi yok sayabilir. Bu da aşılmaz: sürücü,
  Interception, bellek okuma gibi yollar kullanılmaz.

Bu modül yalnızca Windows'ta yüklenir; testler `backends.py` arayüzlerinin sahtelerini kullanır.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from collections.abc import Callable
from ctypes import wintypes

import numpy as np

from jarvis.agent.backends import Clock, Desktop, ForegroundInfo
from jarvis.agent.geometry import Rect

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

ULONG_PTR = ctypes.c_size_t
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE, MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0001, 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP = 0x0020, 0x0040
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0800, 0x1000
MOUSEEVENTF_VIRTUALDESK, MOUSEEVENTF_ABSOLUTE = 0x4000, 0x8000
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0001, 0x0002, 0x0004
WHEEL_DELTA = 120
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79
SRCCOPY, CAPTUREBLT = 0x00CC0020, 0x40000000
MONITOR_DEFAULTTONEAREST = 2
MONITORINFOF_PRIMARY = 1
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
VK_ESCAPE = 0x1B

_BUTTONS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


_user32 = None
_gdi32 = None
_local = threading.local()


def _dlls():  # noqa: ANN202
    global _user32, _gdi32
    if _user32 is None:
        u = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
        g = ctypes.WinDLL("gdi32", use_last_error=True)  # type: ignore[attr-defined]
        u.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        u.SendInput.restype = wintypes.UINT
        u.GetDC.argtypes = [wintypes.HWND]
        u.GetDC.restype = wintypes.HDC
        u.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        u.GetForegroundWindow.restype = wintypes.HWND
        u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        u.MonitorFromWindow.restype = wintypes.HMONITOR
        u.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
        u.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        u.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        u.GetAsyncKeyState.argtypes = [ctypes.c_int]
        u.GetAsyncKeyState.restype = ctypes.c_short
        u.VkKeyScanW.argtypes = [wintypes.WCHAR]
        u.VkKeyScanW.restype = ctypes.c_short
        u.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
        u.MapVirtualKeyW.restype = wintypes.UINT
        g.CreateCompatibleDC.argtypes = [wintypes.HDC]
        g.CreateCompatibleDC.restype = wintypes.HDC
        g.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
        g.CreateCompatibleBitmap.restype = wintypes.HBITMAP
        g.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        g.SelectObject.restype = wintypes.HGDIOBJ
        g.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HDC,
                             ctypes.c_int, ctypes.c_int, wintypes.DWORD]
        g.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                                ctypes.POINTER(BITMAPINFO), wintypes.UINT]
        g.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        g.DeleteDC.argtypes = [wintypes.HDC]
        _user32, _gdi32 = u, g
    return _user32, _gdi32


def _dpi_aware_thread() -> None:
    """Bu thread'i Per Monitor v2 DPI farkındalığına al (koordinatlar fiziksel piksel olsun)."""
    if getattr(_local, "dpi", False):
        return
    u, _ = _dlls()
    try:
        fn = u.SetThreadDpiAwarenessContext
        fn.argtypes = [ctypes.c_void_p]
        fn.restype = ctypes.c_void_p
        fn(ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2))
    except (AttributeError, OSError):  # Windows 10 1607 öncesi
        log.debug("SetThreadDpiAwarenessContext yok", exc_info=True)
    _local.dpi = True


def _rect(r: wintypes.RECT) -> Rect:
    return Rect(r.left, r.top, r.right - r.left, r.bottom - r.top)


class Win32Screen:
    def _monitor_info(self, hmon) -> tuple[Rect, bool] | None:  # noqa: ANN001
        u, _ = _dlls()
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if not u.GetMonitorInfoW(hmon, ctypes.byref(info)):
            return None
        return _rect(info.rcMonitor), bool(info.dwFlags & MONITORINFOF_PRIMARY)

    def monitors(self) -> list[Rect]:
        _dpi_aware_thread()
        u, _ = _dlls()
        found: list[tuple[Rect, bool]] = []
        proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                       ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

        def cb(hmon, _hdc, _rc, _lp):  # noqa: ANN001, ANN202
            mi = self._monitor_info(hmon)
            if mi is not None:
                found.append(mi)
            return True

        u.EnumDisplayMonitors(None, None, proc_type(cb), 0)
        found.sort(key=lambda m: (not m[1], m[0].left, m[0].top))
        return [r for r, _p in found]

    def monitor_of_foreground(self) -> Rect | None:
        _dpi_aware_thread()
        u, _ = _dlls()
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return None
        mi = self._monitor_info(u.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST))
        return mi[0] if mi else None

    def capture(self, monitor: Rect) -> np.ndarray:
        _dpi_aware_thread()
        u, g = _dlls()
        w, h = monitor.width, monitor.height
        screen_dc = u.GetDC(None)
        if not screen_dc:
            raise OSError("Ekran aygıt bağlamı alınamadı")
        mem_dc = g.CreateCompatibleDC(screen_dc)
        bmp = g.CreateCompatibleBitmap(screen_dc, w, h)
        old = g.SelectObject(mem_dc, bmp)
        try:
            if not g.BitBlt(mem_dc, 0, 0, w, h, screen_dc, monitor.left, monitor.top, SRCCOPY | CAPTUREBLT):
                raise OSError(f"BitBlt başarısız ({ctypes.get_last_error()})")
            bmi = BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = w
            bmi.bmiHeader.biHeight = -h          # yukarıdan aşağı
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            buf = ctypes.create_string_buffer(w * h * 4)
            if g.GetDIBits(mem_dc, bmp, 0, h, buf, ctypes.byref(bmi), 0) != h:
                raise OSError("GetDIBits başarısız")
        finally:
            g.SelectObject(mem_dc, old)
            g.DeleteObject(bmp)
            g.DeleteDC(mem_dc)
            u.ReleaseDC(None, screen_dc)
        bgra = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)
        return np.ascontiguousarray(bgra[:, :, 2::-1])


class Win32Input:
    def _send(self, *inputs: INPUT) -> None:
        u, _ = _dlls()
        arr = (INPUT * len(inputs))(*inputs)
        sent = u.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
        if sent != len(inputs):
            raise OSError(f"SendInput girdiyi iletemedi (hata {ctypes.get_last_error()})")

    @staticmethod
    def _mouse(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> INPUT:
        return INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0))

    def move(self, x: int, y: int) -> None:
        _dpi_aware_thread()
        u, _ = _dlls()
        u.SetCursorPos(int(x), int(y))
        vx, vy = u.GetSystemMetrics(SM_XVIRTUALSCREEN), u.GetSystemMetrics(SM_YVIRTUALSCREEN)
        vw, vh = u.GetSystemMetrics(SM_CXVIRTUALSCREEN), u.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        nx = round((x - vx) * 65535 / max(vw - 1, 1))
        ny = round((y - vy) * 65535 / max(vh - 1, 1))
        self._send(self._mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, nx, ny))

    def cursor(self) -> tuple[int, int]:
        _dpi_aware_thread()
        u, _ = _dlls()
        pt = wintypes.POINT()
        u.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    def button(self, button: str, down: bool) -> None:
        flags = _BUTTONS[button][0 if down else 1]
        self._send(self._mouse(flags))

    def wheel(self, vertical_clicks: int, horizontal_clicks: int) -> None:
        if vertical_clicks:
            self._send(self._mouse(MOUSEEVENTF_WHEEL, data=vertical_clicks * WHEEL_DELTA))
        if horizontal_clicks:
            self._send(self._mouse(MOUSEEVENTF_HWHEEL, data=horizontal_clicks * WHEEL_DELTA))

    def key(self, vk: int, down: bool, extended: bool = False) -> None:
        u, _ = _dlls()
        scan = u.MapVirtualKeyW(vk, 0) & 0xFFFF
        flags = (0 if down else KEYEVENTF_KEYUP) | (KEYEVENTF_EXTENDEDKEY if extended else 0)
        self._send(INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(vk, scan, flags, 0, 0)))

    def unicode_char(self, ch: str) -> None:
        units = ch.encode("utf-16-le")
        events = []
        for i in range(0, len(units), 2):
            code = int.from_bytes(units[i:i + 2], "little")
            events.append(INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE, 0, 0)))
            events.append(INPUT(type=INPUT_KEYBOARD,
                                ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0)))
        self._send(*events)

    def char_vk(self, ch: str) -> int | None:
        u, _ = _dlls()
        res = u.VkKeyScanW(ch) & 0xFFFF
        if res == 0xFFFF or (res >> 8) & 0xFF:
            return None   # bu düzende yok ya da Shift/AltGr gerektiriyor: Unicode yolu
        return res & 0xFF


class Win32Probe:
    def foreground(self) -> ForegroundInfo:
        u, _ = _dlls()
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            # Güvenli masaüstü (UAC) açıkken ön plan penceresi okunamaz
            return ForegroundInfo(process="")
        title = ctypes.create_unicode_buffer(512)
        u.GetWindowTextW(hwnd, title, 512)
        cls = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(hwnd, cls, 256)
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        name = ""
        try:
            import psutil

            name = psutil.Process(pid.value).name().lower()
        except Exception:  # noqa: BLE001  erişim yok (yükseltilmiş işlem) ya da kapandı
            name = "?"
        return ForegroundInfo(title.value, cls.value, name, int(pid.value))

    def process_running(self, exe_name: str) -> bool:
        import psutil

        target = exe_name.lower()
        for p in psutil.process_iter(["name"]):
            if (p.info.get("name") or "").lower() == target:
                return True
        return False


class Win32EscKillSwitch:
    """Esc'ye kısa sürede iki kez basılınca tetiklenir (GetAsyncKeyState yoklaması, 25 ms).

    Düşük seviyeli klavye kancası (WH_KEYBOARD_LL) yerine yoklama seçildi: Python'daki bir kanca
    geri çağrısı GIL beklerken sistemin tüm klavye girdisini yavaşlatabilir. JARVIS'in kendi
    gönderdiği Esc tuşları `note_injected_escape` ile yok sayılır."""

    def __init__(self, window_s: float = 0.7, poll_s: float = 0.025) -> None:
        self.window_s = window_s
        self.poll_s = poll_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._injected_at = -10.0

    def note_injected_escape(self) -> None:
        self._injected_at = time.monotonic()

    def start(self, on_trigger: Callable[[], None]) -> None:
        self.stop()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(on_trigger,), name="jarvis-esc-esc", daemon=True)
        self._thread.start()

    def _run(self, on_trigger: Callable[[], None]) -> None:
        u, _ = _dlls()
        u.GetAsyncKeyState(VK_ESCAPE)       # "son çağrıdan beri basıldı" bitini sıfırla
        was_down = False
        presses: list[float] = []
        while not self._stop.wait(self.poll_s):
            state = u.GetAsyncKeyState(VK_ESCAPE)
            down = bool(state & 0x8000)
            tapped = bool(state & 0x0001) and not down   # yoklamalar arasında basılıp bırakılmış
            now = time.monotonic()
            if (down and not was_down) or tapped:
                if now - self._injected_at > 0.4:
                    presses = [t for t in presses if now - t <= self.window_s] + [now]
                    if len(presses) >= 2:
                        presses.clear()
                        try:
                            on_trigger()
                        except Exception:  # noqa: BLE001
                            log.exception("Acil durdurma geri çağrısı hata verdi")
            was_down = down

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)
        self._thread = None


def default_desktop() -> Desktop | None:
    """Gerçek Windows masaüstü; başka sistemlerde None."""
    if not IS_WINDOWS:
        return None
    return Desktop(Win32Screen(), Win32Input(), Win32Probe(), Win32EscKillSwitch(), Clock())
