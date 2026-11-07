"""Sağdaki enstrüman şeridi: gerçek sistem verileri ve Claude kullanım özeti."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from jarvis.ai.model_registry import spec_for
from jarvis.ai.usage import BudgetStatus
from jarvis.config.settings import Settings
from jarvis.interface.components import Divider, Sparkline, ThinBar
from jarvis.interface.format import tokens, usd
from jarvis.interface.theme import C
from jarvis.system.monitor import SystemSnapshot

TIER_COLORS = {"light": C.CYAN, "main": C.MENEKSE, "advanced": C.AMBER}


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


class ModelSplit(QWidget):
    """Bu ayki harcamanın modellere dağılımı: renkli çubuk + açıklama."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 6, 0, 4)
        lay.setSpacing(6)
        self.bar = QWidget()
        self.bar.setFixedHeight(6)
        self.bar_lay = QHBoxLayout(self.bar)
        self.bar_lay.setContentsMargins(0, 0, 0, 0)
        self.bar_lay.setSpacing(2)
        self.legend = QLabel(objectName="Not")
        self.legend.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(self.bar)
        lay.addWidget(self.legend)
        self.set_split({})

    def set_split(self, by_model: dict[str, float]) -> None:
        while self.bar_lay.count():
            w = self.bar_lay.takeAt(0).widget()
            if w:
                w.deleteLater()
        tiers: dict[str, float] = {}
        for model, cost in by_model.items():
            tier = spec_for(model).tier
            tiers[tier] = tiers.get(tier, 0.0) + cost
        total = sum(tiers.values())
        if total <= 0:
            seg = QFrame()
            seg.setStyleSheet(f"background:{C.CELIK}; border-radius:3px;")
            self.bar_lay.addWidget(seg)
            self.legend.setText("Bu ay henüz Claude kullanılmadı.")
            return
        names = {"light": "Haiku", "main": "Sonnet", "advanced": "Opus"}
        legend = []
        for tier in ("light", "main", "advanced"):
            if tiers.get(tier, 0) <= 0:
                continue
            share = tiers[tier] / total
            seg = QFrame()
            seg.setStyleSheet(f"background:{TIER_COLORS[tier]}; border-radius:3px;")
            self.bar_lay.addWidget(seg, max(1, int(share * 1000)))
            pct = "&lt;1" if share < 0.01 else f"{share * 100:.0f}"
            legend.append(f"<span style='color:{TIER_COLORS[tier]}'>&#9632;</span> {names[tier]} %{pct}")
        self.legend.setText("&nbsp;&nbsp;".join(legend))


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
        self.claude_status.set("–")
        self.today = Metric("Bugün")
        self.month_bar = ThinBar()
        self.month = Metric("Bu ay", self.month_bar)
        self.models = ModelSplit()
        self.in_tok = Metric("Giriş tokeni")
        self.cache_tok = Metric("Önbellekten okunan")
        self.out_tok = Metric("Çıkış tokeni")
        self.searches = Metric("Web araması")
        for m in (self.claude_status, self.today, self.month):
            lay.addWidget(m)
        lay.addWidget(self.models)
        for m in (self.in_tok, self.cache_tok, self.out_tok, self.searches):
            lay.addWidget(m)
        self.policy = QLabel("", objectName="Not")
        self.policy.setWordWrap(True)
        lay.addSpacing(4)
        lay.addWidget(self.policy)
        lay.addStretch(1)
        self._settings: Settings | None = None

    def apply_settings(self, s: Settings) -> None:
        self._settings = s
        policy = {
            "fallback_light": "Limit dolunca ekonomik modele (Haiku) geçilir.",
            "block": "Limit dolunca Claude istekleri durdurulur.",
            "ask": "Limit dolunca her istekte sana sorulur.",
        }[s.budget.on_limit]
        self.policy.setText(f"Tutarlar tahminidir, kesin fatura Anthropic Console'dadır. {policy}")

    def update_claude_status(self, chat) -> None:  # noqa: ANN001
        from jarvis.core.chat_service import ClaudeStatus
        color = {ClaudeStatus.CONNECTED: C.CYAN, ClaudeStatus.NO_KEY: C.AMBER,
                 ClaudeStatus.OFFLINE: C.AMBER, ClaudeStatus.ERROR: C.KIZIL}.get(chat.status, C.SIS)
        self.claude_status.value.setStyleSheet(f"color:{color};")
        self.claude_status.set(chat.status.value, tooltip=chat.status_detail)

    def update_usage(self, b: BudgetStatus) -> None:
        self.today.set(usd(b.today.cost_usd), f"/ {usd(b.daily_limit)}" if b.daily_limit else "")
        self.month.set(usd(b.month.cost_usd), f"/ {usd(b.monthly_limit)}" if b.monthly_limit else "")
        self.month_bar.set_value(b.month.cost_usd / b.monthly_limit if b.monthly_limit else 0)
        level_color = {"ok": "", "warn": f"color:{C.AMBER};", "exceeded": f"color:{C.KIZIL};"}[b.level]
        self.month.value.setStyleSheet(level_color)
        self.today.value.setStyleSheet(level_color if b.daily_limit and b.today.cost_usd >= b.daily_limit * b.warn_ratio else "")
        self.models.set_split(b.month.by_model)
        self.in_tok.set(tokens(b.month.input_tokens + b.month.cache_creation_tokens))
        self.cache_tok.set(tokens(b.month.cache_read_tokens))
        self.out_tok.set(tokens(b.month.output_tokens))
        self.searches.set(str(b.month.web_search_requests))

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
