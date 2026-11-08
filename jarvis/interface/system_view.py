"""Sistem ekranı: işlemler, donanım ayrıntıları, kurulu uygulamalar ve denetim kaydı.

Tüm okuma işleri araç thread'lerinde yapılır; arayüz thread'i beklemez.
İşlem sonlandırma ve uygulama açma araç kaydından geçer: izin denetimi,
onay penceresi ve denetim kaydı diğer yollarla aynıdır.
"""

from __future__ import annotations

import html
import logging
import sys
from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QPushButton, QTableWidget, QTableWidgetItem, QTabWidget,
                               QVBoxLayout, QWidget)

from jarvis.core.events import Topics
from jarvis.interface.theme import C
from jarvis.system.monitor import SystemSnapshot
from jarvis.tools import hardware, processes, system_info

log = logging.getLogger(__name__)

PROCESS_REFRESH_MS = 4000

_TABLE_CSS = (f"QTableWidget {{ border:1px solid {C.CELIK}; border-radius:12px; gridline-color:{C.CELIK}; }}"
              f"QHeaderView::section {{ background:{C.DERINLIK}; color:{C.SIS}; border:0; padding:6px; }}"
              f"QTableWidget::item:selected {{ background: rgba(82,228,242,0.12); color:{C.BUZ}; }}")


def _fmt(v, unit: str = "", digits: int = 0) -> str:  # noqa: ANN001
    if v is None:
        return "okunamıyor"
    return f"{v:.{digits}f}{unit}" if isinstance(v, float) else f"{v}{unit}"


def hardware_html(snap: SystemSnapshot | None, cpu: dict | None, disks: list[dict] | None, net: list[dict] | None,
                  overview: dict | None) -> str:
    """Donanım sekmesinin içeriği. Okunamayan değerler "okunamıyor" yazar, uydurulmaz."""
    rows: list[tuple[str, str]] = []
    if cpu:
        rows.append(("İşlemci", html.escape(cpu.get("name") or "")))
        rows.append(("Çekirdek", f"{cpu.get('physical_cores') or '?'} fiziksel, {cpu.get('logical_cores') or '?'} mantıksal"))
        if cpu.get("frequency_mhz"):
            rows.append(("Frekans", f"{cpu['frequency_mhz']} MHz"))
        if cpu.get("temperature_c") is not None:
            rows.append(("CPU sıcaklığı", f"{cpu['temperature_c']} °C ({html.escape(cpu.get('temperature_source', ''))})"))
        else:
            rows.append(("CPU sıcaklığı", "desteklenmiyor: " + html.escape(cpu.get("temperature_unavailable", ""))))
    if snap is not None:
        rows.append(("CPU kullanımı", f"%{snap.cpu_percent:.0f}"))
        rows.append(("Bellek", f"{snap.ram_used_gb:.1f} / {snap.ram_total_gb:.1f} GB (%{snap.ram_percent:.0f})"))
        if snap.gpu is not None:
            g = snap.gpu
            rows.append(("Ekran kartı", html.escape(g.name)))
            rows.append(("GPU sıcaklığı", _fmt(g.temperature_c, " °C")))
            rows.append(("GPU kullanımı", _fmt(g.utilization_pct, " %")))
            mem = (f"{g.memory_used_mb / 1024:.1f} / {g.memory_total_mb / 1024:.1f} GB"
                   if g.memory_used_mb is not None and g.memory_total_mb else "okunamıyor")
            rows.append(("GPU belleği", mem))
            rows.append(("GPU fanı", _fmt(g.fan_pct, " %") if g.fan_pct is not None else "bu kartta okunamıyor"))
        else:
            rows.append(("Ekran kartı", html.escape(snap.gpu_unavailable_reason or "bilgi yok")))
        if snap.net_down_bps is not None:
            rows.append(("Ağ hızı", f"↓ {snap.net_down_bps / 1e6:.1f}  ↑ {(snap.net_up_bps or 0) / 1e6:.1f} Mbit/s"))
    for d in disks or []:
        rows.append((f"Disk {html.escape(d['mount'])}", f"{d['free_gb']:.0f} GB boş / {d['total_gb']:.0f} GB "
                                                        f"(%{d['percent']:.0f} dolu)"))
    for n in net or []:
        rows.append((f"Ağ: {html.escape(n['interface'])}", html.escape(", ".join(n["ipv4"] + n["ipv6"]) or "-")))
    if overview:
        rows.append(("İşletim sistemi", html.escape(overview["os"])))
        rows.append(("Çalışma süresi", html.escape(overview["uptime_text"])))
        b = overview.get("battery") or {}
        rows.append(("Pil", f"%{b['percent']}" + (" (şarjda)" if b.get("plugged_in") else "") if b.get("present")
                     else "yok"))
    if not rows:
        return f"<p style='color:{C.SIS}'>Ölçümler yükleniyor…</p>"
    body = "".join(f"<tr><td style='color:{C.SIS};padding:4px 18px 4px 0'>{k}</td>"
                   f"<td style='color:{C.BUZ};padding:4px 0'>{v}</td></tr>" for k, v in rows)
    return f"<table>{body}</table>"


