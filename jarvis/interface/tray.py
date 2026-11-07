"""Bildirim alanı (sistem tepsisi) simgesi.

Menü: pencereyi göster/gizle, mikrofonu kapat/aç, tamamen kapat.
Simgeye tıklamak pencereyi gösterir ya da gizler.

Bildirim alanı olmayan sistemlerde (ör. bazı Linux masaüstleri, ekransız
testler) simge oluşturulmaz; pencereyi kapatmak her zamanki gibi uygulamayı
kapatır.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from jarvis.core.assistant import Assistant
from jarvis.core.events import Topics
from jarvis.interface.app_icon import app_icon

log = logging.getLogger(__name__)

HIDDEN_HINT = "JARVIS arka planda çalışmaya devam ediyor. Tamamen kapatmak için simgeye sağ tıkla."


def tray_available() -> bool:
    return QSystemTrayIcon.isSystemTrayAvailable()


class TrayController(QObject):
    def __init__(self, window, assistant: Assistant) -> None:  # noqa: ANN001  (MainWindow)
        super().__init__(window)
        self.window = window
        self.assistant = assistant
        self.icon = QSystemTrayIcon(app_icon(), self)
        self.icon.setToolTip("JARVIS")

        self.menu = QMenu()
        self.toggle_action = QAction("Pencereyi göster", self.menu)
        self.toggle_action.triggered.connect(self.toggle_window)
        self.mic_action = QAction("Mikrofonu kapat", self.menu)
        self.mic_action.setCheckable(True)  # işaretli = mikrofon KAPALI
        self.mic_action.triggered.connect(lambda checked: assistant.set_microphone_enabled(not checked))
        self.quit_action = QAction("Tamamen kapat", self.menu)
        self.quit_action.triggered.connect(window.quit_app)
        self.menu.addAction(self.toggle_action)
        self.menu.addAction(self.mic_action)
        self.menu.addSeparator()
        self.menu.addAction(self.quit_action)
        self.menu.aboutToShow.connect(self.refresh)
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._on_activated)

        self._unsubscribe = assistant.bus.subscribe(Topics.MICROPHONE_TOGGLED, self._on_microphone)
        self._on_microphone(assistant.settings.voice.microphone_enabled)
        self._hint_shown = False
        window.tray = self
        window.close_requested.connect(self.remove)
        self.refresh()

    @classmethod
    def create(cls, window, assistant: Assistant) -> "TrayController | None":  # noqa: ANN001
        if not tray_available():
            log.info("Bildirim alanı bulunamadı, simge eklenmedi")
            return None
        tray = cls(window, assistant)
        tray.icon.show()
        return tray

    def window_shown(self) -> bool:
        return self.window.isVisible() and not self.window.isMinimized()

    def refresh(self) -> None:
        self.toggle_action.setText("Pencereyi gizle" if self.window_shown() else "Pencereyi göster")

    def toggle_window(self) -> None:
        if self.window_shown():
            self.window.hide()
        else:
            self.window.bring_to_front()
        self.refresh()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.toggle_window()

    def _on_microphone(self, enabled: bool) -> None:
        self.mic_action.setChecked(not enabled)
        self.icon.setToolTip("JARVIS" if enabled else "JARVIS (mikrofon kapalı)")

    def notify_hidden(self) -> None:
        """Pencere kapatılıp bildirim alanına inince bir kez bilgi ver."""
        if self._hint_shown:
            return
        self._hint_shown = True
        if self.icon.isVisible() and QSystemTrayIcon.supportsMessages():
            self.icon.showMessage("JARVIS", HIDDEN_HINT, app_icon(), 4000)

    def remove(self) -> None:
        """Kapanışta simgeyi hemen kaldır (Windows'ta hayalet simge kalmasın)."""
        self._unsubscribe()
        self.icon.hide()
