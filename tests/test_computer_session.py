"""Bilgisayar kullanan ajan oturumu: sahte API turları (araç seti blokları), sahte masaüstü ve saat."""

import asyncio
import json

import pytest

from jarvis.agent.backends import ForegroundInfo
from jarvis.agent.service import AgentStatus, ComputerAgent
from jarvis.agent.session import APP_TOOL_NAME, NOT_EXECUTED, SessionResult
from jarvis.ai.model_registry import TokenUsage, spec_for
from jarvis.ai.usage import CostLine, UsageTracker
from jarvis.config.settings import SettingsStore
from jarvis.core.events import EventBus, Topics
from jarvis.core.state import AssistantState, StateMachine
from jarvis.security.audit_logger import AuditLogger
from jarvis.security.permission_manager import Permission
from jarvis.storage.database import Database
from jarvis.tools import schema as js
from jarvis.tools.registry import ToolOutput, ToolRegistry, ToolSpec
from tests.agent_fakes import make_desktop
from tests.fake_api import FakeAPI, text_stream, toolset_stream

VALORANT_FG = ForegroundInfo("VALORANT", "UnrealWindow", "valorant-win64-shipping.exe", 7)
GOAL = "Valorant'ı aç ve takımlı ölüm kalım maçı başlat"


class Env:
    def __init__(self, tmp_path, api, desktop=None, registry=False, **computer):
        self.store = SettingsStore(tmp_path / "settings.json")
        self.store.load()
        for k, v in computer.items():
            setattr(self.store.settings.computer, k, v)
        self.bus, self.state = EventBus(), StateMachine()
        self.db = Database(tmp_path / "j.db")
        self.tracker = UsageTracker(self.db)
        self.api = api
        self.client = api.client()
        self.desktop = desktop or make_desktop()
        self.agent = ComputerAgent(self.store, self.bus, self.state, self.tracker, lambda: self.client,
                                   desktop_factory=lambda: self.desktop)
        if registry:
            self.agent.registry = ToolRegistry(audit=AuditLogger(self.db))
        self.events: list[AgentStatus] = []
        self.bus.subscribe(Topics.AGENT_STATUS, self.events.append)

    def run(self, goal: str = GOAL) -> SessionResult:
        async def go():
            self.agent.bind_loop(asyncio.get_running_loop())
            return await self.agent.run(goal)
        return asyncio.run(go())

    def bodies(self) -> list[dict]:
        return [r["body"] for r in self.api.requests if r["path"] == "/v1/messages"]

    def last_results(self, n: int = -1) -> list[dict]:
        return self.bodies()[n]["messages"][-1]["content"]


