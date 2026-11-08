"""Bilgisayar kullanan ajan testleri için sahte masaüstü (ekran, girdi, pencere, saat, acil durdurma)."""

from __future__ import annotations

import numpy as np

from jarvis.agent.backends import Desktop, ForegroundInfo
from jarvis.agent.geometry import Rect


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.slept = 0.0
        self.on_sleep = None    # her beklemede çağrılır (ör. ortada acil durdurmayı tetiklemek için)

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += max(0.0, seconds)
        self.slept += max(0.0, seconds)
        if self.on_sleep:
            self.on_sleep(self)


class FakeScreen:
    def __init__(self, monitors: list[Rect] | None = None, static: bool = False) -> None:
        self.mons = monitors or [Rect(0, 0, 2560, 1440)]
        self.static = static
        self.captures = 0
        self.frame_fn = None

    def monitors(self) -> list[Rect]:
        return list(self.mons)

    def monitor_of_foreground(self) -> Rect | None:
        return self.mons[-1]

    def capture(self, monitor: Rect) -> np.ndarray:
        self.captures += 1
        if self.frame_fn is not None:
            return self.frame_fn(monitor, self.captures)
        h, w = monitor.height, monitor.width
        frame = np.zeros((h, w, 3), np.uint8)
        frame[:, :, 0] = (np.arange(w) * 255 // max(w - 1, 1)).astype(np.uint8)[None, :]
        frame[:, :, 1] = (np.arange(h) * 255 // max(h - 1, 1)).astype(np.uint8)[:, None]
        if not self.static:
            frame[:, :, 2] = (self.captures * 53) % 256   # her kare farklı
        else:
            frame[:, :, 2] = 90
        return frame


class FakeInput:
    def __init__(self) -> None:
        self.events: list[tuple] = []
        self.pos = (0, 0)

    def move(self, x: int, y: int) -> None:
        self.pos = (x, y)
        self.events.append(("move", x, y))

    def cursor(self) -> tuple[int, int]:
        return self.pos

    def button(self, button: str, down: bool) -> None:
        self.events.append(("button", button, down))

    def wheel(self, vertical_clicks: int, horizontal_clicks: int) -> None:
        self.events.append(("wheel", vertical_clicks, horizontal_clicks))

    def key(self, vk: int, down: bool, extended: bool = False) -> None:
        self.events.append(("key", vk, down))

    def unicode_char(self, ch: str) -> None:
        self.events.append(("char", ch))

    def char_vk(self, ch: str) -> int | None:
        return ord(ch.upper()) if ch.isascii() and ch.isalnum() else None

    def typed(self) -> str:
        return "".join(e[1] for e in self.events if e[0] == "char")

    def clicks(self) -> list[tuple]:
        return [e for e in self.events if e[0] == "button"]


class FakeProbe:
    def __init__(self, fg: ForegroundInfo | None = None) -> None:
        self.fg = fg or ForegroundInfo("Belgesiz - Not Defteri", "Notepad", "notepad.exe", 42)
        self.running: set[str] = set()

    def foreground(self) -> ForegroundInfo:
        return self.fg

    def process_running(self, exe_name: str) -> bool:
        return exe_name.lower() in self.running


class FakeKillSwitch:
    def __init__(self) -> None:
        self.callback = None
        self.started = 0
        self.stopped = 0
        self.injected = 0

    def start(self, on_trigger) -> None:  # noqa: ANN001
        self.callback = on_trigger
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1
        self.callback = None

    def note_injected_escape(self) -> None:
        self.injected += 1

    def trigger(self) -> None:
        if self.callback:
            self.callback()


def make_desktop(monitors: list[Rect] | None = None, static: bool = False,
                 fg: ForegroundInfo | None = None) -> Desktop:
    return Desktop(FakeScreen(monitors, static), FakeInput(), FakeProbe(fg), FakeKillSwitch(), FakeClock())
