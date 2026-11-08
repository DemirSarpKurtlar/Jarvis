"""Ekran araçları: ekran görüntüsünü kaydet ve "ekranımda ne var?" (görüntüyü Claude'a göster).

* `take_screenshot`: tam çözünürlüklü PNG'yi Resimler\\Screenshots\\JARVIS klasörüne kaydeder
  (dosya korumalı alanının "Resimler" kökü içinde; kök yoksa kaydetmez). Dosya yazıldıktan sonra
  boyutu doğrulanır.
* `look_at_screen`: ekranı en uzun kenarı 1568 px olacak şekilde küçültüp tool_result içinde görüntü
  bloğu olarak Claude'a verir. Diske yazılmaz, denetim kaydına ve konuşma geçmişine girmez.
  Görüntü güvenilmeyen içerik olarak işaretlenir (ekrandaki yazılar talimat sayılmaz).

Yakalama `jarvis/system/screen.py` (çoklu monitör, DPI) üzerinden yapılır.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

from jarvis.security.action_validator import ValidationError
from jarvis.security.permission_manager import Permission
from jarvis.system import screen
from jarvis.tools import schema as js
from jarvis.tools.registry import (WINDOWS_ONLY, ToolContext, ToolError, ToolImage, ToolOutput, ToolRegistry,
                                   ToolSpec)

log = logging.getLogger(__name__)

SCREENSHOT_SUBDIR = ("Screenshots", "JARVIS")
LOOK_MAX_EDGE = screen.DEFAULT_MAX_LONG_EDGE
TARGETS = {"screen": "all", "monitor": "monitor", "active_window": "window"}
_WHAT = {"all": "tüm ekranların", "monitor": "ekranın", "window": "pencerenin"}


class ScreenTools:
    def __init__(self, ctx: ToolContext, backend: screen.ScreenBackend | None = None,
                 now=datetime.now) -> None:  # noqa: ANN001
        self.ctx = ctx
        self._backend = backend
        self.now = now

    @property
    def backend(self) -> screen.ScreenBackend:
        if self._backend is None:
            self._backend = screen.default_backend()
        return self._backend

    def _grab(self, target: str, monitor: int, max_edge: int | None) -> screen.Capture:
        return screen.grab(TARGETS[target], monitor=monitor or None, max_long_edge=max_edge, backend=self.backend)

    # ---------- kaydet ----------
    def _folder(self) -> Path:
        sb = self.ctx.sandbox
        root = sb.root_for_alias("pictures") if sb is not None else None
        if root is None:
            raise ToolError("resimler kökü yok", "Resimler klasörüne erişim iznim yok; ekran görüntüsünü kaydedemiyorum.")
        folder = root.path.joinpath(*SCREENSHOT_SUBDIR)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            return sb.resolve(str(folder), must_exist=True)
        except ValidationError as e:
            raise ToolError("klasör", e.user_message) from e
        except OSError as e:
            raise ToolError(f"klasör: {e}", f"Ekran görüntüsü klasörü oluşturulamadı: {e.strerror or e}") from e

    def take_screenshot(self, args: dict) -> ToolOutput:
        cap = self._grab(args["target"], args["monitor"], None)
        folder = self._folder()
        stamp = self.now().strftime("%Y-%m-%d_%H-%M-%S")
        path = folder / f"JARVIS_{stamp}.png"
        n = 2
        while path.exists():
            path = folder / f"JARVIS_{stamp}_{n}.png"
            n += 1
        try:
            path = self.ctx.sandbox.resolve(str(path), for_write=True)
        except ValidationError as e:
            raise ToolError("yol", e.user_message) from e
        tmp = path.with_name(path.name + ".part")
        try:
            with open(tmp, "wb") as f:
                f.write(cap.png)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except OSError as e:
            tmp.unlink(missing_ok=True)
            raise ToolError(f"yazılamadı: {e}", f"Ekran görüntüsü kaydedilemedi: {e.strerror or e}") from e
        if not path.is_file() or path.stat().st_size != len(cap.png):
            raise ToolError("doğrulanamadı", "Ekran görüntüsü kaydedildi gibi görünüyor ama dosya doğrulanamadı.")
        what = _WHAT[TARGETS[args["target"]]]
        mon = f" ({cap.monitor}. monitör)" if cap.monitor and args["target"] == "monitor" else ""
        return ToolOutput(f"{what.capitalize()} görüntüsünü kaydettim{mon}: {path.name} "
                          f"({cap.width}×{cap.height}), Resimler\\Screenshots\\JARVIS klasöründe.",
                          {"path": str(path), "width": cap.width, "height": cap.height, "monitor": cap.monitor})

    # ---------- Claude'a göster ----------
    def look_at_screen(self, args: dict) -> ToolOutput:
        cap = self._grab(args["target"], 0, LOOK_MAX_EDGE)
        log.info("Ekran görüntüsü Claude'a gönderiliyor: %dx%d (%d bayt)", cap.width, cap.height, len(cap.png))
        data = {"question": args["question"], "image_width": cap.width, "image_height": cap.height,
                "screen_region": [cap.region.left, cap.region.top, cap.region.width, cap.region.height],
                "monitor": cap.monitor}
        if cap.title:
            data["window_title"] = cap.title
        return ToolOutput("Ekran görüntüsünü aldım, inceliyorum.", data, untrusted=True,
                          image=ToolImage(cap.png, "image/png", cap.width, cap.height))


def register(reg: ToolRegistry, ctx: ToolContext, backend: screen.ScreenBackend | None = None) -> ScreenTools:
    t = ScreenTools(ctx, backend)
    target = js.string("screen = tüm monitörler, monitor = tek monitör, active_window = öndeki pencere",
                       enum=list(TARGETS))
    reg.register(ToolSpec(
        name="take_screenshot", title="Ekran görüntüsü al", category="ekran", platforms=WINDOWS_ONLY,
        description=("Ekran görüntüsünü PNG olarak Resimler\\Screenshots\\JARVIS klasörüne kaydeder. / Saves a "
                     "screenshot PNG to Pictures\\Screenshots\\JARVIS."),
        input_schema=js.obj(target=target,
                            monitor=js.integer("monitor için numara, 0 = öndeki pencerenin monitörü / monitor no",
                                               0, 9)),
        permission=Permission.SAFE, handler=t.take_screenshot, timeout_s=20))
    reg.register(ToolSpec(
        name="look_at_screen", title="Ekrana bak", category="ekran", platforms=WINDOWS_ONLY,
        description=("Kullanıcının ekranının görüntüsünü alıp sana gösterir. 'Ekranımda ne var', 'bu hatayı açıkla', "
                     "'ekrana bak' gibi isteklerde kullan. / Captures the user's screen and returns it as an image; "
                     "use when asked about what is on screen."),
        input_schema=js.obj(question=js.string("Kullanıcının ekranla ilgili sorusu / the user's question", 500),
                            target=target),
        permission=Permission.READ, handler=t.look_at_screen, timeout_s=20))
    return t
