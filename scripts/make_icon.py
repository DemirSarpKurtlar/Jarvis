"""JARVIS uygulama simgesini üretir.

Sol raydaki logonun aynısı: koyu lacivert zemin (#060C18) üzerinde parlayan
camgöbeği (#52E4F2) halka ve ortada nokta. QPainter ile her boyut ayrı
çizilir (küçük boyutlarda parıltı kapatılır, çizgiler kalınlaştırılır ki
16 px'te de okunabilsin).

Çıktılar:
    jarvis/assets/icon/jarvis.ico   16-256 px, çok boyutlu (Windows exe, kurulum, pencere)
    jarvis/assets/icon/jarvis.png   256 px

Kullanım:
    python scripts/make_icon.py            # varsayılan klasöre yazar
    python scripts/make_icon.py --out DIR  # başka klasöre yazar

ICO dosyası elle yazılır (Pillow gerekmez): 256 px PNG olarak, daha küçükler
klasik 32 bit BMP (DIB) olarak saklanır. Windows Explorer, PyInstaller ve
Inno Setup bu biçimi sorunsuz okur.
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "jarvis" / "assets" / "icon"
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)

GECE = "#060C18"
DERINLIK = "#0B1526"
CELIK = "#1A2943"
CYAN = "#52E4F2"


def render(size: int):  # noqa: ANN201  (QImage)
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QBrush, QColor, QImage, QPainter, QPen, QRadialGradient

    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    s = float(size)
    c = QPointF(s / 2, s / 2)

    # Zemin: yuvarlatılmış kare, ortası hafif açık lacivert
    bg = QRadialGradient(c, s * 0.62)
    bg.setColorAt(0.0, QColor(DERINLIK))
    bg.setColorAt(1.0, QColor(GECE))
    p.setBrush(QBrush(bg))
    if size >= 32:
        p.setPen(QPen(QColor(CELIK), max(1.0, s / 64)))
    else:
        p.setPen(Qt.PenStyle.NoPen)
    inset = 0.5 if size >= 32 else 0.0
    radius = s * 0.22
    p.drawRoundedRect(QRectF(inset, inset, s - 2 * inset, s - 2 * inset), radius, radius)

    ring_r = s * 0.30
    ring_w = max(1.8 if size < 32 else 1.6, s * 0.052)
    dot_r = max(1.7, ring_r * 0.26)
    cyan = QColor(CYAN)

    # Parıltı: halkanın iki yanına yayılan saydam çizgiler (yalnızca büyük boyutlarda)
    if size >= 64:
        p.setBrush(Qt.BrushStyle.NoBrush)
        steps = 16
        for i in range(steps, 0, -1):
            g = QColor(cyan)
            g.setAlphaF(0.012 + 0.009 * (steps - i))
            p.setPen(QPen(g, ring_w + s * 0.0055 * i * 2))
            p.drawEllipse(c, ring_r, ring_r)
        glow = QRadialGradient(c, dot_r * 3.2)
        g0 = QColor(cyan)
        g0.setAlphaF(0.55)
        g1 = QColor(cyan)
        g1.setAlphaF(0.0)
        glow.setColorAt(0.0, g0)
        glow.setColorAt(1.0, g1)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(glow))
        p.drawEllipse(c, dot_r * 3.2, dot_r * 3.2)

    # Halka
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(cyan, ring_w))
    p.drawEllipse(c, ring_r, ring_r)

    # Merkez nokta
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(cyan)
    p.drawEllipse(c, dot_r, dot_r)
    p.end()
    return img


def _png_bytes(img) -> bytes:  # noqa: ANN001
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice

    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    if not img.save(buf, "PNG"):
        raise RuntimeError("PNG üretilemedi")
    buf.close()
    return bytes(data.data())


def _dib_bytes(img) -> bytes:  # noqa: ANN001
    """32 bit BGRA, alttan üste satırlar + boş AND maskesi (ICO içindeki BMP biçimi)."""
    from PySide6.QtGui import QImage

    img = img.convertToFormat(QImage.Format.Format_ARGB32)
    w, h = img.width(), img.height()
    header = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, w * h * 4, 0, 0, 0, 0)
    stride = img.bytesPerLine()
    raw = bytes(img.constBits())[: stride * h]
    rows = [raw[y * stride: y * stride + w * 4] for y in range(h)]  # ARGB32 = bellekte B,G,R,A
    pixels = b"".join(reversed(rows))
    mask_stride = ((w + 31) // 32) * 4
    mask = b"\x00" * (mask_stride * h)
    return header + pixels + mask


def build_ico(images: dict[int, object]) -> bytes:
    entries = []
    for size in sorted(images):
        img = images[size]
        data = _png_bytes(img) if size >= 256 else _dib_bytes(img)
        entries.append((size, data))
    out = struct.pack("<HHH", 0, 1, len(entries))
    offset = 6 + 16 * len(entries)
    blobs = b""
    for size, data in entries:
        dim = 0 if size >= 256 else size  # 0 = 256
        out += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
        blobs += data
    return out + blobs


def parse_ico_sizes(data: bytes) -> list[int]:
    """ICO başlığındaki boyutları döndürür (testler ve kontrol için)."""
    reserved, kind, count = struct.unpack_from("<HHH", data, 0)
    if reserved != 0 or kind != 1:
        raise ValueError("ICO dosyası değil")
    sizes = []
    for i in range(count):
        w, _h, _c, _r, _planes, _bpp, length, offset = struct.unpack_from("<BBBBHHII", data, 6 + 16 * i)
        if offset + length > len(data):
            raise ValueError("ICO kaydı dosya dışına taşıyor")
        sizes.append(w or 256)
    return sizes


def make(out_dir: Path) -> tuple[Path, Path]:
    from PySide6.QtGui import QGuiApplication

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    _app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])  # noqa: F841
    out_dir.mkdir(parents=True, exist_ok=True)
    images = {size: render(size) for size in ICO_SIZES}
    ico = out_dir / "jarvis.ico"
    png = out_dir / "jarvis.png"
    ico.write_bytes(build_ico(images))
    png.write_bytes(_png_bytes(images[256]))
    return ico, png


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="çıktı klasörü")
    args = ap.parse_args(argv)
    ico, png = make(args.out)
    print(f"{ico} ({', '.join(map(str, parse_ico_sizes(ico.read_bytes())))} px)")
    print(png)
    return 0


if __name__ == "__main__":
    sys.exit(main())
