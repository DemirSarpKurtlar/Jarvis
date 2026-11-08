"""Klavye girişi ve pano: saf ctypes Win32 (SendInput, Clipboard API).

* `KeyboardBackend.type_text`: metni KEYEVENTF_UNICODE ile yazar; Türkçe karakterler (ç, ğ, ı, İ, ş...)
  klavye düzeninden bağımsız doğru çıkar. "\n" Enter tuşudur.
* `KeyboardBackend.copy_selection`: öndeki pencereye "kopyala" kısayolu gönderir. Komut istemi ve
  terminallerde Ctrl+C çalışan programı durdurabileceği için orada Ctrl+Insert kullanılır.
* `ClipboardBackend`: metni okur, önceki içeriği (bellek tabanlı tüm biçimler: metin, HTML, DIB
  resim, dosya listesi...) saklayıp geri yükler. Parola yöneticilerinin "pano geçmişine alma"
  işaretini tanır.

Windows dışında her işlem `NotSupportedError` fırlatır. Testler bu protokollere uyan sahteleri verir.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Protocol

from jarvis.tools.registry import NotSupportedError, ToolError

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

# Ctrl+C yerine Ctrl+Insert gönderilen pencere sınıfları (konsol/terminal: Ctrl+C = işlemi durdur)
TERMINAL_CLASSES = frozenset({"consolewindowclass", "cascadia_hosting_window_class", "mintty", "puttytype",
                              "virtualconsoleclass", "pseudoconsolewindow"})


@dataclass(frozen=True)
class ForegroundInfo:
    hwnd: int
    pid: int
    title: str
    class_name: str

    @property
    def is_own(self) -> bool:
        return self.pid == os.getpid()

    @property
    def is_terminal(self) -> bool:
        return self.class_name.lower() in TERMINAL_CLASSES


@dataclass
class ClipboardSnapshot:
    formats: list[tuple[int, bytes]] = field(default_factory=list)
    lost: list[int] = field(default_factory=list)     # saklanamayan biçimler (GDI tutamağı vb.)


class KeyboardBackend(Protocol):
    def foreground(self) -> ForegroundInfo | None: ...
    def type_text(self, text: str) -> None: ...
    def copy_selection(self, use_insert: bool) -> None: ...


class ClipboardBackend(Protocol):
    def sequence(self) -> int: ...
    def get_text(self) -> str | None: ...
    def is_sensitive(self) -> bool: ...
    def snapshot(self) -> ClipboardSnapshot: ...
    def restore(self, snap: ClipboardSnapshot) -> None: ...


# ---------- Windows ----------
class _Win32:  # pragma: no cover - yalnızca Windows
    """Ortak ctypes tanımları (kendi WinDLL örnekleri: başka modüllerin argtypes'ı ile çakışmaz)."""

    def __init__(self) -> None:
        if not IS_WINDOWS:
            raise NotSupportedError()
        import ctypes
        from ctypes import wintypes as wt

        self.ct, self.wt = ctypes, wt
        u = self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        k = self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ULONG_PTR = ctypes.c_size_t

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                        ("dwExtraInfo", ULONG_PTR)]

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                        ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]

        class _U(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wt.DWORD), ("u", _U)]

        self.INPUT, self.KEYBDINPUT = INPUT, KEYBDINPUT
        u.SendInput.argtypes, u.SendInput.restype = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int], wt.UINT
        u.GetForegroundWindow.argtypes, u.GetForegroundWindow.restype = [], wt.HWND
        u.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
        u.GetWindowThreadProcessId.restype = wt.DWORD
        u.GetWindowTextLengthW.argtypes, u.GetWindowTextLengthW.restype = [wt.HWND], ctypes.c_int
        u.GetWindowTextW.argtypes, u.GetWindowTextW.restype = [wt.HWND, wt.LPWSTR, ctypes.c_int], ctypes.c_int
        u.GetClassNameW.argtypes, u.GetClassNameW.restype = [wt.HWND, wt.LPWSTR, ctypes.c_int], ctypes.c_int
        u.GetAsyncKeyState.argtypes, u.GetAsyncKeyState.restype = [ctypes.c_int], ctypes.c_short
        # pano
        u.OpenClipboard.argtypes, u.OpenClipboard.restype = [wt.HWND], wt.BOOL
        u.CloseClipboard.argtypes, u.CloseClipboard.restype = [], wt.BOOL
        u.EmptyClipboard.argtypes, u.EmptyClipboard.restype = [], wt.BOOL
        u.GetClipboardSequenceNumber.argtypes, u.GetClipboardSequenceNumber.restype = [], wt.DWORD
        u.IsClipboardFormatAvailable.argtypes, u.IsClipboardFormatAvailable.restype = [wt.UINT], wt.BOOL
        u.GetClipboardData.argtypes, u.GetClipboardData.restype = [wt.UINT], wt.HANDLE
        u.SetClipboardData.argtypes, u.SetClipboardData.restype = [wt.UINT, wt.HANDLE], wt.HANDLE
        u.EnumClipboardFormats.argtypes, u.EnumClipboardFormats.restype = [wt.UINT], wt.UINT
        u.RegisterClipboardFormatW.argtypes, u.RegisterClipboardFormatW.restype = [wt.LPCWSTR], wt.UINT
        u.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
        u.CreateWindowExW.restype = wt.HWND
        u.DestroyWindow.argtypes, u.DestroyWindow.restype = [wt.HWND], wt.BOOL
        k.GlobalLock.argtypes, k.GlobalLock.restype = [wt.HGLOBAL], wt.LPVOID
        k.GlobalUnlock.argtypes, k.GlobalUnlock.restype = [wt.HGLOBAL], wt.BOOL
        k.GlobalSize.argtypes, k.GlobalSize.restype = [wt.HGLOBAL], ctypes.c_size_t
        k.GlobalAlloc.argtypes, k.GlobalAlloc.restype = [wt.UINT, ctypes.c_size_t], wt.HGLOBAL
        k.GlobalFree.argtypes, k.GlobalFree.restype = [wt.HGLOBAL], wt.HGLOBAL


