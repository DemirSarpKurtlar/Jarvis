"""Rutinler: kullanıcı tanımlı çok adımlı komutlar ("Çalışma modu", "Oyun modu", "İyi geceler").

Bir rutin; bir ad, tetikleyici sözler ve sıralı adımlardan oluşur. Her adım ya araç kaydındaki bir
araçtır (open_app, close_app, set_volume, open_url ...) ya da iki sözde adımdan biridir:

- ``wait``: {"seconds": 1-60} kadar bekle
- ``say``:  {"text": ...} metni söyle (metinler rutinin sonundaki cevapta okunur)

Güvenlik:
- Adımlar kaydedilirken araç şemasına göre doğrulanır; çalışırken her adım yine `ToolRegistry.execute`
  üzerinden (şema, prepare, izin, denetim kaydı) yürür. Rutin hiçbir izni atlamaz.
- Onay gerektiren adımlar rutin başlamadan TEK bir onay penceresinde listelenir. Yalnızca orada
  listelenen adımlar onaylanmış sayılır; çalışırken beklenmedik biçimde onay isteyen bir adım reddedilir.
- Rutin içinden rutin çalıştırılamaz/oluşturulamaz; toplu dosya işlemleri rutine konamaz.
- Claude rutin oluşturabilir (`create_routine`) ama adımları gösteren onay penceresinden geçer.

Depo: SQLite `routines` tablosu (storage/database.py, RUTIN geçişi). Örnek rutinler kapalı gelir.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from jarvis.security.permission_manager import ConfirmPrompt, Permission
from jarvis.tools import intents
from jarvis.tools import schema as js
from jarvis.tools.registry import ToolContext, ToolError, ToolOutput, ToolRegistry, ToolSpec
from jarvis.tools.text import fold, phonetic_key, suffix_variants

log = logging.getLogger(__name__)

WAIT = "wait"
SAY = "say"
PSEUDO_STEPS: dict[str, tuple[str, dict]] = {
    WAIT: ("Bekle", js.obj(seconds=js.integer("Saniye / seconds", 1, 60))),
    SAY: ("Söyle", js.obj(text=js.string("Söylenecek metin / text to say", 300, 1))),
}
# Rutin adımı olamayan araçlar: rutin içinden rutin, toplu dosya işlemleri
BLOCKED_STEP_TOOLS = frozenset({"run_routine", "create_routine", "list_routines", "set_briefing_city",
                                "delete_path", "move_path", "organize_folder"})
MAX_STEPS = 20
MAX_PHRASES = 8
MAX_TOTAL_WAIT_S = 300
MAX_ROUTINES = 50
RUN_TIMEOUT_S = 600.0
NAME_RE = re.compile(r"[\w][\w\s'’\-\.]{0,59}")


@dataclass
class Routine:
    id: int | None
    name: str
    phrases: list[str] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    enabled: bool = False
    last_run_at: str | None = None


# ---------- doğrulama ----------
def _clean_text(s: Any, what: str, max_len: int, min_len: int = 1) -> str:
    if not isinstance(s, str):
        raise ToolError("tip", f"{what} metin olmalı.")
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) < min_len:
        raise ToolError("kısa", f"{what} boş olamaz." if min_len <= 1 else f"{what} en az {min_len} karakter olmalı.")
    if len(s) > max_len:
        raise ToolError("uzun", f"{what} en çok {max_len} karakter olabilir.")
    return s


def validate_step(reg: ToolRegistry | None, step: Any, index: int) -> dict:
    """Tek adımı doğrular; {"tool", "args"} döndürür. Hata mesajları Türkçe ve adım numaralı."""
    where = f"{index}. adım"
    if not isinstance(step, dict) or not isinstance(step.get("tool"), str):
        raise ToolError("adım", f"{where}: araç adı eksik.")
    tool = step["tool"].strip()
    args = step.get("args", {})
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ToolError("adım", f"{where}: argümanlar bir JSON nesnesi olmalı.")
    if tool in PSEUDO_STEPS:
        schema = PSEUDO_STEPS[tool][1]
    else:
        if tool in BLOCKED_STEP_TOOLS:
            raise ToolError("adım", f"{where}: '{tool}' bir rutin adımı olamaz.")
        spec = reg.get(tool) if reg is not None else None
        if spec is None:
            raise ToolError("adım", f"{where}: '{tool}' diye bir araç yok.")
        if spec.permission is Permission.FORBIDDEN:
            raise ToolError("adım", f"{where}: '{spec.title}' güvenlik nedeniyle kullanılamaz.")
        schema = spec.input_schema
    try:
        js.validate(schema, args)
    except js.SchemaError as e:
        raise ToolError("şema", f"{where} ({tool}): " + "; ".join(e.errors[:3])) from None
    if tool == SAY:
        args = {"text": _clean_text(args["text"], f"{where} metni", 300)}
    return {"tool": tool, "args": dict(args)}


def validate_definition(reg: ToolRegistry | None, name: Any, phrases: Any, steps: Any,
                        existing: list[Routine] | None = None, exclude_id: int | None = None
                        ) -> tuple[str, list[str], list[dict]]:
    name = _clean_text(name, "Rutin adı", 60, 2)
    if not NAME_RE.fullmatch(name):
        raise ToolError("ad", "Rutin adı yalnızca harf, rakam, boşluk ve - ' . içerebilir.")
    if not isinstance(phrases, list):
        raise ToolError("söz", "Tetikleyici sözler bir liste olmalı.")
    if len(phrases) > MAX_PHRASES:
        raise ToolError("söz", f"En çok {MAX_PHRASES} tetikleyici söz olabilir.")
    clean_phrases: list[str] = []
    for p in phrases:
        p = _clean_text(p, "Tetikleyici söz", 80, 3)
        if fold(p) and fold(p) not in {fold(x) for x in clean_phrases}:
            clean_phrases.append(p)
    if not isinstance(steps, list) or not steps:
        raise ToolError("adım", "Rutinin en az bir adımı olmalı.")
    if len(steps) > MAX_STEPS:
        raise ToolError("adım", f"Bir rutinde en çok {MAX_STEPS} adım olabilir.")
    clean_steps = [validate_step(reg, s, i) for i, s in enumerate(steps, 1)]
    total_wait = sum(s["args"]["seconds"] for s in clean_steps if s["tool"] == WAIT)
    if total_wait > MAX_TOTAL_WAIT_S:
        raise ToolError("bekleme", f"Toplam bekleme en çok {MAX_TOTAL_WAIT_S} saniye olabilir.")
    # Başka bir rutinle çakışan ad/söz: hangisinin çalışacağı belirsiz olur
    mine = {fold(name)} | {fold(p) for p in clean_phrases}
    for r in existing or []:
        if exclude_id is not None and r.id == exclude_id:
            continue
        if fold(r.name) == fold(name):
            raise ToolError("ad", f"'{r.name}' adında bir rutin zaten var.")
        clash = mine & ({fold(r.name)} | {fold(p) for p in r.phrases})
        if clash:
            raise ToolError("söz", f"'{sorted(clash)[0]}' sözü '{r.name}' rutininde de var.")
    return name, clean_phrases, clean_steps


def describe_step(reg: ToolRegistry | None, step: dict) -> str:
    """Kullanıcıya gösterilecek kısa Türkçe adım açıklaması."""
    tool, args = step.get("tool", "?"), step.get("args") or {}
    if tool == WAIT:
        return f"{args.get('seconds')} saniye bekle"
    if tool == SAY:
        return f"Söyle: “{args.get('text', '')}”"
    spec = reg.get(tool) if reg is not None else None
    title = spec.title if spec is not None else tool
    if tool in ("open_app", "close_app", "switch_to_app", "launch_game") and "name" in args:
        return f"{title}: {args['name']}" + (" (zorla)" if args.get("force") else "")
    if tool == "set_volume" and "level" in args:
        return f"{title}: %{args['level']}"
    if tool == "open_url" and "url" in args:
        return f"{title}: {args['url']}"
    if not args:
        return title
    text = ", ".join(f"{k}={v}" for k, v in args.items())
    return f"{title} ({text[:120]})"


# ---------- depo ----------
def _row(r: Any) -> Routine:  # noqa: ANN401
    try:
        phrases = json.loads(r["phrases_json"]) or []
        steps = json.loads(r["steps_json"]) or []
    except (TypeError, ValueError):
        phrases, steps = [], []
    return Routine(r["id"], r["name"], [p for p in phrases if isinstance(p, str)],
                   [s for s in steps if isinstance(s, dict)], bool(r["enabled"]), r["last_run_at"])


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class RoutineStore:
    """SQLite `routines` tablosu. Kısa sorgular; arayüz thread'inden de çağrılabilir."""

    def __init__(self, db: Any, registry: ToolRegistry | None = None) -> None:  # noqa: ANN401
        self.db = db
        self.registry = registry

    def list(self) -> list[Routine]:
        return [_row(r) for r in self.db.query("SELECT * FROM routines ORDER BY name COLLATE NOCASE")]

    def get(self, routine_id: int) -> Routine | None:
        rows = self.db.query("SELECT * FROM routines WHERE id = ?", (routine_id,))
        return _row(rows[0]) if rows else None

    def find(self, name: str) -> Routine | None:
        """Ada göre (büyük/küçük harf ve Türkçe karakter duyarsız); yoksa tetikleyici söze göre."""
        key = fold(name or "")
        if not key:
            return None
        routines = self.list()
        for r in routines:
            if fold(r.name) == key:
                return r
        for r in routines:
            if key in {fold(p) for p in r.phrases}:
                return r
        ph = phonetic_key(key)
        for r in routines:
            if ph and ph in {phonetic_key(r.name), *(phonetic_key(p) for p in r.phrases)}:
                return r
        return None

    def add(self, name: str, phrases: list[str], steps: list[dict], enabled: bool = False) -> Routine:
        name, phrases, steps = validate_definition(self.registry, name, phrases, steps, self.list())
        if len(self.list()) >= MAX_ROUTINES:
            raise ToolError("çok", f"En çok {MAX_ROUTINES} rutin olabilir.")
        now = _now()
        rid = self.db.execute(
            "INSERT INTO routines (name, phrases_json, steps_json, enabled, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, json.dumps(phrases, ensure_ascii=False), json.dumps(steps, ensure_ascii=False), int(enabled),
             now, now))
        return Routine(rid, name, phrases, steps, enabled)

    def update(self, routine_id: int, name: str, phrases: list[str], steps: list[dict],
               enabled: bool | None = None) -> Routine:
        current = self.get(routine_id)
        if current is None:
            raise ToolError("yok", "Rutin bulunamadı.")
        name, phrases, steps = validate_definition(self.registry, name, phrases, steps, self.list(), routine_id)
        en = current.enabled if enabled is None else enabled
        self.db.execute(
            "UPDATE routines SET name = ?, phrases_json = ?, steps_json = ?, enabled = ?, updated_at = ? "
            "WHERE id = ?",
            (name, json.dumps(phrases, ensure_ascii=False), json.dumps(steps, ensure_ascii=False), int(en), _now(),
             routine_id))
        return Routine(routine_id, name, phrases, steps, en, current.last_run_at)

    def set_enabled(self, routine_id: int, enabled: bool) -> bool:
        if self.get(routine_id) is None:
            return False
        self.db.execute("UPDATE routines SET enabled = ?, updated_at = ? WHERE id = ?",
                        (int(enabled), _now(), routine_id))
        return True

    def delete(self, routine_id: int) -> bool:
        if self.get(routine_id) is None:
            return False
        self.db.execute("DELETE FROM routines WHERE id = ?", (routine_id,))
        return True

    def mark_run(self, routine_id: int) -> None:
        self.db.execute("UPDATE routines SET last_run_at = ? WHERE id = ?", (_now(), routine_id))

    # ---------- yerel tetikleme ----------
    def match_text(self, text: str) -> Routine | None:
        """Kullanıcının sözü açık bir rutinin adı/tetikleyicisi mi? (Claude'suz, okunuşa toleranslı)"""
        cands = trigger_candidates(text)
        if not cands:
            return None
        keys = set(cands)
        ph_keys = {phonetic_key(c) for c in cands if len(c) >= 3}
        for r in self.list():
            if not r.enabled:
                continue
            words = [r.name, *r.phrases]
            if keys & {fold(w) for w in words}:
                return r
        for r in self.list():
            if not r.enabled:
                continue
            if ph_keys & {phonetic_key(w) for w in [r.name, *r.phrases] if len(fold(w)) >= 3}:
                return r
        return None


