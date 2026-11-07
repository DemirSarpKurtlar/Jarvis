"""Token, maliyet ve bütçe takibi.

Her Claude çağrısı (reddedilenler ve hatayla bitip faturalananlar dahil)
`usage` tablosuna yazılır. Gün ve ay, kullanıcının yerel saatine göre
hesaplanır. Tutarlar tahmindir; kesin fatura Anthropic Console'dadır.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from jarvis.ai.model_registry import ModelSpec, TokenUsage, token_cost_usd, web_search_cost_usd
from jarvis.config.settings import BudgetSettings
from jarvis.storage.database import Database

BudgetLevel = Literal["ok", "warn", "exceeded"]


@dataclass(frozen=True)
class CostLine:
    """Bir modelin bir çağrıdaki payı (yedek model devreye girerse birden fazla olur)."""

    spec: ModelSpec
    usage: TokenUsage

    @property
    def cost_usd(self) -> float:
        return token_cost_usd(self.spec, self.usage) + web_search_cost_usd(self.usage.web_search_requests)


@dataclass(frozen=True)
class PeriodTotals:
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    web_search_requests: int = 0
    requests: int = 0
    by_model: dict[str, float] = field(default_factory=dict)  # model -> USD


@dataclass(frozen=True)
class BudgetStatus:
    today: PeriodTotals
    month: PeriodTotals
    daily_limit: float
    monthly_limit: float
    warn_ratio: float

    def _ratio(self) -> float:
        rs = []
        if self.daily_limit > 0:
            rs.append(self.today.cost_usd / self.daily_limit)
        if self.monthly_limit > 0:
            rs.append(self.month.cost_usd / self.monthly_limit)
        return max(rs, default=0.0)

    @property
    def level(self) -> BudgetLevel:
        r = self._ratio()
        if r >= 1.0:
            return "exceeded"
        if r >= self.warn_ratio:
            return "warn"
        return "ok"

    @property
    def exceeded_which(self) -> str:
        if self.daily_limit > 0 and self.today.cost_usd >= self.daily_limit:
            return "günlük"
        return "aylık"


def _now_local() -> datetime:
    return datetime.now().astimezone()


class UsageTracker:
    def __init__(self, db: Database) -> None:
        self.db = db

    def record(self, lines: list[CostLine], request_id: str | None, route_reason: str,
               now: datetime | None = None) -> float:
        now = now or _now_local()
        ts = now.astimezone(timezone.utc).isoformat(timespec="seconds")
        rows = []
        total = 0.0
        for line in lines:
            u, cost = line.usage, line.cost_usd
            total += cost
            rows.append((ts, now.strftime("%Y-%m-%d"), now.strftime("%Y-%m"), line.spec.id, line.spec.tier,
                         u.input_tokens, u.output_tokens, u.cache_creation_tokens, u.cache_read_tokens,
                         u.web_search_requests, cost, int(line.spec.pricing_assumed), request_id, route_reason))
        if rows:
            self.db.executemany(
                "INSERT INTO usage (ts_utc, local_day, local_month, model, tier, input_tokens, output_tokens,"
                " cache_creation_tokens, cache_read_tokens, web_search_requests, cost_usd, pricing_assumed,"
                " request_id, route_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
        return total

    def _totals(self, column: str, value: str) -> PeriodTotals:
        row = self.db.query(
            f"SELECT COALESCE(SUM(cost_usd),0) c, COALESCE(SUM(input_tokens),0) i,"
            f" COALESCE(SUM(output_tokens),0) o, COALESCE(SUM(cache_creation_tokens),0) cw,"
            f" COALESCE(SUM(cache_read_tokens),0) cr, COALESCE(SUM(web_search_requests),0) ws,"
            f" COUNT(DISTINCT COALESCE(request_id, id)) n FROM usage WHERE {column} = ?",
            (value,),
        )[0]
        by_model = {r["model"]: r["c"] for r in self.db.query(
            f"SELECT model, SUM(cost_usd) c FROM usage WHERE {column} = ? GROUP BY model", (value,))}
        return PeriodTotals(row["c"], row["i"], row["o"], row["cw"], row["cr"], row["ws"], row["n"], by_model)

    def status(self, budget: BudgetSettings, now: datetime | None = None) -> BudgetStatus:
        now = now or _now_local()
        return BudgetStatus(
            today=self._totals("local_day", now.strftime("%Y-%m-%d")),
            month=self._totals("local_month", now.strftime("%Y-%m")),
            daily_limit=budget.daily_limit_usd,
            monthly_limit=budget.monthly_limit_usd,
            warn_ratio=budget.warn_at_ratio,
        )
