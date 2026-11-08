"""Ekran görüntüsü uzayı ile fiziksel ekran pikselleri arasında dönüşüm.

Claude koordinatları, kendisine gönderdiğimiz (küçültülmüş) ekran görüntüsünün piksel uzayında
verir; zoom'dan sonra da aynı uzay geçerlidir. Gerçek tıklama ise fiziksel piksellerle, sanal
masaüstü koordinatlarında yapılır (çoklu ekranda ikinci ekranın solu negatif olabilir). Ölçek
oturum başında bir kez hesaplanır ve oturum boyunca değişmez.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# API sınırı: uzun kenar en çok 2576 px, toplam ~3,75 MP. Pay bırakılır.
API_MAX_LONG_EDGE = 2576
API_MAX_PIXELS = 3_700_000
# Doğruluk/maliyet dengesi için önerilen boyut (~1366x768)
DEFAULT_LONG_EDGE = 1366


class CoordinateError(ValueError):
    """Koordinat ekran görüntüsünün dışında ya da biçimsiz."""


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

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom


def fit_size(width: int, height: int, long_edge: int = DEFAULT_LONG_EDGE,
             max_pixels: int = API_MAX_PIXELS) -> tuple[int, int]:
    """(width, height) en-boy oranı korunarak uzun kenarı `long_edge`'i ve toplam pikseli aşmayacak
    şekilde küçültülür. Asla büyütülmez (küçük ekran olduğu gibi kalır)."""
    if width <= 0 or height <= 0:
        raise ValueError("Geçersiz ekran boyutu")
    long_edge = min(long_edge, API_MAX_LONG_EDGE)
    s = min(1.0, long_edge / max(width, height), math.sqrt(max_pixels / (width * height)))
    return max(1, int(width * s)), max(1, int(height * s))


def _point(value: object, name: str = "coordinate") -> tuple[int, int]:
    if (not isinstance(value, (list, tuple)) or len(value) != 2
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)):
        raise CoordinateError(f"{name} [x, y] biçiminde iki sayı olmalı.")
    return int(round(value[0])), int(round(value[1]))


@dataclass(frozen=True)
class ScreenScaler:
    """Bir ekranın (monitor, fiziksel piksel) ekran görüntüsü uzayına (width x height) eşlenmesi."""

    monitor: Rect
    width: int
    height: int

    @classmethod
    def fit(cls, monitor: Rect, long_edge: int = DEFAULT_LONG_EDGE) -> ScreenScaler:
        w, h = fit_size(monitor.width, monitor.height, long_edge)
        return cls(monitor, w, h)

    @property
    def scale_x(self) -> float:
        return self.monitor.width / self.width

    @property
    def scale_y(self) -> float:
        return self.monitor.height / self.height

    def to_physical(self, point: object, name: str = "coordinate") -> tuple[int, int]:
        """Ekran görüntüsündeki (x, y) -> sanal masaüstündeki fiziksel piksel (pikselin ortası)."""
        x, y = _point(point, name)
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise CoordinateError(f"{name} ({x}, {y}) ekran görüntüsünün dışında "
                                  f"(0-{self.width - 1}, 0-{self.height - 1}).")
        px = min(self.monitor.width - 1, int((x + 0.5) * self.scale_x))
        py = min(self.monitor.height - 1, int((y + 0.5) * self.scale_y))
        return self.monitor.left + px, self.monitor.top + py

    def to_screenshot(self, px: int, py: int) -> tuple[int, int]:
        """Fiziksel piksel -> ekran görüntüsü koordinatı (imleç konumunu bildirmek için).
        Ekran dışındaki nokta en yakın kenara sıkıştırılır."""
        x = int((px - self.monitor.left) / self.scale_x)
        y = int((py - self.monitor.top) / self.scale_y)
        return min(max(x, 0), self.width - 1), min(max(y, 0), self.height - 1)

    def region_to_physical(self, region: object) -> Rect:
        """Zoom bölgesi [x0, y0, x1, y1] (ekran görüntüsü uzayı) -> ekranın içindeki fiziksel dikdörtgen
        (ekranın sol üst köşesine göre, kırpma için)."""
        if (not isinstance(region, (list, tuple)) or len(region) != 4
                or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in region)):
            raise CoordinateError("region [x0, y0, x1, y1] biçiminde dört sayı olmalı.")
        x0, y0, x1, y1 = (int(round(v)) for v in region)
        x0, x1 = sorted((x0, x1))
        y0, y1 = sorted((y0, y1))
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(self.width, x1), min(self.height, y1)
        if x1 - x0 < 2 or y1 - y0 < 2:
            raise CoordinateError("Zoom bölgesi çok küçük ya da ekran görüntüsünün dışında.")
        left = int(x0 * self.scale_x)
        top = int(y0 * self.scale_y)
        right = min(self.monitor.width, max(left + 1, int(math.ceil(x1 * self.scale_x))))
        bottom = min(self.monitor.height, max(top + 1, int(math.ceil(y1 * self.scale_y))))
        return Rect(left, top, right - left, bottom - top)