_LEAD = re.compile(r"^(?:hey\s+)?jarvis\b\s*")
_TAIL_WORDS = re.compile(r"\s+(?:jarvis|lutfen)$")
_VERB_TAIL = re.compile(r"^(.+?)\s+(?:rutinini\s+|rutini\s+)?(?:ac|baslat|calistir|etkinlestir|aktif et|"
                        r"devreye al|gec)$")


def trigger_candidates(text: str) -> list[str]:
    """Söylenen cümleden olası tetikleyici anahtarlar (katlanmış): "Jarvis, çalışma modunu aç" ->
    ["calisma modunu ac", "calisma modunu", "calisma modu"]."""
    if not isinstance(text, str) or len(text) > 200:
        return []
    f = fold(text)
    f = _LEAD.sub("", f).strip()
    for _ in range(2):
        f = _TAIL_WORDS.sub("", f).strip()
    f = re.sub(r"^lutfen\s+", "", f)
    if not f:
        return []
    out = [f]
    m = _VERB_TAIL.match(f)
    if m:
        out.append(m.group(1))
    more: list[str] = []
    for c in out:
        words = c.split()
        if not words:
            continue
        for v in suffix_variants(words[-1]):
            more.append(" ".join([*words[:-1], fold(v)]))
    seen: list[str] = []
    for c in out + more:
        c = c.strip()
        if c and c not in seen:
            seen.append(c)
    return seen


