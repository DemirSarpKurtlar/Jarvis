"""Aşama 2: model kayıt defteri, maliyet, yönlendirici, kullanım, anahtar deposu, Claude istemcisi."""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx2
import keyring
import pytest

from tests.fake_api import FakeAPI, text_stream
from jarvis.ai.claude_client import ErrorKind, ClaudeError
from jarvis.ai.model_registry import (KNOWN_MODELS, ModelRegistry, TokenUsage, spec_for, token_cost_usd,
                                      web_search_cost_usd)
from jarvis.ai.prompts import STABLE_SYSTEM_PROMPT, system_blocks
from jarvis.ai.router import classify, route, tr_lower
from jarvis.ai.usage import CostLine, UsageTracker
from jarvis.config.settings import Settings
from jarvis.security.credentials import CredentialError, CredentialStore
from jarvis.storage.database import Database

KEY = "sk-ant-api03-" + "A" * 40
HAIKU, SONNET, OPUS = (KNOWN_MODELS[m] for m in ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"))


def run(coro):
    return asyncio.run(coro)


# ---------- Kayıt defteri ve maliyet ----------
def test_registry_maps_tiers_to_decided_models():
    reg = ModelRegistry(Settings().ai)
    assert reg.for_tier("light").id == "claude-haiku-5-5"
    assert reg.for_tier("main").id == "claude-sonnet-5-5"
    assert reg.for_tier("advanced").id == "claude-opus-5-5"


def test_unknown_model_uses_conservative_assumed_pricing():
    s = spec_for("claude-gelecek-9", "light")
    assert s.pricing_assumed and s.pricing == HAIKU.pricing
    assert s.web_search_tool is None and not s.server_fallback


def test_only_supported_models_get_server_fallback_and_search():
    assert not HAIKU.server_fallback and HAIKU.web_search_tool is None
    assert SONNET.server_fallback and SONNET.web_search_tool == "web_search_20260209"
    assert OPUS.server_fallback


def test_cost_matches_official_rates():
    u = TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000,
                   cache_creation_tokens=1_000_000, cache_read_tokens=1_000_000)
    assert token_cost_usd(SONNET, u) == pytest.approx(2 + 10 + 2.5 + 0.10)
    assert token_cost_usd(OPUS, u) == pytest.approx(4 + 20 + 5 + 0.20)
    assert web_search_cost_usd(3) == pytest.approx(0.03)


def test_haiku_long_prompt_rate_card():
    short = TokenUsage(input_tokens=100_000, output_tokens=1000)
    long = TokenUsage(input_tokens=100_001, output_tokens=1000)
    assert token_cost_usd(HAIKU, short) == pytest.approx((100_000 * 0.10 + 1000 * 0.50) / 1e6)
    assert token_cost_usd(HAIKU, long) == pytest.approx((100_001 * 0.50 + 1000 * 2.50) / 1e6)


# ---------- Yönlendirici ----------
@pytest.mark.parametrize("text,tier,web", [
    ("Merhaba, nasılsın?", "light", False),
    ("Bana bir fıkra anlat", "light", False),
    ("Python'da liste ile tuple farkı ne, açıkla", "main", False),
    ("Bilgisayarım neden yavaşladı, sorun ne olabilir?", "main", False),
    ("Bugün dolar kuru ne kadar?", "main", True),
    ("İnternetten araştır: en son çıkan RTX kartı hangisi", "main", True),
    ("2026 seçim sonuçları", "main", True),
    ("Bu mimariyi derin analiz et", "advanced", False),
])
def test_classify(text, tier, web):
    got_tier, got_web, _ = classify(text)
    assert (got_tier, got_web) == (tier, web)


def test_turkish_lowercase():
    assert tr_lower("İNTERNETTEN ARAŞTIR") == "internetten araştır"
    assert tr_lower("ILIK") == "ılık"


