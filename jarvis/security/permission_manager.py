"""İzin seviyeleri ve onay kararları.

Her araç bir taban seviyeyle kaydedilir. Bazı araçlar girdiye göre seviyesini
yükseltir (örn. `move_path` üzerine yazacaksa CONFIRM olur). Seviye hiçbir
zaman girdiye göre düşürülemez.

- READ: yalnızca okur, sormadan çalışır.
- SAFE: geri alınabilir küçük bir değişiklik yapar (uygulama açmak, ses ayarı).
- CONFIRM: kalıcı ya da yıkıcı olabilir; kullanıcı her seferinde onaylar.
- FORBIDDEN: hiçbir koşulda çalışmaz (yönetici yetkisi, güvenlik ayarı değişikliği vb.).

Onay penceresi engellemez: `ConfirmCallback` bir coroutine'dir; arayüz bir
asyncio Future'ı yanıtlar (bkz. `ToolConfirmRequest`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import IntEnum

log = logging.getLogger(__name__)


class Permission(IntEnum):
    READ = 0
    SAFE = 1
    CONFIRM = 2
    FORBIDDEN = 3

    @property
    def label(self) -> str:
        return {0: "okuma", 1: "güvenli", 2: "onaylı", 3: "yasak"}[int(self)]


@dataclass(frozen=True)
class ConfirmPrompt:
    tool: str
    title: str
    message: str
    danger: bool = False          # kalıcı silme gibi geri alınamaz işlemler: güçlü uyarı
    confirm_label: str = "Onayla"
    cancel_label: str = "Vazgeç"


ConfirmCallback = Callable[[ConfirmPrompt], Awaitable[bool]]


@dataclass
class ToolConfirmRequest:
    """Arayüze giden onay isteği. Arayüz `future`'ı True/False ile yanıtlar."""

    prompt: ConfirmPrompt
    future: asyncio.Future


@dataclass(frozen=True)
class Decision:
    level: Permission
    allowed: bool          # onay gerekmeden çalışabilir mi
    needs_confirmation: bool
    reason: str = ""


class PermissionManager:
    def __init__(self, read_only: bool = False, disabled_tools: set[str] | None = None) -> None:
        # Salt okunur modda yalnızca READ araçları çalışır
        self.read_only = read_only
        self.disabled_tools = set(disabled_tools or ())

    def decide(self, tool: str, base: Permission, escalated: Permission | None = None) -> Decision:
        level = max(base, escalated) if escalated is not None else base
        if tool in self.disabled_tools:
            return Decision(Permission.FORBIDDEN, False, False, "Bu araç ayarlardan kapatılmış.")
        if level is Permission.FORBIDDEN:
            return Decision(level, False, False, "Bu işlem güvenlik nedeniyle yapılamaz.")
        if self.read_only and level > Permission.READ:
            return Decision(level, False, False,
                            "Salt okunur mod açık: yalnızca bilgi okuyabilirim, değişiklik yapamam.")
        if level is Permission.CONFIRM:
            return Decision(level, False, True)
        return Decision(level, True, False)
