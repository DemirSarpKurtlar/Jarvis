"""Ekran/pencere/klavye/pano araçları için sahte arka uçlar (gerçek Windows'a dokunmaz)."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from jarvis.core.events import EventBus
from jarvis.security.action_validator import PathSandbox
from jarvis.system.screen import Monitor, Rect
from jarvis.tools.registry import ToolContext
from jarvis.tools.win_input import ClipboardSnapshot, ForegroundInfo
from jarvis.tools.winapi import WindowInfo
from tests.tool_fakes import make_ctx

M1 = Monitor(1, Rect(0, 0, 1920, 1080), Rect(0, 0, 1920, 1040), True, 1.0, r"\\.\DISPLAY1")
M2 = Monitor(2, Rect(1920, 0, 2560, 1440), Rect(1920, 0, 2560, 1400), False, 1.25, r"\\.\DISPLAY2")


def monitor_at(mons: list[Monitor], rect: Rect | None) -> int | None:
    if rect is None:
        return None
    x, y = rect.center
    for m in mons:
        if m.rect.contains(x, y):
            return m.index
    return mons[0].index if mons else None


class FakeScreen:
    """İki monitör; her yakalama düz renkli BGRA görüntü döndürür (renk sırası sınanabilsin)."""

    def __init__(self, monitors: list[Monitor] | None = None, active: tuple[int, str] | None = (101, "Yeni sekme - "
                                                                                                    "Google Chrome"),
                 rects: dict[int, Rect] | None = None, bgr: tuple[int, int, int] = (200, 60, 30)) -> None:
        self.mons = monitors if monitors is not None else [M1, M2]
        self.active = active
        self.rects = rects if rects is not None else {101: Rect(2000, 100, 1200, 800)}
        self.bgr = bgr
        self.grabs: list[Rect] = []

    def monitors(self) -> list[Monitor]:
        return list(self.mons)

    def grab(self, rect: Rect) -> np.ndarray:
        self.grabs.append(rect)
        a = np.zeros((rect.height, rect.width, 4), np.uint8)
        a[..., 0], a[..., 1], a[..., 2], a[..., 3] = self.bgr[0], self.bgr[1], self.bgr[2], 255
        return a

    def active_window(self) -> tuple[int, str] | None:
        return self.active

    def window_rect(self, hwnd: int) -> Rect | None:
        return self.rects.get(hwnd)

    def monitor_of_window(self, hwnd: int) -> int | None:
        return monitor_at(self.mons, self.rects.get(hwnd))


class FakeLayout:
    """Pencere yöneticisi: durum, konum, en küçük boyut sınırı ve "değişmeyen" pencereler."""

    def __init__(self, windows: list[tuple[int, str, int, Rect]], monitors: list[Monitor] | None = None,
                 active: int | None = None) -> None:
        self.mons = monitors if monitors is not None else [M1, M2]
        self.order = [w[0] for w in windows]
        self.info = {h: WindowInfo(h, t, pid) for h, t, pid, _ in windows}
        self.rect = {h: r for h, _, _, r in windows}
        self.st = {h: "normal" for h in self.order}
        self.min_size: dict[int, tuple[int, int]] = {}
        self.stuck: set[int] = set()          # bu pencereler durum değiştirmez
        self.active_hwnd = active if active is not None else (self.order[0] if self.order else None)
        self.calls: list[tuple] = []

    def windows(self) -> list[WindowInfo]:
        return [self.info[h] for h in self.order]

    def active(self) -> WindowInfo | None:
        return self.info.get(self.active_hwnd) if self.active_hwnd is not None else None

    def monitors(self) -> list[Monitor]:
        return list(self.mons)

    def monitor_of(self, hwnd: int) -> int | None:
        return monitor_at(self.mons, self.rect.get(hwnd))

    def frame(self, hwnd: int) -> Rect | None:
        if self.st.get(hwnd) == "maximized":
            m = next(m for m in self.mons if m.index == self.monitor_of(hwnd))
            return m.work
        return self.rect.get(hwnd)

    def state(self, hwnd: int) -> str:
        return self.st.get(hwnd, "closed")

    def show(self, hwnd: int, how: str) -> None:
        self.calls.append(("show", hwnd, how))
        if hwnd in self.stuck:
            return
        self.st[hwnd] = {"maximize": "maximized", "minimize": "minimized", "restore": "normal"}[how]

    def place(self, hwnd: int, rect: Rect) -> None:
        self.calls.append(("place", hwnd, rect))
        mw, mh = self.min_size.get(hwnd, (0, 0))
        self.st[hwnd] = "normal"
        self.rect[hwnd] = Rect(rect.left, rect.top, max(mw, rect.width), max(mh, rect.height))


class FakeKeyboard:
    def __init__(self, fg: ForegroundInfo | None = None, clipboard: FakeClipboard | None = None,
                 selection: str | None = "Seçili cümle burada.") -> None:
        self.fg = fg or ForegroundInfo(500, 4242, "Belge - Not Defteri", "Notepad")
        self.clipboard = clipboard
        self.selection = selection
        self.typed: list[str] = []
        self.copies: list[bool] = []
        self.fail_typing: Exception | None = None

    def foreground(self) -> ForegroundInfo | None:
        return self.fg

    def type_text(self, text: str) -> None:
        if self.fail_typing is not None:
            raise self.fail_typing
        self.typed.append(text)

    def copy_selection(self, use_insert: bool) -> None:
        self.copies.append(use_insert)
        if self.selection is not None and self.clipboard is not None:
            self.clipboard.set(self.selection)


class FakeClipboard:
    def __init__(self, text: str | None = "eski pano", sensitive: bool = False) -> None:
        self.text = text
        self.sensitive = sensitive
        self.seq = 1
        self.restored: list[ClipboardSnapshot] = []
        self.lost: list[int] = []

    def set(self, text: str) -> None:
        self.text = text
        self.seq += 1

    def sequence(self) -> int:
        return self.seq

    def get_text(self) -> str | None:
        return self.text

    def is_sensitive(self) -> bool:
        return self.sensitive

    def snapshot(self) -> ClipboardSnapshot:
        data = (self.text or "").encode("utf-16-le") + b"\x00\x00"
        return ClipboardSnapshot([(13, data)], list(self.lost))

    def restore(self, snap: ClipboardSnapshot) -> None:
        self.restored.append(snap)
        fmt, data = snap.formats[0]
        self.text = data.decode("utf-16-le").rstrip("\x00")
        self.seq += 1


class FakeBridge:
    def __init__(self, can_speak: bool = True) -> None:
        self.can_speak = can_speak
        self.unavailable_reason = "Hoparlör bulunamadı."
        self.spoken: list[tuple[str, str]] = []
        self.dictation_active = False
        self.sessions: list = []

    def speak(self, text: str, language: str = "tr", max_chars: int = 1500) -> bool:
        self.spoken.append((text, language))
        return True

    def start_dictation(self, session) -> None:  # noqa: ANN001
        self.dictation_active = True
        self.sessions.append(session)

    def stop_dictation(self) -> bool:
        was = self.dictation_active
        self.dictation_active = False
        return was


class StubVoiceManager:
    """VoiceBridge'in kullandığı VoiceManager yüzeyi (indirme/ses yükleme testleri için)."""

    def __init__(self) -> None:
        self.components = SimpleNamespace(player=object(), tts=object())
        self.bus = EventBus()
        self.progress: list = []
        self._cancel = threading.Event()
        self.spoken: list[tuple[str, object]] = []
        self.dictation = None
        self._loop = None
        self.status = SimpleNamespace(detail="")

    def _progress_threadsafe(self, p) -> None:  # noqa: ANN001
        self.progress.append(p)

    def speak_external(self, text: str, tts: object = None, max_chars: int = 1500) -> bool:
        self.spoken.append((text, tts))
        return True


def ekran_ctx(tmp_path: Path, **extra) -> ToolContext:  # noqa: ANN003
    """Resimler kökü de olan test bağlamı; arka uçlar `extra` ile verilir."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    ctx = make_ctx(tmp_path)
    pics = tmp_path / "Pictures"
    pics.mkdir(exist_ok=True)
    roots = {r.key: r.path for r in ctx.sandbox.roots}
    roots["pictures"] = pics
    ctx.sandbox = PathSandbox(roots, allow_appdata_roots=True)
    extra.setdefault("layout_pids", lambda entry, name: set())   # psutil'e bakma: başlıkla eşleşsin
    ctx.extra.update(extra)
    return ctx
