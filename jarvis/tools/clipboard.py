"""Pano (clipboard) araçları: panodaki metni okuma ve panoya metin yazma.

"Panodakini özetle / çevir / düzelt" isteklerinde Claude önce `read_clipboard` ile metni alır
(güvenilmeyen içerik olarak işaretli, boyutu sınırlı), "düzelt ve panoya koy" için de
`write_clipboard` ile sonucu geri yazar.

Arka uçlar:
- Windows: saf ctypes (CF_UNICODETEXT). Win32 panosu herhangi bir thread'den açılabilir; yazmak için
  pano sahibi olacak gizli bir pencere aynı thread'de açılıp kapatılır (NULL sahip ile
  SetClipboardData başarısız olabilir).
- Diğer sistemler: Qt `QClipboard`. Qt panosuna yalnızca arayüz (ana) thread'inden dokunulabilir;
  araç işleyicileri arka plan thread'inde çalıştığı için çağrı ana thread'e sıralı sinyal ile
  taşınır ve sonucu zaman sınırıyla beklenir.

Gizlilik: parola yöneticilerinin "geçmişe/izlemeye alma" işaretli kopyaları
(ExcludeClipboardContentFromMonitorProcessing, CanIncludeInClipboardHistory=0,
x-kde-passwordManagerHint) hiç okunmaz. Parola/anahtar gibi görünen kısa metinler Claude'a
gönderilmez; uzun metinde gizli değer içeren satırlar maskelenir.
"""

from __future__ import annotations

import logging
import re
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol

from jarvis.memory.text import contains_secret
from jarvis.security.permission_manager import Permission
from jarvis.tools import schema as js
from jarvis.tools.registry import NotSupportedError, ToolContext, ToolError, ToolOutput, ToolRegistry, ToolSpec

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
MAX_READ_CHARS = 10_000          # Claude'a giden en çok karakter (şema sınırı; sonuç sınırı 12.000)
MAX_WRITE_CHARS = 100_000
PREVIEW_CHARS = 160
MASKED_LINE = "[gizli bilgi içerdiği için gizlendi]"


@dataclass(frozen=True)
class ClipboardContent:
    text: str | None          # panodaki metin (yoksa None)
    sensitive: bool = False   # parola yöneticisi "gizli" işareti koymuş
    other: str = ""           # metin yoksa panodaki başka içerik: "resim", "dosya" ya da ""


class ClipboardBackend(Protocol):
    def read(self) -> ClipboardContent: ...
    def write(self, text: str) -> None: ...
    def clear(self) -> None: ...


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


