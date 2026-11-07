"""Ana pencere: sol ray, ortada sahne (panel ve diğer sayfalar), sağda şerit."""

from __future__ import annotations

import logging

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QSlider,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from jarvis import __version__
from jarvis.core.assistant import Assistant
from jarvis.core.events import Topics
from jarvis.core.state import AssistantState, StateChange
from jarvis.interface.components import icon
from jarvis.interface.telemetry_panel import TelemetryPanel
from jarvis.interface.theme import C, Fonts
from jarvis.interface.visualizer import ReactorWidget

log = logging.getLogger(__name__)

NAV_ITEMS = [
    ("panel", "Panel"),
    ("konusmalar", "Konuşmalar"),
    ("hafiza", "Hafıza"),
    ("sistem", "Sistem"),
    ("otomasyon", "Otomasyon"),
    ("ayarlar", "Ayarlar"),
]

# Henüz yapılmamış bölümler: sahte içerik yerine dürüst bir açıklama gösterilir
COMING = {
    "konusmalar": ("Konuşmalar", "Geçmiş konuşmalar Claude bağlantısıyla birlikte Aşama 2'de burada listelenecek."),
    "hafiza": ("Hafıza", "Hatırladıklarımı görüp düzenleyebileceğin ve silebileceğin ekran Aşama 4'te geliyor."),
    "sistem": ("Sistem", "Ayrıntılı işlem ve donanım ekranı Aşama 5'te geliyor. Özet bilgiler sağdaki şeritte."),
    "otomasyon": ("Otomasyon", "Rutinler ve zamanlanmış görevler Aşama 6'da geliyor."),
    "ayarlar": ("Ayarlar", "Ayarlar ekranı Aşama 7'de geliyor. O zamana kadar ayarlar şu dosyada duruyor:"),
}

DEMO_SEQUENCE = [
    (AssistantState.WAKE_DETECTED, ""),
    (AssistantState.LISTENING, ""),
    (AssistantState.PROCESSING, "Sonnet 5.5 ile, hafızadan 2 kayıt kullanılıyor"),
    (AssistantState.EXECUTING, "Not Defteri açılıyor"),
    (AssistantState.SPEAKING, ""),
    (AssistantState.STANDBY, ""),
]