_win: _Win32 | None = None


def _w() -> _Win32:  # pragma: no cover - yalnızca Windows
    global _win
    if _win is None:
        _win = _Win32()
    return _win


INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x1
KEYEVENTF_KEYUP = 0x2
KEYEVENTF_UNICODE = 0x4
VK_RETURN, VK_TAB, VK_CONTROL, VK_INSERT = 0x0D, 0x09, 0x11, 0x2D
VK_SHIFT, VK_MENU, VK_LWIN, VK_RWIN = 0x10, 0x12, 0x5B, 0x5C


def text_to_key_events(text: str) -> list[tuple[int, int, int]]:
    """Metni (vk, scan, bayraklar) olaylarına çevirir. Testlenebilir saf fonksiyon.

    Her UTF-16 birimi için KEYEVENTF_UNICODE basma/bırakma (emoji vekil çifti iki birimdir);
    satır sonu Enter, sekme Tab tuşudur. "\\r\\n" tek Enter sayılır.
    """
    events: list[tuple[int, int, int]] = []
    for ch in text.replace("\r\n", "\n").replace("\r", "\n"):
        if ch == "\n":
            events += [(VK_RETURN, 0, 0), (VK_RETURN, 0, KEYEVENTF_KEYUP)]
        elif ch == "\t":
            events += [(VK_TAB, 0, 0), (VK_TAB, 0, KEYEVENTF_KEYUP)]
        elif ord(ch) < 32 or ord(ch) == 127:
            continue      # diğer denetim karakterleri yazılmaz
        else:
            data = ch.encode("utf-16-le")
            for i in range(0, len(data), 2):
                unit = int.from_bytes(data[i:i + 2], "little")
                events += [(0, unit, KEYEVENTF_UNICODE), (0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)]
    return events


