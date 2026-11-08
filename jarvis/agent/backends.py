"""Masaüstü arka uç arayüzleri. Gerçek uygulama `win32.py`'de; testler sahtelerini verir.

Yakalama arayüzü (`ScreenCapture`) bilerek küçük tutuldu: ileride `jarvis/system/screen.py`'deki
ortak ekran görüntüsü modülüne devredilebilir (bkz. `win32.Win32Screen`).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Protocol

import numpy as np

from jarvis.agent.geometry import Rect


@dataclass(frozen=True)
class ForegroundInfo:
    title: str = ""
    class_name: str = ""
    process: str = ""        # küçük harf exe adı ("valorant-win64-shipping.exe")
    pid: int = 0


class ScreenCapture(Protocol):
    def monitors(self) -> list[Rect]:
        """Fiziksel pikselde, sanal masaüstü koordinatlarında ekranlar; ilki birincil ekran."""

    def monitor_of_foreground(self) -> Rect | None:
        """Ön plandaki pencerenin bulunduğu ekran (bilinmiyorsa None)."""

    def capture(self, monitor: Rect) -> np.ndarray:
        """Ekranın tam çözünürlüklü RGB karesi (yükseklik, genişlik, 3) uint8."""


class InputDevice(Protocol):
    def move(self, x: int, y: int) -> None:
        """İmleci fiziksel (sanal masaüstü) piksele taşır."""

    def cursor(self) -> tuple[int, int]: ...

    def button(self, button: str, down: bool) -> None:
        """button: left / right / middle."""

    def wheel(self, vertical_clicks: int, horizontal_clicks: int) -> None:
        """Pozitif dikey: yukarı; pozitif yatay: sağa."""

    def key(self, vk: int, down: bool, extended: bool = False) -> None: ...

    def unicode_char(self, ch: str) -> None:
        """Karakteri klavye düzeninden bağımsız (KEYEVENTF_UNICODE) bas-bırak."""

    def char_vk(self, ch: str) -> int | None:
        """Tek karakterin bu klavye düzenindeki VK kodu (yoksa None)."""


class WindowProbe(Protocol):
    def foreground(self) -> ForegroundInfo: ...

    def process_running(self, exe_name: str) -> bool: ...


class KillSwitch(Protocol):
    def start(self, on_trigger: Callable[[], None]) -> None:
        """Arka planda dinlemeye başlar; tetiklenince `on_trigger` (başka thread'den) çağrılır."""

    def stop(self) -> None: ...

    def note_injected_escape(self) -> None:
        """JARVIS'in kendi gönderdiği Esc tuşu acil durdurmayı tetiklemesin."""


class Clock:
    """Gerçek saat; testler sahtesini verir (beklemeler anında biter)."""

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


@dataclass
class Desktop:
    screen: ScreenCapture
    input: InputDevice
    probe: WindowProbe
    kill_switch: KillSwitch | None = None
    clock: Clock | None = None

    def __post_init__(self) -> None:
        if self.clock is None:
            self.clock = Clock()
