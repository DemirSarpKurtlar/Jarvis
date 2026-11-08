"""Rutinler bölümü (Otomasyon ekranının altında) ve sabah brifingi ayarları.

- Rutin listesi: aç/kapat, çalıştır (onaylı adımlar tek pencerede sorulur), düzenle, sil.
- Basit düzenleyici: ad, tetikleyici sözler (virgülle), adımlar (araç seçici + JSON girdi). Kaydederken
  her adım araç şemasına göre doğrulanır; hata adımı numarasıyla gösterilir.
- Sabah brifingi: hava durumu şehri (doğrulanarak kaydedilir), otomatik brifing, haber kaynağı.

Çalıştırma ve şehir kaydı araç kaydı üzerinden (izin, onay ve denetim kaydıyla) yapılır.
"""

from __future__ import annotations

import json
import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPushButton, QScrollArea, QVBoxLayout, QWidget)

from jarvis.core.events import Topics
from jarvis.interface.memory_view import Switch
from jarvis.interface.theme import C
from jarvis.security.permission_manager import Permission
from jarvis.tools.briefing import NEWS_SOURCES
from jarvis.tools.registry import ToolError
from jarvis.tools.routines import BLOCKED_STEP_TOOLS, PSEUDO_STEPS, SAY, WAIT, Routine, describe_step

log = logging.getLogger(__name__)

EDIT_STYLE = f"border:1px solid {C.CELIK}; border-radius:8px; padding:6px 10px;"
# Düzenleyicide önce gösterilecek, rutinlerde en çok kullanılan araçlar
COMMON_TOOLS = ["open_app", "close_app", "switch_to_app", "launch_game", "set_volume", "change_volume", "set_mute",
                "open_url", "web_search_in_browser", "open_settings", "lock_screen", "morning_briefing"]


def default_args(schema: dict) -> dict:
    """Şemadan doldurulacak örnek girdi: metin "", sayı en küçük değer, enum ilk seçenek, mantıksal False."""
    out: dict = {}
    for key, sub in (schema.get("properties") or {}).items():
        if "enum" in sub:
            out[key] = sub["enum"][0]
        elif sub.get("type") == "integer":
            lo = sub.get("minimum", 0)
            out[key] = 0 if lo <= 0 <= sub.get("maximum", lo) else lo
        elif sub.get("type") == "boolean":
            out[key] = False
        else:
            out[key] = ""
    return out


def step_choices(registry) -> list[tuple[str, str]]:  # noqa: ANN001
    """(etiket, araç adı) listesi: sözde adımlar + sık kullanılanlar + diğer izinli araçlar."""
    items = [(f"{PSEUDO_STEPS[WAIT][0]} (saniye)", WAIT), (PSEUDO_STEPS[SAY][0], SAY)]
    specs = [s for s in registry.specs() if s.name not in BLOCKED_STEP_TOOLS and s.permission is not Permission.FORBIDDEN]
    by_name = {s.name: s for s in specs}
    for name in COMMON_TOOLS:
        if name in by_name:
            s = by_name.pop(name)
            items.append((s.title + ("" if s.supported else " (bu sistemde çalışmaz)"), s.name))
    for s in sorted(by_name.values(), key=lambda x: x.title):
        items.append((s.title + ("" if s.supported else " (bu sistemde çalışmaz)"), s.name))
    return items


