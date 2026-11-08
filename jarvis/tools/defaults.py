"""Varsayılan araç kaydını kurar.

    ctx = ToolContext(sandbox=PathSandbox(), snapshot=lambda: last_snapshot, apps=AppIndex(), ...)
    registry = build_registry(ctx, audit=AuditLogger(db))

Testler `ToolContext`'e sahte arka uçlar verir (uygulama dizini, pencereler, ses, açıcı).
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Executor

from jarvis.security.audit_logger import AuditLogger
from jarvis.security.permission_manager import PermissionManager
from jarvis.tools import applications, files, hardware, processes, scheduler, system_info, windows_actions
from jarvis.tools.registry import ToolContext, ToolRegistry


def default_context(sandbox, snapshot: Callable = lambda: None) -> ToolContext:  # noqa: ANN001
    """Gerçek arka uçlarla bağlam (Windows dışında pencere/ses arka uçları "desteklenmiyor" der)."""
    from jarvis.tools.winapi import default_audio, default_opener, default_windows

    return ToolContext(sandbox=sandbox, snapshot=snapshot, apps=applications.AppIndex(), windows=default_windows(),
                       audio=default_audio(), opener=default_opener)


def build_registry(ctx: ToolContext, audit: AuditLogger | None = None, permissions: PermissionManager | None = None,
                   executor: Executor | None = None, trash: Callable[[str], None] | None = None,
                   action_backends: dict | None = None) -> ToolRegistry:
    reg = ToolRegistry(audit=audit, permissions=permissions, executor=executor)
    applications.register(reg, ctx)
    processes.register(reg, ctx)
    hardware.register(reg, ctx)
    system_info.register(reg, ctx)
    files.register(reg, ctx, **({"trash": trash} if trash else {}))
    windows_actions.register(reg, ctx, **(action_backends or {}))
    scheduler.register(reg, ctx)
    if ctx.scheduler is None:
        ctx.scheduler = scheduler.TaskScheduler(reg)
    # --- RUTIN ---
    from jarvis.tools import briefing, routines
    briefing.register(reg, ctx)
    routines.register(reg, ctx)
    # --- RUTIN sonu ---
    return reg
