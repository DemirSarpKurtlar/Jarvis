"""Resmi Anthropic Python SDK üzerinden Claude istemcisi.

- Yanıtlar akışlı (streaming) gelir; metin parça parça arayüze iletilir.
- API adresi her zaman açıkça verilir: bir ortam değişkeni anahtarı başka
  bir sunucuya yönlendiremez.
- Hatalar türlerine ayrılır ve kullanıcıya Türkçe, ne yapması gerektiğini
  söyleyen bir mesajla iletilir. Yeniden deneme SDK'nın sınırlı yeniden
  deneme mekanizmasıyla yapılır (sonsuz döngü yok).
- Sunucu tarafı yedek model (`fallbacks: "default"`) yalnızca destekleyen
  modellerde açılır (Sonnet 5.5, Opus 5.5; Haiku 5.5'ta yok).
- Konuşma geçmişine yalnızca metin yazılır; düşünme (thinking) blokları geri
  gönderilmez. Böylece geçmiş kırpıldığında ya da model değiştiğinde API'nin
  "düzenlenmiş geçmiş" kontrolüne takılınmaz.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

import anthropic

from jarvis.ai.model_registry import ModelSpec, TokenUsage, spec_for
from jarvis.ai.usage import CostLine
from jarvis.core.errors import JarvisError

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.anthropic.com"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_PAUSE_CONTINUATIONS = 3
WEB_SEARCH_MAX_USES = 3


class ErrorKind(str, Enum):
    NO_KEY = "no_key"
    AUTH = "auth"
    PERMISSION = "permission"
    NOT_FOUND = "not_found"
    BAD_REQUEST = "bad_request"
    RATE_LIMIT = "rate_limit"
    OVERLOADED = "overloaded"
    SERVER = "server"
    CONNECTION = "connection"
    TIMEOUT = "timeout"
    SPEND_LIMIT = "spend_limit"
    UNKNOWN = "unknown"


class ClaudeError(JarvisError):
    def __init__(self, kind: ErrorKind, message: str, user_message: str) -> None:
        super().__init__(message, user_message)
        self.kind = kind

    @property
    def is_offline(self) -> bool:
        return self.kind in (ErrorKind.CONNECTION, ErrorKind.TIMEOUT)


def map_error(exc: Exception, model: str = "") -> ClaudeError:
    """SDK hatasını türüne göre sınıflandırır (en özelden en genele)."""
    rid = getattr(getattr(exc, "response", None), "headers", {}).get("request-id", "") if hasattr(exc, "response") else ""
    tech = f"{exc.__class__.__name__}: {getattr(exc, 'message', exc)} {rid}".strip()
    if isinstance(exc, anthropic.AuthenticationError):
        return ClaudeError(ErrorKind.AUTH, tech, "Claude API anahtarı geçersiz ya da iptal edilmiş. Ayarlar'dan anahtarı yenile.")
    if isinstance(exc, anthropic.PermissionDeniedError):
        return ClaudeError(ErrorKind.PERMISSION, tech, "API anahtarının bu işleme ya da modele erişim izni yok.")
    if isinstance(exc, anthropic.NotFoundError):
        return ClaudeError(ErrorKind.NOT_FOUND, tech, f"'{model}' modeli bulunamadı. Ayarlar'daki model kimliğini kontrol et.")
    if isinstance(exc, anthropic.RateLimitError):
        return ClaudeError(ErrorKind.RATE_LIMIT, tech, "Claude API hız sınırına ulaşıldı. Biraz bekleyip tekrar dene.")
    if isinstance(exc, anthropic.APITimeoutError):
        return ClaudeError(ErrorKind.TIMEOUT, tech, "Claude'a bağlantı zaman aşımına uğradı. İnternet bağlantını kontrol et.")
    if isinstance(exc, anthropic.APIConnectionError):
        return ClaudeError(ErrorKind.CONNECTION, tech, "Claude'a ulaşılamıyor; internet bağlantısı yok gibi görünüyor. Yerel özellikler çalışmaya devam ediyor.")
    if isinstance(exc, anthropic.APIStatusError):
        status = exc.status_code
        body = str(getattr(exc, "message", "")).lower()
        if status == 529 or isinstance(exc, anthropic.OverloadedError):
            return ClaudeError(ErrorKind.OVERLOADED, tech, "Claude şu an çok yoğun. Birkaç dakika sonra tekrar dene.")
        if "credit balance" in body or "spend limit" in body or isinstance(exc, anthropic.EnforcedSpendLimitReachedError):
            return ClaudeError(ErrorKind.SPEND_LIMIT, tech, "Anthropic hesabında kredi ya da harcama limiti doldu. Console'dan bakiyeni kontrol et.")
        if status >= 500:
            return ClaudeError(ErrorKind.SERVER, tech, "Claude sunucusunda geçici bir hata oluştu. Biraz sonra tekrar dene.")
        if status == 400:
            return ClaudeError(ErrorKind.BAD_REQUEST, tech, "Claude isteği reddetti (geçersiz istek). Ayrıntı log dosyasında.")
    return ClaudeError(ErrorKind.UNKNOWN, tech, "Claude ile iletişimde beklenmeyen bir hata oluştu. Ayrıntı log dosyasında.")


@dataclass
class ChatResult:
    text: str
    model_requested: str
    model_served: str
    stop_reason: str | None
    cost_lines: list[CostLine]
    request_id: str | None = None
    refusal_category: str | None = None
    citations: list[tuple[str, str]] = field(default_factory=list)  # (başlık, url)
    fallback_used: bool = False

    @property
    def usage(self) -> TokenUsage:
        total = TokenUsage()
        for line in self.cost_lines:
            total = total + line.usage
        return total

    @property
    def cost_usd(self) -> float:
        return sum(line.cost_usd for line in self.cost_lines)

    @property
    def refused(self) -> bool:
        return self.stop_reason == "refusal"


def _usage_lines(message, requested: ModelSpec) -> tuple[list[CostLine], bool]:  # noqa: ANN001
    """Yanıtın kullanım bilgisini model başına maliyet satırlarına çevirir."""
    u = message.usage
    searches = getattr(getattr(u, "server_tool_use", None), "web_search_requests", 0) or 0
    iterations = getattr(u, "iterations", None) or []
    fallback = any(getattr(it, "type", "") == "fallback_message" for it in iterations)
    if fallback:
        lines: list[CostLine] = []
        for it in iterations:
            if getattr(it, "type", "") not in ("message", "fallback_message"):
                continue
            model_id = getattr(it, "model", None) or requested.id
            lines.append(CostLine(spec_for(str(model_id), requested.tier), TokenUsage(
                it.input_tokens, it.output_tokens, it.cache_creation_input_tokens, it.cache_read_input_tokens)))
        if lines:
            first = lines[0]
            lines[0] = CostLine(first.spec, first.usage + TokenUsage(web_search_requests=searches))
            return lines, True
    served = spec_for(str(getattr(message, "model", requested.id) or requested.id), requested.tier)
    return [CostLine(served, TokenUsage(
        u.input_tokens or 0, u.output_tokens or 0,
        u.cache_creation_input_tokens or 0, u.cache_read_input_tokens or 0, searches))], False


def _extract(message) -> tuple[str, list[tuple[str, str]]]:  # noqa: ANN001
    parts: list[str] = []
    cites: dict[str, str] = {}
    for block in message.content:
        if getattr(block, "type", "") != "text":
            continue
        parts.append(block.text)
        for c in getattr(block, "citations", None) or []:
            url = getattr(c, "url", None)
            if url and url not in cites:
                cites[url] = getattr(c, "title", None) or url
    return "".join(parts).strip(), [(t, u) for u, t in cites.items()]


class ClaudeClient:
    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL, max_retries: int = 2,
                 timeout_s: float = 90.0, http_client=None) -> None:  # noqa: ANN001
        kwargs = {}
        if http_client is not None:  # yalnızca testler (sahte HTTP katmanı) için
            kwargs["http_client"] = http_client
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key,
            base_url=base_url,
            max_retries=max_retries,
            timeout=anthropic.Timeout(timeout_s, connect=10.0),
            **kwargs,
        )

    async def close(self) -> None:
        await self._client.close()

    async def verify(self, model_id: str) -> None:
        """Anahtarı ücretsiz bir uç noktayla (model bilgisi) doğrular."""
        try:
            await self._client.models.retrieve(model_id)
        except Exception as exc:  # noqa: BLE001
            raise map_error(exc, model_id) from exc

    async def chat(
        self,
        spec: ModelSpec,
        system: list[dict],
        messages: list[dict],
        *,
        web_search: bool = False,
        effort: str | None = None,
        on_text: Callable[[str], None] | None = None,
    ) -> ChatResult:
        params: dict = {
            "model": spec.id,
            "max_tokens": spec.max_output_tokens,
            "system": system,
            "messages": list(messages),
            "output_config": {"effort": effort or spec.default_effort},
        }
        betas: list[str] = []
        if web_search and spec.web_search_tool:
            params["tools"] = [{"type": spec.web_search_tool, "name": "web_search",
                                "max_uses": WEB_SEARCH_MAX_USES,
                                "user_location": {"type": "approximate", "country": "TR"}}]
        if spec.server_fallback:
            betas.append(FALLBACK_BETA)
            params["fallbacks"] = "default"

        all_lines: list[CostLine] = []
        fallback_used = False
        text_parts: list[str] = []
        citations: list[tuple[str, str]] = []
        final = None
        request_id: str | None = None
        try:
            for _ in range(MAX_PAUSE_CONTINUATIONS + 1):
                extra = {"betas": betas} if betas else {}
                async with self._client.beta.messages.stream(**params, **extra) as stream:
                    async for event in stream:
                        if event.type == "text" and on_text is not None:
                            on_text(event.text)
                    final = await stream.get_final_message()
                    request_id = stream.request_id
                lines, fb = _usage_lines(final, spec)
                all_lines += lines
                fallback_used |= fb
                t, c = _extract(final)
                if t:
                    text_parts.append(t)
                citations += [x for x in c if x not in citations]
                if final.stop_reason != "pause_turn":
                    break
                # Sunucu tarafı araç döngüsü duraklattı: aynı turu kaldığı yerden sürdür
                params["messages"] = params["messages"] + [{"role": "assistant", "content": final.content}]
        except anthropic.AnthropicError as exc:
            err = map_error(exc, spec.id)
            log.error("Claude hatası (%s): %s", err.kind.value, err)
            raise err from exc

        assert final is not None
        stop_details = getattr(final, "stop_details", None)
        result = ChatResult(
            text="\n\n".join(text_parts),
            model_requested=spec.id,
            model_served=str(final.model),
            stop_reason=final.stop_reason,
            cost_lines=all_lines,
            request_id=request_id,
            refusal_category=getattr(stop_details, "category", None) if final.stop_reason == "refusal" else None,
            citations=citations,
            fallback_used=fallback_used,
        )
        u = result.usage
        log.info("Claude yanıtı: model=%s durma=%s giriş=%d çıkış=%d önbellek_yazma=%d önbellek_okuma=%d "
                 "arama=%d maliyet=$%.5f istek=%s", result.model_served, result.stop_reason, u.input_tokens,
                 u.output_tokens, u.cache_creation_tokens, u.cache_read_tokens, u.web_search_requests,
                 result.cost_usd, result.request_id)
        return result