# ---------- çalıştırma ----------
class RoutineRunner:
    """`run_routine` aracının işleyicisi. Arka plan thread'inde çalışır; adımları ana döngüdeki
    `ToolRegistry.execute` ile (tüm güvenlik adımlarıyla) sırayla yürütür."""

    def __init__(self, reg: ToolRegistry, ctx: ToolContext, store_getter, sleep=None) -> None:  # noqa: ANN001
        self.reg = reg
        self.ctx = ctx
        self._store = store_getter
        self._sleep = sleep
        self._lock = threading.Lock()

    @property
    def store(self) -> RoutineStore:
        st = self._store()
        if st is None:
            raise ToolError("depo yok", "Rutin deposu hazır değil.")
        return st

    def _step_level(self, step: dict) -> tuple[Permission | None, str]:
        """Adımın çalışırken isteyeceği izin seviyesi ve onay metni (önceden kestirim)."""
        spec = self.reg.get(step["tool"])
        if spec is None or not spec.supported:
            return None, ""
        prepared = dict(step["args"])
        escalated = None
        try:
            if spec.prepare is not None:
                prepared = spec.prepare(dict(step["args"]))
            if spec.escalate is not None:
                escalated = spec.escalate(prepared)
        except Exception:  # noqa: BLE001  ör. uygulama bulunamadı: adım çalışırken aynı hatayı dürüstçe verir
            return spec.permission, ""
        decision = self.reg.permissions.decide(spec.name, spec.permission, escalated)
        if not decision.allowed and not decision.needs_confirmation:
            raise ToolError("yasak", f"'{describe_step(self.reg, step)}' adımı çalıştırılamaz: {decision.reason}")
        if decision.needs_confirmation:
            msg = ""
            if spec.confirm is not None:
                try:
                    msg = spec.confirm(prepared).message
                except Exception:  # noqa: BLE001
                    msg = ""
            return Permission.CONFIRM, msg
        return decision.level, ""

    def prepare(self, args: dict) -> dict:
        r = self.store.find(args["name"])
        if r is None:
            names = ", ".join(x.name for x in self.store.list()[:10]) or "yok"
            raise ToolError("yok", f"'{args['name']}' adında bir rutin bulamadım. Rutinler: {names}.")
        if not r.enabled:
            raise ToolError("kapalı", f"'{r.name}' rutini kapalı. Otomasyon ekranındaki Rutinler bölümünden "
                                      "açabilirsin.")
        risky: list[dict] = []
        for i, step in enumerate(r.steps, 1):
            if step.get("tool") in PSEUDO_STEPS:
                continue
            level, msg = self._step_level(step)
            if level is Permission.CONFIRM:
                risky.append({"index": i, "text": describe_step(self.reg, step), "detail": msg})
        return {"name": r.name, "_routine_id": r.id, "_risky": risky}

    @staticmethod
    def escalate(prepared: dict) -> Permission | None:
        return Permission.CONFIRM if prepared.get("_risky") else None

    def confirm(self, prepared: dict) -> ConfirmPrompt:
        lines = [f"'{prepared['name']}' rutininde onay gerektiren adımlar var:"]
        for r in prepared.get("_risky", []):
            lines.append(f"• {r['index']}. {r['text']}" + (f" — {r['detail']}" if r.get("detail") else ""))
        lines.append("Onaylarsan yalnızca bu adımlar bu çalıştırma için onaylanmış sayılır. Rutini çalıştırayım mı?")
        return ConfirmPrompt("run_routine", f"Rutin: {prepared['name']}", "\n".join(lines), confirm_label="Çalıştır")

    def run(self, args: dict) -> ToolOutput:
        loop = getattr(self.ctx.scheduler, "loop", None)
        if loop is None or loop.is_closed():
            raise ToolError("döngü yok", "Rutin çalıştırıcı henüz hazır değil.")
        if not self._lock.acquire(blocking=False):
            raise ToolError("meşgul", "Başka bir rutin çalışıyor; bitince tekrar dene.")
        try:
            return self._run(args, loop)
        finally:
            self._lock.release()

    def _run(self, args: dict, loop: asyncio.AbstractEventLoop) -> ToolOutput:
        r = self.store.get(args["_routine_id"])
        if r is None:
            raise ToolError("yok", "Rutin bu arada silinmiş.")
        approved = {x["index"] for x in args.get("_risky", [])}
        deadline = time.monotonic() + RUN_TIMEOUT_S - 15
        said: list[str] = []
        report: list[dict] = []
        problems: list[str] = []
        self.store.mark_run(r.id)
        for i, step in enumerate(r.steps, 1):
            tool, step_args = step.get("tool", ""), step.get("args") or {}
            label = describe_step(self.reg, step)
            if time.monotonic() > deadline:
                problems.append(f"{i}. adımdan sonrası süre dolduğu için yapılmadı")
                break
            if tool == SAY:
                said.append(str(step_args.get("text", ""))[:300])
                report.append({"step": i, "tool": tool, "ok": True, "summary": label})
                continue
            if tool == WAIT:
                secs = max(0, min(60, int(step_args.get("seconds", 0))))
                (self._sleep or time.sleep)(min(secs, max(0.0, deadline - time.monotonic())))
                report.append({"step": i, "tool": tool, "ok": True, "summary": label})
                continue
            if tool in BLOCKED_STEP_TOOLS:   # elle düzenlenmiş veritabanına karşı
                problems.append(f"{label}: rutin adımı olamaz")
                report.append({"step": i, "tool": tool, "ok": False, "error": "izin verilmeyen adım"})
                continue

            async def confirm_step(prompt: ConfirmPrompt, _i: int = i) -> bool:
                # Rutin başlarken kullanıcıya gösterilen tek onay yalnızca o adımları kapsar
                if _i in approved:
                    return True
                log.warning("Rutin adımı beklenmedik biçimde onay istedi, reddedildi (%s)", prompt.tool)
                return False

            spec = self.reg.get(tool)
            wait_s = (spec.timeout_s if spec is not None else 30.0) + 15
            fut = asyncio.run_coroutine_threadsafe(
                self.reg.execute(tool, dict(step_args), confirm_step, source="rutin"), loop)
            try:
                res = fut.result(timeout=max(5.0, min(wait_s, deadline - time.monotonic() + 5)))
            except Exception:  # noqa: BLE001  zaman aşımı ya da döngü kapandı
                fut.cancel()
                problems.append(f"{label}: zamanında bitmedi")
                report.append({"step": i, "tool": tool, "ok": False, "error": "zaman aşımı"})
                continue
            if res.ok:
                report.append({"step": i, "tool": tool, "ok": True, "summary": res.summary})
            else:
                err = res.error or res.summary
                if res.decision == "reddedildi" and i not in approved:
                    err = "önceden onaylanmamış bir onay istedi, yapılmadı"
                problems.append(f"{label}: {err}")
                report.append({"step": i, "tool": tool, "ok": False, "error": err})
        ok_n = sum(1 for x in report if x["ok"])
        total = len(r.steps)
        lead = " ".join(s for s in said if s)
        if not problems:
            status = f"'{r.name}' rutini tamamlandı ({total} adım)."
        else:
            status = (f"'{r.name}' rutini: {total} adımın {ok_n} tanesi yapıldı. Sorunlar: "
                      + "; ".join(problems[:5]) + ".")
        summary = f"{lead} {status}".strip() if lead else status
        return ToolOutput(summary, {"routine": r.name, "steps": report, "ok_steps": ok_n, "total_steps": total})


