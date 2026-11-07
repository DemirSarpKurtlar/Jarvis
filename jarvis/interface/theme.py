"""Görsel tema: renkler, fontlar, durum görünümleri ve Qt stil sayfası.

Değerler `design/taslaklar/jarvis-arayuz-v1.html` taslağıyla birebir aynıdır.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from PySide6.QtGui import QColor, QFontDatabase, QGuiApplication

from jarvis.core.state import AssistantState
from jarvis.paths import ASSETS_DIR

log = logging.getLogger(__name__)


class C:
    GECE = "#060C18"       # zemin
    DERINLIK = "#0B1526"   # panel zemini
    CELIK = "#1A2943"      # çizgiler
    SIS = "#6F86A6"        # ikincil metin
    BUZ = "#D6E9F3"        # ana metin
    CYAN = "#52E4F2"       # ana ışık
    MENEKSE = "#8F9BFF"    # düşünme
    AMBER = "#F3B648"      # yürütme / uyarı
    KIZIL = "#FF5A6A"      # hata / mikrofon


@dataclass(frozen=True)
class StateLook:
    color: str
    title: str
    subtitle: str


STATE_LOOKS: dict[AssistantState, StateLook] = {
    AssistantState.STANDBY: StateLook(C.CYAN, "Beklemede", "Yazarak başlayabilirsin. Sesli uyandırma Aşama 3'te geliyor"),
    AssistantState.WAKE_DETECTED: StateLook("#9FF3FA", "Seni duydum", "Dinlemeye geçiyorum"),
    AssistantState.LISTENING: StateLook(C.CYAN, "Dinliyorum", "Konuşman bitince kendim anlarım"),
    AssistantState.PROCESSING: StateLook(C.MENEKSE, "Düşünüyorum", "Cevabı hazırlıyorum"),
    AssistantState.EXECUTING: StateLook(C.AMBER, "Yürütüyorum", "İşlem yapılıyor"),
    AssistantState.SPEAKING: StateLook(C.CYAN, "Konuşuyorum", "Sözümü kesmek için konuşmaya başla"),
    AssistantState.ERROR: StateLook(C.KIZIL, "Bir sorun var", "Ayrıntı için üstteki uyarıya bak"),
}


@dataclass(frozen=True)
class Fonts:
    display: str
    body: str


_fonts: Fonts | None = None


def load_fonts() -> Fonts:
    """Paketle gelen OFL fontlarını yükler; olmazsa sistem fontuna düşer."""
    global _fonts
    if _fonts is not None:
        return _fonts
    if QGuiApplication.instance() is None:
        # Qt, uygulama nesnesi yokken font veritabanına erişilirse çöker
        raise RuntimeError("load_fonts() QApplication oluşturulduktan sonra çağrılmalı")

    # Değişken fontlar Qt'de güvenilir ağırlık seçimi vermediği için sabit
    # ağırlıklı dosyalar kullanılır (bkz. jarvis/assets/fonts/README.md).
    def add(family: str, files: list[str], fallback: str) -> str:
        loaded: set[str] = set()
        for filename in files:
            path = ASSETS_DIR / "fonts" / filename
            fid = QFontDatabase.addApplicationFont(str(path))
            if fid < 0:
                log.warning("Font yüklenemedi: %s", path.name)
                continue
            loaded.update(QFontDatabase.applicationFontFamilies(fid))
        if family in loaded or family in QFontDatabase.families():
            return family
        log.warning("%s fontu yok, yerine %s kullanılacak", family, fallback)
        return fallback

    _fonts = Fonts(
        display=add("Saira", ["Saira-Light.ttf", "Saira-Regular.ttf", "Saira-Medium.ttf"], "Segoe UI"),
        body=add("IBM Plex Sans",
                 ["IBMPlexSans-Regular.ttf", "IBMPlexSans-Medium.ttf", "IBMPlexSans-SemiBold.ttf"], "Segoe UI"),
    )
    log.debug("Fontlar: %s", _fonts)
    return _fonts


def qcolor(hex_: str, alpha: float = 1.0) -> QColor:
    c = QColor(hex_)
    c.setAlphaF(max(0.0, min(1.0, alpha)))
    return c


def stylesheet(f: Fonts) -> str:
    return f"""
    QWidget {{ background: transparent; color: {C.BUZ}; font-family: "{f.body}"; font-size: 14px; }}
    QMainWindow, #Kok {{ background: {C.GECE}; }}
    QToolTip {{ background: {C.DERINLIK}; color: {C.BUZ}; border: 1px solid {C.CELIK}; padding: 6px 8px; }}

    /* Sol ray */
    #Ray {{ border-right: 1px solid {C.CELIK}; }}
    #Ray QToolButton {{
        border: 0; border-radius: 10px; padding: 8px 0 6px 0; color: {C.SIS};
        font-size: 11px; min-width: 62px; max-width: 62px;
    }}
    #Ray QToolButton:hover {{ color: {C.BUZ}; }}
    #Ray QToolButton:checked {{ color: {C.CYAN}; background: rgba(82,228,242,0.07); }}
    #Ray QToolButton#KapatDugmesi:hover {{ color: {C.KIZIL}; }}

    /* Üst bar */
    #Kelime {{ font-family: "{f.display}"; font-size: 20px; font-weight: 300; letter-spacing: 8px; }}
    #Isaret {{ border: 1px solid {C.CELIK}; border-radius: 13px; padding: 4px 12px; color: {C.SIS}; font-size: 13px; }}

    /* Uyarı bandı */
    #UyariBandi {{ background: rgba(255,90,106,0.08); border: 1px solid rgba(255,90,106,0.45); border-radius: 12px; }}
    #UyariBandi QLabel {{ color: {C.BUZ}; }}
    #UyariBandi[seviye="uyari"] {{ background: rgba(243,182,72,0.07); border-color: rgba(243,182,72,0.45); }}

    /* Altyazı */
    #Kim {{ color: {C.SIS}; font-size: 12px; }}
    #SozJarvis {{ color: {C.BUZ}; font-size: 15px; }}
    #SozSen {{ color: {C.SIS}; font-size: 15px; }}
    #Meta {{ color: {C.SIS}; font-size: 12px; }}
    QScrollBar:vertical {{ width: 6px; background: transparent; }}
    QScrollBar::handle:vertical {{ background: {C.CELIK}; border-radius: 3px; min-height: 30px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
    QComboBox, QDoubleSpinBox {{ border: 1px solid {C.CELIK}; border-radius: 8px; padding: 6px 10px; background: {C.DERINLIK}; }}
    QComboBox QAbstractItemView {{ background: {C.DERINLIK}; border: 1px solid {C.CELIK}; selection-background-color: rgba(82,228,242,0.15); }}
    QCheckBox {{ spacing: 8px; }}
    QMessageBox, QDialog {{ background: {C.DERINLIK}; }}

    /* Kontrol yuvası */
    #Yuva {{ background: rgba(11,21,38,0.85); border: 1px solid {C.CELIK}; border-radius: 18px; }}
    #MikDugmesi {{
        min-width: 50px; max-width: 50px; min-height: 50px; max-height: 50px; border-radius: 26px;
        border: 1.5px solid {C.KIZIL}; background: rgba(255,90,106,0.10);
    }}
    #MikDugmesi:checked {{ border-color: {C.CELIK}; background: transparent; }}
    #Giris {{ border: 0; padding: 10px 4px; font-size: 15px; color: {C.BUZ}; }}
    #Giris:disabled {{ color: {C.SIS}; }}
    #KonusDugmesi {{
        border: 1px solid rgba(82,228,242,0.5); background: rgba(82,228,242,0.08); color: {C.CYAN};
        border-radius: 12px; padding: 9px 16px; font-weight: 500; font-size: 13px;
    }}
    #KonusDugmesi:disabled {{ border-color: {C.CELIK}; background: transparent; color: {C.SIS}; }}
    QSlider::groove:horizontal {{ height: 4px; background: {C.CELIK}; border-radius: 2px; }}
    QSlider::sub-page:horizontal {{ background: {C.CYAN}; border-radius: 2px; }}
    QSlider::handle:horizontal {{ background: {C.CYAN}; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; }}

    /* Sağ şerit */
    #Serit {{ border-left: 1px solid {C.CELIK}; background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 rgba(11,21,38,0.9), stop:1 rgba(6,12,24,0.6)); }}
    #SeritBaslik {{ font-family: "{f.display}"; font-size: 15px; font-weight: 500; letter-spacing: 1px; }}
    #OlcumAd {{ color: {C.SIS}; font-size: 13px; }}
    #OlcumDeger {{ font-size: 15px; font-weight: 500; }}
    #Not {{ color: {C.SIS}; font-size: 12px; }}
    #Ayrac {{ background: {C.CELIK}; max-height: 1px; min-height: 1px; }}

    /* Boş sayfalar */
    #BosBaslik {{ font-family: "{f.display}"; font-size: 28px; font-weight: 400; }}
    #BosMetin {{ color: {C.SIS}; font-size: 15px; }}
    #Dugme {{ border: 1px solid {C.CELIK}; border-radius: 10px; padding: 7px 14px; font-size: 13px; }}
    #Dugme:hover {{ border-color: {C.SIS}; }}
    """
