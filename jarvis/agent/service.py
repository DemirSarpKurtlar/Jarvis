"""Bilgisayar kullanan ajanı JARVIS'e bağlar.

- `ComputerAgent`: aynı anda tek oturum; Esc Esc acil durdurma, "dur" komutu, bütçe ve maliyet kaydı,
  ilerleme olayları (`Topics.AGENT_STATUS`), durum makinesi ayrıntısı ("3. adım: tıklıyorum ...").
- `register(reg, ctx)`: Claude'un ve yerel komutların çağırdığı `use_computer(goal)` aracı (CONFIRM).
  Araç işleyicisi araç thread'inde çalışır; oturumun kendisi asyncio döngüsünde yürür
  (`run_coroutine_threadsafe`), ekran/girdi işleri ajanın tek thread'lik yürütücüsünde.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from jarvis.agent.backends import Desktop
from jarvis.agent.safety import gameplay_goal_reason
from jarvis.agent.session import ComputerUseSession, SessionHooks, SessionResult, run_session_safely
from jarvis.ai.claude_client import AgentTurn
from jarvis.ai.model_registry import spec_for
from jarvis.core.events import EventBus, Topics
from jarvis.core.state import AssistantState, StateMachine
from jarvis.security.permission_manager import ConfirmPrompt, Permission
from jarvis.tools import schema as js
from jarvis.tools.registry import ToolContext, ToolError, ToolOutput, ToolRegistry, ToolSpec, WINDOWS_ONLY
from jarvis.tools.text import fold

log = logging.getLogger(__name__)

AGENT_KEY = "computer_agent"
INDICATOR_TEXT = "Bilgisayarı kullanıyorum — durdurmak için Esc Esc"
_STOP_WORDS = re.compile(r"^(?:(?:hey )?jarvis )?(?:dur|durdur|dur artik|durdur artik|kes|yeter|iptal|iptal et|"
                         r"stop|birak|vazgec|dur dur)(?: lutfen)?$")


@dataclass(frozen=True)
class AgentStatus:
    active: bool
    goal: str = ""
    step: int = 0
    text: str = ""
    result_status: str | None = None
    indicator: str = INDICATOR_TEXT


def _default_desktop() -> Desktop | None:
    from jarvis.agent.win32 import default_desktop

    return default_desktop()


class ComputerAgent:
    def __init__(self, store: Any, bus: EventBus, state: StateMachine, tracker: Any,
                 client_getter: Callable[[], Any], publish_usage: Callable[[], Awaitable[None]] | None = None,
                 desktop_factory: Callable[[], Desktop | None] = _default_desktop,
                 executor: ThreadPoolExecutor | None = None) -> None:
        self.store = store
        self.bus = bus
        self.state = state
        self.tracker = tracker
        self.client_getter = client_getter
        self.publish_usage = publish_usage
        self.desktop_factory = desktop_factory
        self.executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis-agent")
        self.registry: ToolRegistry | None = None      # uygulama açma ve denetim kaydı için
        self.loop: asyncio.AbstractEventLoop | None = None
        self.session: ComputerUseSession | None = None
        self.last_result: SessionResult | None = None
        self._lock = threading.Lock()

    @property
    def settings(self):  # noqa: ANN201
        return self.store.settings.computer

    @property
    def active(self) -> bool:
        return self.session is not None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    # ---------- durdurma ----------
    def request_stop(self, reason: str = "stop") -> bool:
        s = self.session
        if s is None:
            return False
        s.request_stop(reason)
        return True

    def handle_stop_text(self, text: str) -> bool:
        """Oturum çalışırken "dur" / "durdur" gibi bir komut gelirse durdurur ve True döner."""
        if not self.active or not isinstance(text, str) or len(text) > 40:
            return False
        if not _STOP_WORDS.match(fold(text).strip(" .!")):
            return False
        self.request_stop("dur")
        self.bus.publish(Topics.NOTICE, "Durduruyorum; bilgisayarı kullanmayı bırakıyorum.")
        return True

    # ---------- bağlar ----------
    async def _in_thread(self, fn: Callable, *args: Any) -> Any:  # noqa: ANN401
        return await asyncio.get_running_loop().run_in_executor(self.executor, fn, *args)

    async def _budget_reason(self, estimate: float, session_cost: float) -> str | None:
        s = self.settings
        if s.max_cost_usd > 0 and session_cost + estimate > s.max_cost_usd:
            return (f"Bu görev için ayrılan ${s.max_cost_usd:.2f} sınırına ulaştım ve durdum "
                    f"(harcanan ~${session_cost:.3f}). Ayarlar'dan artırabilirsin.")
        if self.tracker is None:
            return None
        budget = self.store.settings.budget
        st = await self._in_thread(self.tracker.status, budget)
        if st.daily_limit > 0 and st.today.cost_usd + estimate > st.daily_limit:
            return (f"Günlük Claude bütçen (${st.daily_limit:.2f}) doldu ya da dolmak üzere; bilgisayar kullanmayı "
                    "durdurdum.")
        if st.monthly_limit > 0 and st.month.cost_usd + estimate > st.monthly_limit:
            return f"Aylık Claude bütçen (${st.monthly_limit:.2f}) doldu; bilgisayar kullanmayı durdurdum."
        return None

    async def _record_cost(self, turn: AgentTurn) -> None:
        if self.tracker is None:
            return
        await self._in_thread(self.tracker.record, turn.cost_lines, turn.request_id, "bilgisayar")
        if self.publish_usage is not None:
            try:
                await self.publish_usage()
            except Exception:  # noqa: BLE001
                log.debug("Kullanım yayınlanamadı", exc_info=True)

    def _progress(self, goal: str) -> Callable[[int, str], None]:
        def report(step: int, text: str) -> None:
            detail = f"{step}. adım: {text}"
            self.bus.publish(Topics.AGENT_STATUS, AgentStatus(True, goal, step, detail))
            if self.state.state is AssistantState.EXECUTING:
                self.state.transition(AssistantState.EXECUTING, detail)
        return report

    def _audit(self, **kw: Any) -> None:
        reg = self.registry
        if reg is None:
            return
        reg.audit.record(source="bilgisayar", permission=Permission.CONFIRM.label, **kw)

    async def _open_app(self, name: str) -> tuple[bool, str]:
        reg = self.registry
        if reg is None:
            return False, "Uygulama açma aracı yok."
        game = await reg.execute("launch_game", {"name": name}, None, source="bilgisayar")
        if game.ok:
            return True, game.summary
        app = await reg.execute("open_app", {"name": name}, None, source="bilgisayar")
        if app.ok:
            return True, app.summary
        return False, app.error or game.error or "Açılamadı."

    # ---------- çalıştırma ----------
    async def run(self, goal: str) -> SessionResult:
        s = self.settings
        if not s.enabled:
            return SessionResult("error", "Bilgisayarı kullanma özelliği Ayarlar'dan kapatılmış.")
        refusal = gameplay_goal_reason(goal)
        if refusal:
            return SessionResult("refused", refusal)
        client = self.client_getter()
        if client is None:
            return SessionResult("error", "Claude API anahtarı girilmedi; bilgisayarı kullanmak için Claude gerekli.")
        with self._lock:
            if self.session is not None:
                return SessionResult("error", "Zaten bilgisayarı kullanan bir görev çalışıyor.")
            desktop = self.desktop_factory()
            if desktop is None:
                return SessionResult("error", "Bilgisayarı kullanma özelliği yalnızca Windows'ta çalışır.")
            spec = spec_for(s.model, "light")
            hooks = SessionHooks(self._budget_reason, self._record_cost, self._progress(goal), self._audit,
                                 self._open_app if self.registry is not None else None)
            session = ComputerUseSession(goal, client, spec, desktop, s, self.executor, hooks)
            self.session = session
        ks = desktop.kill_switch if s.kill_switch else None
        self.bus.publish(Topics.AGENT_STATUS, AgentStatus(True, goal, 0, "Başlıyorum"))
        try:
            if ks is not None:
                try:
                    ks.start(lambda: session.request_stop("esc"))
                except Exception:  # noqa: BLE001
                    log.warning("Esc Esc acil durdurma başlatılamadı", exc_info=True)
                    ks = None
            log.info("Bilgisayar oturumu başlıyor: model=%s hedef=%d karakter", spec.id, len(goal))
            result = await run_session_safely(session)
            if result.status == "stopped" and session._stop_reason == "esc":
                result.summary = "Esc Esc ile durdurdun; bilgisayarı kullanmayı bıraktım."
        finally:
            if ks is not None:
                try:
                    ks.stop()
                except Exception:  # noqa: BLE001
                    log.debug("Acil durdurma kapatılamadı", exc_info=True)
            with self._lock:
                self.session = None
        self.last_result = result
        self.bus.publish(Topics.AGENT_STATUS, AgentStatus(False, goal, result.steps, result.summary, result.status))
        return result

    def run_blocking(self, goal: str, timeout_s: float | None = None) -> SessionResult:
        """Araç thread'inden çağrılır: oturumu asyncio döngüsünde çalıştırıp bekler."""
        loop = self.loop
        if loop is None or loop.is_closed():
            raise ToolError("döngü yok", "Bilgisayar kullanma modülü henüz hazır değil.")
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            raise RuntimeError("run_blocking asyncio döngüsünün kendi thread'inden çağrılamaz")
        fut = asyncio.run_coroutine_threadsafe(self.run(goal), loop)
        try:
            return fut.result(timeout_s)
        except BaseException:
            self.request_stop("time")
            raise

    def shutdown(self) -> None:
        self.request_stop("kapanış")
        self.executor.shutdown(wait=False, cancel_futures=True)


