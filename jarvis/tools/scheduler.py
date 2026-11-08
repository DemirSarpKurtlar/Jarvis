"""Uygulama içi basit zamanlayıcı.

Sınırlar (bilerek):
- Yalnızca JARVIS açıkken çalışır; görevler diske yazılmaz, uygulama kapanınca silinir.
  Windows Görev Zamanlayıcı'ya kayıt yapılmaz (yönetici yetkisi ve kalıcı sistem değişikliği gerekmesin).
- Yalnızca sabit bir eylem listesi zamanlanabilir (hatırlatma, uygulama açma, ekran kilidi, ses ayarı).
  Hepsi SAFE seviyesindedir; zamanı gelince araç kaydından yine tüm denetimlerle ve onay geri çağrısı
  olmadan çalışır, yani onay gerektiren bir şey hiçbir zaman zamanlanmış olarak yürümez.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from jarvis.security.permission_manager import Permission
from jarvis.tools import schema as js
from jarvis.tools.registry import ToolContext, ToolError, ToolOutput, ToolRegistry, ToolResult, ToolSpec

log = logging.getLogger(__name__)

MAX_TASKS = 20

# eylem -> (araç adı, girdi oluşturucu, Türkçe açıklama)
ACTIONS: dict[str, tuple[str | None, Callable[[str, int], dict], str]] = {
    "hatirlatma": (None, lambda text, value: {}, "hatırlatma"),
    "uygulama_ac": ("open_app", lambda text, value: {"name": text}, "uygulama açma"),
    "ekrani_kilitle": ("lock_screen", lambda text, value: {}, "ekran kilidi"),
    "ses_ayarla": ("set_volume", lambda text, value: {"level": value}, "ses ayarı"),
}


@dataclass
class ScheduledTask:
    id: int
    action: str
    text: str
    value: int
    due: datetime
    handle: asyncio.TimerHandle | None = None


class TaskScheduler:
    def __init__(self, registry: ToolRegistry, notify: Callable[[str], None] = lambda m: None) -> None:
        self.registry = registry
        self.notify = notify
        self.loop: asyncio.AbstractEventLoop | None = None
        self._tasks: dict[int, ScheduledTask] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self.results: list[tuple[int, ToolResult | None]] = []

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def schedule(self, action: str, minutes: int, text: str, value: int,
                 delay_s: float | None = None) -> ScheduledTask:
        """Thread güvenli; araç işleyicisi arka plan thread'inden çağırır."""
        if self.loop is None:
            raise ToolError("döngü yok", "Zamanlayıcı henüz hazır değil.")
        tool, build, _ = ACTIONS[action]
        if tool is not None:
            spec = self.registry.get(tool)
            if spec is None or not spec.supported:
                raise ToolError("desteklenmiyor", "Bu eylem bu sistemde zamanlanamaz.")
            if spec.permission > Permission.SAFE:
                raise ToolError("onaylı", "Onay gerektiren işlemler zamanlanamaz.")
        with self._lock:
            if len(self._tasks) >= MAX_TASKS:
                raise ToolError("çok", f"En çok {MAX_TASKS} zamanlanmış görev olabilir.")
            task = ScheduledTask(next(self._ids), action, text, value, datetime.now() + timedelta(minutes=minutes))
            self._tasks[task.id] = task
        delay = minutes * 60 if delay_s is None else delay_s

        def arm() -> None:
            task.handle = self.loop.call_later(delay, lambda: asyncio.ensure_future(self._fire(task.id)))

        self.loop.call_soon_threadsafe(arm)
        return task

    async def _fire(self, task_id: int) -> None:
        with self._lock:
            task = self._tasks.pop(task_id, None)
        if task is None:
            return
        tool, build, _ = ACTIONS[task.action]
        if tool is None:
            self.notify(f"Hatırlatma: {task.text}")
            self.results.append((task.id, None))
            return
        # Onay geri çağrısı yok: CONFIRM gerektiren her şey reddedilir
        result = await self.registry.execute(tool, build(task.text, task.value), None, source="zamanlayici")
        self.results.append((task.id, result))
        self.notify(f"Zamanlanmış görev: {result.summary}")

    def cancel(self, task_id: int) -> bool:
        with self._lock:
            task = self._tasks.pop(task_id, None)
        if task is None:
            return False
        if task.handle is not None and self.loop is not None:
            try:
                self.loop.call_soon_threadsafe(task.handle.cancel)
            except RuntimeError:  # döngü kapanmış: zamanlayıcı zaten çalışmayacak
                pass
        return True

    def list(self) -> list[ScheduledTask]:
        with self._lock:
            return sorted(self._tasks.values(), key=lambda t: t.due)

    def cancel_all(self) -> None:
        for t in self.list():
            self.cancel(t.id)