def test_multiple_actions_per_turn_run_in_order_with_one_result_message(tmp_path):
    api = FakeAPI([
        toolset_stream([("screenshot", {})]),
        toolset_stream([("left_click", {"coordinate": [683, 384]}), ("type", {"text": "valorant"}),
                        ("key", {"text": "Return"}), ("screenshot", {})], text="Arama kutusuna yazıyorum."),
        text_stream(["Sıraya girdim, eşleşme aranıyor."]),
    ])
    env = Env(tmp_path, api, registry=True)
    res = env.run()
    assert res.status == "done" and res.summary == "Sıraya girdim, eşleşme aranıyor."
    assert res.steps == 3 and res.actions == 5

    bodies = env.bodies()
    first = bodies[0]
    assert first["tools"][0] == {"type": "computer_toolset_20260801"}           # ad ve ekran boyutu yok
    assert first["tools"][1]["name"] == APP_TOOL_NAME
    assert "tool_choice" not in first                                            # zorunlu seçim 5.5'te 400
    assert first["context_management"]["edits"][0]["type"] == "clear_tool_uses_20250919"
    assert first["cache_control"] == {"type": "ephemeral"}
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert first["model"] == "claude-haiku-5-5" and first["output_config"] == {"effort": "low"}
    assert "1366x768" in first["messages"][0]["content"]
    beta = [h.get("anthropic-beta", "") for h, r in zip(api.headers, api.requests) if r["path"] == "/v1/messages"]
    assert all("context-management-2025-06-27" in b for b in beta)

    results = bodies[2]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["toolu_c0", "toolu_c1", "toolu_c2", "toolu_c3"]
    assert all(r["toolset_name"] == "computer" and r["is_error"] is False for r in results)
    assert [r["content"] for r in results[:3]] == ["OK", "OK", "OK"]
    assert results[3]["content"][0]["type"] == "image"
    assert results[3]["content"][0]["source"]["media_type"] in ("image/jpeg", "image/png")

    # geçmiş yalnızca eklenerek büyür; düşünme blokları değiştirilmeden geri gider
    assert bodies[2]["messages"][: len(bodies[1]["messages"])] == bodies[1]["messages"]
    assistant = bodies[1]["messages"][1]
    assert assistant["role"] == "assistant" and assistant["content"][0] == {
        "type": "thinking", "thinking": "", "signature": "sig-cu"}
    assert assistant["content"][1]["toolset_name"] == "computer"

    ev = env.desktop.input.events
    kinds = [e[0] for e in ev]
    assert kinds.index("button") < kinds.index("char") < max(i for i, e in enumerate(ev) if e == ("key", 0x0D, True))
    assert env.desktop.input.typed() == "valorant"

    rows = env.db.query("SELECT route_reason FROM usage")
    assert len(rows) == 3 and {r["route_reason"] for r in rows} == {"bilgisayar"}
    audit = env.db.query("SELECT tool, args_json, ok FROM audit_log ORDER BY id")
    tools = [a["tool"] for a in audit]
    assert tools == ["bilgisayar.screenshot", "bilgisayar.left_click", "bilgisayar.type", "bilgisayar.key",
                     "bilgisayar.screenshot"]
    typed = json.loads(audit[2]["args_json"])
    assert typed == {"uzunluk": 8}                                                # metnin kendisi kaydedilmez
    assert env.events[0].active and not env.events[-1].active and env.events[-1].result_status == "done"
    assert any("2. adım" in e.text for e in env.events)
    assert env.desktop.kill_switch.started == 1 and env.desktop.kill_switch.stopped == 1


def test_failure_stops_turn_and_later_blocks_are_not_executed(tmp_path):
    api = FakeAPI([
        toolset_stream([("left_click", {"coordinate": [5000, 10]}), ("type", {"text": "x"}), ("screenshot", {})]),
        text_stream(["Tıklayamadım."]),
    ])
    env = Env(tmp_path, api)
    env.run()
    results = env.last_results()
    assert results[0]["is_error"] is True and "dışında" in results[0]["content"]
    assert [r["content"] for r in results[1:]] == [NOT_EXECUTED, NOT_EXECUTED]
    assert all(r["is_error"] and r["toolset_name"] == "computer" for r in results)
    assert env.desktop.input.events == []
    assert env.desktop.screen.captures == 0


def test_zoom_returns_image_and_coordinates_stay_in_screenshot_space(tmp_path):
    api = FakeAPI([
        toolset_stream([("zoom", {"region": [0, 0, 341, 192]}), ("left_click", {"coordinate": [10, 10]})]),
        text_stream(["Tamam."]),
    ])
    env = Env(tmp_path, api)
    env.run()
    results = env.last_results()
    assert results[0]["content"][0]["type"] == "image"
    move = [e for e in env.desktop.input.events if e[0] == "move"][0]
    sx = 2560 / 1366
    assert move == ("move", int(10.5 * sx), int(10.5 * 1440 / 768))


