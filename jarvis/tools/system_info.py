"""Sistem bilgisi araçları: genel bakış, ağ, diskler, pil, son sistem olayları, performans raporu."""

from __future__ import annotations

import logging
import platform
import socket
import sys
import time
from datetime import datetime, timedelta

import psutil

from jarvis.security.permission_manager import Permission
from jarvis.tools import schema as js
from jarvis.tools.hardware import gpu_status
from jarvis.tools.processes import list_processes
from jarvis.tools.registry import NotSupportedError, ToolContext, ToolOutput, ToolRegistry, ToolSpec, WINDOWS_ONLY

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
_GB = 1024 ** 3


def tr_duration(seconds: float) -> str:
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    parts = ([f"{d} gün"] if d else []) + ([f"{h} saat"] if h else []) + [f"{m} dakika"]
    return " ".join(parts)


def os_name() -> str:
    if IS_WINDOWS:  # pragma: no cover - Windows
        rel, ver, _sp, _ = platform.win32_ver()
        build = int(ver.split(".")[-1]) if ver and ver.split(".")[-1].isdigit() else 0
        name = "Windows 11" if rel == "10" and build >= 22000 else f"Windows {rel}"
        edition = platform.win32_edition() or ""
        return f"{name} {edition} (sürüm {ver})".replace("  ", " ")
    return platform.platform()


def battery() -> dict:
    try:
        b = psutil.sensors_battery()
    except Exception:  # noqa: BLE001
        b = None
    if b is None:
        return {"present": False}
    left = None if b.secsleft in (psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN) or b.secsleft < 0 \
        else int(b.secsleft)
    return {"present": True, "percent": round(b.percent), "plugged_in": bool(b.power_plugged), "seconds_left": left}


def overview() -> dict:
    boot = psutil.boot_time()
    vm = psutil.virtual_memory()
    return {"os": os_name(), "hostname": socket.gethostname(), "python": platform.python_version(),
            "boot_time": datetime.fromtimestamp(boot).strftime("%Y-%m-%d %H:%M"),
            "uptime_s": int(time.time() - boot), "uptime_text": tr_duration(time.time() - boot),
            "cpu_logical": psutil.cpu_count(), "ram_total_gb": round(vm.total / _GB, 1), "battery": battery()}


def disks() -> list[dict]:
    out = []
    for part in psutil.disk_partitions(all=False):
        if "cdrom" in part.opts or not part.fstype:
            continue
        try:
            u = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            continue  # boş kart okuyucu vb.
        if u.total == 0:
            continue  # sanal/boş bağlama noktası
        out.append({"mount": part.mountpoint, "fs": part.fstype, "total_gb": round(u.total / _GB, 1),
                    "free_gb": round(u.free / _GB, 1), "percent": u.percent})
    return out


def network() -> list[dict]:
    stats = psutil.net_if_stats()
    out = []
    for name, addrs in psutil.net_if_addrs().items():
        st = stats.get(name)
        if st is None or not st.isup:
            continue
        ipv4 = [a.address for a in addrs if a.family == socket.AF_INET and not a.address.startswith("127.")]
        ipv6 = [a.address.split("%")[0] for a in addrs if a.family == socket.AF_INET6
                and a.address not in ("::1",) and not a.address.lower().startswith("fe80")]
        if not ipv4 and not ipv6:
            continue
        # MAC adresi bilerek eklenmez (cihazı tanımlayan bir bilgi, gereği yok)
        out.append({"interface": name, "ipv4": ipv4, "ipv6": ipv6[:2], "speed_mbps": st.speed or None})
    return out


def recent_system_events(hours: int, limit: int) -> list[dict]:  # pragma: no cover - Windows
    """Windows Sistem günlüğündeki hata kayıtları (okuma yönetici yetkisi gerektirmez)."""
    if not IS_WINDOWS:
        raise NotSupportedError()
    import win32evtlog
    import win32evtlogutil

    since = datetime.now() - timedelta(hours=hours)
    h = win32evtlog.OpenEventLog(None, "System")
    flags = win32evtlog.EVENTLOG_BACKWARDS_READ | win32evtlog.EVENTLOG_SEQUENTIAL_READ
    out: list[dict] = []
    scanned = 0
    try:
        while len(out) < limit and scanned < 5000:
            batch = win32evtlog.ReadEventLog(h, flags, 0)
            if not batch:
                break
            for ev in batch:
                scanned += 1
                when = ev.TimeGenerated
                when_dt = datetime(when.year, when.month, when.day, when.hour, when.minute, when.second)
                if when_dt < since:
                    return out
                if ev.EventType != win32evtlog.EVENTLOG_ERROR_TYPE:
                    continue
                try:
                    msg = win32evtlogutil.SafeFormatMessage(ev, "System") or ""
                except Exception:  # noqa: BLE001
                    msg = ""
                out.append({"time": when_dt.strftime("%Y-%m-%d %H:%M"), "source": str(ev.SourceName),
                            "event_id": int(ev.EventID & 0xFFFF), "message": " ".join(msg.split())[:300]})
                if len(out) >= limit:
                    break
    finally:
        win32evtlog.CloseEventLog(h)
    return out


