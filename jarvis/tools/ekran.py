"""Ekran, pencere ve ses girişi araçlarını tek çağrıyla kaydeder (defaults.py'de tek satır).

    pencere düzeni   : arrange_windows          (jarvis/tools/window_layout.py)
    ekran görüntüsü  : take_screenshot, look_at_screen (jarvis/tools/screen_tools.py)
    sesli okuma      : read_text_aloud, speak_in_language (jarvis/tools/speech_tools.py)
    dikte modu       : dictation (yalnızca yerel komut; jarvis/tools/dictation.py)

Testler arka uçları `ctx.extra` ile verir: "layout", "screen", "keyboard", "clipboard", "layout_pids".
"""

from __future__ import annotations

from jarvis.tools import dictation, screen_tools, speech_tools, window_layout
from jarvis.tools.registry import ToolContext, ToolRegistry


def register(reg: ToolRegistry, ctx: ToolContext) -> None:
    x = ctx.extra
    window_layout.register(reg, ctx, x.get("layout"), x.get("layout_pids"))
    screen_tools.register(reg, ctx, x.get("screen"))
    speech_tools.register(reg, ctx, x.get("keyboard"), x.get("clipboard"))
    dictation.register(reg, ctx, x.get("keyboard"))
