"""Claude model kayıt defteri ve fiyatlar.

Model kimlikleri ve fiyatlar resmi Anthropic dokümantasyonundan doğrulandı
(platform.claude.com/docs/en/about-claude/pricing, 2026-11-07). Fiyatlar
değişebilir: arayüzdeki tutarlar **tahmindir**, kesin fatura Anthropic
Console'dadır.

Kullanıcı ayarlardan bilinmeyen bir model kimliği girerse fiyat uydurulmaz:
o kademenin varsayılan modelinin fiyatı kullanılır ve `pricing_assumed`
işaretlenir (bütçe takibi körleşmesin diye).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

from jarvis.config.settings import AISettings

Tier = Literal["light", "main", "advanced"]

PRICES_VERIFIED_ON = "2026-11-07"
WEB_SEARCH_USD_PER_REQUEST = 10.0 / 1000  # 1.000 arama başına $10


@dataclass(frozen=True)
class Pricing:
    """USD / 1 milyon token."""

    input: float
    output: float
    cache_write_5m: float
    cache_read: float
    # Uzun istem tarifesi (ör. Haiku 5.5: 100K token üstü)
    long_threshold: int | None = None
    long_input: float = 0.0
    long_output: float = 0.0
    long_cache_write_5m: float = 0.0
    long_cache_read: float = 0.0


@dataclass(frozen=True)
class ModelSpec:
    id: str
    display_name: str
    tier: Tier
    pricing: Pricing
    default_effort: Literal["low", "medium", "high", "xhigh", "max"]
    max_output_tokens: int
    web_search_tool: str | None        # None: bu modelde web araması kullanılmaz
    server_fallback: bool              # `fallbacks: "default"` destekleniyor mu
    pricing_assumed: bool = False


KNOWN_MODELS: dict[str, ModelSpec] = {
    "claude-haiku-5-5": ModelSpec(
        id="claude-haiku-5-5", display_name="Haiku 5.5", tier="light",
        pricing=Pricing(0.10, 0.50, 0.125, 0.01,
                        long_threshold=100_000, long_input=0.50, long_output=2.50,
                        long_cache_write_5m=0.625, long_cache_read=0.05),
        default_effort="low", max_output_tokens=4096,
        web_search_tool=None,       # dinamik filtreli web araması Haiku'da yok
        server_fallback=False,      # Haiku 5.5'ta sunucu tarafı yedek model yok
    ),
    "claude-sonnet-5-5": ModelSpec(
        id="claude-sonnet-5-5", display_name="Sonnet 5.5", tier="main",
        pricing=Pricing(2.00, 10.00, 2.50, 0.10),
        default_effort="medium", max_output_tokens=16000,
        web_search_tool="web_search_20260209", server_fallback=True,
    ),
    "claude-opus-5-5": ModelSpec(
        id="claude-opus-5-5", display_name="Opus 5.5", tier="advanced",
        pricing=Pricing(4.00, 20.00, 5.00, 0.20),
        default_effort="high", max_output_tokens=32000,
        web_search_tool="web_search_20260209", server_fallback=True,
    ),
    # Sunucu tarafı yedek model devreye girerse yanıtı bu modeller verebilir
    "claude-sonnet-5": ModelSpec(
        id="claude-sonnet-5", display_name="Sonnet 5", tier="main",
        pricing=Pricing(2.00, 10.00, 2.50, 0.20),
        default_effort="medium", max_output_tokens=16000,
        web_search_tool="web_search_20260209", server_fallback=False,
    ),
    "claude-opus-5": ModelSpec(
        id="claude-opus-5", display_name="Opus 5", tier="advanced",
        pricing=Pricing(5.00, 25.00, 6.25, 0.50),
        default_effort="high", max_output_tokens=32000,
        web_search_tool="web_search_20260209", server_fallback=False,
    ),
    "claude-opus-4-8": ModelSpec(
        id="claude-opus-4-8", display_name="Opus 4.8", tier="advanced",
        pricing=Pricing(5.00, 25.00, 6.25, 0.50),
        default_effort="high", max_output_tokens=32000,
        web_search_tool="web_search_20260209", server_fallback=False,
    ),
}

DEFAULT_FOR_TIER: dict[Tier, str] = {
    "light": "claude-haiku-5-5",
    "main": "claude-sonnet-5-5",
    "advanced": "claude-opus-5-5",
}


def spec_for(model_id: str, tier: Tier | None = None) -> ModelSpec:
    """Bilinen bir model için kaydı döndürür; bilinmeyende muhafazakâr tahmin."""
    if model_id in KNOWN_MODELS:
        return KNOWN_MODELS[model_id]
    base = KNOWN_MODELS[DEFAULT_FOR_TIER[tier or "advanced"]]
    return replace(base, id=model_id, display_name=model_id, pricing_assumed=True,
                   web_search_tool=None, server_fallback=False)


class ModelRegistry:
    def __init__(self, ai: AISettings) -> None:
        self._ai = ai

    def for_tier(self, tier: Tier) -> ModelSpec:
        model_id = {"light": self._ai.model_light, "main": self._ai.model_main,
                    "advanced": self._ai.model_advanced}[tier]
        return spec_for(model_id, tier)


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    web_search_requests: int = 0

    def __add__(self, o: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            self.input_tokens + o.input_tokens,
            self.output_tokens + o.output_tokens,
            self.cache_creation_tokens + o.cache_creation_tokens,
            self.cache_read_tokens + o.cache_read_tokens,
            self.web_search_requests + o.web_search_requests,
        )


def token_cost_usd(spec: ModelSpec, u: TokenUsage) -> float:
    """Tek bir modelin token maliyeti (web araması hariç)."""
    p = spec.pricing
    prompt = u.input_tokens + u.cache_creation_tokens + u.cache_read_tokens
    if p.long_threshold is not None and prompt > p.long_threshold:
        rates = (p.long_input, p.long_output, p.long_cache_write_5m, p.long_cache_read)
    else:
        rates = (p.input, p.output, p.cache_write_5m, p.cache_read)
    r_in, r_out, r_w, r_r = rates
    return (u.input_tokens * r_in + u.output_tokens * r_out
            + u.cache_creation_tokens * r_w + u.cache_read_tokens * r_r) / 1_000_000


def web_search_cost_usd(requests: int) -> float:
    return requests * WEB_SEARCH_USD_PER_REQUEST
