"""Otomasyon ekranı: zamanlanmış görevleri görme, ekleme ve iptal etme.

Görevler araç kaydı üzerinden (izin ve denetim kaydıyla) eklenir/iptal edilir; bu ekran
JARVIS'e "yarım saat sonra bana toplantıyı hatırlat" demenin görsel karşılığıdır.
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QSpinBox, QVBoxLayout, QWidget)

from jarvis.core.assistant import Assistant
from jarvis.core.events import Topics
from jarvis.interface.theme import C

ACTION_LABELS = [
    ("hatirlatma", "Hatırlatma"),
    ("uygulama_ac", "Uygulama aç"),
    ("ekrani_kilitle", "Ekranı kilitle"),
    ("ses_ayarla", "Ses seviyesini ayarla"),
]
LABEL_OF = dict(ACTION_LABELS)


def remaining(due: datetime, now: datetime | None = None) -> str:
    secs = int((due - (now or datetime.now())).total_seconds())
    if secs <= 0:
        return "şimdi"
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h} sa {m} dk sonra"
    if m:
        return f"{m} dk sonra"
    return f"{s} sn sonra"


class AutomationView(QWidget):
    def __init__(self, assistant: Assistant, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.assistant = assistant
        lay = QVBoxLayout(self)
        lay.setContentsMargins(40, 30, 40, 30)
        lay.setSpacing(10)
        lay.addWidget(QLabel("Otomasyon", objectName="BosBaslik"))
        intro = QLabel("JARVIS'e \"yarım saat sonra bana toplantıyı hatırlat\" ya da \"10 dakika sonra sesi "
                       "yüzde 20 yap\" diyebilir veya buradan görev ekleyebilirsin. Görevler yalnızca JARVIS "
                       "açıkken çalışır; onay gerektiren işlemler zamanlanamaz.", objectName="BosMetin")
        intro.setWordWrap(True)
        intro.setMaximumWidth(760)
        lay.addWidget(intro)

        form = QFormLayout()
        self.action = QComboBox()
        for key, label in ACTION_LABELS:
            self.action.addItem(label, key)
        self.minutes = QSpinBox()
        self.minutes.setRange(1, 1440)
        self.minutes.setValue(30)
        self.minutes.setSuffix(" dakika sonra")
        self.text = QLineEdit()
        self.text.setPlaceholderText("Hatırlatma metni ya da uygulama adı")
        self.text.setStyleSheet(f"border:1px solid {C.CELIK}; border-radius:8px; padding:6px 10px;")
        self.value = QSpinBox()
        self.value.setRange(0, 100)
        self.value.setValue(30)
        self.value.setPrefix("%")
        self.add_btn = QPushButton("Zamanla", objectName="KonusDugmesi")
        self.add_btn.clicked.connect(self._add)
        self.action.currentIndexChanged.connect(self._sync_fields)
        form.addRow("Ne yapılsın", self.action)
        form.addRow("Ne zaman", self.minutes)
        form.addRow("Metin", self.text)
        form.addRow("Ses", self.value)
        lay.addLayout(form)
        row = QHBoxLayout()
        row.addWidget(self.add_btn)
        self.result = QLabel(objectName="Not")
        row.addWidget(self.result, 1)
        lay.addLayout(row)

        lay.addSpacing(14)
        lay.addWidget(QLabel("Bekleyen görevler", objectName="SeritBaslik"))
        self.list_box = QVBoxLayout()
        self.list_box.setSpacing(6)
        lay.addLayout(self.list_box)
        self.empty = QLabel("Bekleyen görev yok.", objectName="BosMetin")
        lay.addWidget(self.empty)
        # --- RUTIN ---
        from jarvis.interface.routines_view import RoutinesPanel
        lay.addSpacing(14)
        self.routines_panel = RoutinesPanel(assistant)
        lay.addWidget(self.routines_panel, 20)
        # --- RUTIN sonu ---
        lay.addStretch(1)

        self._timer = QTimer(self, interval=5000, timeout=self.refresh)
        assistant.bus.subscribe(Topics.TOOL_EXECUTED, self._on_tool)
        self._sync_fields()

    @property
    def scheduler(self):  # noqa: ANN201
        return self.assistant.tool_service.ctx.scheduler

    def _sync_fields(self) -> None:
        key = self.action.currentData()
        self.text.setEnabled(key in ("hatirlatma", "uygulama_ac"))
        self.value.setEnabled(key == "ses_ayarla")

    def _add(self) -> None:
        key = self.action.currentData()
        args = {"action": key, "minutes": self.minutes.value(),
                "text": self.text.text().strip() if key in ("hatirlatma", "uygulama_ac") else "",
                "value": self.value.value() if key == "ses_ayarla" else -1}
        self.add_btn.setEnabled(False)
        task = self.assistant.run_tool("schedule_task", args, "arayuz")
        task.add_done_callback(lambda t: self._after_add(t))

    def _after_add(self, task) -> None:  # noqa: ANN001
        self.add_btn.setEnabled(True)
        if task.cancelled() or task.exception() is not None:
            self.result.setText("Görev eklenemedi.")
            return
        res = task.result()
        self.result.setStyleSheet(f"color:{C.CYAN if res.ok else C.AMBER};")
        self.result.setText(res.summary if res.ok else (res.error or res.summary))
        if res.ok:
            self.text.clear()
        self.refresh()

    def _on_tool(self, result) -> None:  # noqa: ANN001
        if getattr(result, "tool", "") in ("schedule_task", "cancel_scheduled_task") and self.isVisible():
            self.refresh()

    def refresh(self) -> None:
        while self.list_box.count():
            item = self.list_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        tasks = self.scheduler.list() if self.scheduler is not None else []
        self.empty.setVisible(not tasks)
        for t in tasks:
            roww = QWidget()
            r = QHBoxLayout(roww)
            r.setContentsMargins(0, 6, 0, 6)
            what = LABEL_OF.get(t.action, t.action)
            detail = t.text or (f"%{t.value}" if t.action == "ses_ayarla" else "")
            lab = QLabel(f"<b>{t.due:%H:%M}</b> ({remaining(t.due)}): {what}" + (f" — {detail}" if detail else ""))
            lab.setTextFormat(Qt.TextFormat.RichText)
            cancel = QPushButton("İptal", objectName="Dugme")
            cancel.clicked.connect(lambda _=False, tid=t.id: self.assistant.run_tool(
                "cancel_scheduled_task", {"task_id": tid}, "arayuz"))
            r.addWidget(lab, 1)
            r.addWidget(cancel)
            self.list_box.addWidget(roww)

    def showEvent(self, event) -> None:  # noqa: ANN001
        super().showEvent(event)
        self.refresh()
        self._timer.start()

    def hideEvent(self, event) -> None:  # noqa: ANN001
        super().hideEvent(event)
        self._timer.stop()
