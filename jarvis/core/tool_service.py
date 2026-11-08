"""Araç hizmeti: araç kaydını, onay akışını ve yerel komutları asistana bağlar.

- `run(name, args, source)`: aracı tüm güvenlik adımlarıyla çalıştırır; onay
  gerekirse arayüze `Topics.TOOL_CONFIRM` ile engellemeyen bir istek gönderir.
- `handle_local(text)`: yerel Türkçe komut ayrıştırıcısı eşleşirse Claude API'ye
  gitmeden aracı çalıştırır ve sonucu sohbet olaylarıyla (CHAT_STARTED/CHAT_DONE)
  gösterir. Eşleşmezse False döner, istek Claude'a gider.

Aşama 6'da Claude araç kullanımı da `registry.to_claude_tools()` ve `run()`
üzerinden bağlanacak.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from jarvis.config.settings import SettingsStore
from jarvis.core.chat_service import ChatDone, ChatStarted
from jarvis.core.events import EventBus, Topics
from jarvis.core.state import AssistantState, StateMachine
from jarvis.security.action_validator import PathSandbox
from jarvis.security.audit_logger import AuditLogger
from jarvis.security.permission_manager import ConfirmPrompt, PermissionManager, ToolConfirmRequest
from jarvis.storage.database import Database
from jarvis.tools import intents
from jarvis.tools.defaults import build_registry, default_context
from jarvis.tools.registry import ToolContext, ToolRegistry, ToolResult

log = logging.getLogger(__name__)

LOCAL_MODEL_NAME = "Yerel komut"


class ToolService:
    def __init__(self, store: SettingsStore, bus: EventBus, state: StateMachine, db: Database,
                 ctx: ToolContext | None = None, executor: ThreadPoolExecutor | None = None) -> None:
        self.store = store
        self.bus = bus
        self.state = state
        self.last_snapshot = None
        self.executor = executor or ThreadPoolExecutor(max_workers=4, thread_name_prefix="jarvis-tool")
        self._own_executor = executor is None
        s = store.settings.tools
        if ctx is None:
            sandbox = PathSandbox(extra_roots=s.extra_allowed_roots)
            ctx = default_context(sandbox, snapshot=lambda: self.last_snapshot)
        self.ctx = ctx
        self.audit = AuditLogger(db)
        self.permissions = PermissionManager(read_only=s.read_only)
        self.registry: ToolRegistry = build_registry(ctx, audit=self.audit, permissions=self.permissions,
                                                     executor=self.executor)
        self.ctx.scheduler.notify = lambda msg: self.bus.publish(Topics.NOTICE, msg)
        self.busy = False

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.ctx.scheduler.bind(loop)

    async def warm_up(self) -> None:
        """Uygulama dizinini arka planda hazırla: ilk "aç" komutu beklemesin."""
        if self.ctx.apps is None:
            return
        try:
            await asyncio.get_running_loop().run_in_executor(self.executor, self.ctx.apps.entries)
        except Exception:  # noqa: BLE001
            log.warning("Uygulama listesi hazırlanamadı", exc_info=True)

    async def confirm(self, prompt: ConfirmPrompt) -> bool:
        """Arayüzden onay ister; yanıt gelmezse ya da arayüz yoksa hayır sayılır."""
        if not self.bus.has_subscribers(Topics.TOOL_CONFIRM):
            log.warning("Onay istenecek arayüz yok: %s reddedildi", prompt.tool)
            return False
        fut = asyncio.get_running_loop().create_future()
        self.bus.publish(Topics.TOOL_CONFIRM, ToolConfirmRequest(prompt, fut))
        try:
            return bool(await asyncio.wait_for(fut, self.store.settings.tools.confirm_timeout_s))
        except asyncio.TimeoutError:
            self.bus.publish(Topics.NOTICE, "Onay süresi doldu, işlemi yapmadım.")
            return False

    async def run(self, name: str, args: dict, source: str = "arayuz") -> ToolResult:
        result = await self.registry.execute(name, args, self.confirm, source=source)
        self.bus.publish(Topics.TOOL_EXECUTED, result)
        return result

    async def handle_local(self, text: str, chat_busy: bool = False) -> bool:
        intent = intents.parse(text)
        if intent is None:
            return False
        spec = self.registry.get(intent.tool)
        if spec is None or not spec.supported:
            # Bu sistemde çalışmayan yerel komut: Claude'a bırakmak yerine dürüstçe söyle
            self.bus.publish(Topics.CHAT_STARTED, ChatStarted(text, LOCAL_MODEL_NAME, "yerel", False))
            self.bus.publish(Topics.CHAT_DONE, ChatDone(f"{intent.label}: bu işletim sisteminde desteklenmiyor.",
                                                        LOCAL_MODEL_NAME, 0.0))
            return True
        if self.busy or chat_busy:
            self.bus.publish(Topics.NOTICE, "Önceki isteği bitirmeden yenisini gönderemezsin.")
            return True
        self.busy = True
        try:
            if self.state.state is AssistantState.ERROR:
                self.state.reset()
            self.bus.publish(Topics.CHAT_STARTED, ChatStarted(text, LOCAL_MODEL_NAME, "yerel", False))
            self.state.transition(AssistantState.PROCESSING, "Yerel komut")
            self.state.transition(AssistantState.EXECUTING, spec.title)
            result = await self.run(intent.tool, intent.args, source="yerel")
            self.bus.publish(Topics.CHAT_DONE, ChatDone(result.summary, LOCAL_MODEL_NAME, 0.0))
        finally:
            self.busy = False
            if self.state.state not in (AssistantState.STANDBY, AssistantState.ERROR):
                self.state.reset()
        return True

    def shutdown(self) -> None:
        if self.ctx.scheduler is not None:
            self.ctx.scheduler.cancel_all()
        if self._own_executor:
            self.executor.shutdown(wait=False, cancel_futures=True)
