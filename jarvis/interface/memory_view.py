"""Hafıza ekranı: hatırlananları görme, düzenleme, silme; onay bekleyenler; hafızayı aç/kapat.

Taslak: `design/taslaklar/jarvis-arayuz-v1.html` (Hafıza görünümü) ve `5-hafiza.png`.
Veritabanı işleri arka plan executor'ünde yapılır; arayüz thread'i beklemez.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from PySide6.QtCore import QDate, QPoint, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (QAbstractButton, QButtonGroup, QCheckBox, QComboBox, QDateEdit, QDialog, QFrame,
                               QHBoxLayout, QLabel, QLayout, QPlainTextEdit, QPushButton, QScrollArea,
                               QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from jarvis.core.assistant import Assistant
from jarvis.core.events import Topics
from jarvis.interface.components import ICON_PATHS, icon
from jarvis.interface.theme import C, qcolor
from jarvis.memory.store import CATEGORIES, PENDING, SOURCE_USER, Memory, SecretRejected
from jarvis.memory.text import SENSITIVE_KINDS, tr_date

log = logging.getLogger(__name__)

# Taslaktaki düzenle/sil ikonları (ortak ikon tablosuna yalnızca eklenir)
ICON_PATHS.setdefault("duzenle", '<path d="M4 20h4L19 9l-4-4L4 16v4z"/>')
ICON_PATHS.setdefault("sil", '<path d="M5 7h14M10 7V4h4v3M7 7l1 13h8l1-13"/>')

FILTERS: list[tuple[str | None, str]] = [(None, "Tümü")] + [(k, v[1]) for k, v in CATEGORIES.items()]

EMPTY_TEXT = ("Henüz hatırladığım bir şey yok. Sohbet ederken kendinle ilgili önemli bir bilgi verdiğinde ya da "
              "\"bunu hatırla\" dediğinde burada görünür. Her cümleyi kaydetmem.")
EMPTY_FILTER_TEXT = "Bu kategoride kayıt yok."
OFF_TEXT = ("Hafıza kapalı: yeni bilgi kaydetmiyorum, konuşma özetlemiyorum ve kayıtları cevaplarımda kullanmıyorum. "
            "Mevcut kayıtlar silinmedi; istersen aşağıdan silebilirsin.")

STYLE = f"""
#OnayBekleyen {{ border: 1px solid rgba(243,182,72,0.4); border-radius: 14px; background: rgba(243,182,72,0.05); }}
#OnayBaslik {{ color: {C.AMBER}; font-weight: 500; font-size: 13px; }}
#Filtre {{ padding: 6px 13px; border-radius: 14px; border: 1px solid {C.CELIK}; color: {C.SIS}; font-size: 13px; }}
#Filtre:checked {{ color: {C.GECE}; background: {C.CYAN}; border-color: {C.CYAN}; }}
#HafizaSatir {{ border-bottom: 1px solid {C.CELIK}; }}
#HafizaMeta {{ color: {C.SIS}; font-size: 12px; }}
#HafizaKat {{ color: {C.SIS}; font-size: 12px; }}
#HafizaIslem {{ border: 0; padding: 6px; border-radius: 8px; }}
#HafizaIslem:hover {{ background: rgba(255,255,255,0.04); }}
#Birincil {{ border: 1px solid rgba(82,228,242,0.5); color: {C.CYAN}; background: rgba(82,228,242,0.08);
             border-radius: 10px; padding: 7px 14px; font-size: 13px; }}
#Tehlike {{ border: 1px solid rgba(255,90,106,0.55); color: {C.KIZIL}; background: rgba(255,90,106,0.08);
            border-radius: 10px; padding: 7px 14px; font-size: 13px; }}