def _budget(today=0.0, month=0.0, daily=0.5, monthly=5.0):
    from jarvis.ai.usage import BudgetStatus, PeriodTotals
    return BudgetStatus(PeriodTotals(cost_usd=today), PeriodTotals(cost_usd=month), daily, monthly, 0.8)


def test_route_normal_and_warn():
    s = Settings()
    assert route("selam", s, _budget()).budget_note is None
    r = route("selam", s, _budget(month=4.2))
    assert r.budget_note and r.tier == "light"


def test_budget_exceeded_falls_back_to_haiku_without_paid_search():
    s = Settings()  # varsayılan politika: fallback_light
    r = route("Bugün dolar kuru ne kadar, derin analiz et", s, _budget(month=5.0))
    assert r.tier == "light" and not r.web_search and "Haiku" in r.budget_note


def test_budget_block_and_ask_policies():
    s = Settings()
    s.budget.on_limit = "block"
    assert route("selam", s, _budget(today=0.6)).blocked
    s.budget.on_limit = "ask"
    r = route("selam", s, _budget(today=0.6))
    assert r.needs_confirmation and "günlük" in r.budget_note.lower()


def test_manual_model_and_search_toggle():
    s = Settings()
    s.ai.preferred_model = "advanced"
    assert route("selam", s, _budget()).tier == "advanced"
    s.ai.preferred_model = "auto"
    s.ai.web_search_enabled = False
    assert not route("bugün hava durumu", s, _budget()).web_search


# ---------- Kullanım takibi ----------
def test_usage_tracker_day_and_month_totals(tmp_path):
    t = UsageTracker(Database(tmp_path / "u.db"))
    now = datetime(2026, 11, 7, 23, 50, tzinfo=timezone(timedelta(hours=3)))
    t.record([CostLine(SONNET, TokenUsage(1000, 200, 0, 0, 1))], "req_1", "test", now)
    t.record([CostLine(HAIKU, TokenUsage(500, 50))], "req_2", "test", now + timedelta(minutes=20))  # ertesi gün
    st = t.status(Settings().budget, now)
    assert st.today.requests == 1
    assert st.today.cost_usd == pytest.approx((1000 * 2 + 200 * 10) / 1e6 + 0.01)
    assert st.month.requests == 2 and st.month.web_search_requests == 1
    assert set(st.month.by_model) == {"claude-sonnet-5-5", "claude-haiku-5-5"}
    nxt = t.status(Settings().budget, now + timedelta(minutes=20))
    assert nxt.today.requests == 1 and nxt.today.by_model == {"claude-haiku-5-5": pytest.approx(nxt.today.cost_usd)}


def test_database_migration_is_idempotent(tmp_path):
    db = Database(tmp_path / "m.db")
    v = db.schema_version
    db.close()
    assert Database(tmp_path / "m.db").schema_version == v >= 1


# ---------- Anahtar deposu ----------
def test_credentials_roundtrip(memory_keyring):
    cs = CredentialStore()
    assert cs.get_anthropic_key() is None
    cs.set_anthropic_key(KEY)
    assert cs.get_anthropic_key() == KEY
    assert ("JARVIS", "anthropic_api_key") in memory_keyring.data
    cs.delete_anthropic_key()
    assert cs.get_anthropic_key() is None
    cs.delete_anthropic_key()  # ikinci silme hata vermez


def test_credentials_reject_bad_format():
    with pytest.raises(CredentialError):
        CredentialStore().set_anthropic_key("merhaba")


def test_credentials_refuse_insecure_backend():
    from keyring.backends import fail
    keyring.set_keyring(fail.Keyring())
    with pytest.raises(CredentialError) as e:
        CredentialStore().set_anthropic_key(KEY)
    assert "güvenli" in e.value.user_message


def test_credentials_env_override(monkeypatch):
    monkeypatch.setenv("JARVIS_ANTHROPIC_API_KEY", KEY)
    assert CredentialStore().get_anthropic_key() == KEY