# ---------- yerel komut bağlantısı ----------
def _intent_matcher(store: RoutineStore):  # noqa: ANN202
    def match(text: str) -> intents.Intent | None:
        r = store.match_text(text)
        return intents.Intent("run_routine", {"name": r.name}, "Rutin") if r is not None else None
    return match


# ---------- araçlar ----------
def register(reg: ToolRegistry, ctx: ToolContext) -> RoutineRunner:
    db = ctx.extra.get("db")
    store = RoutineStore(db, reg) if db is not None else None
    if store is not None:
        ctx.extra["routines"] = store
    intents.set_routine_matcher(_intent_matcher(store) if store is not None else None)
    runner = RoutineRunner(reg, ctx, lambda: ctx.extra.get("routines"))

    def lst(args: dict) -> ToolOutput:
        routines = runner.store.list()
        data = [{"name": r.name, "enabled": r.enabled, "phrases": r.phrases,
                 "steps": [describe_step(reg, s) for s in r.steps]} for r in routines]
        if not routines:
            return ToolOutput("Hiç rutin yok.", [])
        on = [r.name for r in routines if r.enabled]
        off = [r.name for r in routines if not r.enabled]
        parts = []
        if on:
            parts.append("Açık rutinler: " + ", ".join(on))
        if off:
            parts.append("Kapalı rutinler: " + ", ".join(off))
        return ToolOutput(". ".join(parts) + ".", data)

    reg.register(ToolSpec(
        name="list_routines", title="Rutinleri listele", category="rutin",
        description="Kullanıcının rutinlerini (çok adımlı komutlar) listeler. / Lists user routines.",
        input_schema=js.obj(), permission=Permission.READ, handler=lst))

    reg.register(ToolSpec(
        name="run_routine", title="Rutin çalıştır", category="rutin",
        description=("Kullanıcının tanımladığı bir rutini adıyla çalıştırır; onay gerektiren adımlar tek pencerede "
                     "sorulur. / Runs a user-defined routine by name."),
        input_schema=js.obj(name=js.string("Rutin adı / routine name", 60, 1)),
        permission=Permission.SAFE, handler=runner.run, prepare=runner.prepare, escalate=runner.escalate,
        confirm=runner.confirm, timeout_s=RUN_TIMEOUT_S))

    def prepare_create(args: dict) -> dict:
        st = runner.store
        steps = []
        for i, s in enumerate(args["steps"], 1):
            raw = (s.get("args_json") or "").strip() or "{}"
            try:
                parsed = json.loads(raw)
            except ValueError:
                raise ToolError("json", f"{i}. adımın args_json alanı geçerli JSON değil.") from None
            steps.append({"tool": s["tool"], "args": parsed})
        existing = st.find(args["name"]) if args["replace_existing"] else None
        if existing is not None and fold(existing.name) != fold(args["name"]):
            existing = None   # yalnızca aynı adlı rutin güncellenir
        name, phrases, steps = validate_definition(reg, args["name"], args["phrases"], steps, st.list(),
                                                   existing.id if existing else None)
        return {"name": name, "phrases": phrases, "steps": steps, "_replace_id": existing.id if existing else None}

    def confirm_create(p: dict) -> ConfirmPrompt:
        verb = "güncellemek" if p.get("_replace_id") else "oluşturmak"
        lines = [f"JARVIS '{p['name']}' rutinini {verb} istiyor."]
        lines.append("Tetikleyici sözler: " + (", ".join(f"“{x}”" for x in p["phrases"]) or "(yok, yalnızca adıyla)"))
        lines.append("Adımlar:")
        lines += [f"{i}. {describe_step(reg, s)}" for i, s in enumerate(p["steps"], 1)]
        lines.append("Onay gerektiren adımlar rutin her çalıştığında yine sorulur. Kaydedeyim mi?")
        return ConfirmPrompt("create_routine", "Yeni rutin", "\n".join(lines), confirm_label="Kaydet")

    def create(p: dict) -> ToolOutput:
        st = runner.store
        if p.get("_replace_id"):
            r = st.update(p["_replace_id"], p["name"], p["phrases"], p["steps"], enabled=True)
            verb = "güncellendi"
        else:
            r = st.add(p["name"], p["phrases"], p["steps"], enabled=True)
            verb = "kaydedildi"
        trig = f" “{r.phrases[0]}” dediğinde çalışır." if r.phrases else f" “{r.name} rutinini çalıştır” diyebilirsin."
        return ToolOutput(f"'{r.name}' rutini {verb} ({len(r.steps)} adım).{trig}",
                          {"name": r.name, "phrases": r.phrases, "steps": [describe_step(reg, s) for s in r.steps]})

    step_item = js.obj(
        tool=js.string("Araç adı (ör. open_app, close_app, set_volume, open_url) ya da 'wait'/'say' / tool name", 64,
                       1),
        args_json=js.string("Aracın girdisi JSON nesnesi olarak, ör. {\"name\": \"Steam\"}; wait: {\"seconds\": 5}; "
                            "say: {\"text\": \"...\"} / tool input as a JSON object string", 1000))
    reg.register(ToolSpec(
        name="create_routine", title="Rutin oluştur", category="rutin",
        description=("Kullanıcı için çok adımlı bir rutin kaydeder (ad, tetikleyici sözler, adımlar). Adım = kayıtlı "
                     "bir araç + JSON girdisi, ya da wait/say. / Saves a multi-step routine; user confirms the steps."),
        input_schema=js.obj(
            name=js.string("Rutin adı / routine name", 60, 2),
            phrases={"type": "array", "description": "Tetikleyici sözler / trigger phrases",
                     "items": js.string("Söz / phrase", 80, 3), "maxItems": MAX_PHRASES},
            steps={"type": "array", "description": "Sıralı adımlar / ordered steps", "items": step_item,
                   "minItems": 1, "maxItems": MAX_STEPS},
            replace_existing=js.boolean("Aynı adlı rutini güncelle / overwrite routine with the same name")),
        permission=Permission.CONFIRM, handler=create, prepare=prepare_create, confirm=confirm_create))
    return runner
