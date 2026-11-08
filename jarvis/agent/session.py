"""Bilgisayar kullanan ajan oturumu: tek bir hedef için Claude ile ekran -> eylem döngüsü.

Claude 5.5 modellerinde bilgisayar kullanımı `computer_toolset_20260801` araç setiyle yapılır: araç
listesinde adı olmayan tek bir girdi; Claude'un çağrıları `name` alanı üye adı (screenshot, left_click,
type...) olan ve `toolset_name: "computer"` taşıyan `tool_use` bloklarıdır, bir turda birden çok olabilir.

Kurallar (API ve güvenlik):
- Bir turdaki bloklar SIRAYLA yürütülür; her `tool_use`'a bir `tool_result` (hepsi tek kullanıcı
  mesajında, araç seti üyelerinde `toolset_name: "computer"` ile) döner. İlk hatada durulur: hatalı olan
  `is_error` ile, sonrakiler "Not executed: ..." ile yanıtlanır.
- Asistan içeriği (düşünme blokları dahil) değiştirilmeden geri gönderilir; geçmiş yalnızca eklenerek
  büyür. Eski ekran görüntüleri istemcide SİLİNMEZ (5.5 modellerinde korunan düşünmeyi bozar); bunun
  yerine sunucu tarafı bağlam düzenleme (`clear_tool_uses_20250919`) eski araç sonuçlarını temizler.
- Ret (`refusal`) ya da uzunluk sınırında (`max_tokens`) kesilmiş turda araç çalıştırılmaz.
- Zorunlu `tool_choice` 5.5 modellerinde 400 verir: `auto` (varsayılan) kullanılır.
- Her istek bütçeye yazılır; bütçe, oturum maliyet sınırı, adım ve süre sınırı aşılırsa durulur.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import Executor
from dataclasses import dataclass, field
from typing import Any

from jarvis.agent import imaging
from jarvis.agent.actions import ActionError, ActionExecutor, ActionResult, Interrupted, SafetyStop, describe
from jarvis.agent.backends import Desktop, ForegroundInfo
from jarvis.agent.geometry import ScreenScaler
from jarvis.agent.prompts import COMPUTER_SYSTEM_PROMPT, goal_message
from jarvis.agent.safety import gameplay_goal_reason, is_vanguard_game
from jarvis.ai.claude_client import AgentTurn, ClaudeError
from jarvis.ai.model_registry import ModelSpec
from jarvis.config.settings import ComputerUseSettings

log = logging.getLogger(__name__)

TOOLSET_NAME = "computer"
COMPUTER_TOOLSET = {"type": "computer_toolset_20260801"}
CONTEXT_BETA = "context-management-2025-06-27"
# Eski ekran görüntüleri sunucuda temizlenir: ~25K giriş tokenını geçince, son 4 araç çağrısı kalır.
# clear_at_least: temizlik önbelleği yeniden yazdırır; ancak en az 6K token kazandıracaksa yapılsın.
CONTEXT_MANAGEMENT = {"edits": [{
    "type": "clear_tool_uses_20250919",
    "trigger": {"type": "input_tokens", "value": 25_000},
    "keep": {"type": "tool_uses", "value": 4},
    "clear_at_least": {"type": "input_tokens", "value": 6_000},
}]}
NOT_EXECUTED = "Not executed: an earlier computer action in this turn failed."
APP_TOOL_NAME = "open_app_or_game"
APP_TOOL = {
    "name": APP_TOOL_NAME,
    "description": ("Kurulu bir uygulamayı ya da oyunu adıyla açar (Steam, Epic, Riot, Xbox oyunları dahil). "
                    "Başlat menüsünde aramaktan hızlıdır. / Opens an installed app or game by name."),
    "input_schema": {"type": "object", "additionalProperties": False, "required": ["name"],
                     "properties": {"name": {"type": "string", "description": "Uygulama ya da oyun adı / name"}}},
    "strict": True,
}
MIN_TURN_ESTIMATE_USD = 0.002
NO_CHANGE_RATIO = 0.005        # tüm ekranda değişen hücre oranı bunun altındaysa "değişmedi"
NO_CHANGE_LOCAL = 0.01         # tıklanan noktanın çevresinde
VANGUARD_NO_CHANGE_LIMIT = 2
GENERAL_NO_CHANGE_LIMIT = 4

ANTICHEAT_MESSAGE = ("Valorant yazılımsal tıklamaları engelliyor olabilir: oyun penceresine gönderdiğim tıklamalar "
                     "ekranda hiçbir değişiklik yapmadı. Riot Vanguard hile önleyicisi yazılımla gönderilen fare ve "
                     "klavye girdisini yok sayabiliyor; bunu aşmaya çalışmıyorum. Kalan adımı (mod seçip Başlat) "
                     "sen yapmalısın.")
NO_EFFECT_MESSAGE = ("Tıklamalarım ekranda hiçbir değişiklik yapmıyor; pencere yazılımsal girdiyi engelliyor ya da "
                     "yönetici olarak çalışıyor olabilir. Bu yüzden durdum.")


@dataclass
class SessionResult:
    status: str          # done / stopped / time_limit / step_limit / budget / refused / blocked / anticheat / error
    summary: str         # kullanıcıya ve ana sohbete gidecek Türkçe metin
    steps: int = 0
    actions: int = 0
    cost_usd: float = 0.0
    model: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "done"


@dataclass
class SessionHooks:
    """Oturumun dış dünyayla bağları (hizmet verir; testler sahtesini)."""

    budget_check: Callable[[float, float], Awaitable[str | None]]       # (tahmin, oturum maliyeti) -> neden
    record_cost: Callable[[AgentTurn], Awaitable[None]]
    progress: Callable[[int, str], None] = lambda step, text: None
    audit: Callable[..., None] = lambda **kw: None                        # thread'den çağrılır
    open_app: Callable[[str], Awaitable[tuple[bool, str]]] | None = None


@dataclass
class _ChangeWatch:
    last_thumb: Any = None
    pending_click: tuple[float, float] | None = None
    streak: int = 0


@dataclass
class ComputerUseSession:
    goal: str
    client: Any                      # ClaudeClient (agent_turn)
    spec: ModelSpec
    desktop: Desktop
    settings: ComputerUseSettings
    executor: Executor
    hooks: SessionHooks
    stop_event: threading.Event = field(default_factory=threading.Event)
    steps: int = 0
    actions: int = 0
    cost_usd: float = 0.0
    messages: list[dict] = field(default_factory=list)
    _astop: asyncio.Event | None = None
    _loop: asyncio.AbstractEventLoop | None = None
    _stop_reason: str = "stop"

    # ---------- durdurma ----------
    def request_stop(self, reason: str = "stop") -> None:
        """Herhangi bir thread'den çağrılabilir (Esc Esc, "dur" komutu)."""
        self._stop_reason = reason
        self.stop_event.set()
        loop, ev = self._loop, self._astop
        if loop is not None and ev is not None and not loop.is_closed():
            loop.call_soon_threadsafe(ev.set)

    async def _in_thread(self, fn: Callable, *args: Any) -> Any:  # noqa: ANN401
        return await asyncio.get_running_loop().run_in_executor(self.executor, fn, *args)

    # ---------- ana döngü ----------
    async def run(self) -> SessionResult:
        self._loop = asyncio.get_running_loop()
        self._astop = asyncio.Event()
        if self.stop_event.is_set():
            self._astop.set()
        refusal = gameplay_goal_reason(self.goal)
        if refusal:
            return self._result("refused", refusal)
        clock = self.desktop.clock
        assert clock is not None
        deadline = clock.monotonic() + self.settings.max_minutes * 60
        try:
            scaler = await self._in_thread(self._pick_screen)
        except Exception:  # noqa: BLE001
            log.exception("Ekran bilgisi alınamadı")
            return self._result("error", "Ekran bilgisi alınamadı; bilgisayarı kullanamıyorum.")
        ex = ActionExecutor(self.desktop, scaler, self.stop_event)
        ex.deadline = deadline
        watch = _ChangeWatch()
        system = [{"type": "text", "text": COMPUTER_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]
        tools = [dict(COMPUTER_TOOLSET)] + ([dict(APP_TOOL)] if self.hooks.open_app else [])
        self.messages = [{"role": "user", "content": goal_message(self.goal)
                          + f"\n(Ekran görüntüsü boyutu: {scaler.width}x{scaler.height} piksel.)"}]
        last_cost = 0.0
        try:
            while True:
                if self.stop_event.is_set():
                    return self._stopped()
                if clock.monotonic() >= deadline:
                    return self._time_limit()
                if self.steps >= self.settings.max_steps:
                    return self._result("step_limit", f"Adım sınırına ({self.settings.max_steps}) ulaştım ve durdum; "
                                                      "görev yarım kalmış olabilir.")
                why = await self.hooks.budget_check(max(last_cost * 1.5, MIN_TURN_ESTIMATE_USD), self.cost_usd)
                if why:
                    return self._result("budget", why)
                self.steps += 1
                self.hooks.progress(self.steps, "düşünüyorum")
                turn = await self._request(system, tools, deadline)
                if turn is None:
                    return self._stopped() if self.stop_event.is_set() else self._time_limit()
                await self.hooks.record_cost(turn)
                self.cost_usd += turn.cost_usd
                last_cost = turn.cost_usd
                msg = turn.message
                if turn.stop_reason == "refusal":
                    return self._result("refused", "Claude bu görevi güvenlik nedeniyle reddetti; hiçbir eylem "
                                                   "yapmadım.")
                if turn.stop_reason == "max_tokens":
                    return self._result("error", "Claude'un yanıtı uzunluk sınırında kesildi; yarım kalan eylemi "
                                                 "güvenlik için çalıştırmadım.")
                content = list(getattr(msg, "content", []) or [])
                tool_uses = [b for b in content if getattr(b, "type", "") == "tool_use"]
                if turn.stop_reason == "pause_turn":
                    self.messages.append({"role": "assistant", "content": content})
                    continue
                if turn.stop_reason != "tool_use" or not tool_uses:
                    return self._result("done", turn.text or "Görevi bitirdim.")
                self.messages.append({"role": "assistant", "content": content})
                results, stop = await self._run_blocks(tool_uses, ex, watch)
                if stop is not None:
                    return stop
                self.messages.append({"role": "user", "content": results})
        finally:
            try:
                await self._in_thread(ex.release_all)
            except Exception:  # noqa: BLE001
                log.debug("Bırakma başarısız", exc_info=True)

    def _pick_screen(self) -> ScreenScaler:
        screen = self.desktop.screen
        mons = screen.monitors()
        if not mons:
            raise OSError("Ekran bulunamadı")
        mon = mons[0]
        if self.settings.monitor == "active":
            mon = screen.monitor_of_foreground() or mon
        return ScreenScaler.fit(mon, self.settings.screenshot_long_edge)

    async def _request(self, system: list[dict], tools: list[dict], deadline: float) -> AgentTurn | None:
        """İsteği gönderir; durdurma ya da süre dolarsa iptal eder ve None döner."""
        assert self._astop is not None and self.desktop.clock is not None
        task = asyncio.ensure_future(self.client.agent_turn(
            self.spec, system, self.messages, tools, effort=self.settings.effort, betas=[CONTEXT_BETA],
            context_management=CONTEXT_MANAGEMENT))
        waiter = asyncio.ensure_future(self._astop.wait())
        remaining = max(1.0, deadline - self.desktop.clock.monotonic())
        try:
            done, _ = await asyncio.wait({task, waiter}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
        if task not in done:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if not self.stop_event.is_set():
                self._stop_reason = "time"
            return None
        return task.result()   # ClaudeError yukarıda işlenir

    async def _run_blocks(self, blocks: list, ex: ActionExecutor,
                          watch: _ChangeWatch) -> tuple[list[dict], SessionResult | None]:
        results: list[dict] = []
        failed = False
        for block in blocks:
            is_toolset = getattr(block, "toolset_name", None) == TOOLSET_NAME
            base: dict = {"type": "tool_result", "tool_use_id": block.id}
            if is_toolset:
                base["toolset_name"] = TOOLSET_NAME
            if failed:
                results.append({**base, "content": NOT_EXECUTED, "is_error": True})
                continue
            args = dict(block.input) if isinstance(block.input, dict) else {}
            if not is_toolset:
                content, ok = await self._custom_tool(block.name, args)
                results.append({**base, "content": content, "is_error": not ok})
                failed = not ok
                continue
            self.actions += 1
            self.hooks.progress(self.steps, describe(block.name, args))
            try:
                fg, res = await self._in_thread(self._guarded, ex, block.name, args)
            except SafetyStop as e:
                await self._in_thread(lambda: self._audit(block.name, args, False, str(e), "yasak"))
                return results, self._result("blocked", str(e))
            except Interrupted as e:
                return results, (self._time_limit() if (e.args and e.args[0] == "time")
                                 and not self.stop_event.is_set() else self._stopped())
            except ActionError as e:
                await self._in_thread(lambda: self._audit(block.name, args, False, str(e), "gecersiz"))
                results.append({**base, "content": str(e), "is_error": True})
                failed = True
                continue
            except Exception as e:  # noqa: BLE001
                log.exception("Bilgisayar eylemi başarısız: %s", block.name)
                msg = f"Eylem yapılamadı ({e.__class__.__name__})."
                await self._in_thread(lambda: self._audit(block.name, args, False, msg, "izin"))
                results.append({**base, "content": msg, "is_error": True})
                failed = True
                continue
            await self._in_thread(lambda: self._audit(block.name, args, True, None, "izin"))
            stop = self._watch_changes(watch, block.name, res, fg)
            if stop is not None:
                return results, stop
            results.append({**base, "content": res.content(), "is_error": False})
        return results, None

    @staticmethod
    def _guarded(ex: ActionExecutor, member: str, args: dict) -> tuple[ForegroundInfo, ActionResult]:
        fg = ex.guard_foreground()
        return fg, ex.run(member, args, fg)

    def _watch_changes(self, w: _ChangeWatch, member: str, res: ActionResult,
                       fg: ForegroundInfo) -> SessionResult | None:
        """Tıklamadan sonra ekran hiç değişmiyorsa (hile önleyici girdiyi engelliyor olabilir) döngüye girme."""
        if res.clicked_at is not None:
            w.pending_click = res.clicked_at
            return None
        if res.thumb is None:
            return None
        if w.pending_click is not None and w.last_thumb is not None:
            whole = imaging.change_ratio(w.last_thumb, res.thumb)
            local = imaging.change_ratio(w.last_thumb, res.thumb, around=w.pending_click)
            w.streak = w.streak + 1 if (whole < NO_CHANGE_RATIO and local < NO_CHANGE_LOCAL) else 0
            if w.streak:
                res.note = (res.note + " " if res.note else "") + "Not: son tıklamadan sonra ekranda değişiklik görülmedi."
        w.pending_click = None
        w.last_thumb = res.thumb
        if is_vanguard_game(fg) and w.streak >= VANGUARD_NO_CHANGE_LIMIT:
            return self._result("anticheat", ANTICHEAT_MESSAGE)
        if w.streak >= GENERAL_NO_CHANGE_LIMIT:
            return self._result("anticheat", NO_EFFECT_MESSAGE)
        return None

    async def _custom_tool(self, name: str, args: dict) -> tuple[str, bool]:
        if name == APP_TOOL_NAME and self.hooks.open_app is not None:
            target = args.get("name")
            if not isinstance(target, str) or not target.strip() or len(target) > 200:
                return "name geçerli bir uygulama adı olmalı.", False
            self.hooks.progress(self.steps, f"{target[:40]} açılıyor")
            try:
                ok, text = await self.hooks.open_app(target.strip())
            except Exception:  # noqa: BLE001
                log.exception("Uygulama açılamadı")
                return "Uygulama açılırken beklenmeyen bir hata oluştu.", False
            await self._in_thread(lambda: self._audit(APP_TOOL_NAME, {"name": target[:80]}, ok,
                                                      None if ok else text, "izin"))
            return text, ok
        return f"Bilinmeyen araç: {name}", False

    def _audit(self, member: str, args: dict, ok: bool, error: str | None, decision: str) -> None:
        safe = dict(args)
        if member == "type":
            safe = {"uzunluk": len(str(args.get("text", "")))}   # yazılan metnin kendisi kaydedilmez
        try:
            self.hooks.audit(tool=f"bilgisayar.{member}", args=safe, ok=ok, error=error, decision=decision,
                             summary=describe(member, args) if member != APP_TOOL_NAME else None)
        except Exception:  # noqa: BLE001
            log.exception("Denetim kaydı yazılamadı")

    # ---------- sonuçlar ----------
    def _result(self, status: str, summary: str) -> SessionResult:
        log.info("Bilgisayar oturumu bitti: durum=%s adım=%d eylem=%d maliyet=$%.4f", status, self.steps,
                 self.actions, self.cost_usd)
        return SessionResult(status, summary, self.steps, self.actions, self.cost_usd, self.spec.id)

    def _stopped(self) -> SessionResult:
        return self._result("stopped", "Durdurdun; bilgisayarı kullanmayı bıraktım. "
                                       f"{self.actions} eylem yapmıştım.")

    def _time_limit(self) -> SessionResult:
        return self._result("time_limit", f"Süre sınırına ({self.settings.max_minutes:g} dakika) ulaştım ve "
                                          "durdum; görev yarım kalmış olabilir.")


async def run_session_safely(session: ComputerUseSession) -> SessionResult:
    """Oturumu çalıştırır; Claude hatalarını Türkçe sonuca çevirir (istisna fırlatmaz, iptal hariç)."""
    try:
        return await session.run()
    except ClaudeError as e:
        return session._result("error", e.user_message)