class Sidebar(QFrame):
    def __init__(self, on_navigate, on_quit, parent: QWidget | None = None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self.setObjectName("Ray")
        self.setFixedWidth(76)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(7, 18, 7, 18)
        lay.setSpacing(4)

        logo = QLabel(objectName="Logo")
        logo.setFixedSize(34, 34)
        logo.setStyleSheet(
            f"border: 1.5px solid {C.CYAN}; border-radius: 17px;"
            "background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,"
            f" stop:0 {C.CYAN}, stop:0.22 {C.CYAN}, stop:0.26 transparent, stop:1 transparent);"
        )
        lay.addWidget(logo, 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addSpacing(22)

        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: dict[str, QToolButton] = {}
        for key, label in NAV_ITEMS:
            b = self._button(key, label)
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, k=key: on_navigate(k))
            self.group.addButton(b)
            self.buttons[key] = b
            lay.addWidget(b, 0, Qt.AlignmentFlag.AlignHCenter)
        self.buttons["panel"].setChecked(True)

        lay.addStretch(1)
        quit_btn = self._button("kapat", "Kapat")
        quit_btn.setObjectName("KapatDugmesi")
        quit_btn.setToolTip("JARVIS'i tamamen kapat")
        quit_btn.clicked.connect(on_quit)
        self.quit_button = quit_btn
        lay.addWidget(quit_btn, 0, Qt.AlignmentFlag.AlignHCenter)

    @staticmethod
    def _button(key: str, label: str) -> QToolButton:
        b = QToolButton()
        b.setText(label)
        b.setIcon(icon(key, C.SIS, active=C.CYAN))
        b.setIconSize(QSize(20, 20))
        b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        return b


class Banner(QFrame):
    """Hata ve uyarıları gösteren kapatılabilir bant."""

    def __init__(self, on_close, parent: QWidget | None = None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self.setObjectName("UyariBandi")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 10, 10, 10)
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        close = QToolButton()
        close.setIcon(icon("carpi", C.SIS, size=16))
        close.setToolTip("Kapat")
        close.setStyleSheet("border:0; padding:4px;")
        close.clicked.connect(on_close)
        lay.addWidget(self.text, 1)
        lay.addWidget(close, 0, Qt.AlignmentFlag.AlignTop)
        self.hide()

    def show_message(self, message: str, level: str = "hata") -> None:
        self.setProperty("seviye", level)
        self.style().unpolish(self)
        self.style().polish(self)
        self.text.setText(message)
        self.show()


class PanelPage(QWidget):
    def __init__(self, fonts: Fonts, assistant: Assistant, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.assistant = assistant
        s = assistant.settings
        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 18, 32, 24)
        lay.setSpacing(12)

        # Üst bar
        top = QHBoxLayout()
        top.addWidget(QLabel("JARVIS", objectName="Kelime"))
        top.addStretch(1)
        self.claude_pill = QLabel("Claude: bağlı değil", objectName="Isaret")
        self.claude_pill.setToolTip("Claude bağlantısı Aşama 2'de kurulacak")
        self.mic_pill = QLabel(objectName="Isaret")
        top.addWidget(self.claude_pill)
        top.addWidget(self.mic_pill)
        lay.addLayout(top)

        self.banner = Banner(self._close_banner)
        lay.addWidget(self.banner)

        self.reactor = ReactorWidget(fonts, reduced_motion=s.ui.reduced_motion)
        lay.addWidget(self.reactor, 1)

        # Altyazı
        cap = QGridLayout()
        cap.setColumnMinimumWidth(0, 64)
        cap.setHorizontalSpacing(14)
        who = QLabel("JARVIS", objectName="Kim")
        who.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        self.caption = QLabel(
            "Merhaba! Temel sistem çalışıyor. Yazılı sohbet Aşama 2'de, sesli konuşma Aşama 3'te açılacak.",
            objectName="SozJarvis",
        )
        self.caption.setWordWrap(True)
        cap.addWidget(who, 0, 0)
        cap.addWidget(self.caption, 0, 1)
        cap_wrap = QWidget()
        cap_wrap.setLayout(cap)
        cap_wrap.setMaximumWidth(720)
        lay.addWidget(cap_wrap, 0, Qt.AlignmentFlag.AlignHCenter)

        # Kontrol yuvası
        dock = QFrame(objectName="Yuva")
        d = QHBoxLayout(dock)
        d.setContentsMargins(10, 10, 14, 10)
        d.setSpacing(14)
        self.mic_button = QPushButton(objectName="MikDugmesi")
        self.mic_button.setCheckable(True)  # checked = mikrofon KAPALI
        self.mic_button.setIconSize(QSize(22, 22))
        self.mic_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mic_button.clicked.connect(lambda checked: self.assistant.set_microphone_enabled(not checked))
        self.input = QLineEdit(objectName="Giris")
        self.input.setPlaceholderText("Yazılı sohbet Aşama 2'de açılacak")
        self.input.setEnabled(False)
        self.talk = QPushButton("Konuş", objectName="KonusDugmesi")
        self.talk.setEnabled(False)
        self.talk.setToolTip("Sesli konuşma Aşama 3'te açılacak")
        vol_icon = QLabel()
        vol_icon.setPixmap(icon("ses", C.SIS, size=18).pixmap(18, 18))
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(s.voice.volume)
        self.volume.setFixedWidth(100)
        self.volume.setToolTip("JARVIS'in konuşma ses seviyesi")
        self.volume.sliderReleased.connect(lambda: self.assistant.set_volume(self.volume.value()))
        d.addWidget(self.mic_button)
        d.addWidget(self.input, 1)
        d.addWidget(self.talk)
        d.addWidget(vol_icon)
        d.addWidget(self.volume)
        lay.addWidget(dock)

        self.set_microphone(s.voice.microphone_enabled)

    def set_microphone(self, enabled: bool) -> None:
        self.mic_button.setChecked(not enabled)
        color = C.KIZIL if enabled else C.SIS
        self.mic_button.setIcon(icon("mikrofon" if enabled else "mikrofon_kapali", color, size=22, stroke=1.8))
        self.mic_button.setToolTip("Mikrofonu tamamen kapat" if enabled else "Mikrofonu aç")
        self.mic_pill.setText("Mikrofon: izinli" if enabled else "Mikrofon: kapalı")
        self.mic_pill.setToolTip(
            "Ses motoru Aşama 3'te eklenecek. Şu an mikrofon dinlenmiyor."
            if enabled else "Mikrofon kapalı. Ses motoru eklendiğinde de dinleme yapılmaz."
        )

    def _close_banner(self) -> None:
        self.banner.hide()
        self.assistant.acknowledge_error()


class ComingSoonPage(QWidget):
    def __init__(self, title: str, text: str, extra: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(40, 40, 40, 40)
        lay.addStretch(1)
        lay.addWidget(QLabel(title, objectName="BosBaslik"))
        body = QLabel(text, objectName="BosMetin")
        body.setWordWrap(True)
        body.setMaximumWidth(560)
        lay.addWidget(body)
        if extra:
            ex = QLabel(extra, objectName="BosMetin")
            ex.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            ex.setStyleSheet(f"color:{C.BUZ};")
            lay.addWidget(ex)
        lay.addStretch(2)


class MainWindow(QMainWindow):
    # Kullanıcı pencereyi ya da Kapat düğmesini kullandığında yayılır
    close_requested = Signal()

    def __init__(self, assistant: Assistant, fonts: Fonts, developer: bool = False) -> None:
        super().__init__()
        self.assistant = assistant
        self.setWindowTitle(f"JARVIS {__version__}")
        self.resize(1440, 900)
        self.setMinimumSize(1000, 680)

        root = QWidget(objectName="Kok")
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)

        self.sidebar = Sidebar(self.navigate, self.close)
        self.stack = QStackedWidget()
        self.panel = PanelPage(fonts, assistant)
        self.pages: dict[str, QWidget] = {"panel": self.panel}
        self.stack.addWidget(self.panel)
        for key, (title, text) in COMING.items():
            extra = str(assistant.store.path) if key == "ayarlar" else ""
            page = ComingSoonPage(title, text, extra)
            self.pages[key] = page
            self.stack.addWidget(page)
        self.telemetry = TelemetryPanel()
        self.telemetry.apply_settings(assistant.settings)

        h.addWidget(self.sidebar)
        h.addWidget(self.stack, 1)
        h.addWidget(self.telemetry)
        self.setCentralWidget(root)

        bus = assistant.bus
        bus.subscribe(Topics.STATE_CHANGED, self._on_state)
        bus.subscribe(Topics.SYSTEM_SNAPSHOT, self.telemetry.update_snapshot)
        bus.subscribe(Topics.MICROPHONE_TOGGLED, self.panel.set_microphone)
        bus.subscribe(Topics.APP_ERROR, lambda msg: self.panel.banner.show_message(msg, "hata"))

        if assistant.store.load_warning:
            self.panel.banner.show_message(assistant.store.load_warning, "uyari")

        # Acil kısayol: Ctrl+Shift+M mikrofonu anında kapatır/açar
        QShortcut(QKeySequence("Ctrl+Shift+M"), self, activated=self.panel.mic_button.click)

        self._demo_index = 0
        if developer:
            # F8: durum görünümlerini sırayla dene (yalnızca geliştirici modu)
            QShortcut(QKeySequence("F8"), self, activated=self.demo_next_state)
            QShortcut(QKeySequence("F9"), self, activated=lambda: assistant.state.fail("Deneme hatası (F9)"))

    def navigate(self, key: str) -> None:
        self.stack.setCurrentWidget(self.pages[key])
        self.sidebar.buttons[key].setChecked(True)

    def _on_state(self, change: StateChange) -> None:
        self.panel.reactor.set_state(change.new, change.detail if change.new is not AssistantState.ERROR else "")
        if change.new is AssistantState.ERROR and change.detail:
            self.panel.banner.show_message(change.detail, "hata")

    def demo_next_state(self) -> None:
        sm = self.assistant.state
        if sm.state is AssistantState.ERROR:
            sm.reset()
            self._demo_index = 0
            return
        new, detail = DEMO_SEQUENCE[self._demo_index % len(DEMO_SEQUENCE)]
        if sm.can_transition(new) or new is sm.state:
            sm.transition(new, detail)
        self._demo_index += 1

    def closeEvent(self, event: QCloseEvent) -> None:
        log.info("Pencere kapatılıyor")
        event.accept()
        self.close_requested.emit()