class Win32Keyboard:  # pragma: no cover - yalnızca Windows
    CHUNK = 64      # tek SendInput çağrısında en çok olay (uzun metinde uygulamalar yetişsin)

    def __init__(self) -> None:
        self.w = _w()

    def foreground(self) -> ForegroundInfo | None:
        w = self.w
        hwnd = w.user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = w.wt.DWORD()
        w.user32.GetWindowThreadProcessId(hwnd, w.ct.byref(pid))
        n = w.user32.GetWindowTextLengthW(hwnd)
        title = w.ct.create_unicode_buffer(n + 1)
        w.user32.GetWindowTextW(hwnd, title, n + 1)
        cls = w.ct.create_unicode_buffer(256)
        w.user32.GetClassNameW(hwnd, cls, 256)
        return ForegroundInfo(int(hwnd), int(pid.value), title.value, cls.value)

    def _send(self, events: list[tuple[int, int, int]]) -> None:
        w = self.w
        for start in range(0, len(events), self.CHUNK):
            part = events[start:start + self.CHUNK]
            arr = (w.INPUT * len(part))()
            for i, (vk, scan, flags) in enumerate(part):
                arr[i].type = INPUT_KEYBOARD
                arr[i].u.ki = w.KEYBDINPUT(vk, scan, flags, 0, 0)
            sent = w.user32.SendInput(len(part), arr, w.ct.sizeof(w.INPUT))
            if sent != len(part):
                err = w.ct.get_last_error()
                raise ToolError(f"SendInput {sent}/{len(part)} (hata {err})",
                                "Odaktaki pencereye yazamadım. Pencere yönetici olarak çalışıyorsa Windows buna "
                                "izin vermez.")
            if start + self.CHUNK < len(events):
                time.sleep(0.01)

    def _wait_modifiers_released(self, timeout: float = 1.5) -> None:
        """Kullanıcı hâlâ bir kısayol tuşunu (Ctrl/Alt/Shift/Win) basılı tutuyorsa bırakmasını bekle;
        yoksa gönderdiğimiz tuşlar başka bir kısayola dönüşebilir."""
        end = time.monotonic() + timeout
        keys = (VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN)
        while time.monotonic() < end:
            if not any(self.w.user32.GetAsyncKeyState(k) & 0x8000 for k in keys):
                return
            time.sleep(0.03)

    def type_text(self, text: str) -> None:
        self._wait_modifiers_released()
        self._send(text_to_key_events(text))

    def copy_selection(self, use_insert: bool) -> None:
        self._wait_modifiers_released()
        if use_insert:
            key = [(VK_INSERT, 0, KEYEVENTF_EXTENDEDKEY), (VK_INSERT, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP)]
        else:
            key = [(0x43, 0, 0), (0x43, 0, KEYEVENTF_KEYUP)]   # "C"
        self._send([(VK_CONTROL, 0, 0), *key, (VK_CONTROL, 0, KEYEVENTF_KEYUP)])


CF_UNICODETEXT = 13
# Bellek (HGLOBAL) olmayan, kopyalanamayan biçimler: GDI tutamakları ve sahip çizimi
_GDI_FORMATS = frozenset({2, 3, 9, 14, 0x80, 0x82, 0x83, 0x8E})
# CF_BITMAP, CF_DIB'den; CF_ENHMETAFILE ile CF_METAFILEPICT birbirinden sistemce üretilir
_SYNTHESIZED_FROM = {2: (8, 17), 3: (14,), 14: (3,), 0x82: (), 0x83: (), 0x8E: ()}
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024