def test_kill_switch_mid_run_stops_immediately(tmp_path):
    api = FakeAPI([toolset_stream([("wait", {"duration": 30}), ("screenshot", {})]), text_stream(["x"])])
    env = Env(tmp_path, api)
    clock = env.desktop.clock
    clock.on_sleep = lambda c: env.desktop.kill_switch.trigger() if c.slept > 2 else None
    res = env.run()
    assert res.status == "stopped" and "Esc Esc" in res.summary
    assert len(env.bodies()) == 1                         # durduktan sonra Claude'a istek gitmedi
    assert env.desktop.screen.captures == 0               # sonraki eylem yapılmadı
    assert clock.slept < 3
    assert env.agent.session is None and env.desktop.kill_switch.stopped == 1


def test_stop_text_only_while_active(tmp_path):
    env = Env(tmp_path, FakeAPI([]))
    assert env.agent.handle_stop_text("dur") is False     # çalışan oturum yok: normal komut olarak işlenir

    class Dummy:
        stopped = None

        def request_stop(self, reason):  # noqa: ANN001, ANN201
            self.stopped = reason

    env.agent.session = Dummy()
    notices = []
    env.bus.subscribe(Topics.NOTICE, notices.append)
    assert env.agent.handle_stop_text("Jarvis, dur!") is True
    assert env.agent.handle_stop_text("durdur") is True
    assert env.agent.handle_stop_text("dur bakalım şunu bir anlat bana") is False
    assert env.agent.session.stopped == "dur" and notices


def test_budget_exhausted_before_first_request(tmp_path):
    env = Env(tmp_path, FakeAPI([]))
    spec = spec_for("claude-haiku-5-5")
    env.tracker.record([CostLine(spec, TokenUsage(input_tokens=4_990_000))], "req_old", "test")   # ~$0.499
    res = env.run()
    assert res.status == "budget" and "Günlük" in res.summary
    assert env.bodies() == []


def test_session_cost_cap_stops_mid_run(tmp_path):
    big = {"input_tokens": 20_000}
    api = FakeAPI([toolset_stream([("screenshot", {})], usage=big), toolset_stream([("screenshot", {})], usage=big)])
    env = Env(tmp_path, api, max_cost_usd=0.004)
    res = env.run()
    assert res.status == "budget" and "$0.00" in res.summary
    assert len(env.bodies()) == 1 and res.cost_usd > 0
    assert len(env.db.query("SELECT * FROM usage")) == 1


def test_step_cap(tmp_path):
    api = FakeAPI([toolset_stream([("screenshot", {})]) for _ in range(2)])
    env = Env(tmp_path, api, max_steps=2)
    res = env.run()
    assert res.status == "step_limit" and len(env.bodies()) == 2


@pytest.mark.parametrize("stop_reason,status", [("refusal", "refused"), ("max_tokens", "error")])
def test_refusal_or_truncation_never_runs_tools(tmp_path, stop_reason, status):
    api = FakeAPI([toolset_stream([("left_click", {"coordinate": [5, 5]})], stop_reason=stop_reason)])
    env = Env(tmp_path, api)
    res = env.run()
    assert res.status == status and env.desktop.input.events == []
    assert len(env.db.query("SELECT * FROM usage")) == 1        # faturalanan ret de kaydedilir


def test_sensitive_window_stops_session(tmp_path):
    api = FakeAPI([toolset_stream([("screenshot", {})]), text_stream(["x"])])
    env = Env(tmp_path, api, desktop=make_desktop(fg=ForegroundInfo("", "", "consent.exe")), registry=True)
    res = env.run()
    assert res.status == "blocked" and "UAC" in res.summary
    assert len(env.bodies()) == 1 and env.desktop.screen.captures == 0
    assert env.db.query("SELECT decision FROM audit_log")[0]["decision"] == "yasak"