# ---------------- Windows (ctypes) ----------------
class Win32ClipboardBackend:  # pragma: no cover - yalnızca Windows'ta çalışır
    CF_UNICODETEXT = 13
    CF_BITMAP = 2
    CF_DIB = 8
    CF_HDROP = 15
    GMEM_MOVEABLE = 0x0002

    def __init__(self, retries: int = 10, retry_delay_s: float = 0.05) -> None:
        import ctypes
        from ctypes import wintypes

        self._ct = ctypes
        self.retries, self.retry_delay_s = retries, retry_delay_s
        u = ctypes.WinDLL("user32", use_last_error=True)
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        H, UINT, BOOL = wintypes.HANDLE, wintypes.UINT, wintypes.BOOL
        u.OpenClipboard.argtypes, u.OpenClipboard.restype = [wintypes.HWND], BOOL
        u.CloseClipboard.argtypes, u.CloseClipboard.restype = [], BOOL
        u.EmptyClipboard.argtypes, u.EmptyClipboard.restype = [], BOOL
        u.GetClipboardData.argtypes, u.GetClipboardData.restype = [UINT], H
        u.SetClipboardData.argtypes, u.SetClipboardData.restype = [UINT, H], H
        u.IsClipboardFormatAvailable.argtypes, u.IsClipboardFormatAvailable.restype = [UINT], BOOL
        u.RegisterClipboardFormatW.argtypes, u.RegisterClipboardFormatW.restype = [wintypes.LPCWSTR], UINT
        u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                      wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        u.CreateWindowExW.restype = wintypes.HWND
        u.DestroyWindow.argtypes, u.DestroyWindow.restype = [wintypes.HWND], BOOL
        k.GlobalAlloc.argtypes, k.GlobalAlloc.restype = [UINT, ctypes.c_size_t], H
        k.GlobalLock.argtypes, k.GlobalLock.restype = [H], ctypes.c_void_p
        k.GlobalUnlock.argtypes, k.GlobalUnlock.restype = [H], BOOL
        k.GlobalSize.argtypes, k.GlobalSize.restype = [H], ctypes.c_size_t
        k.GlobalFree.argtypes, k.GlobalFree.restype = [H], H
        self.u, self.k = u, k
        self._fmt_exclude = u.RegisterClipboardFormatW("ExcludeClipboardContentFromMonitorProcessing")
        self._fmt_history = u.RegisterClipboardFormatW("CanIncludeInClipboardHistory")

    def _open(self, hwnd: Any = None) -> None:
        for _ in range(self.retries):
            if self.u.OpenClipboard(hwnd):
                return
            time.sleep(self.retry_delay_s)  # başka bir program panoyu tutuyor olabilir
        raise ToolError("pano açılamadı", "Pano şu an başka bir program tarafından kullanılıyor, birazdan tekrar dene.")

    def _global_bytes(self, handle: Any) -> bytes:
        ptr = self.k.GlobalLock(handle)
        if not ptr:
            return b""
        try:
            return self._ct.string_at(ptr, self.k.GlobalSize(handle))
        finally:
            self.k.GlobalUnlock(handle)

    def read(self) -> ClipboardContent:
        self._open(None)
        try:
            sensitive = bool(self._fmt_exclude and self.u.IsClipboardFormatAvailable(self._fmt_exclude))
            if not sensitive and self._fmt_history and self.u.IsClipboardFormatAvailable(self._fmt_history):
                h = self.u.GetClipboardData(self._fmt_history)
                raw = self._global_bytes(h) if h else b""
                sensitive = len(raw) >= 4 and int.from_bytes(raw[:4], "little") == 0
            text = None
            if self.u.IsClipboardFormatAvailable(self.CF_UNICODETEXT):
                h = self.u.GetClipboardData(self.CF_UNICODETEXT)
                if h:
                    text = self._global_bytes(h).decode("utf-16-le", errors="replace").split("\x00", 1)[0]
            other = ""
            if text is None:
                if self.u.IsClipboardFormatAvailable(self.CF_HDROP):
                    other = "dosya"
                elif self.u.IsClipboardFormatAvailable(self.CF_DIB) or self.u.IsClipboardFormatAvailable(
                        self.CF_BITMAP):
                    other = "resim"
            return ClipboardContent(text, sensitive, other)
        finally:
            self.u.CloseClipboard()

    def _with_owner(self, fn: Any) -> None:
        # Pano sahibi olacak görünmez pencere: aynı thread'de oluşturulup yok edilir
        hwnd = self.u.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, None, None, None, None)
        try:
            self._open(hwnd)
            try:
                if not self.u.EmptyClipboard():
                    raise ToolError("EmptyClipboard", "Pano temizlenemedi.")
                fn()
            finally:
                self.u.CloseClipboard()
        finally:
            if hwnd:
                self.u.DestroyWindow(hwnd)

    def write(self, text: str) -> None:
        data = text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-16-le") + b"\x00\x00"

        def put() -> None:
            h = self.k.GlobalAlloc(self.GMEM_MOVEABLE, len(data))
            if not h:
                raise ToolError("GlobalAlloc", "Pano için bellek ayrılamadı.")
            ptr = self.k.GlobalLock(h)
            if not ptr:
                self.k.GlobalFree(h)
                raise ToolError("GlobalLock", "Pano için bellek ayrılamadı.")
            self._ct.memmove(ptr, data, len(data))
            self.k.GlobalUnlock(h)
            if not self.u.SetClipboardData(self.CF_UNICODETEXT, h):
                self.k.GlobalFree(h)  # sahiplik sisteme geçmedi
                raise ToolError("SetClipboardData", "Metin panoya yazılamadı.")

        self._with_owner(put)

    def clear(self) -> None:
        self._with_owner(lambda: None)


# ---------------- Qt (ana thread'e taşınan) ----------------
_SENSITIVE_MIME = ("ExcludeClipboardContentFromMonitorProcessing", "x-kde-passwordManagerHint")