"""


class FlowLayout(QLayout):
    """Sığmayan öğeleri alt satıra geçiren yerleşim (kategori düğmeleri için)."""

    def __init__(self, parent: QWidget | None = None, spacing: int = 8) -> None:
        super().__init__(parent)
        self._items: list = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:  # noqa: ANN001, N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, i: int):  # noqa: ANN201, N802
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i: int):  # noqa: ANN201, N802
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientation:  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._place(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._place(rect, apply=True)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for it in self._items:
            size = size.expandedTo(it.minimumSize())
        return size

    def _place(self, rect: QRect, apply: bool) -> int:
        x, y, line_h = rect.x(), rect.y(), 0
        for it in self._items:
            hint = it.sizeHint()
            if x + hint.width() > rect.right() + 1 and line_h > 0:
                x, y, line_h = rect.x(), y + line_h + self._spacing, 0
            if apply:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._spacing
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y()


class Switch(QAbstractButton):
    """Taslaktaki aç/kapa anahtarı (40x22)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(40, 22)

    def sizeHint(self) -> QSize:
        return QSize(40, 22)

    def paintEvent(self, _e) -> None:  # noqa: ANN001
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qcolor(C.CYAN if self.isChecked() else C.CELIK))
        p.drawRoundedRect(QRectF(self.rect()), 11, 11)
        p.setBrush(qcolor(C.GECE if self.isChecked() else C.SIS))
        x = self.width() - 19 if self.isChecked() else 3
        p.drawEllipse(QRectF(x, 3, 16, 16))
        p.end()