def register(reg: ToolRegistry, ctx: ToolContext) -> None:
    def sched(args: dict) -> ToolOutput:
        if ctx.scheduler is None:
            raise ToolError("yok", "Zamanlayıcı etkin değil.")
        if args["action"] == "hatirlatma" and not args["text"].strip():
            raise ToolError("metin", "Hatırlatma için bir metin gerekli.")
        if args["action"] == "uygulama_ac" and not args["text"].strip():
            raise ToolError("metin", "Açılacak uygulamanın adı gerekli.")
        if args["action"] == "ses_ayarla" and not 0 <= args["value"] <= 100:
            raise ToolError("değer", "Ses seviyesi 0 ile 100 arasında olmalı.")
        t = ctx.scheduler.schedule(args["action"], args["minutes"], args["text"].strip(), args["value"])
        label = ACTIONS[t.action][2]
        return ToolOutput(f"{t.due:%H:%M} için {label} zamanladım (görev {t.id}). JARVIS kapanırsa iptal olur.",
                          {"id": t.id, "due": t.due.isoformat(timespec="minutes")})

    reg.register(ToolSpec(
        name="schedule_task", title="Görev zamanla", category="zamanlama",
        description=("Belirli dakika sonra izinli bir eylem zamanlar: hatirlatma (text), uygulama_ac (text = uygulama), "
                     "ekrani_kilitle, ses_ayarla (value = yüzde). Yalnızca JARVIS açıkken çalışır. Kullanılmayan "
                     "alanlar için text='' ve value=-1. / Schedules an allowlisted action in N minutes (in-app only)."),
        input_schema=js.obj(action=js.string("Eylem / action", enum=list(ACTIONS)),
                            minutes=js.integer("Kaç dakika sonra / minutes from now", 1, 1440),
                            text=js.string("Hatırlatma metni ya da uygulama adı / text", 300),
                            value=js.integer("Ses yüzdesi, yoksa -1 / value or -1", -1, 100)),
        permission=Permission.SAFE, handler=sched))

    def lst(args: dict) -> ToolOutput:
        tasks = ctx.scheduler.list() if ctx.scheduler else []
        data = [{"id": t.id, "action": t.action, "text": t.text, "value": t.value,
                 "due": t.due.isoformat(timespec="minutes")} for t in tasks]
        if not tasks:
            return ToolOutput("Zamanlanmış görev yok.", [])
        return ToolOutput(f"{len(tasks)} zamanlanmış görev var; ilki {tasks[0].due:%H:%M}.", data)

    reg.register(ToolSpec(
        name="list_scheduled_tasks", title="Zamanlanmış görevler", category="zamanlama",
        description="Zamanlanmış görevleri listeler. / Lists scheduled in-app tasks.",
        input_schema=js.obj(), permission=Permission.READ, handler=lst))

    def cancel(args: dict) -> ToolOutput:
        ok = ctx.scheduler.cancel(args["task_id"]) if ctx.scheduler else False
        return ToolOutput(f"Görev {args['task_id']} iptal edildi." if ok else f"Görev {args['task_id']} bulunamadı.",
                          {"cancelled": ok})

    reg.register(ToolSpec(
        name="cancel_scheduled_task", title="Zamanlanmış görevi iptal et", category="zamanlama",
        description="Zamanlanmış bir görevi iptal eder. / Cancels a scheduled task.",
        input_schema=js.obj(task_id=js.integer("Görev numarası / task id", 1, 1_000_000)),
        permission=Permission.SAFE, handler=cancel))


