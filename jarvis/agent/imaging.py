"""Ekran karesi işlemleri: küçültme, kırpma, kodlama ve iki kare arasındaki değişim.

Kare, (yükseklik, genişlik, 3) boyutlu uint8 RGB numpy dizisidir. Küçültme ve JPEG kodlama Qt'nin
QImage'ıyla yapılır (uygulamada zaten var, QApplication gerektirmez, arka plan thread'inde çalışır);
Qt yüklenemezse numpy ile küçültülür ve PNG (zlib) olarak kodlanır.

Neden JPEG: oyun menüleri gibi fotoğraf benzeri ekranlarda PNG 1-2 MB tutar; eski ekran görüntüleri
istem geçmişinde kaldığı (istemci tarafında silinmez) için 40 adımlık bir oturum istek boyut sınırını
aşabilirdi. JPEG (kalite 80) aynı ekranı ~100-200 KB'ta tutar.
"""

from __future__ import annotations

import logging
import struct
import zlib

import numpy as np

from jarvis.agent.geometry import Rect

log = logging.getLogger(__name__)

JPEG_QUALITY = 80
THUMB_W, THUMB_H = 64, 36


def as_frame(data: np.ndarray) -> np.ndarray:
    a = np.asarray(data)
    if a.ndim != 3 or a.shape[2] != 3 or a.dtype != np.uint8:
        raise ValueError("Kare (yükseklik, genişlik, 3) uint8 RGB olmalı")
    return np.ascontiguousarray(a)


def crop(frame: np.ndarray, r: Rect) -> np.ndarray:
    return np.ascontiguousarray(frame[r.top:r.bottom, r.left:r.right])


def _qimage(frame: np.ndarray):  # noqa: ANN202
    from PySide6.QtGui import QImage

    h, w, _ = frame.shape
    return QImage(frame.data, w, h, w * 3, QImage.Format.Format_RGB888)


def _numpy_resize(frame: np.ndarray, w: int, h: int) -> np.ndarray:
    """Önce tam sayı katlı kutu ortalaması (yazıyı korur), sonra en yakın komşu ile tam boyut."""
    fh, fw, _ = frame.shape
    k = max(1, min(fh // max(h, 1), fw // max(w, 1)))
    a = frame
    if k > 1:
        hh, ww = (fh // k) * k, (fw // k) * k
        a = frame[:hh, :ww].reshape(hh // k, k, ww // k, k, 3).mean(axis=(1, 3)).astype(np.uint8)
    ys = (np.arange(h) * a.shape[0] / h).astype(int)
    xs = (np.arange(w) * a.shape[1] / w).astype(int)
    return np.ascontiguousarray(a[ys][:, xs])


def resize(frame: np.ndarray, w: int, h: int) -> np.ndarray:
    if frame.shape[1] == w and frame.shape[0] == h:
        return frame
    try:
        from PySide6.QtCore import Qt

        src = frame if frame.flags.c_contiguous else np.ascontiguousarray(frame)
        img = _qimage(src).scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio,
                                  Qt.TransformationMode.SmoothTransformation)
        img = img.convertToFormat(img.Format.Format_RGB888)
        bpl = img.bytesPerLine()
        buf = np.frombuffer(img.constBits(), dtype=np.uint8, count=bpl * h).reshape(h, bpl)
        return np.ascontiguousarray(buf[:, : w * 3].reshape(h, w, 3))
    except Exception:  # noqa: BLE001  Qt yoksa numpy
        log.debug("Qt ile küçültülemedi, numpy kullanılıyor", exc_info=True)
        return _numpy_resize(frame, w, h)


def encode_png(frame: np.ndarray) -> bytes:
    h, w, _ = frame.shape
    rows = np.concatenate([np.zeros((h, 1), np.uint8), frame.reshape(h, w * 3)], axis=1)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(rows.tobytes(), 6))
            + chunk(b"IEND", b""))


def encode(frame: np.ndarray, prefer_jpeg: bool = True) -> tuple[str, bytes]:
    """(medya türü, bayt). JPEG kodlanamazsa PNG."""
    if prefer_jpeg:
        try:
            from PySide6.QtCore import QBuffer, QByteArray, QIODevice

            src = frame if frame.flags.c_contiguous else np.ascontiguousarray(frame)
            ba = QByteArray()
            buf = QBuffer(ba)
            buf.open(QIODevice.OpenModeFlag.WriteOnly)
            ok = _qimage(src).save(buf, "JPG", JPEG_QUALITY)
            buf.close()
            if ok and ba.size() > 0:
                return "image/jpeg", bytes(ba.data())
        except Exception:  # noqa: BLE001
            log.debug("JPEG kodlanamadı, PNG kullanılıyor", exc_info=True)
    return "image/png", encode_png(frame)


def thumbnail(frame: np.ndarray) -> np.ndarray:
    """Değişim karşılaştırması için küçük gri tonlu kopya (0-1)."""
    small = _numpy_resize(frame, THUMB_W, THUMB_H)
    return small.astype(np.float32).mean(axis=2) / 255.0


CELL_THRESHOLD = 0.06   # bir hücrenin "değişti" sayılması için gri ton farkı


def change_ratio(a: np.ndarray | None, b: np.ndarray | None, around: tuple[float, float] | None = None,
                 radius: float = 0.15) -> float:
    """İki küçük kopya arasında değişen hücrelerin oranı (0: aynı, 1: hepsi). Biri yoksa 1.

    `around` (x, y oranı 0-1) verilirse yalnızca o noktanın çevresine bakılır (tıklanan düğme)."""
    if a is None or b is None or a.shape != b.shape:
        return 1.0
    diff = np.abs(a - b) > CELL_THRESHOLD
    if around is not None:
        h, w = diff.shape
        cx, cy = around[0] * w, around[1] * h
        rx, ry = max(1, int(radius * w)), max(1, int(radius * h))
        diff = diff[max(0, int(cy) - ry):int(cy) + ry + 1, max(0, int(cx) - rx):int(cx) + rx + 1]
        if diff.size == 0:
            return 1.0
    return float(diff.mean())