def test_key_never_in_settings_file(jarvis_home):
    from jarvis.config.settings import SettingsStore
    from jarvis.paths import AppPaths
    CredentialStore().set_anthropic_key(KEY)
    st = SettingsStore(AppPaths.resolve().settings_file)
    st.load()
    st.save()
    assert KEY not in st.path.read_text(encoding="utf-8")


# ---------- İstem ----------
def test_system_prompt_stable_block_is_cached_and_date_is_separate():
    a = system_blocks(datetime(2026, 11, 7, tzinfo=timezone.utc))
    b = system_blocks(datetime(2026, 11, 8, tzinfo=timezone.utc))
    assert a[0] == b[0] and a[0]["cache_control"] == {"type": "ephemeral"}
    assert a[1] != b[1] and "cache_control" not in a[1]
    assert "7 Kasım 2026" in a[1]["text"]
    assert "güvenilmeyen" in STABLE_SYSTEM_PROMPT


# ---------- Claude istemcisi (gerçek SDK + sahte HTTP) ----------
MSG = [{"role": "user", "content": "selam"}]


def test_chat_streams_text_and_counts_usage():
    api = FakeAPI([text_stream(["Mer", "haba"], model="claude-haiku-5-5")])
    got = []

    async def go():
        c = api.client()
        try:
            return await c.chat(HAIKU, system_blocks(), MSG, on_text=got.append)
        finally:
            await c.close()

    r = run(go())
    assert got == ["Mer", "haba"] and r.text == "Merhaba"
    assert r.usage == TokenUsage(1200, 85, 0, 900, 0)
    assert r.cost_usd == pytest.approx((1200 * 0.10 + 85 * 0.50 + 900 * 0.01) / 1e6)
    assert r.request_id == "req_ok"


def test_haiku_request_has_no_fallbacks_and_no_search_tool():
    api = FakeAPI([text_stream(["ok"])])

    async def go():
        c = api.client()
        await c.chat(HAIKU, system_blocks(), MSG, web_search=True)
        await c.close()

    run(go())
    body = api.requests[0]["body"]
    assert "fallbacks" not in body and "tools" not in body
    assert body["output_config"] == {"effort": "low"}
    assert "temperature" not in body and "thinking" not in body
    assert "anthropic-beta" not in api.headers[0]


def test_sonnet_request_has_fallback_search_and_citations():
    cite = {"type": "web_search_result_location", "url": "https://tcmb.gov.tr", "title": "TCMB",
            "encrypted_index": "x", "cited_text": "..."}
    api = FakeAPI([text_stream(["Kur 34"], model="claude-sonnet-5-5", citations=[cite],
                               usage={"server_tool_use": {"web_search_requests": 2}})])

    async def go():
        c = api.client()
        r = await c.chat(SONNET, system_blocks(), MSG, web_search=True)
        await c.close()
        return r

    r = run(go())
    body = api.requests[0]["body"]
    assert body["fallbacks"] == "default"
    assert body["tools"][0]["type"] == "web_search_20260209" and body["tools"][0]["max_uses"] == 3
    assert api.headers[0]["anthropic-beta"] == "server-side-fallback-2026-07-01"
    assert r.citations == [("TCMB", "https://tcmb.gov.tr")]
    assert r.usage.web_search_requests == 2


def test_refusal_is_reported_with_category():
    api = FakeAPI([text_stream([], stop_reason="refusal",
                               stop_details={"type": "refusal", "category": "cyber", "explanation": "x"})])

    async def go():
        c = api.client()
        r = await c.chat(HAIKU, system_blocks(), MSG)
        await c.close()
        return r

    r = run(go())
    assert r.refused and r.refusal_category == "cyber" and r.cost_usd > 0


