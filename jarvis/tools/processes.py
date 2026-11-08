"""İşlem (process) araçları: en çok kaynak kullananlar, ayrıntı, sonlandırma.

Sonlandırma her zaman kullanıcı onayı ister ve şu işlemlere asla dokunmaz:
Windows'un kritik işlemleri (csrss, lsass, winlogon, svchost...), güvenlik
yazılımı, PID 0-4, JARVIS'in kendisi ve başka bir kullanıcıya/SYSTEM'e ait
işlemler (bunlar yönetici yetkisi ister; JARVIS yönetici yetkisi kullanmaz).
"""

from __future__ import annotations

import getpass
import logging
import os
import time
from typing import Any

import psutil

from jarvis.core.logging_setup import redact
from jarvis.security.permission_manager import ConfirmPrompt, Permission
from jarvis.tools import schema as js
from jarvis.tools.registry import ToolContext, ToolError, ToolOutput, ToolRegistry, ToolSpec
from jarvis.tools.text import fold

log = logging.getLogger(__name__)

_MB = 1024 ** 2

CRITICAL_PROCESSES = frozenset({
    "system", "system idle process", "idle", "registry", "secure system", "memory compression",
    "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe", "lsaiso.exe",
    "svchost.exe", "fontdrvhost.exe", "dwm.exe", "sihost.exe", "ctfmon.exe", "audiodg.exe",
    # güvenlik yazılımı: sonlandırmak güvenlik ayarı değişikliği sayılır
    "msmpeng.exe", "nissrv.exe", "securityhealthservice.exe", "securityhealthsystray.exe", "sgrmbroker.exe",
    "mpdefendercoreservice.exe", "smartscreen.exe",
    # Linux/macOS karşılıkları (testler ve geliştirme)
    "systemd", "init", "kthreadd", "launchd", "kernel_task", "loginwindow", "windowserver",
})


def _current_user() -> str:
    try:
        return getpass.getuser().lower()
    except Exception:  # noqa: BLE001
        return ""


def _same_user(proc_user: str | None) -> bool:
    if not proc_user:
        return False
    me = _current_user()
    return proc_user.lower().split("\\")[-1] == me


def protection_reason(proc: psutil.Process) -> str | None:
    """Bu işlem sonlandırılamazsa nedeni (Türkçe), yoksa None."""
    pid = proc.pid
    if pid <= 4:
        return "Bu bir sistem çekirdek işlemi, sonlandırmam."
    if pid == os.getpid():
        return "Bu JARVIS'in kendisi. Kapatmak için Kapat düğmesini kullan."
    try:
        name = proc.name().lower()
    except psutil.Error:
        return "İşlem bilgisi okunamadı."
    if name in CRITICAL_PROCESSES:
        return f"{proc.name()} Windows'un çalışması için kritik bir işlem; sonlandırmam."
    try:
        user = proc.username()
    except psutil.AccessDenied:
        return "Bu işlem başka bir kullanıcıya ya da sisteme ait; yönetici yetkisi gerekir, yapmıyorum."
    except psutil.Error:
        return "İşlem bilgisi okunamadı."
    if not _same_user(user):
        return "Bu işlem başka bir kullanıcıya ya da sisteme ait; yönetici yetkisi gerekir, yapmıyorum."
    return None


def list_processes(sort: str = "cpu", limit: int = 15, sample_s: float = 0.4) -> list[dict]:
    """CPU yüzdesi iki ölçüm arasında hesaplanır (ilk okuma her zaman 0'dır)."""
    procs = []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            p.cpu_percent(None)
            procs.append(p)
        except psutil.Error:
            continue
    time.sleep(sample_s)
    ncpu = psutil.cpu_count() or 1
    rows = []
    for p in procs:
        try:
            with p.oneshot():
                mem = p.memory_info().rss
                cpu = p.cpu_percent(None) / ncpu  # Görev Yöneticisi gibi: toplam CPU'nun yüzdesi
                try:
                    user = p.username()
                except psutil.Error:
                    user = None
                rows.append({"pid": p.pid, "name": p.info.get("name") or "?", "cpu_percent": round(cpu, 1),
                             "memory_mb": round(mem / _MB, 1), "user": (user or "").split("\\")[-1] or None})
        except psutil.Error:
            continue
    key = "cpu_percent" if sort == "cpu" else "memory_mb"
    rows.sort(key=lambda r: (r[key], r["memory_mb"]), reverse=True)
    return rows[:limit] if limit else rows


def process_details(pid: int) -> dict:
    try:
        p = psutil.Process(pid)
        with p.oneshot():
            info: dict[str, Any] = {"pid": pid, "name": p.name(), "status": p.status()}
            for key, fn in (("exe", p.exe), ("user", p.username), ("threads", p.num_threads)):
                try:
                    info[key] = fn()
                except psutil.Error:
                    info[key] = None
            try:
                info["started"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(p.create_time()))
            except psutil.Error:
                info["started"] = None
            try:
                info["memory_mb"] = round(p.memory_info().rss / _MB, 1)
            except psutil.Error:
                info["memory_mb"] = None
            try:
                # Komut satırı parola/token içerebilir: maskelenir ve kısaltılır
                cmd = " ".join(p.cmdline())
                info["command_line"] = redact(cmd)[:400] if cmd else None
            except psutil.Error:
                info["command_line"] = None
            try:
                parent = p.parent()
                info["parent"] = {"pid": parent.pid, "name": parent.name()} if parent else None
            except psutil.Error:
                info["parent"] = None
    except psutil.NoSuchProcess as e:
        raise ToolError("yok", f"{pid} numaralı işlem bulunamadı.") from e
    except psutil.AccessDenied as e:
        raise ToolError("erişim", "Bu işlemin bilgilerine erişim izni yok.") from e
    info["protected_reason"] = protection_reason(p)
    return info