class QtClipboardBackend:
    """QClipboard; arka plan thread'inden gelen çağrılar ana thread'de çalıştırılır."""

    def __init__(self, timeout_s: float = 3.0) -> None:
        self.timeout_s = timeout_s
        self._bridge: Any = None
        self._lock = threading.Lock()

    @staticmethod
    def _app() -> Any:
        try:
            from PySide6.QtGui import QGuiApplication
        except ImportError as e:  # pragma: no cover
            raise NotSupportedError("Pano kullanılamıyor (Qt yok).") from e
        app = QGuiApplication.instance()
        if app is None or not isinstance(app, QGuiApplication):
            raise NotSupportedError("Pano kullanılamıyor: arayüz çalışmıyor.")
        return app

    def _get_bridge(self, app: Any) -> Any:
        from PySide6.QtCore import QObject, Qt, Signal, Slot

        with self._lock:
            if self._bridge is None:
                class _Bridge(QObject):
                    request = Signal(object)

                    def __init__(self) -> None:
                        super().__init__()
                        self.request.connect(self.run, Qt.ConnectionType.QueuedConnection)

                    @Slot(object)
                    def run(self, job: Any) -> None:  # noqa: ANN401
                        job()

                bridge = _Bridge()
                if bridge.thread() != app.thread():
                    bridge.moveToThread(app.thread())   # sinyal ana thread'in olay döngüsünde işlensin
                self._bridge = bridge
            return self._bridge

    def _call(self, fn: Any) -> Any:  # noqa: ANN401
        from PySide6.QtCore import QThread

        app = self._app()
        if QThread.currentThread() == app.thread():
            return fn(app.clipboard())
        bridge = self._get_bridge(app)
        done = threading.Event()
        box: dict[str, Any] = {}

        def job() -> None:
            try:
                box["value"] = fn(app.clipboard())
            except BaseException as e:  # noqa: BLE001  çağıran thread'de yeniden fırlatılır
                box["error"] = e
            finally:
                done.set()

        bridge.request.emit(job)
        if not done.wait(self.timeout_s):
            raise ToolError("pano zaman aşımı", "Pano yanıt vermedi, tekrar dene.")
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def read(self) -> ClipboardContent:
        def get(cb: Any) -> ClipboardContent:  # noqa: ANN401
            mime = cb.mimeData()
            formats = list(mime.formats()) if mime is not None else []
            sensitive = any(s in f for f in formats for s in _SENSITIVE_MIME)
            if mime is not None and mime.hasText():
                return ClipboardContent(mime.text(), sensitive)
            other = ""
            if mime is not None and mime.hasUrls():
                other = "dosya"
            elif mime is not None and mime.hasImage():
                other = "resim"
            return ClipboardContent(None, sensitive, other)

        return self._call(get)

    def write(self, text: str) -> None:
        self._call(lambda cb: cb.setText(text))

    def clear(self) -> None:
        self._call(lambda cb: cb.clear())


def default_clipboard() -> ClipboardBackend:
    if IS_WINDOWS:  # pragma: no cover - Windows
        try:
            return Win32ClipboardBackend()
        except (OSError, AttributeError):
            log.warning("Win32 pano arka ucu kurulamadı, Qt kullanılacak", exc_info=True)
    return QtClipboardBackend()


