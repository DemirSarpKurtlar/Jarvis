"""Ayarlar > Başlangıç ve pencere (Aşama 9).

* "Windows açılınca başlat": kayıt defterindeki Run değerini anında yazar/siler.
* "Pencereyi kapatınca bildirim alanında çalışmaya devam et".
* "Dinlerken ekranın üstünde göster" (dinleme göstergesi, `interface/overlay.py`).

Desteklenmeyen sistemlerde seçenekler kapalı gösterilir ve nedeni yazılır.
"""

from __future__ import annotations

import logging

from PySide6.QtWidgets import QCheckBox, QLabel, QVBoxLayout, QWidget

from jarvis.core.assistant import Assistant
from jarvis.core.errors import JarvisError
from jarvis.core.events import Topics
from jarvis.integrations.autostart import UNSUPPORTED_MESSAGE, Autostart
from jarvis.interface.theme import C
from jarvis.interface.tray import tray_available

log = logging.getLogger(__name__)

AUTOSTART_NOTE = "Oturum açınca JARVIS pencere açmadan bildirim alanında başlar."
NO_TRAY_NOTE = "Bu sistemde bildirim alanı yok; pencereyi kapatmak JARVIS'i kapatır."
OVERLAY_NOTE = ("JARVIS penceresi öndeyken gösterilmez. Oyunlarda görmek için oyunu Kenarlıksız pencere "
                "modunda çalıştır; gerçek tam ekranın üstüne çizilemez.")


class StartupSection(QWidget):
    def __init__(self, assistant: Assistant, autostart: Autostart | None = None,
                 has_tray: bool | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.assistant = assistant
        self.autostart = autostart or Autostart()
        has_tray = tray_available() if has_tray is None else has_tray
        s = assistant.settings

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        title = QLabel("Başlangıç ve pencere", objectName="SeritBaslik")
        title.setStyleSheet("font-size: 17px; margin-top: 18px;")
        lay.addWidget(title)

        self.autostart_box = QCheckBox("Windows açılınca başlat")
        self.autostart_note = QLabel(objectName="Not")
        self.autostart_note.setWordWrap(True)
        if self.autostart.supported:
            self.autostart_box.setChecked(s.general.autostart)
            self.autostart_note.setText(AUTOSTART_NOTE)
        else:
            self.autostart_box.setChecked(False)
            self.autostart_box.setEnabled(False)
            self.autostart_note.setText(UNSUPPORTED_MESSAGE)
        self.autostart_box.toggled.connect(self._apply_autostart)
        lay.addWidget(self.autostart_box)
        lay.addWidget(self.autostart_note)

        self.tray_box = QCheckBox("Pencereyi kapatınca bildirim alanında çalışmaya devam et")
        self.tray_box.setChecked(s.ui.close_to_tray and has_tray)
        self.tray_box.setEnabled(has_tray)
        self.tray_box.toggled.connect(self._apply_tray)
        lay.addWidget(self.tray_box)
        tray_note = QLabel("Tamamen kapatmak için soldaki Kapat düğmesini ya da simgenin menüsünü kullan."
                           if has_tray else NO_TRAY_NOTE, objectName="Not")
        tray_note.setWordWrap(True)
        lay.addWidget(tray_note)

        # Dinleme göstergesi
        self.overlay_box = QCheckBox("Dinlerken ekranın üstünde göster")
        self.overlay_box.setChecked(s.ui.overlay_enabled)
        self.overlay_box.toggled.connect(self._apply_overlay)
        lay.addWidget(self.overlay_box)
        overlay_note = QLabel(OVERLAY_NOTE, objectName="Not")
        overlay_note.setWordWrap(True)
        lay.addWidget(overlay_note)

    def _save(self) -> None:
        self.assistant.store.save()
        self.assistant.bus.publish(Topics.SETTINGS_CHANGED, self.assistant.settings)

    def _apply_autostart(self, enabled: bool) -> None:
        try:
            self.autostart.set_enabled(enabled)
        except (JarvisError, OSError) as exc:
            log.warning("Başlangıç ayarı uygulanamadı: %s", exc)
            msg = exc.user_message if isinstance(exc, JarvisError) else "Kayıt defterine yazılamadı."
            self.autostart_box.blockSignals(True)
            self.autostart_box.setChecked(not enabled)
            self.autostart_box.blockSignals(False)
            self.autostart_note.setText(f"<span style='color:{C.KIZIL}'>{msg}</span>")
            return
        self.autostart_note.setText(AUTOSTART_NOTE)
        self.assistant.settings.general.autostart = enabled
        self._save()

    def _apply_tray(self, enabled: bool) -> None:
        self.assistant.settings.ui.close_to_tray = enabled
        self._save()

    def _apply_overlay(self, enabled: bool) -> None:
        self.assistant.settings.ui.overlay_enabled = enabled
        self._save()