def test_pause_turn_is_continued_append_only():
    api = FakeAPI([text_stream(["Arıyorum. "], model="claude-sonnet-5-5", stop_reason="pause_turn"),
                   text_stream(["Sonuç bu."], model="claude-sonnet-5-5")])

    async def go():
        c = api.client()
        r = await c.chat(SONNET, system_blocks(), MSG, web_search=True)
        await c.close()
        return r

    r = run(go())
    assert len(api.requests) == 2
    first, second = api.requests[0]["body"]["messages"], api.requests[1]["body"]["messages"]
    assert second[: len(first)] == first           # yalnızca ekleme, düzenleme yok
    assert second[-1]["role"] == "assistant"
    assert r.text == "Arıyorum.\n\nSonuç bu." and r.usage.output_tokens == 170


def test_server_fallback_costs_each_model_separately():
    usage = {"iterations": [
        {"type": "message", "model": "claude-sonnet-5-5", "input_tokens": 1000, "output_tokens": 10,
         "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        {"type": "fallback_message", "model": "claude-sonnet-5", "input_tokens": 1000, "output_tokens": 300,
         "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}]}
    api = FakeAPI([text_stream(["yedek cevap"], model="claude-sonnet-5", usage=usage)])

    async def go():
        c = api.client()
        r = await c.chat(SONNET, system_blocks(), MSG)
        await c.close()
        return r

    r = run(go())
    assert r.fallback_used and [line.spec.id for line in r.cost_lines] == ["claude-sonnet-5-5", "claude-sonnet-5"]
    assert r.cost_usd == pytest.approx((1000 * 2 + 10 * 10 + 1000 * 2 + 300 * 10) / 1e6)


@pytest.mark.parametrize("status,payload,kind", [
    (401, {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}, ErrorKind.AUTH),
    (403, {"type": "error", "error": {"type": "permission_error", "message": "no"}}, ErrorKind.PERMISSION),
    (404, {"type": "error", "error": {"type": "not_found_error", "message": "model"}}, ErrorKind.NOT_FOUND),
    (429, {"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}}, ErrorKind.RATE_LIMIT),
    (529, {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}, ErrorKind.OVERLOADED),
    (500, {"type": "error", "error": {"type": "api_error", "message": "boom"}}, ErrorKind.SERVER),
    (400, {"type": "error", "error": {"type": "invalid_request_error",
                                      "message": "Your credit balance is too low"}}, ErrorKind.SPEND_LIMIT),
    (400, {"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}}, ErrorKind.BAD_REQUEST),
])
def test_http_errors_are_classified(status, payload, kind):
    api = FakeAPI([(status, payload)])

    async def go():
        c = api.client()
        try:
            await c.chat(HAIKU, system_blocks(), MSG)
        finally:
            await c.close()

    with pytest.raises(ClaudeError) as e:
        run(go())
    assert e.value.kind is kind and e.value.user_message
    assert len(api.requests) == 1  # max_retries=0: sonsuz döngü yok


def test_connection_error_means_offline():
    api = FakeAPI([httpx2.ConnectError("ağ yok")])

    async def go():
        c = api.client()
        try:
            await c.chat(HAIKU, system_blocks(), MSG)
        finally:
            await c.close()

    with pytest.raises(ClaudeError) as e:
        run(go())
    assert e.value.is_offline


def test_retries_are_bounded():
    api = FakeAPI([(529, {"type": "error", "error": {"type": "overloaded_error", "message": "x"}})] * 3)

    async def go():
        c = api.client(max_retries=2)
        try:
            await c.chat(HAIKU, system_blocks(), MSG)
        finally:
            await c.close()

    with pytest.raises(ClaudeError):
        run(go())
    assert len(api.requests) == 3  # 1 deneme + 2 tekrar, fazlası yok


def test_env_base_url_cannot_redirect_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://kotu-niyetli.example.com")
    seen = []
    api = FakeAPI([text_stream(["ok"])])
    orig = api.handler

    def spy(req):
        seen.append(req.url.host)
        return orig(req)

    api.handler = spy

    async def go():
        c = api.client()
        await c.chat(HAIKU, system_blocks(), MSG)
        await c.close()

    run(go())
    assert seen == ["api.anthropic.com"]
