"""Pencereler: API anahtarı girişi ve bütçe onayı."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
                               QVBoxLayout, QWidget)

from jarvis.core.errors import JarvisError
from jarvis.interface.theme import C


class ApiKeyDialog(QDialog):
    """Anahtarı alır, doğrulatır, kaydettirir. Anahtar ekranda gizli yazılır."""

    def __init__(self, save: Callable[[str], Awaitable[str]], spawn, backend: str,  # noqa: ANN001
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._save, self._spawn = save, spawn
        self.setWindowTitle("Claude API anahtarı")
        self.setModal(True)
        self.setMinimumWidth(480)
        self.setStyleSheet(f"QDialog {{ background: {C.DERINLIK}; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 22, 24, 20)
        lay.setSpacing(12)
        title = QLabel("Claude API anahtarını gir", objectName="BosBaslik")
        title.setStyleSheet("font-size: 20px;")
        info = QLabel(
            "Anahtarı Anthropic Console'daki API Keys bölümünden alabilirsin. Önce Anthropic'e küçük, "
            f"ücretsiz bir istekle doğrulanır, sonra <b>{backend}</b> içinde saklanır. "
            "Ayar dosyasına, loglara ya da sohbet geçmişine yazılmaz.", objectName="BosMetin")
        info.setWordWrap(True)
        info.setTextFormat(Qt.TextFormat.RichText)
        self.edit = QLineEdit()
        self.edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit.setPlaceholderText("sk-ant-...")
        self.edit.setStyleSheet(f"border:1px solid {C.CELIK}; border-radius:10px; padding:9px 12px;")
        self.result = QLabel("")
        self.result.setWordWrap(True)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel = QPushButton("Vazgeç", objectName="Dugme")
        self.ok = QPushButton("Doğrula ve kaydet", objectName="KonusDugmesi")
        self.cancel.clicked.connect(self.reject)
        self.ok.clicked.connect(self._submit)
        self.edit.returnPressed.connect(self._submit)
        buttons.addWidget(self.cancel)
        buttons.addWidget(self.ok)
        for w in (title, info, self.edit, self.result):
            lay.addWidget(w)
        lay.addLayout(buttons)

    def _submit(self) -> None:
        key = self.edit.text()
        if not key.strip():
            return
        self.ok.setEnabled(False)
        self.result.setStyleSheet(f"color:{C.SIS};")
        self.result.setText("Anahtar doğrulanıyor…")
        self._spawn(self._do(key), "api-key-save")

    async def _do(self, key: str) -> None:
        try:
            msg = await self._save(key)
        except JarvisError as e:
            self.result.setStyleSheet(f"color:{C.KIZIL};")
            self.result.setText(e.user_message)
            self.ok.setEnabled(True)
            return
        self.edit.clear()
        self.result.setStyleSheet(f"color:{C.CYAN};")
        self.result.setText(msg)
        self.accept()


def ask_budget(parent: QWidget, question: str, answer: Callable[[bool], None]) -> QMessageBox:
    """Engellemeyen onay penceresi (exec() asyncio döngüsünü iç içe kilitlerdi)."""
    box = QMessageBox(parent)
    box.setWindowTitle("Bütçe limiti")
    box.setText(question)
    box.setIcon(QMessageBox.Icon.Warning)
    yes = box.addButton("Yine de gönder", QMessageBox.ButtonRole.AcceptRole)
    box.addButton("Gönderme", QMessageBox.ButtonRole.RejectRole)
    box.finished.connect(lambda _r: answer(box.clickedButton() is yes))
    box.open()
    return box


def ask_gpu_download(parent: QWidget, size_text: str, answer: Callable[[bool], None]) -> QMessageBox:
    """Ekran kartı kütüphanelerini indirmeden önce boyutu gösterip onay ister (engellemeyen)."""
    box = QMessageBox(parent)
    box.setWindowTitle("Ekran kartı hızlandırması")
    box.setText("Konuşma tanımanın ekran kartında çalışması için NVIDIA'nın CUDA kütüphanesi (cuBLAS) "
                "indirilecek. Yalnızca JARVIS'in veri klasörüne kurulur; yönetici izni gerekmez.")
    box.setInformativeText(size_text + " İndirme arka planda sürer; JARVIS'i kullanmaya devam edebilirsin.")
    box.setIcon(QMessageBox.Icon.Question)
    yes = box.addButton("İndir", QMessageBox.ButtonRole.AcceptRole)
    no = box.addButton("Vazgeç", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(yes)
    box.setEscapeButton(no)
    box.finished.connect(lambda _r: answer(box.clickedButton() is yes))
    box.open()
    return box


# --- Aşama 5: araç onayı ---
def ask_tool_confirmation(parent: QWidget, prompt, answer: Callable[[bool], None]) -> QMessageBox:  # noqa: ANN001
    """Engellemeyen araç onayı. Varsayılan düğme "Vazgeç"; kalıcı işlemlerde kırmızı uyarı."""
    box = QMessageBox(parent)
    box.setWindowTitle(f"JARVIS onay istiyor: {prompt.title}")
    box.setText(prompt.message)
    box.setIcon(QMessageBox.Icon.Critical if prompt.danger else QMessageBox.Icon.Question)
    if prompt.danger:
        box.setInformativeText("Bu işlem geri alınamayabilir.")
    yes = box.addButton(prompt.confirm_label, QMessageBox.ButtonRole.AcceptRole)
    no = box.addButton(prompt.cancel_label, QMessageBox.ButtonRole.RejectRole)
    if prompt.danger:
        yes.setStyleSheet(f"color:{C.KIZIL};")
    box.setDefaultButton(no)
    box.setEscapeButton(no)
    box.finished.connect(lambda _r: answer(box.clickedButton() is yes))
    box.open()
    return box
# --- Aşama 5 sonu ---
