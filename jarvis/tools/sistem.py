"""Sistem özellikleri araçlarını tek çağrıda kaydeder (defaults.py yalnızca bunu çağırır).

Araçlar: find_space_hogs, clean_temp_files, cancel_long_operation (maintenance), downloads_routine,
radio_control, internet_speed_test (network_tools), winget_packages, windows_update_check (packages).
Güç eylemleri (uyku/hazırda bekletme) `windows_actions.power_action` içindedir.

Testler sahte arka uçları `ctx.extra["sistem"]` sözlüğüyle verir:
    radios, speed, sleep (network_tools) · scanner, cleaner (maintenance) · packages (PackageTools)
    · state_dir (İndirilenler rutini durum klasörü)
Kurulan İndirilenler rutini `ctx.extra["sistem_routine"]` içine konur (günlük çalıştırma için).
"""

from __future__ import annotations

from jarvis.tools import downloads_routine, maintenance, network_tools, packages
from jarvis.tools.registry import ToolContext, ToolRegistry


def register(reg: ToolRegistry, ctx: ToolContext) -> None:
    fakes = ctx.extra.get("sistem", {})
    maintenance.register(reg, ctx, scanner=fakes.get("scanner"), cleaner=fakes.get("cleaner"))
    ctx.extra["sistem_routine"] = downloads_routine.register(reg, ctx, state_dir=fakes.get("state_dir"))
    network_tools.register(reg, ctx, radios=fakes.get("radios"), speed=fakes.get("speed"),
                           **({"sleep": fakes["sleep"]} if "sleep" in fakes else {}))
    packages.register(reg, ctx, tools=fakes.get("packages"))