def test_vanguard_ignoring_clicks_is_reported_instead_of_looping(tmp_path):
    api = FakeAPI([
        toolset_stream([("screenshot", {})]),
        toolset_stream([("left_click", {"coordinate": [683, 40]}), ("screenshot", {})]),
        toolset_stream([("left_click", {"coordinate": [683, 40]}), ("screenshot", {})]),
        text_stream(["x"]),
    ])
    env = Env(tmp_path, api, desktop=make_desktop(static=True, fg=VALORANT_FG))
    res = env.run()
    assert res.status == "anticheat"
    assert res.summary.startswith("Valorant yazılımsal tıklamaları engelliyor olabilir")
    assert len(env.bodies()) == 3
    note = env.bodies()[2]["messages"][-1]["content"][1]["content"]
    assert note[1]["type"] == "text" and "değişiklik görülmedi" in note[1]["text"]


def test_changing_screen_does_not_trigger_anticheat(tmp_path):
    api = FakeAPI([toolset_stream([("screenshot", {})])]
                  + [toolset_stream([("left_click", {"coordinate": [683, 40]}), ("screenshot", {})])] * 3
                  + [text_stream(["Tamam."])])
    env = Env(tmp_path, api, desktop=make_desktop(static=False, fg=VALORANT_FG))
    assert env.run().status == "done"


def test_wall_time_limit(tmp_path):
    api = FakeAPI([toolset_stream([("wait", {"duration": 300})])])
    env = Env(tmp_path, api, max_minutes=0.5)
    res = env.run()
    assert res.status == "time_limit" and env.desktop.clock.slept <= 31


def test_gameplay_goal_refused_without_any_request(tmp_path):
    env = Env(tmp_path, FakeAPI([]))
    res = env.run("Valorant'ta maçı benim yerime oyna ve düşmanları vur")
    assert res.status == "refused" and "hile" in res.summary and env.bodies() == []


def test_unsupported_os_and_missing_key(tmp_path):
    env = Env(tmp_path, FakeAPI([]))
    env.agent.desktop_factory = lambda: None
    res = env.run()
    assert res.status == "error" and "yalnızca Windows" in res.summary
    env.agent.client_getter = lambda: None
    assert "anahtarı" in env.run().summary


def test_pause_turn_is_continued(tmp_path):
    api = FakeAPI([toolset_stream([], text="Bakıyorum", stop_reason="pause_turn"), text_stream(["Bitti."])])
    env = Env(tmp_path, api)
    assert env.run().summary == "Bitti."
    assert env.bodies()[1]["messages"][-1]["role"] == "assistant"


def test_custom_open_app_tool_result_has_no_toolset_name(tmp_path):
    api = FakeAPI([toolset_stream([("open_app_or_game", {"name": "VALORANT"}, None), ("wait", {"duration": 5})]),
                   text_stream(["Açtım."])])
    env = Env(tmp_path, api, registry=True)
    opened = []
    env.agent.registry.register(ToolSpec(
        name="launch_game", title="Oyun başlat", description="x / x", input_schema=js.obj(name=js.string("ad", 200, 1)),
        permission=Permission.SAFE, handler=lambda a: opened.append(a["name"]) or ToolOutput(f"{a['name']} başlatılıyor.")))
    res = env.run()
    assert res.status == "done" and opened == ["VALORANT"]
    results = env.last_results()
    assert results[0] == {"type": "tool_result", "tool_use_id": "toolu_c0", "content": "VALORANT başlatılıyor.",
                          "is_error": False}
    assert results[1]["toolset_name"] == "computer"
    assert not api.rejected


def test_executing_state_detail_shows_progress(tmp_path):
    api = FakeAPI([toolset_stream([("left_click", {"coordinate": [1, 1]})]), text_stream(["Tamam."])])
    env = Env(tmp_path, api)
    details = []
    env.state.add_listener(lambda ch: details.append((ch.new, ch.detail)))
    env.state.transition(AssistantState.PROCESSING)
    env.state.transition(AssistantState.EXECUTING, "Bilgisayarı kullan")
    env.run()
    assert (AssistantState.EXECUTING, "1. adım: tıklıyorum (1, 1)") in details