class Win32Clipboard:  # pragma: no cover - yalnızca Windows
    def __init__(self) -> None:
        self.w = _w()
        u = self.w.user32
        self._exclude = u.RegisterClipboardFormatW("ExcludeClipboardContentFromMonitorProcessing")
        self._history = u.RegisterClipboardFormatW("CanIncludeInClipboardHistory")

    def _open(self, hwnd: int | None = None) -> None:
        for _ in range(25):           # başka bir program panoyu kısa süre tutuyor olabilir
            if self.w.user32.OpenClipboard(hwnd):
                return
            time.sleep(0.02)
        raise ToolError("OpenClipboard", "Panoya erişilemedi; başka bir program kullanıyor olabilir.")

    def sequence(self) -> int:
        return int(self.w.user32.GetClipboardSequenceNumber())

    def _read(self, fmt: int) -> bytes | None:
        w = self.w
        h = w.user32.GetClipboardData(fmt)
        if not h:
            return None
        size = w.kernel32.GlobalSize(h)
        if not size:
            return None
        ptr = w.kernel32.GlobalLock(h)
        if not ptr:
            return None
        try:
            return w.ct.string_at(ptr, size)
        finally:
            w.kernel32.GlobalUnlock(h)

    def get_text(self) -> str | None:
        self._open()
        try:
            if not self.w.user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                return None
            raw = self._read(CF_UNICODETEXT)
        finally:
            self.w.user32.CloseClipboard()
        if raw is None:
            return None
        return raw.decode("utf-16-le", errors="replace").split("\x00", 1)[0]

    def is_sensitive(self) -> bool:
        u = self.w.user32
        if self._exclude and u.IsClipboardFormatAvailable(self._exclude):
            return True
        if self._history and u.IsClipboardFormatAvailable(self._history):
            self._open()
            try:
                raw = self._read(self._history)
            finally:
                u.CloseClipboard()
            return raw is not None and raw[:4] == b"\x00\x00\x00\x00"   # DWORD 0: geçmişe alınmasın
        return False

    def snapshot(self) -> ClipboardSnapshot:
        snap = ClipboardSnapshot()
        self._open()
        try:
            fmt, total, present = 0, 0, []
            while True:
                fmt = self.w.user32.EnumClipboardFormats(fmt)
                if not fmt:
                    break
                present.append(fmt)
            for f in present:
                if f in _GDI_FORMATS:
                    continue
                data = self._read(f)
                if data is None or total + len(data) > MAX_SNAPSHOT_BYTES:
                    snap.lost.append(f)
                    continue
                total += len(data)
                snap.formats.append((f, data))
            kept = {f for f, _ in snap.formats}
            for f in present:
                if f in _GDI_FORMATS and not any(src in kept for src in _SYNTHESIZED_FROM.get(f, ())):
                    snap.lost.append(f)
        finally:
            self.w.user32.CloseClipboard()
        return snap

    def restore(self, snap: ClipboardSnapshot) -> None:
        w = self.w
        # Sahipsiz (NULL) açılan panoya SetClipboardData yazamaz: geçici, yalnızca mesaj alan bir pencere
        owner = w.user32.CreateWindowExW(0, "STATIC", "JARVIS pano", 0, 0, 0, 0, 0, w.wt.HWND(-3), None, None, None)
        try:
            self._open(owner)
            try:
                w.user32.EmptyClipboard()
                for fmt, data in snap.formats:
                    h = w.kernel32.GlobalAlloc(0x0002, max(1, len(data)))   # GMEM_MOVEABLE
                    if not h:
                        continue
                    ptr = w.kernel32.GlobalLock(h)
                    if not ptr:
                        w.kernel32.GlobalFree(h)
                        continue
                    w.ct.memmove(ptr, data, len(data))
                    w.kernel32.GlobalUnlock(h)
                    if not w.user32.SetClipboardData(fmt, h):
                        w.kernel32.GlobalFree(h)   # başarılıysa bellek artık sistemindir
            finally:
                w.user32.CloseClipboard()
        finally:
            if owner:
                w.user32.DestroyWindow(owner)


class UnsupportedKeyboard:
    def foreground(self) -> ForegroundInfo | None:
        raise NotSupportedError()

    def type_text(self, text: str) -> None:
        raise NotSupportedError()

    def copy_selection(self, use_insert: bool) -> None:
        raise NotSupportedError()


class UnsupportedClipboard:
    def sequence(self) -> int:
        raise NotSupportedError()

    def get_text(self) -> str | None:
        raise NotSupportedError()

    def is_sensitive(self) -> bool:
        raise NotSupportedError()

    def snapshot(self) -> ClipboardSnapshot:
        raise NotSupportedError()

    def restore(self, snap: ClipboardSnapshot) -> None:
        raise NotSupportedError()


def default_keyboard() -> KeyboardBackend:
    return Win32Keyboard() if IS_WINDOWS else UnsupportedKeyboard()


def default_clipboard() -> ClipboardBackend:
    return Win32Clipboard() if IS_WINDOWS else UnsupportedClipboard()