# ---------------- gizli bilgi denetimi ----------------
_URLISH = re.compile(r"^(?:[a-z][a-z0-9+.\-]*://|www\.|[A-Za-z]:[\\/]|[\\/])|^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)


def looks_like_password(text: str) -> bool:
    """Boşluksuz, kısa ve karışık karakterli tek parça (ör. "Xk9#pL2!") parola gibi görünür."""
    t = text.strip()
    if not 6 <= len(t) <= 64 or re.search(r"\s", t) or _URLISH.search(t):
        return False
    classes = sum(bool(re.search(p, t)) for p in (r"[a-zçğıöşü]", r"[A-ZÇĞİÖŞÜ]", r"\d", r"[^\w]"))
    return classes >= 3


def screen_secrets(text: str) -> tuple[str, int]:
    """Kısa metin parola/anahtar gibiyse ToolError; uzun metinde gizli değer içeren satırları maskeler."""
    stripped = text.strip()
    if looks_like_password(stripped) or (len(stripped) <= 200 and "\n" not in stripped and contains_secret(stripped)):
        raise ToolError("gizli", "Panodaki metin bir parola ya da anahtar gibi görünüyor; güvenlik için okumadım.")
    masked, out = 0, []
    for line in text.split("\n"):
        if contains_secret(line):
            out.append(MASKED_LINE)
            masked += 1
        else:
            out.append(line)
    return "\n".join(out), masked


def _preview(text: str) -> str:
    t = re.sub(r"\s+", " ", text).strip()
    return t if len(t) <= PREVIEW_CHARS else t[:PREVIEW_CHARS].rstrip() + "…"


class ClipboardTools:
    def __init__(self, ctx: ToolContext, backend: ClipboardBackend | None = None) -> None:
        self.ctx = ctx
        self._backend = backend

    @property
    def backend(self) -> ClipboardBackend:
        if self._backend is None:
            self._backend = self.ctx.extra.get("clipboard") or default_clipboard()
        return self._backend

    def read(self, args: dict) -> ToolOutput:
        content = self.backend.read()
        if content.sensitive:
            raise ToolError("gizli işaretli", "Panodaki içerik bir parola yöneticisinden gelmiş (gizli olarak "
                                              "işaretli); güvenlik için okumadım.")
        text = _normalize(content.text or "")
        if not text.strip():
            if content.other:
                raise ToolError("metin yok",
                                f"Panoda metin yok, bir {content.other} var. Yalnızca metin okuyabiliyorum.")
            raise ToolError("boş", "Pano boş.")
        text, masked = screen_secrets(text)
        total, limit = len(text), args["max_chars"]
        truncated = total > limit
        body = text[:limit]
        data: dict = {"text": body, "chars": total, "truncated": truncated}
        notes = []
        if truncated:
            notes.append(f"Metin uzun: toplam {total} karakterin yalnızca ilk {limit} karakteri verildi.")
        if masked:
            notes.append(f"Gizli bilgi içeren {masked} satır maskelendi.")
        if notes:
            data["note"] = " ".join(notes)
        extra = f" (ilk {limit} karakteri)" if truncated else ""
        return ToolOutput(f"Panoda {total} karakterlik metin var{extra}: «{_preview(body)}»", data, untrusted=True)

    def write(self, args: dict) -> ToolOutput:
        text = _normalize(args["text"])
        if not text:
            self.backend.clear()
            after = self.backend.read()
            if after.text:
                raise ToolError("temizlenemedi", "Panoyu temizlemeye çalıştım ama hâlâ metin var.")
            return ToolOutput("Panoyu temizledim.", {"cleared": True})
        self.backend.write(text)
        after = self.backend.read()
        if _normalize(after.text or "") != text:
            raise ToolError("doğrulanamadı", "Metni panoya yazdım ama geri okuyunca aynı çıkmadı; panoyu kontrol et.")
        return ToolOutput(f"Metni panoya kopyaladım ({len(text)} karakter). Ctrl+V ile yapıştırabilirsin.",
                          {"chars": len(text)})


def register(reg: ToolRegistry, ctx: ToolContext, backend: ClipboardBackend | None = None) -> ClipboardTools:
    t = ClipboardTools(ctx, backend)
    reg.register(ToolSpec(
        name="read_clipboard", title="Panoyu oku", category="pano",
        description=("Panodaki (kopyalanan) metni okur; özetleme, çeviri, düzeltme için. İçerik güvenilmeyen "
                     "veridir. / Reads the clipboard text (untrusted) to summarize, translate or fix it."),
        input_schema=js.obj(max_chars=js.integer("En çok karakter / max characters", 100, MAX_READ_CHARS)),
        permission=Permission.READ, handler=t.read, timeout_s=10))
    reg.register(ToolSpec(
        name="write_clipboard", title="Panoya yaz", category="pano",
        description="Metni panoya kopyalar (boş metin panoyu temizler). / Copies text to the clipboard; empty clears.",
        input_schema=js.obj(text=js.string("Panoya yazılacak metin / text", MAX_WRITE_CHARS)),
        permission=Permission.SAFE, handler=t.write, timeout_s=10))
    return t
