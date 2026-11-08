"""Pencere düzeni: büyüt/küçült, sol/sağ yarı, yan yana diz, hepsini küçült, diğer monitöre taşı.

"Chrome ile VS Code'u yan yana diz", "bu pencereyi büyüt", "hepsini küçült", "pencereyi sol yarıya al",
"Spotify'ı diğer monitöre taşı". Saf ctypes Win32 (EnumWindows, ShowWindowAsync, SetWindowPos,
MonitorFromWindow, DwmGetWindowAttribute). Her işlemden sonra pencerenin durumu/yeri yeniden okunur;
istenen sonuç oluşmadıysa dürüstçe söylenir.

"Bu pencere" (ad verilmezse): öndeki pencere. Komut JARVIS'in kendi penceresine yazıldıysa
Z sırasında altındaki ilk pencere (kullanıcı az önce çalıştığı pencereyi kastediyor); hangi
pencereye dokunulduğu özette başlığıyla söylenir. JARVIS'in kendi pencereleri asla taşınmaz.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from collections.abc import Callable
from difflib import SequenceMatcher
from typing import Protocol

from jarvis.security.permission_manager import Permission
from jarvis.system.screen import Monitor, Rect, ScreenBackend
from jarvis.system.screen import default_backend as default_screen
from jarvis.tools import schema as js
from jarvis.tools.registry import (WINDOWS_ONLY, NotSupportedError, ToolContext, ToolError, ToolOutput, ToolRegistry,
                                   ToolSpec)
from jarvis.tools.text import fold, suffix_variants
from jarvis.tools.winapi import WindowInfo

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
ACTIONS = ["maximize", "minimize", "restore", "left_half", "right_half", "side_by_side", "minimize_all",
           "restore_all", "move_to_monitor"]
TOLERANCE_PX = 24          # uygulamalar boyutu birkaç piksel yuvarlayabilir
_SHELL_CLASSES = frozenset({"progman", "workerw", "shell_traywnd", "shell_secondarytraywnd",
                            "windows.ui.core.corewindow"})


class LayoutBackend(Protocol):
    def windows(self) -> list[WindowInfo]: ...           # kullanıcı pencereleri, Z sırasıyla (en üstteki önce)
    def active(self) -> WindowInfo | None: ...
    def monitors(self) -> list[Monitor]: ...
    def monitor_of(self, hwnd: int) -> int | None: ...
    def frame(self, hwnd: int) -> Rect | None: ...       # görünen çerçeve
    def state(self, hwnd: int) -> str: ...               # "normal" | "minimized" | "maximized"
    def show(self, hwnd: int, how: str) -> None: ...     # "maximize" | "minimize" | "restore"
    def place(self, hwnd: int, rect: Rect) -> None: ...  # görünen çerçeveyi rect'e oturtur


class Win32Layout:  # pragma: no cover - yalnızca Windows
    SW = {"maximize": 3, "minimize": 6, "restore": 9}

    def __init__(self, screen: ScreenBackend | None = None) -> None:
        if not IS_WINDOWS:
            raise NotSupportedError()
        import ctypes
        from ctypes import wintypes as wt

        self.ct, self.wt = ctypes, wt
        self.screen = screen or default_screen()
        u = self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        try:
            self.dwm = ctypes.WinDLL("dwmapi")
            self.dwm.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]
            self.dwm.DwmGetWindowAttribute.restype = ctypes.c_long
        except OSError:
            self.dwm = None

        class RECT(ctypes.Structure):
            _fields_ = [("left", wt.LONG), ("top", wt.LONG), ("right", wt.LONG), ("bottom", wt.LONG)]

        self.RECT = RECT
        self.ENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        u.EnumWindows.argtypes, u.EnumWindows.restype = [self.ENUMPROC, wt.LPARAM], wt.BOOL
        u.IsWindowVisible.argtypes, u.IsWindowVisible.restype = [wt.HWND], wt.BOOL
        u.IsWindow.argtypes, u.IsWindow.restype = [wt.HWND], wt.BOOL
        u.IsIconic.argtypes, u.IsIconic.restype = [wt.HWND], wt.BOOL
        u.IsZoomed.argtypes, u.IsZoomed.restype = [wt.HWND], wt.BOOL
        u.GetWindow.argtypes, u.GetWindow.restype = [wt.HWND, wt.UINT], wt.HWND
        u.GetWindowLongW.argtypes, u.GetWindowLongW.restype = [wt.HWND, ctypes.c_int], wt.LONG
        u.GetWindowTextLengthW.argtypes, u.GetWindowTextLengthW.restype = [wt.HWND], ctypes.c_int
        u.GetWindowTextW.argtypes, u.GetWindowTextW.restype = [wt.HWND, wt.LPWSTR, ctypes.c_int], ctypes.c_int
        u.GetClassNameW.argtypes, u.GetClassNameW.restype = [wt.HWND, wt.LPWSTR, ctypes.c_int], ctypes.c_int
        u.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
        u.GetWindowThreadProcessId.restype = wt.DWORD
        u.GetWindowRect.argtypes, u.GetWindowRect.restype = [wt.HWND, ctypes.POINTER(RECT)], wt.BOOL
        u.ShowWindowAsync.argtypes, u.ShowWindowAsync.restype = [wt.HWND, ctypes.c_int], wt.BOOL
        u.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
        u.SetWindowPos.restype = wt.BOOL

    def _text(self, fn, hwnd: int, size: int | None = None) -> str:  # noqa: ANN001
        n = size or (self.user32.GetWindowTextLengthW(hwnd) + 1)
        buf = self.ct.create_unicode_buffer(n)
        fn(hwnd, buf, n)
        return buf.value

    def _pid(self, hwnd: int) -> int:
        pid = self.wt.DWORD()
        self.user32.GetWindowThreadProcessId(hwnd, self.ct.byref(pid))
        return int(pid.value)

    def _cloaked(self, hwnd: int) -> bool:
        if self.dwm is None:
            return False
        val = self.wt.DWORD()
        ok = self.dwm.DwmGetWindowAttribute(hwnd, 14, self.ct.byref(val), self.ct.sizeof(val)) == 0  # DWMWA_CLOAKED
        return ok and bool(val.value)

    def _info(self, hwnd: int) -> WindowInfo | None:
        u = self.user32
        if not u.IsWindowVisible(hwnd) or u.GetWindow(hwnd, 4):           # görünmez ya da sahipli
            return None
        if u.GetWindowLongW(hwnd, -20) & 0x00000080:                       # WS_EX_TOOLWINDOW
            return None
        title = self._text(u.GetWindowTextW, hwnd)
        if not title or self._cloaked(hwnd):
            return None
        if self._text(u.GetClassNameW, hwnd, 256).lower() in _SHELL_CLASSES:
            return None
        pid = self._pid(hwnd)
        if pid == os.getpid():
            return None
        return WindowInfo(int(hwnd), title, pid)

    def windows(self) -> list[WindowInfo]:
        out: list[WindowInfo] = []

        def cb(hwnd, _):  # noqa: ANN001, ANN202
            info = self._info(hwnd)
            if info is not None:
                out.append(info)
            return True

        self.user32.EnumWindows(self.ENUMPROC(cb), 0)
        return out

    def active(self) -> WindowInfo | None:
        got = self.screen.active_window()
        if got is None:
            return None
        hwnd, title = got
        return WindowInfo(hwnd, title, self._pid(hwnd))

    def monitors(self) -> list[Monitor]:
        return self.screen.monitors()

    def monitor_of(self, hwnd: int) -> int | None:
        return self.screen.monitor_of_window(hwnd)

    def frame(self, hwnd: int) -> Rect | None:
        return self.screen.window_rect(hwnd)

    def state(self, hwnd: int) -> str:
        if not self.user32.IsWindow(hwnd):
            return "closed"
        if self.user32.IsIconic(hwnd):
            return "minimized"
        return "maximized" if self.user32.IsZoomed(hwnd) else "normal"

    def show(self, hwnd: int, how: str) -> None:
        # Async: yanıt vermeyen bir uygulama JARVIS'i kilitlemesin
        if not self.user32.ShowWindowAsync(hwnd, self.SW[how]):
            log.debug("ShowWindowAsync %s: önceki durum aynıydı ya da reddedildi", how)

    def place(self, hwnd: int, rect: Rect) -> None:
        if self.state(hwnd) != "normal":
            self.show(hwnd, "restore")
            time.sleep(0.15)
        outer = self.RECT()
        self.user32.GetWindowRect(hwnd, self.ct.byref(outer))
        vis = self.frame(hwnd)
        # Windows 10/11'de pencerenin görünmez yeniden boyutlandırma kenarları var: onları telafi et
        dl = dt = dr = db = 0
        if vis is not None and outer.right > outer.left:
            dl, dt = vis.left - outer.left, vis.top - outer.top
            dr, db = outer.right - vis.right, outer.bottom - vis.bottom
        flags = 0x0004 | 0x0010 | 0x0200      # SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOOWNERZORDER
        x, y, w, h = rect.left - dl, rect.top - dt, rect.width + dl + dr, rect.height + dt + db
        if not self.user32.SetWindowPos(hwnd, None, x, y, w, h, flags):
            raise ToolError(f"SetWindowPos ({self.ct.get_last_error()})",
                            "Pencereyi taşıyamadım. Yönetici olarak çalışan pencerelere Windows izin vermiyor.")
        # Farklı ölçekli monitöre geçişte pencere DPI değişince kendini yeniden boyutlandırabilir: ikinci kez
        time.sleep(0.05)
        self.user32.SetWindowPos(hwnd, None, x, y, w, h, flags)


class UnsupportedLayout:
    def __getattr__(self, name: str):  # noqa: ANN204
        def fail(*a, **kw):  # noqa: ANN002, ANN003, ANN202
            raise NotSupportedError()
        return fail


def default_layout() -> LayoutBackend:
    return Win32Layout() if IS_WINDOWS else UnsupportedLayout()  # type: ignore[return-value]


def half(work: Rect, side: str) -> Rect:
    w = work.width // 2
    if side == "left":
        return Rect(work.left, work.top, w, work.height)
    return Rect(work.left + w, work.top, work.width - w, work.height)


def close_enough(a: Rect | None, b: Rect, tol: int = TOLERANCE_PX) -> bool:
    return a is not None and all(abs(x - y) <= tol for x, y in
                                 ((a.left, b.left), (a.top, b.top), (a.right, b.right), (a.bottom, b.bottom)))


def _short(title: str, n: int = 60) -> str:
    return title if len(title) <= n else title[: n - 1] + "…"


class LayoutTools:
    def __init__(self, ctx: ToolContext, backend: LayoutBackend | None = None,
                 pids_for: Callable[[object, str], set[int]] | None = None,
                 sleep: Callable[[float], None] = time.sleep, wait_s: float = 1.5) -> None:
        self.ctx = ctx
        self._backend = backend
        self._pids_for = pids_for
        self.sleep = sleep
        self.wait_s = wait_s
        self._minimized: list[int] = []      # "hepsini küçült" ile küçültülenler (geri getirmek için)

    @property
    def b(self) -> LayoutBackend:
        if self._backend is None:
            self._backend = default_layout()
        return self._backend

    # ---------- pencere bulma ----------
    def pids_for(self, entry: object, name: str) -> set[int]:
        if self._pids_for is not None:
            return self._pids_for(entry, name)
        from jarvis.tools.applications import running_matches

        return {p.pid for p in running_matches(entry, name)}  # type: ignore[arg-type]

    def find(self, name: str, exclude: set[int] = frozenset()) -> WindowInfo:  # type: ignore[assignment]
        name = name.strip()
        wins = [w for w in self.b.windows() if w.hwnd not in exclude]
        if not name:
            active = self.b.active()
            if active is not None and active.hwnd not in exclude:
                return active
            if wins:
                return wins[0]
            raise ToolError("pencere yok", "Ekranda düzenlenecek bir pencere bulamadım.")
        entry = self.ctx.apps.best(name) if self.ctx.apps is not None else None
        pids = self.pids_for(entry, name)
        cands = [w for w in wins if w.pid in pids]
        if not cands:
            # Pencere başlığına göre ("rapor.docx - Word", "Yeni sekme - Google Chrome")
            keys = [k for k in {*(fold(c) for c in suffix_variants(name)), fold(getattr(entry, "name", "") or "")}
                    if len(k) >= 2]

            def score(w: WindowInfo) -> float:
                t = fold(w.title)
                return max((1.0 if re.search(rf"\b{re.escape(k)}\b", t) else SequenceMatcher(None, k, t).ratio())
                           for k in keys)

            scored = sorted(((score(w), i, w) for i, w in enumerate(wins)), key=lambda x: (-x[0], x[1]))
            cands = [w for s, _, w in scored if s >= 0.8]
        if not cands:
            label = getattr(entry, "name", None) or name
            raise ToolError("pencere yok", f"Açık bir {label} penceresi bulamadım.")
        return cands[0]

    def _monitor(self, hwnd: int) -> Monitor:
        mons = self.b.monitors()
        if not mons:
            raise ToolError("monitör yok", "Monitör bilgisi okunamadı.")
        idx = self.b.monitor_of(hwnd)
        return next((m for m in mons if m.index == idx), next((m for m in mons if m.primary), mons[0]))

    def _wait(self, cond: Callable[[], bool]) -> bool:
        end = time.monotonic() + self.wait_s
        while True:
            if cond():
                return True
            if time.monotonic() >= end:
                return False
            self.sleep(0.05)

    # ---------- işlemler ----------
    def _show(self, w: WindowInfo, how: str) -> ToolOutput:
        expected = {"maximize": "maximized", "minimize": "minimized", "restore": "normal"}[how]
        self.b.show(w.hwnd, how)
        ok = self._wait(lambda: self.b.state(w.hwnd) == expected)
        verb = {"maximize": "büyüttüm", "minimize": "küçülttüm", "restore": "eski boyutuna getirdim"}[how]
        if not ok:
            return ToolOutput(f"'{_short(w.title)}' penceresine istek gönderdim ama durumu değişmedi; uygulama buna "
                              "izin vermiyor olabilir.", {"title": w.title, "state": self.b.state(w.hwnd), "ok": False})
        return ToolOutput(f"'{_short(w.title)}' penceresini {verb}.", {"title": w.title, "state": expected, "ok": True})

    def _place(self, w: WindowInfo, rect: Rect) -> bool:
        self.b.place(w.hwnd, rect)
        return self._wait(lambda: close_enough(self.b.frame(w.hwnd), rect))

    def _half(self, w: WindowInfo, side: str) -> ToolOutput:
        target = half(self._monitor(w.hwnd).work, side)
        ok = self._place(w, target)
        where = "sol" if side == "left" else "sağ"
        if not ok:
            return ToolOutput(f"'{_short(w.title)}' penceresini {where} yarıya taşıdım ama istenen boyuta tam oturmadı "
                              "(uygulama en küçük/en büyük boyutu sınırlıyor olabilir).",
                              {"title": w.title, "ok": False})
        return ToolOutput(f"'{_short(w.title)}' penceresini ekranın {where} yarısına aldım.",
                          {"title": w.title, "ok": True})

    def _side_by_side(self, app: str, second: str) -> ToolOutput:
        first = self.find(app)
        other = self.find(second, exclude={first.hwnd}) if second.strip() else None
        if other is None:
            rest = [w for w in self.b.windows() if w.hwnd != first.hwnd]
            if not rest:
                raise ToolError("ikinci pencere yok", "Yan yana dizmek için ikinci bir pencere bulamadım.")
            other = rest[0]
        if other.hwnd == first.hwnd:
            raise ToolError("aynı pencere", "İki uygulama da aynı pencereyi gösteriyor; iki farklı pencere gerekli.")
        work = self._monitor(first.hwnd).work
        ok1 = self._place(first, half(work, "left"))
        ok2 = self._place(other, half(work, "right"))
        names = f"'{_short(first.title, 40)}' solda, '{_short(other.title, 40)}' sağda"
        if not (ok1 and ok2):
            return ToolOutput(f"Pencereleri yan yana dizdim ({names}) ama biri istenen boyuta tam oturmadı.",
                              {"left": first.title, "right": other.title, "ok": False})
        return ToolOutput(f"Pencereleri yan yana dizdim: {names}.",
                          {"left": first.title, "right": other.title, "ok": True})

    def _minimize_all(self) -> ToolOutput:
        wins = [w for w in self.b.windows() if self.b.state(w.hwnd) != "minimized"]
        if not wins:
            return ToolOutput("Küçültülecek açık pencere yok.", {"minimized": 0})
        for w in wins:
            self.b.show(w.hwnd, "minimize")
        self._wait(lambda: all(self.b.state(w.hwnd) in ("minimized", "closed") for w in wins))
        done = [w for w in wins if self.b.state(w.hwnd) == "minimized"]
        self._minimized = [w.hwnd for w in done]
        if len(done) < len(wins):
            return ToolOutput(f"{len(done)} pencereyi küçülttüm; {len(wins) - len(done)} pencere küçülmedi.",
                              {"minimized": len(done), "failed": len(wins) - len(done)})
        return ToolOutput(f"Tüm pencereleri küçülttüm ({len(done)} pencere). Geri getirmek için "
                          "'pencereleri geri getir' diyebilirsin.", {"minimized": len(done)})

    def _restore_all(self) -> ToolOutput:
        hwnds = [h for h in self._minimized if self.b.state(h) == "minimized"]
        if not hwnds:
            self._minimized = []
            return ToolOutput("Geri getirilecek pencere yok; önce 'hepsini küçült' demelisin.", {"restored": 0})
        for h in reversed(hwnds):        # en alttakinden başla: en üstteki yine en üstte kalsın
            self.b.show(h, "restore")
        self._wait(lambda: all(self.b.state(h) != "minimized" for h in hwnds))
        back = [h for h in hwnds if self.b.state(h) not in ("minimized", "closed")]
        self._minimized = []
        return ToolOutput(f"{len(back)} pencereyi geri getirdim.", {"restored": len(back)})

    def _move_to_monitor(self, w: WindowInfo, monitor: int) -> ToolOutput:
        mons = self.b.monitors()
        if len(mons) < 2:
            raise ToolError("tek monitör", "Bilgisayara tek monitör bağlı; taşınacak başka monitör yok.")
        src = self._monitor(w.hwnd)
        if monitor == 0:
            dst = mons[src.index % len(mons)]          # sıradaki monitör (sonuncudan sonra birinci)
        else:
            dst = next((m for m in mons if m.index == monitor), None)
            if dst is None:
                raise ToolError("monitör yok", f"{monitor}. monitör yok; {len(mons)} monitör bağlı.")
        if dst.index == src.index:
            return ToolOutput(f"'{_short(w.title)}' zaten {dst.index}. monitörde.", {"monitor": dst.index})
        was_max = self.b.state(w.hwnd) == "maximized"
        cur = self.b.frame(w.hwnd) or src.work
        sw, dw = src.work, dst.work
        # Konum ve boyut çalışma alanına oranla korunur
        fx, fy = (cur.left - sw.left) / max(1, sw.width), (cur.top - sw.top) / max(1, sw.height)
        width = min(dw.width, max(200, round(cur.width * dw.width / max(1, sw.width))))
        height = min(dw.height, max(150, round(cur.height * dw.height / max(1, sw.height))))
        left = min(max(dw.left, dw.left + round(fx * dw.width)), dw.right - width)
        top = min(max(dw.top, dw.top + round(fy * dw.height)), dw.bottom - height)
        target = Rect(left, top, width, height)
        if was_max:
            self.b.place(w.hwnd, target)
            self.b.show(w.hwnd, "maximize")
            ok = self._wait(lambda: self.b.monitor_of(w.hwnd) == dst.index and self.b.state(w.hwnd) == "maximized")
        else:
            ok = self._place(w, target) and self.b.monitor_of(w.hwnd) == dst.index
        if not ok and self.b.monitor_of(w.hwnd) != dst.index:
            return ToolOutput(f"'{_short(w.title)}' penceresini {dst.index}. monitöre taşıyamadım.",
                              {"monitor": self.b.monitor_of(w.hwnd), "ok": False})
        return ToolOutput(f"'{_short(w.title)}' penceresini {dst.index}. monitöre taşıdım.",
                          {"monitor": dst.index, "ok": True})

    def arrange(self, args: dict) -> ToolOutput:
        action = args["action"]
        if action == "minimize_all":
            return self._minimize_all()
        if action == "restore_all":
            return self._restore_all()
        if action == "side_by_side":
            return self._side_by_side(args["app"], args["second_app"])
        w = self.find(args["app"])
        if action in ("maximize", "minimize", "restore"):
            return self._show(w, action)
        if action in ("left_half", "right_half"):
            return self._half(w, "left" if action == "left_half" else "right")
        return self._move_to_monitor(w, args["monitor"])


def register(reg: ToolRegistry, ctx: ToolContext, backend: LayoutBackend | None = None,
             pids_for: Callable[[object, str], set[int]] | None = None, **kw) -> LayoutTools:  # noqa: ANN003
    t = LayoutTools(ctx, backend, pids_for, **kw)
    reg.register(ToolSpec(
        name="arrange_windows", title="Pencere düzeni", category="ekran", platforms=WINDOWS_ONLY,
        description=("Pencereleri düzenler: büyüt, küçült, eski boyut, sol/sağ yarı, iki uygulamayı yan yana, "
                     "hepsini küçült/geri getir, başka monitöre taşı. app boşsa öndeki pencere. / Arranges windows "
                     "(maximize, minimize, halves, side by side, minimize all, move to another monitor)."),
        input_schema=js.obj(
            action=js.string("İşlem / action", enum=ACTIONS),
            app=js.string("Uygulama adı ya da pencere başlığı; boş = öndeki pencere / app or title, empty = active",
                          200),
            second_app=js.string("side_by_side için sağdaki uygulama, değilse boş / right-hand app or empty", 200),
            monitor=js.integer("move_to_monitor: hedef monitör no, 0 = sıradaki / target monitor, 0 = next", 0, 9)),
        permission=Permission.SAFE, handler=t.arrange, timeout_s=20))
    return t
