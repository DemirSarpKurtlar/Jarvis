"""Yazılı sohbet akışı: bütçe -> yönlendirme -> Claude -> maliyet kaydı -> konuşma kaydı.

Bu sınıf arayüzü bilmez; her şeyi olay veriyolu üzerinden duyurur.
Ses motoru (Aşama 3) da aynı `send()` yöntemini kullanacak.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from collections.abc import Callable
from concurrent.futures import Executor
from dataclasses import dataclass, field
from enum import Enum

from jarvis.ai.claude_client import ClaudeClient, ClaudeError, ErrorKind, ToolOutcome
from jarvis.ai.model_registry import ModelRegistry, spec_for
from jarvis.ai.prompts import system_blocks
from jarvis.ai.router import route
from jarvis.ai.usage import BudgetStatus, UsageTracker
from jarvis.config.settings import SettingsStore
from jarvis.core.conversation import ConversationManager
from jarvis.core.events import EventBus, Topics
from jarvis.core.state import AssistantState, StateMachine
from jarvis.security.credentials import CredentialError, CredentialStore, looks_like_anthropic_key
from jarvis.storage.database import Database
from jarvis.agent.service import AGENT_KEY, ComputerAgent  # BILGISAYAR

log = logging.getLogger(__name__)


class ClaudeStatus(str, Enum):
    NO_KEY = "Anahtar girilmedi"
    CHECKING = "Kontrol ediliyor"
    CONNECTED = "Bağlı"
    OFFLINE = "Bağlantı yok"
    ERROR = "Hata"


@dataclass(frozen=True)
class ChatStarted:
    user_text: str
    model_name: str
    reason: str
    web_search: bool
    note: str | None = None


@dataclass(frozen=True)
class ChatDone:
    text: str
    model_name: str
    cost_usd: float
    citations: list[tuple[str, str]] = field(default_factory=list)
    refused: bool = False
    fallback_used: bool = False
    note: str | None = None


@dataclass
class ConfirmRequest:
    question: str
    future: asyncio.Future


ClientFactory = Callable[..., ClaudeClient]


class ChatService:
    def __init__(self, store: SettingsStore, bus: EventBus, state: StateMachine, db: Database,
                 executor: Executor, credentials: CredentialStore | None = None,
                 client_factory: ClientFactory = ClaudeClient) -> None:
        self.store = store
        self.bus = bus
        self.state = state
        self.executor = executor
        self.credentials = credentials or CredentialStore()
        self.client_factory = client_factory
        self.tracker = UsageTracker(db)
        self.conversation = ConversationManager(db)
        self.status = ClaudeStatus.NO_KEY
        self.status_detail = ""
        self.busy = False
        self._client: ClaudeClient | None = None
        self.memory = None  # Aşama 4: isteğe bağlı MemoryService (Assistant bağlar)
        # --- BILGISAYAR: ekrana bakıp fare/klavye kullanan ajan (use_computer aracı) ---
        self.computer = ComputerAgent(store, bus, state, self.tracker, lambda: self._client, self.publish_usage)
        # --- BILGISAYAR sonu ---
        self.tools = None   # Aşama 6: isteğe bağlı ToolService (Assistant bağlar); Claude araç kullanır

    # --- BILGISAYAR: araç hizmeti bağlanınca ajan da araç bağlamına eklenir ---
    @property
    def tools(self):  # noqa: ANN201
        return self._tools

    @tools.setter
    def tools(self, value) -> None:  # noqa: ANN001
        self._tools = value
        if value is not None and getattr(value, "ctx", None) is not None:
            value.ctx.extra[AGENT_KEY] = self.computer
            self.computer.registry = value.registry
    # --- BILGISAYAR sonu ---

    @property
    def registry(self) -> ModelRegistry:
        return ModelRegistry(self.store.settings.ai)

    async def _run(self, fn, *args):  # noqa: ANN001, ANN202
        return await asyncio.get_running_loop().run_in_executor(self.executor, fn, *args)

    def _set_status(self, status: ClaudeStatus, detail: str = "") -> None:
        self.status, self.status_detail = status, detail
        self.bus.publish(Topics.CLAUDE_STATUS, self)

    async def budget(self) -> BudgetStatus:
        return await self._run(self.tracker.status, self.store.settings.budget)

    async def publish_usage(self) -> None:
        self.bus.publish(Topics.USAGE_UPDATED, await self.budget())

    # ---------- anahtar ve bağlantı ----------
    async def start(self) -> None:
        self.computer.bind_loop(asyncio.get_running_loop())  # BILGISAYAR
        await self.publish_usage()
        # SDK'yı arka plan thread'inde önceden yükle: arayüz thread'i beklemesin
        await self._run(importlib.import_module, "anthropic")
        await self.reload_client(verify=True)
        if self.memory is not None:  # Aşama 4: önceki oturumdan kalan konuşmaları özetle
            self.memory.schedule_maintenance(self.conversation.conversation_id)

    async def _close_client(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:  # noqa: BLE001
                log.debug("İstemci kapatılırken hata", exc_info=True)
            self._client = None

    def _new_client(self, key: str) -> ClaudeClient:
        return self.client_factory(key, max_retries=self.store.settings.ai.max_retries)

    async def reload_client(self, verify: bool = True) -> None:
        await self._close_client()
        key = await self._run(self.credentials.get_anthropic_key)
        if not key:
            self._set_status(ClaudeStatus.NO_KEY)
            return
        self._client = self._new_client(key)
        if not verify:
            self._set_status(ClaudeStatus.CONNECTED)
            return
        self._set_status(ClaudeStatus.CHECKING)
        try:
            await self._client.verify(self.registry.for_tier("light").id)
            self._set_status(ClaudeStatus.CONNECTED)
        except ClaudeError as e:
            self._set_status(ClaudeStatus.OFFLINE if e.is_offline else ClaudeStatus.ERROR, e.user_message)

    async def save_key(self, key: str) -> str:
        """Anahtarı doğrular ve güvenli depoya kaydeder. Kullanıcıya gösterilecek sonucu döndürür."""
        key = key.strip()
        if not looks_like_anthropic_key(key):
            raise CredentialError("biçim", "Bu bir Claude API anahtarına benzemiyor. Anahtar 'sk-ant-' ile başlamalı.")
        probe = self._new_client(key)
        offline = False
        try:
            await probe.verify(self.registry.for_tier("light").id)
        except ClaudeError as e:
            if not e.is_offline:
                raise CredentialError(str(e), e.user_message) from e
            offline = True
        finally:
            await probe.close()
        await self._run(self.credentials.set_anthropic_key, key)
        await self.reload_client(verify=offline)
        if offline:
            return "Anahtar kaydedildi ama internet olmadığı için doğrulanamadı."
        return "Anahtar doğrulandı ve Windows Credential Manager'a kaydedildi."

    async def delete_key(self) -> None:
        await self._run(self.credentials.delete_anthropic_key)
        await self.reload_client(verify=False)

    # ---------- sohbet ----------
    async def send(self, text: str) -> ChatDone | None:
        text = text.strip()
        if not text:
            return None
        if self.busy:
            self.bus.publish(Topics.NOTICE, "Önceki isteği bitirmeden yenisini gönderemezsin.")
            return None
        if self._client is None:
            self.bus.publish(Topics.CHAT_FAILED,
                             "Claude API anahtarı girilmedi. Ayarlar ekranındaki Claude bölümünden ekleyebilirsin.")
            return None
        self.busy = True
        try:
            return await self._send(text)
        finally:
            self.busy = False
            if self.state.state not in (AssistantState.STANDBY, AssistantState.ERROR):
                self.state.reset()

    async def _send(self, text: str) -> ChatDone | None:
        settings = self.store.settings
        if self.state.state is AssistantState.ERROR:
            self.state.reset()

        r = route(text, settings, await self.budget())
        if r.blocked:
            self.bus.publish(Topics.CHAT_FAILED, r.budget_note or "Bütçe doldu.")
            return None
        if r.needs_confirmation:
            fut = asyncio.get_running_loop().create_future()
            self.bus.publish(Topics.CONFIRM_REQUEST, ConfirmRequest(r.budget_note or "", fut))
            if not await fut:
                self.bus.publish(Topics.NOTICE, "İstek gönderilmedi.")
                return None

        spec = self.registry.for_tier(r.tier)
        web = r.web_search and spec.web_search_tool is not None
        notes = [n for n in (r.budget_note,) if n]
        if r.web_search and not web:
            notes.append(f"{spec.display_name} web araması yapamıyor; cevap güncel olmayabilir.")
        if spec.pricing_assumed:
            notes.append(f"'{spec.id}' modelinin fiyatı bilinmiyor; maliyet tahmini yaklaşık.")
        note = " ".join(notes) or None

        await self._run(self.conversation.log_user, text)
        messages = self.conversation.api_messages(text)
        memory_text = await self._memory_context(text)  # Aşama 4
        used = len(self.memory.last_used) if self.memory is not None and memory_text else 0
        self.state.transition(AssistantState.PROCESSING,
                              f"{spec.display_name} ile düşünüyorum" + (", web'de arıyorum" if web else "")
                              + (f", hafızadan {used} kayıt kullanılıyor" if used else ""))
        self.bus.publish(Topics.CHAT_STARTED, ChatStarted(text, spec.display_name, r.reason, web, note))

        client_tools, executor = self._tool_bridge(spec)
        try:
            result = await self._client.chat(  # type: ignore[union-attr]
                spec, system_blocks(extra=memory_text), messages, web_search=web,
                on_text=lambda t: self.bus.publish(Topics.CHAT_DELTA, t),
                client_tools=client_tools, tool_executor=executor,
            )
        except ClaudeError as e:
            if e.is_offline:
                self._set_status(ClaudeStatus.OFFLINE, e.user_message)
            elif e.kind in (ErrorKind.AUTH, ErrorKind.PERMISSION):
                self._set_status(ClaudeStatus.ERROR, e.user_message)
            self.bus.publish(Topics.CHAT_FAILED, e.user_message)
            self.state.fail(e.user_message)
            return None

        # Faturalanan her şey kaydedilir (ret dahil)
        cost = await self._run(self.tracker.record, result.cost_lines, result.request_id, r.reason)
        served_name = spec_for(result.model_served, r.tier).display_name
        if result.fallback_used:
            note = ((note + " ") if note else "") + f"Yanıtı yedek model ({served_name}) verdi."
        if result.tool_runs:  # Aşama 6: Claude'un bilgisayarda yaptıkları görünür olsun
            done_list = "; ".join((t.summary or t.name) + ("" if t.ok else " (yapılamadı)") for t in result.tool_runs)
            note = ((note + " ") if note else "") + f"Yapılan işlemler: {done_list}"
        if result.tool_limit_hit:
            note = ((note + " ") if note else "") + "İşlem adımı sınırına ulaşıldı; görev yarım kalmış olabilir."

        if result.refused:
            cat = f" ({result.refusal_category})" if result.refusal_category else ""
            done = ChatDone(f"Bu isteğe yanıt veremiyorum; Claude'un güvenlik filtresi devreye girdi{cat}.",
                            served_name, cost, refused=True, note=note)
        elif not result.text:
            done = ChatDone("Claude boş bir yanıt döndürdü. Soruyu biraz farklı sorabilir misin?",
                            served_name, cost, note=note)
        else:
            answer = result.text
            if result.stop_reason == "max_tokens":
                note = ((note + " ") if note else "") + "Yanıt uzunluk sınırına ulaştığı için kesildi."
            await self._run(self.conversation.commit_exchange, text, answer, result.model_served, cost)
            if self.memory is not None:  # Aşama 4: arka planda; cevabı geciktirmez
                self.memory.after_exchange(self.conversation.conversation_id, text, answer)
            done = ChatDone(answer, served_name, cost, result.citations, fallback_used=result.fallback_used,
                            note=note)
        self._set_status(ClaudeStatus.CONNECTED)
        # Önce maliyet paneli güncellensin; cevap "tamam" olduğunda tutar da güncel olsun
        await self.publish_usage()
        self.bus.publish(Topics.CHAT_DONE, done)
        return done

    # ---- Aşama 6: Claude araç kullanımı ----
    def _tool_bridge(self, spec):  # noqa: ANN001, ANN202
        """Claude'a gidecek araç listesi ve çağrıları güvenlik katmanından geçiren yürütücü."""
        if self.tools is None or not self.store.settings.tools.claude_can_use_tools:
            return None, None
        self.computer.bind_loop(asyncio.get_running_loop())  # BILGISAYAR
        # BILGISAYAR: çekirdek dışı araçlar ertelenir, Claude gerektiğinde araç aramasıyla bulur (token tasarrufu)
        tools = self.tools.registry.to_claude_tools(tool_search=True)

        async def execute(name: str, args: dict) -> ToolOutcome:
            reg_spec = self.tools.registry.get(name)
            title = getattr(reg_spec, "title", name)
            if self.state.can_transition(AssistantState.EXECUTING):
                self.state.transition(AssistantState.EXECUTING, title)
            try:
                res = await self.tools.run(name, args, source="claude")
            finally:
                if self.state.state is AssistantState.EXECUTING:
                    self.state.transition(AssistantState.PROCESSING, f"{spec.display_name} ile düşünüyorum")
            return ToolOutcome(res.to_claude_content(), is_error=not res.ok, summary=res.summary)

        return tools, execute

    # ---- Aşama 4: hafıza ----
    async def _memory_context(self, text: str) -> str | None:
        if self.memory is None:
            return None
        try:
            return await self.memory.context_for(text, self.conversation.conversation_id)
        except Exception:  # noqa: BLE001
            log.exception("Hafıza bağlamı hazırlanamadı; istek hafızasız gönderiliyor")
            return None

    async def shutdown(self) -> None:
        self.computer.shutdown()  # BILGISAYAR
        if self.memory is not None:
            await self.memory.shutdown(self.conversation.conversation_id)
        await self._close_client()