def find_processes(name: str) -> list[psutil.Process]:
    q = fold(name).removesuffix(" exe")
    out = []
    for p in psutil.process_iter(["pid", "name"]):
        n = fold((p.info.get("name") or "")).removesuffix(" exe")
        if n and (n == q or n.replace(" ", "") == q.replace(" ", "")):
            out.append(p)
    return out


def terminate(procs: list[psutil.Process], force: bool, wait_s: float = 3.0) -> tuple[list[int], list[int]]:
    """Sonlandırır; (kapananlar, hâlâ açık olanlar) PID listesi döner."""
    for p in procs:
        try:
            p.kill() if force else p.terminate()
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied as e:
            raise ToolError("erişim", "Bu işlemi sonlandırma iznim yok (yönetici yetkisi gerekir).") from e
    gone, alive = psutil.wait_procs(procs, timeout=wait_s)
    return [p.pid for p in gone], [p.pid for p in alive]


# ---------- araçlar ----------
def register(reg: ToolRegistry, ctx: ToolContext) -> None:
    def top(args: dict) -> ToolOutput:
        rows = list_processes(args["sort"], args["limit"])
        what = "işlemci" if args["sort"] == "cpu" else "bellek"
        if not rows:
            return ToolOutput("İşlem listesi okunamadı.", [])
        head = ", ".join(f"{r['name']} ({r['cpu_percent']}%)" if args["sort"] == "cpu"
                         else f"{r['name']} ({r['memory_mb']:.0f} MB)" for r in rows[:3])
        return ToolOutput(f"En çok {what} kullananlar: {head}.", rows)

    reg.register(ToolSpec(
        name="list_processes", title="İşlemleri listele", category="sistem",
        description=("En çok işlemci ya da bellek kullanan işlemleri listeler. / Lists the top processes by CPU or "
                     "memory usage (CPU percent is of total machine capacity)."),
        input_schema=js.obj(sort=js.string("Sıralama ölçütü / sort key", enum=["cpu", "memory"]),
                            limit=js.integer("Kaç işlem / how many", 1, 30)),
        permission=Permission.READ, handler=top))

    def details(args: dict) -> ToolOutput:
        info = process_details(args["pid"])
        return ToolOutput(f"{info['name']} (PID {info['pid']}): {info.get('memory_mb') or '?'} MB bellek.", info,
                          untrusted=True)

    reg.register(ToolSpec(
        name="process_details", title="İşlem ayrıntısı", category="sistem",
        description="Bir işlemin ayrıntıları (yol, kullanıcı, bellek, komut satırı). / Details of one process by PID.",
        input_schema=js.obj(pid=js.integer("İşlem numarası / process id", 0, 2**31 - 1)),
        permission=Permission.READ, handler=details))

    def prepare_kill(args: dict) -> dict:
        try:
            p = psutil.Process(args["pid"])
            name = p.name()
        except psutil.NoSuchProcess as e:
            raise ToolError("yok", f"{args['pid']} numaralı işlem bulunamadı.") from e
        except psutil.AccessDenied as e:
            raise ToolError("erişim", "Bu işlem başka bir kullanıcıya ya da sisteme ait; dokunmuyorum.") from e
        # PID yeniden kullanılmış olabilir: beklenen adla karşılaştır
        if fold(name) != fold(args["name"]):
            raise ToolError("ad uyuşmuyor",
                            f"{args['pid']} numaralı işlemin adı '{name}', '{args['name']}' değil. Sonlandırmadım.")
        reason = protection_reason(p)
        if reason:
            raise ToolError("korumalı", reason)
        return {**args, "name": name}

    def kill(args: dict) -> ToolOutput:
        p = psutil.Process(args["pid"])
        if fold(p.name()) != fold(args["name"]):
            raise ToolError("ad değişti", "İşlem onay sırasında değişti, sonlandırmadım.")
        gone, alive = terminate([p], args["force"])
        if alive:
            return ToolOutput(f"{args['name']} sonlandırma isteğine yanıt vermedi. Zorla kapatmayı deneyebilirim.",
                              {"closed": False, "pid": args["pid"]})
        return ToolOutput(f"{args['name']} (PID {args['pid']}) sonlandırıldı.", {"closed": True, "pid": args["pid"]})

    def kill_prompt(args: dict) -> ConfirmPrompt:
        return ConfirmPrompt(
            "kill_process", "İşlemi sonlandır",
            f"{args['name']} (PID {args['pid']}) {'zorla ' if args['force'] else ''}sonlandırılacak. "
            "Kaydedilmemiş veriler kaybolabilir. Onaylıyor musun?",
            danger=args["force"], confirm_label="Sonlandır")

    reg.register(ToolSpec(
        name="kill_process", title="İşlemi sonlandır", category="sistem",
        description=("Bir işlemi sonlandırır; ad, PID ile eşleşmelidir. Kritik sistem işlemleri ve başka kullanıcıların "
                     "işlemleri reddedilir. / Terminates a process (name must match PID). Critical system processes "
                     "and other users' processes are refused. Prefer close_app for normal applications."),
        input_schema=js.obj(pid=js.integer("İşlem numarası / process id", 0, 2**31 - 1),
                            name=js.string("İşlem adı, örn. notepad.exe / process name", 260, 1),
                            force=js.boolean("Zorla sonlandır / force kill")),
        permission=Permission.CONFIRM, handler=kill, prepare=prepare_kill, confirm=kill_prompt))