def performance(snapshot) -> dict:  # noqa: ANN001
    vm = psutil.virtual_memory()
    report = {"cpu_percent": snapshot.cpu_percent if snapshot else psutil.cpu_percent(interval=0.3),
              "ram_percent": vm.percent, "disks": disks(), "gpu": gpu_status(snapshot),
              "top_cpu": list_processes("cpu", 5), "top_memory": list_processes("memory", 5, sample_s=0.1)}
    if snapshot and snapshot.net_down_bps is not None:
        report["network_down_mbps"] = round(snapshot.net_down_bps / 1e6, 2)
        report["network_up_mbps"] = round((snapshot.net_up_bps or 0) / 1e6, 2)
    warnings = []
    if report["cpu_percent"] >= 90:
        warnings.append("İşlemci neredeyse tam yükte.")
    if vm.percent >= 90:
        warnings.append("Bellek neredeyse dolu.")
    for d in report["disks"]:
        if d["percent"] >= 90:
            warnings.append(f"{d['mount']} diskinde yer azaldı (%{d['percent']:.0f} dolu).")
    g = report["gpu"]
    if g.get("available") and (g.get("temperature_c") or 0) >= 85:
        warnings.append(f"Ekran kartı sıcak: {g['temperature_c']} °C.")
    report["warnings"] = warnings
    return report


def register(reg: ToolRegistry, ctx: ToolContext) -> None:
    def ov(args: dict) -> ToolOutput:
        o = overview()
        b = o["battery"]
        bat = f", pil %{b['percent']}" + (" (şarjda)" if b["plugged_in"] else "") if b["present"] else ""
        return ToolOutput(f"{o['os']}. Bilgisayar {o['uptime_text']}dır açık{bat}.", o)

    reg.register(ToolSpec(
        name="system_overview", title="Sistem özeti", category="sistem",
        description=("İşletim sistemi, bilgisayar adı, açılış zamanı ve çalışma süresi, pil. / OS, hostname, boot time, "
                     "uptime, battery."),
        input_schema=js.obj(), permission=Permission.READ, handler=ov))

    def net(args: dict) -> ToolOutput:
        n = network()
        snap = ctx.snapshot()
        rate = ""
        if snap is not None and snap.net_down_bps is not None:
            rate = f" Anlık hız: indirme {snap.net_down_bps / 1e6:.1f}, yükleme {(snap.net_up_bps or 0) / 1e6:.1f} Mbit/s."
        if not n:
            return ToolOutput("Etkin bir ağ bağlantısı görünmüyor." + rate, [])
        ips = ", ".join(f"{i['interface']}: {', '.join(i['ipv4']) or '-'}" for i in n[:3])
        return ToolOutput(f"Ağ bağlantıları: {ips}.{rate}", n)

    reg.register(ToolSpec(
        name="network_info", title="Ağ bilgisi", category="sistem",
        description="Etkin ağ arabirimleri, yerel IP adresleri ve anlık hız. / Active interfaces, local IPs, current rate.",
        input_schema=js.obj(), permission=Permission.READ, handler=net))

    def dk(args: dict) -> ToolOutput:
        d = disks()
        txt = ", ".join(f"{x['mount']} {x['free_gb']:.0f} GB boş (%{x['percent']:.0f} dolu)" for x in d[:4])
        return ToolOutput(f"Diskler: {txt}." if d else "Disk bilgisi okunamadı.", d)

    reg.register(ToolSpec(
        name="disk_info", title="Disk bilgisi", category="sistem",
        description="Disklerin boyutu, boş alanı ve doluluk oranı. / Disk sizes, free space, usage.",
        input_schema=js.obj(), permission=Permission.READ, handler=dk))

    def bat(args: dict) -> ToolOutput:
        b = battery()
        if not b["present"]:
            return ToolOutput("Bu bilgisayarda pil yok ya da okunamıyor.", b)
        left = f", yaklaşık {tr_duration(b['seconds_left'])} kaldı" if b.get("seconds_left") else ""
        return ToolOutput(f"Pil %{b['percent']}, {'şarjda' if b['plugged_in'] else 'pilden çalışıyor'}{left}.", b)

    reg.register(ToolSpec(
        name="battery_status", title="Pil durumu", category="sistem",
        description="Pil yüzdesi, şarj durumu, kalan süre. / Battery percent, charging, time left.",
        input_schema=js.obj(), permission=Permission.READ, handler=bat))

    def events(args: dict) -> ToolOutput:
        ev = recent_system_events(args["hours"], args["limit"])
        if not ev:
            return ToolOutput(f"Son {args['hours']} saatte sistem günlüğünde hata kaydı yok.", [])
        return ToolOutput(f"Son {args['hours']} saatte {len(ev)} sistem hatası kaydı var; en yenisi: "
                          f"{ev[0]['source']} ({ev[0]['time']}).", ev, untrusted=True)

    reg.register(ToolSpec(
        name="recent_system_events", title="Son sistem hataları", category="sistem", platforms=WINDOWS_ONLY,
        description=("Windows Sistem günlüğündeki son hata kayıtları. / Recent error events from the Windows System "
                     "event log."),
        input_schema=js.obj(hours=js.integer("Kaç saat geriye / hours back", 1, 168),
                            limit=js.integer("En çok kayıt / max events", 1, 50)),
        permission=Permission.READ, handler=events, timeout_s=30))

    def perf(args: dict) -> ToolOutput:
        r = performance(ctx.snapshot())
        head = f"İşlemci %{r['cpu_percent']:.0f}, bellek %{r['ram_percent']:.0f}"
        if r["top_cpu"]:
            head += f"; en çok işlemci kullanan {r['top_cpu'][0]['name']}"
        tail = " " + " ".join(r["warnings"]) if r["warnings"] else " Her şey normal görünüyor."
        return ToolOutput(head + "." + tail, r)

    reg.register(ToolSpec(
        name="performance_report", title="Performans raporu", category="sistem",
        description=("Anlık performans özeti: CPU, RAM, disk, GPU, en çok kaynak kullanan işlemler ve uyarılar. / "
                     "Performance summary with top processes and warnings."),
        input_schema=js.obj(), permission=Permission.READ, handler=perf))
