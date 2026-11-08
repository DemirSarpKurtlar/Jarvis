"""Donanım araçları: GPU, CPU, bellek.

GPU değerleri sistem izleyicisinin (NVML) son ölçümünden gelir; ayrı bir NVML
oturumu açılmaz. CPU sıcaklığı yalnızca güvenilir bir kaynak varsa verilir:

- Windows: LibreHardwareMonitor / OpenHardwareMonitor çalışıyorsa WMI üzerinden
  "CPU Package" sensörü; yoksa ACPI termal bölgesi (MSAcpi_ThermalZoneTemperature,
  genellikle yönetici yetkisi ister, değer işlemci çekirdeği değil anakart
  bölgesidir; bu yüzden kaynağıyla birlikte "yaklaşık" diye bildirilir).
- Linux: çekirdeğin coretemp/k10temp/zenpower sürücüleri.

Hiçbiri yoksa sıcaklık uydurulmaz: "desteklenmiyor" ve nedeni döner.
"""

from __future__ import annotations

import logging
import platform
import sys
from dataclasses import asdict

import psutil

from jarvis.security.permission_manager import Permission
from jarvis.tools import schema as js
from jarvis.tools.registry import ToolContext, ToolOutput, ToolRegistry, ToolSpec

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
_GB = 1024 ** 3


def _plausible(c: float) -> bool:
    return 5.0 <= c <= 115.0


def cpu_temperature() -> tuple[float | None, str]:
    """(°C, kaynak) ya da (None, neden)."""
    if IS_WINDOWS:  # pragma: no cover - Windows
        return _cpu_temp_windows()
    reader = getattr(psutil, "sensors_temperatures", None)
    if reader is None:
        return None, "Bu sistemde sıcaklık sensörü okunamıyor."
    try:
        temps = reader()
    except Exception:  # noqa: BLE001
        return None, "Sıcaklık sensörleri okunamadı."
    for driver in ("coretemp", "k10temp", "zenpower", "cpu_thermal"):
        entries = temps.get(driver) or []
        pkg = [e for e in entries if (e.label or "").lower().startswith(("package", "tctl", "tdie"))] or entries
        vals = [e.current for e in pkg if e.current is not None and _plausible(e.current)]
        if vals:
            return round(max(vals), 1), f"{driver} sürücüsü"
    return None, "Güvenilir bir işlemci sıcaklık sensörü bulunamadı."


def _cpu_temp_windows() -> tuple[float | None, str]:  # pragma: no cover - Windows
    try:
        from jarvis.tools.winapi import com_thread

        com_thread()
        import win32com.client
    except ImportError:
        return None, "pywin32 kurulu değil, sıcaklık okunamıyor."
    locator = win32com.client.Dispatch("WbemScripting.SWbemLocator")
    for ns, label in (("root\\LibreHardwareMonitor", "LibreHardwareMonitor"),
                      ("root\\OpenHardwareMonitor", "OpenHardwareMonitor")):
        try:
            svc = locator.ConnectServer(".", ns)
            rows = svc.ExecQuery("SELECT Name, Value FROM Sensor WHERE SensorType='Temperature'")
            vals = [(str(r.Name), float(r.Value)) for r in rows]
        except Exception:  # noqa: BLE001  ad alanı yok: program çalışmıyor
            continue
        pkg = [v for n, v in vals if "package" in n.lower() or "tctl" in n.lower() or "tdie" in n.lower()]
        cores = [v for n, v in vals if n.lower().startswith(("cpu core", "core"))]
        chosen = [v for v in (pkg or cores) if _plausible(v)]
        if chosen:
            return round(max(chosen), 1), label
    try:
        svc = locator.ConnectServer(".", "root\\wmi")
        rows = svc.ExecQuery("SELECT CurrentTemperature FROM MSAcpi_ThermalZoneTemperature")
        vals = [float(r.CurrentTemperature) / 10.0 - 273.15 for r in rows]
        vals = [v for v in vals if _plausible(v)]
        if vals:
            return round(max(vals), 1), "ACPI termal bölgesi (yaklaşık, işlemci çekirdeği değil)"
    except Exception:  # noqa: BLE001  çoğunlukla yönetici yetkisi gerekir
        pass
    return None, ("Güvenilir bir işlemci sıcaklık sensörü yok. LibreHardwareMonitor çalışıyorsa okuyabilirim; "
                  "Windows'un kendi ACPI değeri yönetici yetkisi istiyor ya da desteklenmiyor.")


