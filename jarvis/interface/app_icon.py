"""Uygulama simgesi (pencere, görev çubuğu, bildirim alanı).

Dosyalar `scripts/make_icon.py` ile üretilir. Çok boyutlu ICO, Qt'nin ICO
eklentisi varsa tercih edilir (16 px'e özel çizilmiş boyutlar da kullanılsın);
yoksa 256 px PNG ölçeklenir.
"""

from __future__ import annotations

from PySide6.QtGui import QIcon, QImageReader

from jarvis.paths import ASSETS_DIR

ICON_DIR = ASSETS_DIR / "icon"
ICO_FILE = ICON_DIR / "jarvis.ico"
PNG_FILE = ICON_DIR / "jarvis.png"

_icon: QIcon | None = None


def app_icon() -> QIcon:
    global _icon
    if _icon is None or _icon.isNull():
        icon = QIcon()
        if ICO_FILE.exists() and b"ico" in {bytes(f.data()) for f in QImageReader.supportedImageFormats()}:
            icon = QIcon(str(ICO_FILE))
        if icon.isNull() and PNG_FILE.exists():
            icon = QIcon(str(PNG_FILE))
        _icon = icon
    return _icon