# ---------- araç ----------
CONFIRM_TAIL = "JARVIS fareyi ve klavyeyi kullanacak; durdurmak için Esc'ye 2 kez bas veya sohbete 'dur' yaz."
TOOL_TIMEOUT_S = 30 * 60 + 120   # oturum kendi süre sınırını (ayar, en çok 30 dk) uygular


def _agent(ctx: ToolContext) -> ComputerAgent:
    agent = ctx.extra.get(AGENT_KEY)
    if agent is None:
        raise ToolError("ajan yok", "Bilgisayar kullanma modülü hazır değil (Claude bağlantısı kurulmadı).")
    return agent


def register(reg: ToolRegistry, ctx: ToolContext) -> None:
    def prepare(args: dict) -> dict:
        goal = " ".join(args["goal"].split())
        if len(goal) < 3:
            raise ToolError("kısa", "Ne yapmamı istediğini biraz daha açık yazar mısın?")
        refusal = gameplay_goal_reason(goal)
        if refusal:
            raise ToolError("oyun", refusal)
        agent = _agent(ctx)
        if not agent.settings.enabled:
            raise ToolError("kapalı", "Bilgisayarı kullanma özelliği Ayarlar'dan kapatılmış.")
        if agent.active:
            raise ToolError("meşgul", "Zaten bilgisayarı kullanan bir görev çalışıyor.")
        return {"goal": goal}

    def handler(args: dict) -> ToolOutput:
        result = _agent(ctx).run_blocking(args["goal"])
        data = {"status": result.status, "steps": result.steps, "actions": result.actions,
                "cost_usd": round(result.cost_usd, 4)}
        if not result.ok:
            raise ToolError(result.status, result.summary)
        # Özet, ekran görüntülerine bakan modelden geliyor: güvenilmeyen veri
        return ToolOutput(result.summary, data, untrusted=True)

    def confirm(args: dict) -> ConfirmPrompt:
        return ConfirmPrompt("use_computer", "Bilgisayarı JARVIS kullanacak",
                             f"Hedef: {args['goal']}\n\n{CONFIRM_TAIL}", confirm_label="Başlat")

    reg.register(ToolSpec(
        name="use_computer", title="Bilgisayarı kullan", category="bilgisayar", platforms=WINDOWS_ONLY,
        description=("Ekrana bakıp fare ve klavyeyle herhangi bir uygulamayı kullanır (GUI otomasyonu): başka bir "
                     "araçla yapılamayan, tıklama/yazma gerektiren işler için. Hedefi tek cümlede, sonucu tarif "
                     "ederek ver. Oyunlarda yalnızca menü/lobi (ör. mod seçip sıraya girme), maç içi oynama yok. "
                     "Parola/ödeme girmez. / Operates any app by looking at the screen and using mouse and "
                     "keyboard; use only when no other tool can do the task. Menus/lobbies only in games."),
        input_schema=js.obj(goal=js.string("Tamamlanacak hedef, Türkçe / goal to accomplish", 500, 3)),
        permission=Permission.CONFIRM, handler=handler, prepare=prepare, confirm=confirm,
        timeout_s=TOOL_TIMEOUT_S, core=True))
