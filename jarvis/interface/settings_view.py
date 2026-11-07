"""Ayarlar ekranı (Aşama 2 kapsamı: Claude bağlantısı, model seçimi, bütçe).

Değişiklikler anında doğrulanıp kaydedilir; "Kaydet" düğmesi gerekmez.
Görsel cilası ve diğer bölümler Aşama 7'de eklenecek.
"""

from __future__ import annotations

from PySide6.QtCore import QLocale, Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QFrame, QHBoxLayout,
                               QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget)

from jarvis.core.assistant import Assistant
from jarvis.core.chat_service import ChatService, ClaudeStatus
from jarvis.core.events import Topics
from jarvis.interface.dialogs import ApiKeyDialog
from jarvis.interface.theme import C
from jarvis.paths import AppPaths

MODEL_CHOICES = [
    ("auto", "Otomatik (önerilen): soruya göre en ucuz yeterli model"),
    ("light", "Her zaman Haiku 5.5 (en ucuz)"),
    ("main", "Her zaman Sonnet 5.5"),
    ("advanced", "Her zaman Opus 5.5 (en pahalı)"),
]
POLICY_CHOICES = [
    ("fallback_light", "Ekonomik modele (Haiku 5.5) geç"),
    ("ask", "Her istekte bana sor"),
    ("block", "Claude isteklerini durdur"),
]


def _section(title: str) -> QLabel:
    lab = QLabel(title, objectName="SeritBaslik")
    lab.setStyleSheet("font-size: 17px; margin-top: 18px;")
    return lab


class SettingsView(QWidget):
    def __init__(self, assistant: Assistant, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.assistant = assistant
        s = assistant.settings
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(40, 30, 40, 40)
        lay.setSpacing(8)
        body.setMaximumWidth(820)
        scroll.setWidget(body)
        outer.addWidget(scroll)

        lay.addWidget(QLabel("Ayarlar", objectName="BosBaslik"))

        # --- Claude bağlantısı ---
        lay.addWidget(_section("Claude bağlantısı"))
        self.status = QLabel()
        self.status.setWordWrap(True)
        backend = assistant.chat.credentials.backend_name()
        store_lbl = QLabel(f"Anahtar deposu: {backend}", objectName="BosMetin")
        row = QHBoxLayout()
        self.key_btn = QPushButton("Anahtarı gir", objectName="KonusDugmesi")
        self.del_btn = QPushButton("Anahtarı sil", objectName="Dugme")
        self.key_btn.clicked.connect(self.open_key_dialog)
        self.del_btn.clicked.connect(lambda: assistant.spawn(assistant.chat.delete_key(), "api-key-delete"))
        row.addWidget(self.key_btn)
        row.addWidget(self.del_btn)
        row.addStretch(1)
        lay.addWidget(self.status)
        lay.addWidget(store_lbl)
        lay.addLayout(row)

        # --- Model ---
        lay.addWidget(_section("Model seçimi"))
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        self.model = QComboBox()
        for key, label in MODEL_CHOICES:
            self.model.addItem(label, key)
        self.model.setCurrentIndex([k for k, _ in MODEL_CHOICES].index(s.ai.preferred_model))
        self.model.currentIndexChanged.connect(self._apply)
        self.web = QCheckBox("Güncel bilgi gerektiğinde web'de ara (1.000 arama $10, bütçeye dahil)")
        self.web.setChecked(s.ai.web_search_enabled)
        self.web.toggled.connect(self._apply)
        form.addRow("Model", self.model)
        lay.addLayout(form)
        lay.addWidget(self.web)

        # --- Bütçe ---
        lay.addWidget(_section("Bütçe"))
        bform = QFormLayout()
        self.daily = self._money(s.budget.daily_limit_usd)
        self.monthly = self._money(s.budget.monthly_limit_usd)
        self.policy = QComboBox()
        for key, label in POLICY_CHOICES:
            self.policy.addItem(label, key)
        self.policy.setCurrentIndex([k for k, _ in POLICY_CHOICES].index(s.budget.on_limit))
        self.policy.currentIndexChanged.connect(self._apply)
        bform.addRow("Günlük limit", self.daily)
        bform.addRow("Aylık limit", self.monthly)
        bform.addRow("Limit dolunca", self.policy)
        lay.addLayout(bform)
        hint = QLabel("0 girilen limit uygulanmaz. Limitin %80'ine gelince uyarırım. "
                      "Tutarlar tahminidir, kesin fatura Anthropic Console'dadır.", objectName="Not")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        # --- Dosyalar ---
        lay.addWidget(_section("Dosyalar"))
        p = AppPaths.resolve()
        files = QLabel(f"Ayarlar: {p.settings_file}<br>Loglar: {p.log_file}<br>Veritabanı: {assistant.db.path}",
                       objectName="BosMetin")
        files.setTextFormat(Qt.TextFormat.RichText)
        files.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        files.setWordWrap(True)
        lay.addWidget(files)
        lay.addStretch(1)

        assistant.bus.subscribe(Topics.CLAUDE_STATUS, self.show_status)
        self.show_status(assistant.chat)

    def _money(self, value: float) -> QDoubleSpinBox:
        sb = QDoubleSpinBox()
        sb.setLocale(QLocale(QLocale.Language.Turkish, QLocale.Country.Turkey))  # 0,50
        sb.setPrefix("$ ")
        sb.setDecimals(2)
        sb.setRange(0, 10_000)
        sb.setSingleStep(0.5)
        sb.setValue(value)
        sb.setMaximumWidth(160)
        sb.valueChanged.connect(self._apply)
        return sb

    def _apply(self) -> None:
        s = self.assistant.settings
        s.ai.preferred_model = self.model.currentData()
        s.ai.web_search_enabled = self.web.isChecked()
        s.budget.daily_limit_usd = round(self.daily.value(), 2)
        s.budget.monthly_limit_usd = round(self.monthly.value(), 2)
        s.budget.on_limit = self.policy.currentData()
        self.assistant.store.save()
        self.assistant.bus.publish(Topics.SETTINGS_CHANGED, s)
        self.assistant.spawn(self.assistant.chat.publish_usage(), "usage-refresh")

    def show_status(self, chat: ChatService) -> None:
        color = {ClaudeStatus.CONNECTED: C.CYAN, ClaudeStatus.CHECKING: C.SIS,
                 ClaudeStatus.NO_KEY: C.AMBER, ClaudeStatus.OFFLINE: C.AMBER, ClaudeStatus.ERROR: C.KIZIL}[chat.status]
        detail = f"<br><span style='color:{C.SIS}'>{chat.status_detail}</span>" if chat.status_detail else ""
        self.status.setText(f"<span style='color:{color}'>Durum: {chat.status.value}</span>{detail}")
        has_key = chat.status is not ClaudeStatus.NO_KEY
        self.key_btn.setText("Anahtarı değiştir" if has_key else "Anahtarı gir")
        self.del_btn.setEnabled(has_key)

    def open_key_dialog(self) -> None:
        dlg = ApiKeyDialog(self.assistant.chat.save_key, self.assistant.spawn,
                           self.assistant.chat.credentials.backend_name(), self)
        dlg.open()
        self._dialog = dlg  # açık kalsın diye referans