class ImportanceBar(QWidget):
    """Beş dilimli önem göstergesi."""

    def __init__(self, value: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.value = max(0, min(5, value))
        self.setFixedSize(5 * 6 + 4 * 3, 14)
        self.setToolTip(f"Önem {self.value}/5")

    def paintEvent(self, _e) -> None:  # noqa: ANN001
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(5):
            p.setBrush(qcolor(C.CYAN if i < self.value else C.CELIK))
            p.drawRoundedRect(QRectF(i * 9, 0, 6, 14), 2, 2)
        p.end()


def meta_text(m: Memory) -> str:
    if m.category == "conversation_summary":
        parts = [f"{tr_date(m.created_at)} konuşmasının özeti"]
    elif m.is_inference:
        parts = ["Çıkarım, senin onayınla"]
    elif m.source == SOURCE_USER:
        parts = ["Senin eklediğin ya da düzelttiğin bilgi"]
    else:
        parts = [f"{tr_date(m.created_at)} konuşmasından"]
    if m.use_count:
        parts.append(f"{m.use_count} kez kullanıldı")
    if m.expires_at and m.category != "conversation_summary":
        parts.append(f"geçici, {tr_date(m.expires_at)} tarihinde silinecek")
    text = ", ".join(parts)
    return text[:1].upper() + text[1:]


def pending_reason(m: Memory) -> str:
    if m.sensitive:
        what = SENSITIVE_KINDS.get(m.sensitive_kind or "other", SENSITIVE_KINDS["other"])
        return f"Bu {what} ilgili bir bilgi, onayın olmadan kaydetmiyorum."
    return "Bunu sen açıkça söylemedin, konuşmadan çıkardım. Onaylamazsan kesin bilgi olarak saklamam."


class MemoryRow(QFrame):
    def __init__(self, m: Memory, on_edit, on_delete, parent: QWidget | None = None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self.memory = m
        self.setObjectName("HafizaSatir")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 12, 4, 12)
        lay.setSpacing(18)
        col = QVBoxLayout()
        col.setSpacing(2)
        self.text = QLabel(m.content)
        self.text.setWordWrap(True)
        self.text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.meta = QLabel(meta_text(m), objectName="HafizaMeta")
        col.addWidget(self.text)
        col.addWidget(self.meta)
        lay.addLayout(col, 1)
        self.category = QLabel(m.category_label, objectName="HafizaKat")
        self.category.setFixedWidth(120)
        lay.addWidget(self.category)
        self.importance = ImportanceBar(m.importance)
        lay.addWidget(self.importance)
        lay.addSpacing(16)
        self.edit_btn = QToolButton(objectName="HafizaIslem")
        self.edit_btn.setIcon(icon("duzenle", C.SIS, active=C.BUZ, size=16, stroke=1.7))
        self.edit_btn.setToolTip("Düzenle")
        self.edit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.edit_btn.clicked.connect(lambda: on_edit(m))
        self.delete_btn = QToolButton(objectName="HafizaIslem")
        self.delete_btn.setIcon(icon("sil", C.SIS, active=C.KIZIL, size=16, stroke=1.7))
        self.delete_btn.setToolTip("Sil")
        self.delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.delete_btn.clicked.connect(lambda: on_delete(m))
        lay.addWidget(self.edit_btn)
        lay.addWidget(self.delete_btn)


class PendingCard(QFrame):
    remember = Signal(int)
    discard = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("OnayBekleyen")
        self.memory: Memory | None = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(18)
        col = QVBoxLayout()
        col.setSpacing(6)
        self.title = QLabel("Bunu hatırlamamı ister misin?", objectName="OnayBaslik")
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        col.addWidget(self.title)
        col.addWidget(self.text)
        lay.addLayout(col, 1)
        self.discard_btn = QPushButton("Kaydetme", objectName="Dugme")
        self.remember_btn = QPushButton("Hatırla", objectName="Birincil")
        for b in (self.discard_btn, self.remember_btn):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            lay.addWidget(b, 0, Qt.AlignmentFlag.AlignVCenter)
        self.discard_btn.clicked.connect(lambda: self.memory and self.discard.emit(self.memory.id))
        self.remember_btn.clicked.connect(lambda: self.memory and self.remember.emit(self.memory.id))
        self.hide()

    def show_memory(self, m: Memory | None, total: int) -> None:
        self.memory = m
        if m is None:
            self.hide()
            return
        more = f" ({total} bilgi onay bekliyor)" if total > 1 else ""
        self.title.setText("Bunu hatırlamamı ister misin?" + more)
        self.text.setText(f"“{m.content}” {pending_reason(m)}")
        self.show()


class MemoryEditDialog(QDialog):
    """Kaydı düzeltme penceresi. Kaydedilen metin artık senin onayladığın bilgi sayılır."""

    def __init__(self, m: Memory, save, spawn, parent: QWidget | None = None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self.memory, self._save, self._spawn = m, save, spawn
        self.setWindowTitle("Hafıza kaydını düzenle")
        self.setModal(True)
        self.setMinimumWidth(520)
        self.setStyleSheet(f"QDialog {{ background: {C.DERINLIK}; }}"
                           f"QPlainTextEdit {{ border:1px solid {C.CELIK}; border-radius:10px; padding:8px; }}"
                           f"QDateEdit {{ border:1px solid {C.CELIK}; border-radius:8px; padding:6px 10px; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 22, 24, 20)
        lay.setSpacing(10)
        title = QLabel("Kaydı düzenle", objectName="BosBaslik")
        title.setStyleSheet("font-size: 20px;")
        lay.addWidget(title)
        self.content = QPlainTextEdit(m.content)
        self.content.setFixedHeight(90)
        lay.addWidget(self.content)
        row = QHBoxLayout()
        self.category = QComboBox()
        for key, (label, _) in CATEGORIES.items():
            self.category.addItem(label, key)
        self.category.setCurrentIndex(list(CATEGORIES).index(m.category) if m.category in CATEGORIES else 0)
        self.importance = QComboBox()
        for i in range(1, 6):
            self.importance.addItem(f"Önem {i}/5", i)
        self.importance.setCurrentIndex(m.importance - 1)
        row.addWidget(self.category, 1)
        row.addWidget(self.importance)
        lay.addLayout(row)
        exp_row = QHBoxLayout()
        self.temporary = QCheckBox("Geçici: bu tarihte sil")
        self.expires = QDateEdit()
        self.expires.setCalendarPopup(True)
        self.expires.setDisplayFormat("dd.MM.yyyy")
        self.expires.setMinimumDate(QDate.currentDate().addDays(1))
        if m.expires_at:
            d = m.expires_at.astimezone()
            self.expires.setDate(QDate(d.year, d.month, d.day))
        else:
            self.expires.setDate(QDate.currentDate().addDays(30))
        self.temporary.setChecked(m.expires_at is not None)
        self.expires.setEnabled(self.temporary.isChecked())
        self.temporary.toggled.connect(self.expires.setEnabled)
        exp_row.addWidget(self.temporary)
        exp_row.addWidget(self.expires)
        exp_row.addStretch(1)
        lay.addLayout(exp_row)
        self.result = QLabel("")
        self.result.setWordWrap(True)
        lay.addWidget(self.result)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel = QPushButton("Vazgeç", objectName="Dugme")
        self.ok = QPushButton("Kaydet", objectName="Birincil")
        self.cancel.clicked.connect(self.reject)
        self.ok.clicked.connect(self._submit)
        buttons.addWidget(self.cancel)
        buttons.addWidget(self.ok)
        lay.addLayout(buttons)

    def _expires_value(self) -> datetime | None:
        if not self.temporary.isChecked():
            return None
        d = self.expires.date()
        # Seçilen günün sonuna kadar geçerli (yerel saat)
        return datetime(d.year(), d.month(), d.day(), 23, 59).astimezone().astimezone(timezone.utc)

    def _submit(self) -> None:
        text = self.content.toPlainText().strip()
        if not text:
            self.result.setStyleSheet(f"color:{C.KIZIL};")
            self.result.setText("Kayıt boş olamaz.")
            return
        self.ok.setEnabled(False)
        self._spawn(self._do(text), "hafiza-duzenle")

    async def _do(self, text: str) -> None:
        try:
            await self._save(self.memory.id, content=text, category=self.category.currentData(),
                             importance=self.importance.currentData(), expires_at=self._expires_value())
        except SecretRejected as e:
            self.result.setStyleSheet(f"color:{C.KIZIL};")
            self.result.setText(e.user_message)
            self.ok.setEnabled(True)
            return
        except ValueError as e:
            self.result.setStyleSheet(f"color:{C.KIZIL};")
            self.result.setText(str(e))
            self.ok.setEnabled(True)
            return
        self.accept()


class DeleteDialog(QDialog):
    """Taslaktaki silme onayı (kırmızı çerçeve)."""

    def __init__(self, m: Memory, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.memory = m
        self.setWindowTitle("Hafıza kaydını sil")
        self.setModal(True)
        self.setMinimumWidth(460)
        self.setStyleSheet(f"QDialog {{ background: {C.DERINLIK}; border: 1px solid rgba(255,90,106,0.45); }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 24, 26, 22)
        lay.setSpacing(12)
        title = QLabel("Bu kayıt silinsin mi?", objectName="BosBaslik")
        title.setStyleSheet("font-size: 20px;")
        body = QLabel("Kayıt hafızadan kalıcı olarak silinir ve geri alınamaz.", objectName="BosMetin")
        body.setWordWrap(True)
        quote = QLabel(m.content)
        quote.setWordWrap(True)
        quote.setStyleSheet(f"background: {C.GECE}; border-radius: 10px; padding: 10px 14px;")
        lay.addWidget(title)
        lay.addWidget(body)
        lay.addWidget(quote)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel = QPushButton("Vazgeç", objectName="Dugme")
        self.ok = QPushButton("Sil", objectName="Tehlike")
        self.cancel.clicked.connect(self.reject)
        self.ok.clicked.connect(self.accept)
        buttons.addWidget(self.cancel)
        buttons.addWidget(self.ok)
        lay.addLayout(buttons)


class MemoryView(QWidget):
    def __init__(self, assistant: Assistant, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.assistant = assistant
        self.service = assistant.memory
        self.setStyleSheet(STYLE)
        self._memories: list[Memory] = []
        self._pending: list[Memory] = []
        self._filter: str | None = None
        self._dirty = True
        self._dialog: QDialog | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        body.setMaximumWidth(920)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(36, 26, 36, 30)
        lay.setSpacing(22)
        scroll.setWidget(body)
        outer.addWidget(scroll)

        header = QHBoxLayout()
        header.addWidget(QLabel("Hafıza", objectName="BosBaslik"))
        header.addStretch(1)
        self.switch_label = QLabel(objectName="HafizaMeta")
        self.switch_label.setStyleSheet(f"color:{C.SIS}; font-size: 13px;")
        self.switch = Switch()
        self.switch.setChecked(assistant.settings.memory.enabled)
        self.switch.toggled.connect(self._toggle)
        header.addWidget(self.switch_label)
        header.addWidget(self.switch)
        lay.addLayout(header)

        self.off_note = QLabel(OFF_TEXT, objectName="BosMetin")
        self.off_note.setWordWrap(True)
        lay.addWidget(self.off_note)

        self.card = PendingCard()
        self.card.remember.connect(lambda mid: self.assistant.spawn(self._confirm(mid), "hafiza-onay"))
        self.card.discard.connect(lambda mid: self.assistant.spawn(self._discard(mid), "hafiza-red"))
        lay.addWidget(self.card)

        chips_box = QWidget()
        chips = FlowLayout(chips_box)
        self.filter_group = QButtonGroup(self)
        self.filter_group.setExclusive(True)
        self.filter_buttons: dict[str | None, QPushButton] = {}
        for key, label in FILTERS:
            b = QPushButton(label, objectName="Filtre")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=key: self.set_filter(k))
            self.filter_group.addButton(b)
            self.filter_buttons[key] = b
            chips.addWidget(b)
        self.filter_buttons[None].setChecked(True)
        lay.addWidget(chips_box)

        self.list_box = QWidget()
        self.list_box.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self.list_layout = QVBoxLayout(self.list_box)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(0)
        self.list_box.setStyleSheet(f"#HafizaListe {{ border-top: 1px solid {C.CELIK}; }}")
        self.list_box.setObjectName("HafizaListe")
        lay.addWidget(self.list_box)
        self.empty = QLabel(EMPTY_TEXT, objectName="BosMetin")
        self.empty.setWordWrap(True)
        lay.addWidget(self.empty)
        lay.addStretch(1)
        self.rows: list[MemoryRow] = []

        self._apply_enabled(assistant.settings.memory.enabled)
        assistant.bus.subscribe(Topics.MEMORY_CHANGED, self._on_changed)
        assistant.bus.subscribe(Topics.SETTINGS_CHANGED, lambda s: self._sync_switch(s.memory.enabled))

    # ---------- veri ----------
    async def refresh(self) -> None:
        self._dirty = False
        items = await self.service.list_memories()
        self._pending = [m for m in items if m.status == PENDING]
        self._memories = [m for m in items if m.status != PENDING]
        self._render()

    def _on_changed(self, _payload=None) -> None:  # noqa: ANN001
        if self.isVisible():
            self.assistant.spawn(self.refresh(), "hafiza-yenile")
        else:
            self._dirty = True

    def showEvent(self, event) -> None:  # noqa: ANN001
        super().showEvent(event)
        if self._dirty:
            self.assistant.spawn(self.refresh(), "hafiza-yenile")

    def _render(self) -> None:
        self.card.show_memory(self._pending[0] if self._pending else None, len(self._pending))
        for r in self.rows:
            r.setParent(None)
            r.deleteLater()
        self.rows = []
        shown = [m for m in self._memories if self._filter is None or m.category == self._filter]
        for m in shown:
            row = MemoryRow(m, self.open_edit, self.ask_delete)
            self.list_layout.addWidget(row)
            self.rows.append(row)
        if not self._memories:
            self.empty.setText(EMPTY_TEXT)
        else:
            self.empty.setText(EMPTY_FILTER_TEXT)
        self.empty.setVisible(not shown)

    def set_filter(self, key: str | None) -> None:
        self._filter = key
        self.filter_buttons[key].setChecked(True)
        self._render()

    # ---------- işlemler ----------
    async def _confirm(self, memory_id: int) -> None:
        await self.service.confirm(memory_id)
        await self.refresh()

    async def _discard(self, memory_id: int) -> None:
        await self.service.delete(memory_id)
        await self.refresh()

    def open_edit(self, m: Memory) -> None:
        dlg = MemoryEditDialog(m, self.service.update, self.assistant.spawn, self)
        dlg.accepted.connect(lambda: self.assistant.spawn(self.refresh(), "hafiza-yenile"))
        dlg.open()
        self._dialog = dlg

    def ask_delete(self, m: Memory) -> None:
        dlg = DeleteDialog(m, self)
        dlg.accepted.connect(lambda: self.assistant.spawn(self._discard(m.id), "hafiza-sil"))
        dlg.open()   # exec() asyncio döngüsünü iç içe kilitlerdi
        self._dialog = dlg

    def _toggle(self, enabled: bool) -> None:
        if enabled != self.assistant.settings.memory.enabled:
            self.service.set_enabled(enabled)
        self._apply_enabled(enabled)

    def _sync_switch(self, enabled: bool) -> None:
        if self.switch.isChecked() != enabled:
            self.switch.blockSignals(True)
            self.switch.setChecked(enabled)
            self.switch.blockSignals(False)
            self.switch.update()
        self._apply_enabled(enabled)

    def _apply_enabled(self, enabled: bool) -> None:
        self.switch_label.setText("Hafıza açık" if enabled else "Hafıza kapalı")
        self.switch.setToolTip("Hafızayı kapat" if enabled else "Hafızayı aç")
        self.off_note.setVisible(not enabled)