class SystemView(QWidget):
    def __init__(self, assistant, parent: QWidget | None = None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self.assistant = assistant
        self._snap: SystemSnapshot | None = None
        self._proc_loading = False
        self._static: dict = {}

        lay = QVBoxLayout(self)
        lay.setContentsMargins(40, 30, 40, 30)
        top = QHBoxLayout()
        top.addWidget(QLabel("Sistem", objectName="BosBaslik"))
        top.addStretch(1)
        self.refresh_button = QPushButton("Yenile", objectName="Dugme")
        self.refresh_button.clicked.connect(self.refresh)
        top.addWidget(self.refresh_button)
        lay.addLayout(top)
        self.status = QLabel("", objectName="Not")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        self.tabs = QTabWidget()
        lay.addWidget(self.tabs, 1)

        # İşlemler
        pw = QWidget()
        pl = QVBoxLayout(pw)
        row = QHBoxLayout()
        self.proc_filter = QLineEdit(objectName="Giris")
        self.proc_filter.setPlaceholderText("İşlem adına göre süz")
        self.proc_filter.textChanged.connect(self._apply_filter)
        self.force_box = QCheckBox("Zorla")
        self.force_box.setToolTip("Yanıt vermeyen işlemler için. Kaydedilmemiş veriler kaybolur.")
        self.kill_button = QPushButton("Sonlandır", objectName="Dugme")
        self.kill_button.clicked.connect(self.kill_selected)
        row.addWidget(self.proc_filter, 1)
        row.addWidget(self.force_box)
        row.addWidget(self.kill_button)
        pl.addLayout(row)
        self.proc_table = QTableWidget(0, 5)
        self.proc_table.setHorizontalHeaderLabels(["PID", "Ad", "CPU %", "Bellek (MB)", "Kullanıcı"])
        self.proc_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.proc_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.proc_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.proc_table.verticalHeader().hide()
        self.proc_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.proc_table.setStyleSheet(_TABLE_CSS)
        self.proc_table.setSortingEnabled(True)
        self.proc_table.sortByColumn(2, Qt.SortOrder.DescendingOrder)
        pl.addWidget(self.proc_table, 1)
        self.tabs.addTab(pw, "İşlemler")

        # Donanım
        self.hw_label = QLabel(hardware_html(None, None, None, None, None))
        self.hw_label.setTextFormat(Qt.TextFormat.RichText)
        self.hw_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.hw_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        hw = QWidget()
        hl = QVBoxLayout(hw)
        hl.addWidget(self.hw_label, 1)
        self.tabs.addTab(hw, "Donanım")

        # Uygulamalar
        aw = QWidget()
        al = QVBoxLayout(aw)
        arow = QHBoxLayout()
        self.app_search = QLineEdit(objectName="Giris")
        self.app_search.setPlaceholderText("Uygulama ara (örn. not defteri, chrome)")
        self.app_search.returnPressed.connect(self.search_apps)
        self.app_open = QPushButton("Aç", objectName="Dugme")
        self.app_open.clicked.connect(self.open_selected_app)
        arow.addWidget(self.app_search, 1)
        arow.addWidget(self.app_open)
        al.addLayout(arow)
        self.app_list = QListWidget()
        self.app_list.setStyleSheet(f"QListWidget {{ border:1px solid {C.CELIK}; border-radius:12px; padding:6px; }}")
        self.app_list.itemDoubleClicked.connect(lambda _i: self.open_selected_app())
        al.addWidget(self.app_list, 1)
        self.tabs.addTab(aw, "Uygulamalar")

        # Denetim kaydı
        self.audit_table = QTableWidget(0, 6)
        self.audit_table.setHorizontalHeaderLabels(["Zaman", "Kaynak", "Araç", "İzin", "Karar", "Sonuç"])
        self.audit_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.audit_table.verticalHeader().hide()
        self.audit_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.audit_table.setStyleSheet(_TABLE_CSS)
        self.tabs.addTab(self.audit_table, "Denetim kaydı")

        self.tabs.currentChanged.connect(lambda _i: self._load_current_tab())
        self.timer = QTimer(self)
        self.timer.setInterval(PROCESS_REFRESH_MS)
        self.timer.timeout.connect(self._tick)
        assistant.bus.subscribe(Topics.SYSTEM_SNAPSHOT, self._on_snapshot)
        assistant.bus.subscribe(Topics.TOOL_EXECUTED, lambda _r: self._audit_if_visible())

    # ---------- yardımcılar ----------
    async def _io(self, fn, *args):  # noqa: ANN001, ANN202
        import asyncio

        return await asyncio.get_running_loop().run_in_executor(self.assistant.tool_service.executor, fn, *args)

    def _spawn(self, coro, name: str) -> None:  # noqa: ANN001
        self.assistant.spawn(coro, name)

    def showEvent(self, event) -> None:  # noqa: ANN001
        super().showEvent(event)
        self.timer.start()
        self.refresh()

    def hideEvent(self, event) -> None:  # noqa: ANN001
        super().hideEvent(event)
        self.timer.stop()

    def refresh(self) -> None:
        self._load_current_tab(force=True)

    def _tick(self) -> None:
        if self.isVisible() and self.tabs.currentIndex() == 0:
            self._spawn(self.load_processes(), "system-processes")

    def _load_current_tab(self, force: bool = False) -> None:
        i = self.tabs.currentIndex()
        if i == 0:
            self._spawn(self.load_processes(), "system-processes")
        elif i == 1:
            self._spawn(self.load_hardware(), "system-hardware")
        elif i == 2:
            if force or self.app_list.count() == 0:
                self.search_apps()
        elif i == 3:
            self._spawn(self.load_audit(), "system-audit")

    def _on_snapshot(self, snap: SystemSnapshot) -> None:
        self._snap = snap
        if self.isVisible() and self.tabs.currentIndex() == 1:
            self._render_hw()

    def _render_hw(self) -> None:
        s = self._static
        self.hw_label.setText(hardware_html(self._snap, s.get("cpu"), s.get("disks"), s.get("net"), s.get("overview")))

    # ---------- işlemler ----------
    async def load_processes(self) -> None:
        if self._proc_loading:
            return
        self._proc_loading = True
        try:
            rows = await self._io(processes.list_processes, "cpu", 0, 0.3)
        except Exception:  # noqa: BLE001
            log.exception("İşlem listesi okunamadı")
            self.status.setText("İşlem listesi okunamadı.")
            return
        finally:
            self._proc_loading = False
        self.set_processes(rows)

    def set_processes(self, rows: list[dict]) -> None:
        selected = self.selected_process()
        t = self.proc_table
        t.setSortingEnabled(False)
        t.setRowCount(len(rows))
        for i, r in enumerate(rows):
            for col, val in enumerate((r["pid"], r["name"], r["cpu_percent"], r["memory_mb"], r["user"] or "")):
                item = QTableWidgetItem()
                item.setData(Qt.ItemDataRole.DisplayRole, val)  # sayılar sayısal sıralanır
                if col in (0, 2, 3):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                t.setItem(i, col, item)
        t.setSortingEnabled(True)
        self._apply_filter()
        if selected:
            for i in range(t.rowCount()):
                if t.item(i, 0).data(Qt.ItemDataRole.DisplayRole) == selected[0]:
                    t.selectRow(i)
                    break

    def _apply_filter(self) -> None:
        q = self.proc_filter.text().strip().lower()
        for i in range(self.proc_table.rowCount()):
            name = self.proc_table.item(i, 1)
            self.proc_table.setRowHidden(i, bool(q) and (name is None or q not in name.text().lower()))

    def selected_process(self) -> tuple[int, str] | None:
        rows = self.proc_table.selectionModel().selectedRows() if self.proc_table.selectionModel() else []
        if not rows:
            return None
        r = rows[0].row()
        return int(self.proc_table.item(r, 0).data(Qt.ItemDataRole.DisplayRole)), self.proc_table.item(r, 1).text()

    def kill_selected(self) -> None:
        sel = self.selected_process()
        if sel is None:
            self.status.setText("Önce listeden bir işlem seç.")
            return
        pid, name = sel
        task = self.assistant.run_tool("kill_process", {"pid": pid, "name": name, "force": self.force_box.isChecked()})
        task.add_done_callback(self._after_tool)

    def _after_tool(self, task) -> None:  # noqa: ANN001
        if task.cancelled() or task.exception() is not None:
            self.status.setText("İşlem tamamlanamadı.")
            return
        r = task.result()
        self.status.setStyleSheet(f"color:{C.CYAN if r.ok else C.AMBER};")
        self.status.setText(r.summary)
        if self.tabs.currentIndex() == 0:
            self._spawn(self.load_processes(), "system-processes")

    # ---------- donanım ----------
    async def load_hardware(self) -> None:
        self._render_hw()
        try:
            self._static["cpu"] = await self._io(hardware.cpu_status)
            self._static["disks"] = await self._io(system_info.disks)
            self._static["net"] = await self._io(system_info.network)
            self._static["overview"] = await self._io(system_info.overview)
        except Exception:  # noqa: BLE001
            log.exception("Donanım bilgisi okunamadı")
            self.status.setText("Donanım bilgisinin bir kısmı okunamadı.")
        self._render_hw()

    # ---------- uygulamalar ----------
    def search_apps(self) -> None:
        self._spawn(self._search_apps(self.app_search.text().strip()), "system-apps")

    async def _search_apps(self, query: str) -> None:
        index = self.assistant.tool_service.ctx.apps
        if index is None:
            return
        if query:
            found = await self._io(lambda: [e for e, _s in index.find(query, limit=60, launchable_only=True)])
        else:
            found = await self._io(lambda: sorted({e.name: e for e in index.entries() if e.launchable}.values(),
                                                  key=lambda e: e.name.lower()))
        self.app_list.clear()
        for e in found:
            item = QListWidgetItem(e.name + ("  (oyun)" if e.kind == "game" else ""))
            item.setData(Qt.ItemDataRole.UserRole, e.name)
            self.app_list.addItem(item)
        if not found:
            self.status.setText("Uygulama listesi yalnızca Windows'ta okunabilir." if sys.platform != "win32"
                                else "Eşleşen uygulama yok.")

    def open_selected_app(self) -> None:
        item = self.app_list.currentItem()
        if item is None:
            self.status.setText("Önce listeden bir uygulama seç.")
            return
        name = item.data(Qt.ItemDataRole.UserRole)
        tool = "launch_game" if item.text().endswith("(oyun)") else "open_app"
        self.assistant.run_tool(tool, {"name": name}).add_done_callback(self._after_tool)

    # ---------- denetim kaydı ----------
    def _audit_if_visible(self) -> None:
        if self.isVisible() and self.tabs.currentIndex() == 3:
            self._spawn(self.load_audit(), "system-audit")

    async def load_audit(self) -> None:
        entries = await self._io(self.assistant.audit.recent, 200)
        t = self.audit_table
        t.setRowCount(len(entries))
        for i, e in enumerate(entries):
            try:
                when = datetime.fromisoformat(e.ts_utc).astimezone().strftime("%d.%m %H:%M:%S")
            except ValueError:
                when = e.ts_utc
            result = (e.summary or "") if e.ok else (e.error or "başarısız")
            for col, val in enumerate((when, e.source, e.tool, e.permission, e.decision, result)):
                t.setItem(i, col, QTableWidgetItem(str(val)))
