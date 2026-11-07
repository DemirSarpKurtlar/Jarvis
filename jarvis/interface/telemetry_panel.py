"""Sağdaki enstrüman şeridi: gerçek sistem verileri ve Claude kullanım özeti."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QGridLayout, QLabel, QVBoxLayout, QWidget

from jarvis.config.settings import Settings
from jarvis.interface.components import Divider, Sparkline, ThinBar
from jarvis.system.monitor import SystemSnapshot


def tr_num(x: float, digits: int = 1) -> str:
    """Türkçe ondalık biçimi: 9.8 -> '9,8'."""
    return f"{x:.{digits}f}".replace(".", ",")


def tr_rate(bps: float | None) -> str:
    if bps is None:
        return "ölçülüyor"
    if bps >= 1_000_000:
        return f"{tr_num(bps / 1_000_000)} Mb/s"
    return f"{tr_num(bps / 1_000, 0)} kb/s"


def tr_duration(seconds: float) -> str:
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d} gün {h} sa"
    return f"{h} sa {m} dk"


class Metric(QWidget):
    """Ad + değer satırı, altında isteğe bağlı çubuk ya da sparkline."""

    def __init__(self, name: str, extra: QWidget | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 6, 0, 6)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        self.name = QLabel(name, objectName="OlcumAd")
        self.value = QLabel("–", objectName="OlcumDeger")
        self.value.setTextFormat(Qt.TextFormat.RichText)
        self.value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        grid.addWidget(self.name, 0, 0)
        grid.addWidget(self.value, 0, 1)
        self.extra = extra
        if extra is not None:
            grid.addWidget(extra, 1, 0, 1, 2)

    def set(self, main: str, unit: str = "", tooltip: str = "") -> None:
        unit_html = f'<span style="color:#6F86A6;font-size:12px;font-weight:400"> {unit}</span>' if unit else ""
        self.value.setText(f"{main}{unit_html}")
        self.setToolTip(tooltip)


class TelemetryPanel(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Serit")
        self.setFixedWidth(300)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 22, 24, 22)
        lay.setSpacing(2)

        lay.addWidget(QLabel("Sistem", objectName="SeritBaslik"))
        lay.addSpacing(6)
        self.cpu_spark = Sparkline()
        self.cpu = Metric("İşlemci", self.cpu_spark)
        self.gpu_bar = ThinBar(hot=True)
        self.gpu = Metric("Ekran kartı", self.gpu_bar)
        self.ram_bar = ThinBar()
        self.ram = Metric("Bellek", self.ram_bar)
        self.disk_bar = ThinBar()
        self.disk = Metric("Disk", self.disk_bar)
        self.net = Metric("Ağ")
        self.uptime = Metric("Açık kalma süresi")
        for m in (self.cpu, self.gpu, self.ram, self.disk, self.net, self.uptime):
            lay.addWidget(m)

        lay.addSpacing(16)
        lay.addWidget(Divider())
        lay.addSpacing(16)
        lay.addWidget(QLabel("Claude", objectName="SeritBaslik"))
        lay.addSpacing(6)
        self.claude_status = Metric("Bağlantı")
        self.claude_status.set("Kurulmadı")
        self.budget = Metric("Aylık limit")
        self.policy = QLabel("", objectName="Not")
        self.policy.setWordWrap(True)
        lay.addWidget(self.claude_status)
        lay.addWidget(self.budget)
        note = QLabel(
            "Claude bağlantısı, token ve maliyet takibi Aşama 2'de eklenecek.", objectName="Not"
        )
        note.setWordWrap(True)
        lay.addSpacing(4)
        lay.addWidget(self.policy)
        lay.addWidget(note)
        lay.addStretch(1)

    def apply_settings(self, s: Settings) -> None:
        self.budget.set(f"${tr_num(s.budget.monthly_limit_usd, 2)}")
        policy = {
            "fallback_light": "Limit dolunca ekonomik modele (Haiku) geçilir.",
            "block": "Limit dolunca Claude istekleri durdurulur.",
            "ask": "Limit dolunca her istekte sana sorulur.",
        }[s.budget.on_limit]
        self.policy.setText(policy)

    def update_snapshot(self, snap: SystemSnapshot) -> None:
        self.cpu.set(tr_num(snap.cpu_percent, 0), "%")
        self.cpu_spark.push(snap.cpu_percent)

        g = snap.gpu
        if g is None:
            self.gpu.set("Okunamıyor", tooltip=snap.gpu_unavailable_reason or "")
            self.gpu.name.setToolTip(snap.gpu_unavailable_reason or "")
            self.gpu_bar.set_value(None)
        else:
            parts = []
            if g.utilization_pct is not None:
                parts.append(f"kullanım %{g.utilization_pct}")
            if g.memory_used_mb is not None and g.memory_total_mb:
                parts.append(f"bellek {tr_num(g.memory_used_mb / 1024)}/{tr_num(g.memory_total_mb / 1024)} GB")
            if g.fan_pct is not None:
                parts.append(f"fan %{g.fan_pct}")
            tip = f"{g.name}\n" + ", ".join(parts)
            if g.temperature_c is not None:
                self.gpu.set(str(g.temperature_c), "°C", tip)
                self.gpu_bar.set_value(g.temperature_c / 100)
            else:
                self.gpu.set(f"%{g.utilization_pct}" if g.utilization_pct is not None else "–", "", tip)
                self.gpu_bar.set_value((g.utilization_pct or 0) / 100)

        self.ram.set(tr_num(snap.ram_used_gb), f"/ {tr_num(snap.ram_total_gb, 0)} GB")
        self.ram_bar.set_value(snap.ram_percent / 100)

        self.disk.name.setText(f"Disk {snap.disk_path.rstrip(chr(92))}")
        self.disk.set(tr_num(snap.disk_free_gb, 0), "GB boş")
        self.disk_bar.set_value(snap.disk_percent / 100)

        self.net.set(tr_rate(snap.net_down_bps), "indirme", f"Yükleme: {tr_rate(snap.net_up_bps)}")
        self.uptime.set(tr_duration(snap.uptime_s))