class StepRow(QWidget):
    def __init__(self, choices: list[tuple[str, str]], registry, step: dict | None, on_remove,  # noqa: ANN001
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.registry = registry
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.tool = QComboBox()
        for label, name in choices:
            self.tool.addItem(label, name)
        self.args = QLineEdit()
        self.args.setStyleSheet(EDIT_STYLE)
        self.args.setPlaceholderText('{"name": "Spotify"}')
        self.remove = QPushButton("Sil", objectName="Dugme")
        self.remove.clicked.connect(lambda: on_remove(self))
        self.tool.setMaximumWidth(250)
        lay.addWidget(self.tool, 1)
        lay.addWidget(self.args, 2)
        lay.addWidget(self.remove)
        if step is not None:
            i = self.tool.findData(step.get("tool"))
            if i < 0:   # kayıtta olup bu sürümde olmayan araç: yine de göster
                self.tool.addItem(str(step.get("tool")), step.get("tool"))
                i = self.tool.count() - 1
            self.tool.setCurrentIndex(i)
            self.args.setText(json.dumps(step.get("args") or {}, ensure_ascii=False))
        else:
            self._fill_template()
        self.tool.currentIndexChanged.connect(self._fill_template)

    def _fill_template(self) -> None:
        name = self.tool.currentData()
        if name in PSEUDO_STEPS:
            schema = PSEUDO_STEPS[name][1]
        else:
            spec = self.registry.get(name)
            schema = spec.input_schema if spec is not None else {}
        self.args.setText(json.dumps(default_args(schema), ensure_ascii=False))

    def value(self, index: int) -> dict:
        text = self.args.text().strip() or "{}"
        try:
            args = json.loads(text)
        except ValueError:
            raise ToolError("json", f"{index}. adımın girdisi geçerli JSON değil.") from None
        return {"tool": self.tool.currentData(), "args": args}


class RoutineEditor(QDialog):
    """Yeni rutin ya da mevcut rutini düzenleme penceresi."""

    def __init__(self, store, registry, routine: Routine | None = None, on_saved=None,  # noqa: ANN001
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.store, self.registry, self.routine, self.on_saved = store, registry, routine, on_saved
        self.setWindowTitle("Rutini düzenle" if routine else "Yeni rutin")
        self.setMinimumWidth(720)
        self.setStyleSheet(f"QDialog {{ background: {C.DERINLIK}; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(10)
        lay.addWidget(QLabel("Ad"))
        self.name = QLineEdit(routine.name if routine else "")
        self.name.setPlaceholderText("ör. Çalışma modu")
        self.name.setStyleSheet(EDIT_STYLE)
        lay.addWidget(self.name)
        lay.addWidget(QLabel("Tetikleyici sözler (virgülle ayır)"))
        self.phrases = QLineEdit(", ".join(routine.phrases) if routine else "")
        self.phrases.setPlaceholderText("ör. çalışma modu, çalışma zamanı")
        self.phrases.setStyleSheet(EDIT_STYLE)
        lay.addWidget(self.phrases)
        lay.addWidget(QLabel("Adımlar (sırayla çalışır; onay gerektirenler rutin başlarken tek pencerede sorulur)"))
        self.choices = step_choices(registry)
        self.steps_box = QVBoxLayout()
        self.steps_box.setSpacing(6)
        lay.addLayout(self.steps_box)
        self.rows: list[StepRow] = []
        for s in (routine.steps if routine else []):
            self.add_step(s)
        if not self.rows:
            self.add_step(None)
        add = QPushButton("Adım ekle", objectName="Dugme")
        add.clicked.connect(lambda: self.add_step(None))
        lay.addWidget(add, 0, Qt.AlignmentFlag.AlignLeft)
        self.enabled = QCheckBox("Açık (tetikleyici sözlerle çalışsın)")
        self.enabled.setChecked(routine.enabled if routine else True)
        lay.addWidget(self.enabled)
        self.error = QLabel("")
        self.error.setWordWrap(True)
        self.error.setStyleSheet(f"color:{C.AMBER};")
        lay.addWidget(self.error)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Vazgeç", objectName="Dugme")
        cancel.clicked.connect(self.reject)
        self.save_btn = QPushButton("Kaydet", objectName="KonusDugmesi")
        self.save_btn.clicked.connect(self.save)
        buttons.addWidget(cancel)
        buttons.addWidget(self.save_btn)
        lay.addLayout(buttons)

    def add_step(self, step: dict | None) -> StepRow:
        row = StepRow(self.choices, self.registry, step, self._remove)
        self.rows.append(row)
        self.steps_box.addWidget(row)
        return row

    def _remove(self, row: StepRow) -> None:
        if row in self.rows:
            self.rows.remove(row)
            row.hide()
            row.deleteLater()

    def save(self) -> bool:
        try:
            phrases = [p.strip() for p in self.phrases.text().split(",") if p.strip()]
            steps = [r.value(i) for i, r in enumerate(self.rows, 1)]
            if self.routine is None:
                saved = self.store.add(self.name.text(), phrases, steps, enabled=self.enabled.isChecked())
            else:
                saved = self.store.update(self.routine.id, self.name.text(), phrases, steps,
                                          enabled=self.enabled.isChecked())
        except ToolError as e:
            self.error.setText(e.user_message)
            return False
        except Exception:  # noqa: BLE001
            log.exception("Rutin kaydedilemedi")
            self.error.setText("Rutin kaydedilemedi.")
            return False
        if self.on_saved:
            self.on_saved(saved)
        self.accept()
        return True


class BriefingDialog(QDialog):
    """Sabah brifingi ayarları: şehir (doğrulanarak), otomatik brifing, haberler; brifingi hemen deneme."""

    def __init__(self, assistant, parent: QWidget | None = None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self.assistant = assistant
        s = assistant.settings.briefing
        self.setWindowTitle("Sabah brifingi")
        self.setMinimumWidth(560)
        self.setStyleSheet(f"QDialog {{ background: {C.DERINLIK}; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(10)
        info = QLabel("\"Günaydın Jarvis\" ya da \"sabah brifingi\" dediğinde tarih, hava durumu, bilgisayarın "
                      "durumu, bugünkü görevler ve haber başlıkları okunur. Hava durumu open-meteo.com'dan, "
                      "başlıklar seçtiğin haber kaynağının RSS akışından gelir; ikisi de ücretsiz ve anahtarsız.",
                      objectName="BosMetin")
        info.setWordWrap(True)
        lay.addWidget(info)
        row = QHBoxLayout()
        row.addWidget(QLabel("Şehir"))
        self.city = QLineEdit(s.city)
        self.city.setPlaceholderText("ör. Ankara")
        self.city.setStyleSheet(EDIT_STYLE)
        self.city_btn = QPushButton("Kaydet", objectName="Dugme")
        self.city_btn.clicked.connect(self.save_city)
        row.addWidget(self.city, 1)
        row.addWidget(self.city_btn)
        lay.addLayout(row)
        self.auto = QCheckBox("Günün ilk açılışında brifingi kendiliğinden ver (05:00–12:00, günde bir kez)")
        self.auto.setChecked(s.auto_enabled)
        self.auto.toggled.connect(self._save_flags)
        lay.addWidget(self.auto)
        nrow = QHBoxLayout()
        self.news = QCheckBox("Haber başlıkları")
        self.news.setChecked(s.news_enabled)
        self.news.toggled.connect(self._save_flags)
        self.source = QComboBox()
        for key, (name, _url) in NEWS_SOURCES.items():
            self.source.addItem(name, key)
        self.source.setCurrentIndex(max(0, self.source.findData(s.news_source)))
        self.source.currentIndexChanged.connect(self._save_flags)
        nrow.addWidget(self.news)
        nrow.addWidget(self.source, 1)
        lay.addLayout(nrow)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.status)
        buttons = QHBoxLayout()
        self.try_btn = QPushButton("Brifingi şimdi al", objectName="Dugme")
        self.try_btn.clicked.connect(self.try_now)
        close = QPushButton("Kapat", objectName="KonusDugmesi")
        close.clicked.connect(self.accept)
        buttons.addWidget(self.try_btn)
        buttons.addStretch(1)
        buttons.addWidget(close)
        lay.addLayout(buttons)

    def _save_flags(self, *_a) -> None:  # noqa: ANN002
        s = self.assistant.settings.briefing
        s.auto_enabled = self.auto.isChecked()
        s.news_enabled = self.news.isChecked()
        s.news_source = self.source.currentData()
        self.source.setEnabled(s.news_enabled)
        try:
            self.assistant.store.save()
        except OSError:
            self.status.setText("Ayar kaydedilemedi.")

    def _show(self, task, ok_color: str = C.CYAN) -> None:  # noqa: ANN001
        self.city_btn.setEnabled(True)
        self.try_btn.setEnabled(True)
        if task.cancelled() or task.exception() is not None:
            self.status.setText("İşlem tamamlanamadı.")
            return
        res = task.result()
        self.status.setStyleSheet(f"color:{ok_color if res.ok else C.AMBER};")
        self.status.setText(res.summary if res.ok else (res.error or res.summary))
        if res.ok and res.tool == "set_briefing_city":
            self.city.setText(self.assistant.settings.briefing.city)

    def save_city(self) -> None:
        self.city_btn.setEnabled(False)
        self.status.setStyleSheet(f"color:{C.SIS};")
        self.status.setText("Şehir doğrulanıyor…")
        task = self.assistant.run_tool("set_briefing_city", {"city": self.city.text().strip()[:80]}, "arayuz")
        task.add_done_callback(self._show)

    def try_now(self) -> None:
        self.try_btn.setEnabled(False)
        self.status.setStyleSheet(f"color:{C.SIS};")
        self.status.setText("Brifing hazırlanıyor…")
        task = self.assistant.run_tool("morning_briefing", {}, "arayuz")
        task.add_done_callback(self._show)


class RoutinesPanel(QWidget):
    def __init__(self, assistant, parent: QWidget | None = None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self.assistant = assistant
        self._dialog: QDialog | None = None
        self._box: QMessageBox | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        head = QHBoxLayout()
        head.addWidget(QLabel("Rutinler", objectName="SeritBaslik"))
        head.addStretch(1)
        self.brief_btn = QPushButton("Sabah brifingi…", objectName="Dugme")
        self.brief_btn.clicked.connect(self.open_briefing)
        self.new_btn = QPushButton("Yeni rutin", objectName="KonusDugmesi")
        self.new_btn.clicked.connect(lambda: self.open_editor(None))
        head.addWidget(self.brief_btn)
        head.addWidget(self.new_btn)
        lay.addLayout(head)
        intro = QLabel("Tek sözle çalışan adım listeleri (\"çalışma modu\" → VS Code ve Spotify açılsın, ses %30). "
                       "Örnekler kapalı gelir: kendi uygulamalarına göre düzenleyip aç.", objectName="BosMetin")
        intro.setWordWrap(True)
        intro.setMaximumWidth(760)
        lay.addWidget(intro)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setMinimumHeight(140)
        body = QWidget()
        self.list_box = QVBoxLayout(body)
        self.list_box.setContentsMargins(0, 0, 0, 0)
        self.list_box.setSpacing(4)
        self.list_box.addStretch(1)
        scroll.setWidget(body)
        lay.addWidget(scroll, 1)
        self.empty = QLabel("Henüz rutin yok.", objectName="BosMetin")
        lay.addWidget(self.empty)
        self.result = QLabel("", objectName="Not")
        self.result.setWordWrap(True)
        lay.addWidget(self.result)
        assistant.bus.subscribe(Topics.TOOL_EXECUTED, self._on_tool)

    @property
    def store(self):  # noqa: ANN201
        return self.assistant.tool_service.ctx.extra.get("routines")

    @property
    def registry(self):  # noqa: ANN201
        return self.assistant.tools

    def rows(self) -> list[QWidget]:
        return [self.list_box.itemAt(i).widget() for i in range(self.list_box.count())
                if self.list_box.itemAt(i).widget() is not None]

    def refresh(self) -> None:
        for w in self.rows():
            self.list_box.removeWidget(w)
            w.hide()
            w.deleteLater()
        routines = self.store.list() if self.store is not None else []
        self.empty.setVisible(not routines)
        for r in routines:
            self.list_box.insertWidget(self.list_box.count() - 1, self._row(r))

    def _row(self, r: Routine) -> QWidget:
        w = QFrame(objectName="RutinSatiri")
        w.setProperty("routine_id", r.id)
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 6, 0, 6)
        sw = Switch()
        sw.setToolTip("Açık: tetikleyici sözlerle çalışır / Kapalı: hiç çalışmaz")
        sw.setChecked(r.enabled)
        sw.toggled.connect(lambda on, rid=r.id: self.set_enabled(rid, on))
        steps = " → ".join(describe_step(self.registry, s) for s in r.steps[:6]) + (" …" if len(r.steps) > 6 else "")
        phrases = ", ".join(f"“{p}”" for p in r.phrases) or "yalnızca adıyla"
        lab = QLabel(f"<b>{_esc(r.name)}</b> <span style='color:{C.SIS}'>({_esc(phrases)})</span><br>"
                     f"<span style='color:{C.SIS}; font-size:12px'>{_esc(steps)}</span>")
        lab.setTextFormat(Qt.TextFormat.RichText)
        lab.setWordWrap(True)
        run = QPushButton("Çalıştır", objectName="Dugme")
        run.setEnabled(r.enabled)
        if not r.enabled:
            run.setToolTip("Önce rutini aç")
            run.setStyleSheet(f"color:{C.CELIK};")
        run.clicked.connect(lambda _=False, name=r.name: self.run(name))
        edit = QPushButton("Düzenle", objectName="Dugme")
        edit.clicked.connect(lambda _=False, rid=r.id: self.open_editor(rid))
        delete = QPushButton("Sil", objectName="Dugme")
        delete.clicked.connect(lambda _=False, rid=r.id, name=r.name: self.ask_delete(rid, name))
        h.addWidget(sw)
        h.addWidget(lab, 1)
        for b in (run, edit, delete):
            h.addWidget(b)
        return w

    # ---------- eylemler ----------
    def set_enabled(self, routine_id: int, enabled: bool) -> None:
        if self.store is not None:
            self.store.set_enabled(routine_id, enabled)
        self.refresh()

    def run(self, name: str) -> None:
        self.result.setStyleSheet(f"color:{C.SIS};")
        self.result.setText(f"'{name}' çalışıyor…")
        task = self.assistant.run_tool("run_routine", {"name": name}, "arayuz")
        task.add_done_callback(self._after_run)

    def _after_run(self, task) -> None:  # noqa: ANN001
        if task.cancelled() or task.exception() is not None:
            self.result.setText("Rutin çalıştırılamadı.")
            return
        res = task.result()
        self.result.setStyleSheet(f"color:{C.CYAN if res.ok else C.AMBER};")
        self.result.setText(res.summary if res.ok else (res.error or res.summary))

    def open_editor(self, routine_id: int | None) -> RoutineEditor | None:
        if self.store is None:
            self.result.setText("Rutin deposu hazır değil.")
            return None
        routine = self.store.get(routine_id) if routine_id is not None else None
        dlg = RoutineEditor(self.store, self.registry, routine, lambda _r: self.refresh(), self)
        self._dialog = dlg
        dlg.open()
        return dlg

    def open_briefing(self) -> BriefingDialog:
        dlg = BriefingDialog(self.assistant, self)
        self._dialog = dlg
        dlg.open()
        return dlg

    def ask_delete(self, routine_id: int, name: str) -> QMessageBox:
        box = QMessageBox(self)
        box.setWindowTitle("Rutini sil")
        box.setText(f"'{name}' rutini silinsin mi? Bu geri alınamaz.")
        box.setIcon(QMessageBox.Icon.Question)
        yes = box.addButton("Sil", QMessageBox.ButtonRole.AcceptRole)
        no = box.addButton("Vazgeç", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(no)
        box.setEscapeButton(no)
        box.finished.connect(lambda _r: self.delete(routine_id) if box.clickedButton() is yes else None)
        self._box = box
        box.open()
        return box

    def delete(self, routine_id: int) -> None:
        if self.store is not None and self.store.delete(routine_id):
            self.result.setStyleSheet(f"color:{C.SIS};")
            self.result.setText("Rutin silindi.")
        self.refresh()

    def _on_tool(self, result) -> None:  # noqa: ANN001
        if getattr(result, "tool", "") in ("create_routine", "run_routine") and self.isVisible():
            self.refresh()

    def showEvent(self, event) -> None:  # noqa: ANN001
        super().showEvent(event)
        self.refresh()


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