def cpu_name() -> str:
    if IS_WINDOWS:  # pragma: no cover - Windows
        import winreg

        from jarvis.tools.winapi import reg_get

        v = reg_get(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
                    "ProcessorNameString")
        if isinstance(v, str) and v.strip():
            return v.strip()
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as f:
            for line in f:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "bilinmiyor"


def cpu_status(sample_s: float = 0.3) -> dict:
    per_core = psutil.cpu_percent(interval=sample_s, percpu=True)
    freq = None
    try:
        f = psutil.cpu_freq()
        freq = round(f.current) if f and f.current else None
    except Exception:  # noqa: BLE001
        pass
    temp, temp_src = cpu_temperature()
    return {
        "name": cpu_name(),
        "usage_percent": round(sum(per_core) / max(1, len(per_core)), 1),
        "per_core_percent": [round(x) for x in per_core],
        "logical_cores": psutil.cpu_count(),
        "physical_cores": psutil.cpu_count(logical=False),
        "frequency_mhz": freq,
        "temperature_c": temp,
        "temperature_source" if temp is not None else "temperature_unavailable": temp_src,
    }


def memory_status() -> dict:
    vm, sw = psutil.virtual_memory(), psutil.swap_memory()
    return {"total_gb": round(vm.total / _GB, 1), "used_gb": round((vm.total - vm.available) / _GB, 1),
            "available_gb": round(vm.available / _GB, 1), "percent": vm.percent,
            "swap_total_gb": round(sw.total / _GB, 1), "swap_percent": sw.percent}


def gpu_status(snapshot) -> dict:  # noqa: ANN001
    if snapshot is None:
        return {"available": False, "reason": "Henüz sistem ölçümü yok, birkaç saniye sonra tekrar dene."}
    if snapshot.gpu is None:
        return {"available": False, "reason": snapshot.gpu_unavailable_reason or "GPU bilgisi yok"}
    d = asdict(snapshot.gpu)
    d["available"] = True
    return d


def gpu_summary(g: dict) -> str:
    if not g.get("available"):
        return f"Ekran kartı bilgisi okunamıyor: {g['reason']}."
    parts = [g["name"]]
    if g.get("temperature_c") is not None:
        parts.append(f"sıcaklık {g['temperature_c']} °C")
    if g.get("utilization_pct") is not None:
        parts.append(f"kullanım %{g['utilization_pct']}")
    if g.get("memory_used_mb") is not None and g.get("memory_total_mb"):
        parts.append(f"bellek {g['memory_used_mb'] / 1024:.1f}/{g['memory_total_mb'] / 1024:.1f} GB")
    if g.get("fan_pct") is not None:
        parts.append(f"fan %{g['fan_pct']}")
    else:
        parts.append("fan hızı bu kartta okunamıyor")
    return ", ".join(parts) + "."


def register(reg: ToolRegistry, ctx: ToolContext) -> None:
    def gpu(args: dict) -> ToolOutput:
        g = gpu_status(ctx.snapshot())
        return ToolOutput(gpu_summary(g), g)

    reg.register(ToolSpec(
        name="gpu_status", title="Ekran kartı durumu", category="donanım",
        description=("NVIDIA ekran kartının adı, sıcaklığı, kullanımı, belleği ve fan hızı (desteklenen değerler). / "
                     "NVIDIA GPU name, temperature, utilization, memory and fan when supported."),
        input_schema=js.obj(), permission=Permission.READ, handler=gpu))

    def cpu(args: dict) -> ToolOutput:
        c = cpu_status()
        t = (f", sıcaklık {c['temperature_c']} °C ({c['temperature_source']})" if c["temperature_c"] is not None
             else ". Sıcaklık: desteklenmiyor")
        return ToolOutput(f"İşlemci kullanımı %{c['usage_percent']:.0f}{t}.", c)

    reg.register(ToolSpec(
        name="cpu_status", title="İşlemci durumu", category="donanım",
        description=("İşlemci adı, kullanım (çekirdek başına), frekans; sıcaklık yalnızca güvenilir sensör varsa. / "
                     "CPU name, usage, frequency; temperature only from reliable sensors."),
        input_schema=js.obj(), permission=Permission.READ, handler=cpu))

    def mem(args: dict) -> ToolOutput:
        m = memory_status()
        return ToolOutput(f"Bellek: {m['used_gb']:.1f}/{m['total_gb']:.1f} GB kullanımda (%{m['percent']:.0f}).", m)

    reg.register(ToolSpec(
        name="memory_status", title="Bellek durumu", category="donanım",
        description="RAM ve sanal bellek kullanımı. / RAM and swap usage.",
        input_schema=js.obj(), permission=Permission.READ, handler=mem))

