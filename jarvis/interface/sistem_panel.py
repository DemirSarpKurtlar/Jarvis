"""Otomasyon ekranındaki "Sistem rutinleri" bölümü.

- Ekran kartı sıcaklık uyarısı: aç/kapat ve eşik (ayarlara hemen kaydedilir).
- İndirilenler'i her gün düzenle: aç/kapat ve son düzenlemeyi geri al. Açma ve geri alma araç kaydı
  üzerinden gider; onay penceresi (önizlemeyle) çıkar, reddedilirse kutu eski hâline döner.
"""

from __future__ import annotations

from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QPushButton, QSpinBox, QVBoxLayout, QWidget

from jarvis.interface.theme import C


class SistemPanel(QWidget):
    def __init__(self, assistant, parent: QWidget | None = None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self.assistant = assistant
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(QLabel("Sistem rutinleri", objectName="SeritBaslik"))

        s = assistant.settings.sistem
        row = QHBoxLayout()
        self.gpu = QCheckBox("Ekran kartı ısınınca uyar")
        self.gpu.setChecked(s.gpu_alert_enabled)
        self.gpu.toggled.connect(self._gpu_changed)
        self.threshold = QSpinBox()
        self.threshold.setRange(60, 105)
        self.threshold.setValue(s.gpu_alert_threshold_c)
        self.threshold.setSuffix(" °C")
        self.threshold.setPrefix("eşik ")
        self.threshold.valueChanged.connect(self._gpu_changed)
        row.addWidget(self.gpu)
        row.addWidget(self.threshold)
        row.addStretch(1)
        lay.addLayout(row)

        row2 = QHBoxLayout()
        self.downloads = QCheckBox("İndirilenler'i her gün türlerine göre düzenle (silmez)")
        self.downloads.clicked.connect(self._downloads_clicked)
        self.undo = QPushButton("Son düzenlemeyi geri al", objectName="Dugme")
        self.undo.clicked.connect(lambda: self._run("geri_al"))
        row2.addWidget(self.downloads)
        row2.addWidget(self.undo)
        row2.addStretch(1)
        lay.addLayout(row2)
        self.note = QLabel(objectName="Not")
        self.note.setWordWrap(True)
        lay.addWidget(self.note)

    @property
    def routine(self):  # noqa: ANN201
        return self.assistant.tool_service.ctx.extra.get("sistem_routine")

    def _gpu_changed(self, *_a) -> None:  # noqa: ANN002
        s = self.assistant.settings.sistem
        s.gpu_alert_enabled = self.gpu.isChecked()
        s.gpu_alert_threshold_c = self.threshold.value()
        self.threshold.setEnabled(self.gpu.isChecked())
        self.assistant.store.save()

    def sync(self) -> None:
        r = self.routine
        st = r.status() if r is not None else {"enabled": False, "undo_available": False}
        self.downloads.blockSignals(True)
        self.downloads.setChecked(bool(st["enabled"]))
        self.downloads.blockSignals(False)
        self.downloads.setEnabled(r is not None)
        self.undo.setEnabled(bool(st.get("undo_available")))
        self.threshold.setEnabled(self.gpu.isChecked())

    def _downloads_clicked(self, checked: bool) -> None:
        self._run("ac" if checked else "kapat")

    def _run(self, action: str) -> None:
        self.downloads.setEnabled(False)
        self.undo.setEnabled(False)
        task = self.assistant.run_tool("downloads_routine", {"action": action}, "arayuz")
        task.add_done_callback(self._done)

    def _done(self, task) -> None:  # noqa: ANN001
        if not task.cancelled() and task.exception() is None:
            res = task.result()
            self.note.setStyleSheet(f"color:{C.CYAN if res.ok else C.AMBER};")
            self.note.setText(res.summary if res.ok else (res.error or res.summary))
        self.sync()

    def showEvent(self, event) -> None:  # noqa: ANN001
        super().showEvent(event)
        self.sync()
