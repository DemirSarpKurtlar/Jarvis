"""Dinleme göstergesinin (taslak B) her durumunu 2x DPR ile çizip PNG olarak kaydeder.

Taslakla görsel karşılaştırma içindir; testlerde çalışmaz.
Kullanım: python scripts/overlay_screenshots.py [ÇIKIŞ_KLASÖRÜ] [--karsilastir KLASÖR]

`--karsilastir` verilirse her durum için taslak ekran görüntüsünün üst şeridi ile uygulamanın
çizimi alt alta tek görüntüde birleştirilir (yalnızca gözle kontrol için).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["QT_SCALE_FACTOR"] = "2"   # 2x DPR: taslak ekran görüntüleriyle aynı yoğunluk

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QPointF, QRect, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QRadialGradient  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from jarvis.app import configure_application  # noqa: E402
from jarvis.core.state import AssistantState  # noqa: E402
from jarvis.interface.overlay import ListeningOverlay, OverlayView  # noqa: E402
from jarvis.interface.theme import STATE_LOOKS, C  # noqa: E402

W, H = 1440, 120       # taslaktaki ekran genişliği; yalnızca üst şerit
MOCKUP_DIR = ROOT / "design" / "taslaklar" / "dinleme-gostergesi"

L = STATE_LOOKS
REPLY = "Not Defteri açık. En üste “Yarınki toplantı” başlığını yazdım, devamını söyleyebilirsin."
# (dosya, görünüm, saniye: durumun başından beri geçen süre, ses seviyesi, fare üstünde, arka plan, taslak)
STATES = [
    ("1-uyandi", OverlayView("wake", L[AssistantState.WAKE_DETECTED].color, "Seni duydum"), 0.3, 0, False,
     "oyun", "1-uyandi-b.png"),
    ("2-dinliyor", OverlayView("listening", C.CYAN, "Dinliyorum"), 1.5, 0.75, False, "oyun", "2-dinliyor-b.png"),
    ("2b-soz", OverlayView("thinking", C.MENEKSE, "Not Defteri'ni aç ve yarınki toplantı için bir not başlat",
                           style="transcript"), 0.6, 0, False, "oyun", "2-dinliyor-b.png"),
    ("3-dusunuyor", OverlayView("thinking", C.MENEKSE, "Düşünüyorum", "Haiku 5.5"), 1.2, 0, False, "oyun",
     "3-dusunuyor-b.png"),
    ("4-yurutuyor", OverlayView("executing", C.AMBER, "Not Defteri açılıyor"), 1.2, 0, False, "oyun",
     "4-yurutuyor-b.png"),
    ("5-konusuyor", OverlayView("speaking", C.CYAN, REPLY, style="speech"), 1.0, 0.6, False, "oyun",
     "5-konusuyor-b.png"),
    ("6-hata", OverlayView("error", C.KIZIL, "Mikrofona ulaşılamıyor", "Ayarlar'dan mikrofon seç"), 1.0, 0, False,
     "oyun", "6-hata-b.png"),
    ("7-beklemeye-donus", OverlayView("standby", C.CYAN), 1.0, 0, False, "oyun", "7-gizlenme-b.png"),
    ("8-mikrofon-kapali", OverlayView("mic_off", C.SIS, "Mikrofon kapalı", "JARVIS penceresinden açabilirsin"), 1.0, 0,
     False, "oyun", None),
    ("9-acik-masaustu-fare", OverlayView("speaking", C.CYAN, REPLY, style="speech"), 1.0, 0.6, True, "masaustu",
     "9-acik-masaustu-b-fare.png"),
]


def background(kind: str) -> QImage:
    img = QImage(W * 2, H * 2, QImage.Format.Format_ARGB32_Premultiplied)
    img.setDevicePixelRatio(2)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    if kind == "masaustu":
        p.fillRect(QRectF(0, 0, W, H), QColor("#ffffff"))
        p.fillRect(QRectF(0, 0, W, 34), QColor("#f6f7f9"))
        p.fillRect(QRectF(0, 64, W, 1), QColor("#e6e8eb"))
    else:  # taslaktaki oyun gökyüzüne yakın koyu, bulanık zemin
        g = QLinearGradient(0, 0, 0, 470)
        g.setColorAt(0, QColor("#0a0f2e"))
        g.setColorAt(1, QColor("#1c1a52"))
        p.fillRect(QRectF(0, 0, W, H), g)
        for cx, col in ((0.16, QColor(255, 232, 196, 150)), (0.84, QColor(206, 226, 255, 140))):
            rg = QRadialGradient(QPointF(W * cx, 0), W * 0.36)
            rg.setColorAt(0, col)
            rg.setColorAt(0.62, QColor(col.red(), col.green(), col.blue(), 0))
            p.fillRect(QRectF(0, 0, W, H), rg)
    p.end()
    return img


def render(app: QApplication, fonts, out: Path, compare: Path | None) -> list[Path]:  # noqa: ANN001
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for name, view, secs, level, hover, bg, mock in STATES:
        t = [1000.0]
        ov = ListeningOverlay(fonts)
        ov.clock = lambda: t[0]
        ov.set_view(view)
        ov.set_hovered(hover)
        ov.present(QRect(0, 0, W, 810))
        ov._view_t = ov._shown_t = ov._tab_t = ov._color_t = ov._shake_t = t[0] - 5  # iniş/geçiş bitti
        ov._view_t = t[0] - secs
        ov.set_level(level)
        ov._level = level
        for _ in range(40):                     # halka çubukları yumuşatmayla otursun
            t[0] += 1 / 30
            ov._view_t += 1 / 30
            ov.grab()
        ov._view_t = t[0] - secs
        shot = ov.grab().toImage()
        ov.dismiss()
        ov.deleteLater()

        img = background(bg)
        p = QPainter(img)
        p.drawImage(QRectF(0, 0, W, shot.height() / shot.devicePixelRatio()), shot)
        p.end()
        path = out / f"{name}.png"
        img.save(str(path))
        written.append(path)
        if compare is not None and mock:
            compare.mkdir(parents=True, exist_ok=True)
            m = QImage(str(MOCKUP_DIR / mock))
            if m.isNull():
                continue
            strip = m.copy(0, 0, m.width(), H * 2)
            both = QImage(W * 2, H * 4 + 8, QImage.Format.Format_ARGB32_Premultiplied)
            both.fill(QColor("#ff00ff"))
            p = QPainter(both)
            p.drawImage(0, 0, strip)
            flat = img.copy()
            flat.setDevicePixelRatio(1)
            p.drawImage(0, H * 2 + 8, flat)
            p.end()
            both.save(str(compare / f"{name}-karsilastirma.png"))
    app.processEvents()
    return written


def main(argv: list[str]) -> int:
    out = Path(argv[0]) if argv and not argv[0].startswith("--") else MOCKUP_DIR / "uygulama"
    compare = Path(argv[argv.index("--karsilastir") + 1]) if "--karsilastir" in argv else None
    app = QApplication.instance() or QApplication(sys.argv[:1])
    fonts = configure_application(app)
    for path in render(app, fonts, out, compare):
        print(path.relative_to(ROOT) if path.is_relative_to(ROOT) else path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
