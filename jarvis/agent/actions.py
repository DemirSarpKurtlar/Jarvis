"""Claude'un bilgisayar araç seti üyelerini yürütür (arka plan thread'inde, engelleyici).

Her çağrı: biçim doğrulama -> ön plan penceresi korumaları -> eylem -> sonuç. Koordinatlar ekran
görüntüsü uzayındadır ve `ScreenScaler` ile fiziksel piksele çevrilir. `screenshot` ve `zoom` görüntü,
`cursor_position` metin, diğerleri "OK" döndürür. Acil durdurma (`stop`) beklemeler ve uzun yazma
sırasında da denetlenir; basılı tuş/düğme her durumda bırakılır.
"""

from __future__ import annotations

import base64
import logging
import threading
from dataclasses import dataclass, field

import numpy as np

from jarvis.agent import imaging
from jarvis.agent.backends import Desktop, ForegroundInfo
from jarvis.agent.geometry import CoordinateError, ScreenScaler, fit_size
from jarvis.agent.keys import EXTENDED_VK, Combo, KeyError_, parse_combo, parse_modifiers
from jarvis.agent.safety import game_action_reason, key_reason, secret_reason, sensitive_reason

log = logging.getLogger(__name__)

MEMBERS = ("screenshot", "zoom", "left_click", "right_click", "middle_click", "double_click", "triple_click",
           "left_click_drag", "mouse_move", "left_mouse_down", "left_mouse_up", "cursor_position", "scroll",
           "type", "key", "hold_key", "wait")
CLICKS = {"left_click": ("left", 1), "right_click": ("right", 1), "middle_click": ("middle", 1),
          "double_click": ("left", 2), "triple_click": ("left", 3)}
MAX_TYPE_CHARS = 2000
MAX_SCROLL = 30
MAX_WAIT_S = 300
VK_RETURN, VK_TAB, VK_ESCAPE = 0x0D, 0x09, 0x1B


class ActionError(Exception):
    """Eylem yapılamadı; mesaj Claude'a `is_error` sonucu olarak gider."""


class SafetyStop(Exception):
    """Hassas pencere ya da yasak eylem: oturum durdurulmalı (mesaj Türkçe, kullanıcıya gider)."""


class Interrupted(Exception):
    """Kullanıcı durdurdu (Esc Esc, "dur") ya da süre doldu. `args[0]`: "stop" / "time"."""


@dataclass
class ActionResult:
    text: str = "OK"
    image: tuple[str, str] | None = None       # (medya türü, base64)
    note: str = ""                              # görüntünün yanına eklenecek kısa not
    clicked_at: tuple[float, float] | None = None   # tıklanan nokta (0-1 oranı), değişim denetimi için
    thumb: np.ndarray | None = field(default=None, repr=False)

    def content(self) -> str | list[dict]:
        """tool_result içeriği."""
        if self.image is None:
            return self.text
        media, data = self.image
        out: list[dict] = [{"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}]
        if self.note:
            out.append({"type": "text", "text": self.note})
        return out


def describe(member: str, args: dict) -> str:
    """İlerleme bildirimi için kısa Türkçe açıklama (yazılan metnin kendisi gösterilmez)."""
    coord = args.get("coordinate")
    at = f" ({coord[0]}, {coord[1]})" if isinstance(coord, (list, tuple)) and len(coord) == 2 else ""
    return {
        "screenshot": "ekrana bakıyorum",
        "zoom": "ekranın bir bölümüne yakından bakıyorum",
        "left_click": f"tıklıyorum{at}",
        "right_click": f"sağ tıklıyorum{at}",
        "middle_click": f"orta tuşla tıklıyorum{at}",
        "double_click": f"çift tıklıyorum{at}",
        "triple_click": f"üç kez tıklıyorum{at}",
        "left_click_drag": "sürüklüyorum",
        "mouse_move": f"fareyi taşıyorum{at}",
        "left_mouse_down": "fare düğmesine basıyorum",
        "left_mouse_up": "fare düğmesini bırakıyorum",
        "cursor_position": "imleç konumuna bakıyorum",
        "scroll": f"kaydırıyorum ({args.get('scroll_direction', '')})",
        "type": f"yazıyorum ({len(str(args.get('text', '')))} karakter)",
        "key": f"{str(args.get('text', ''))[:30]} tuşuna basıyorum",
        "hold_key": f"{str(args.get('text', ''))[:30]} tuşunu basılı tutuyorum",
        "wait": f"{args.get('duration', '')} saniye bekliyorum",
    }.get(member, member)


class ActionExecutor:
    def __init__(self, desktop: Desktop, scaler: ScreenScaler, stop: threading.Event,
                 prefer_jpeg: bool = True) -> None:
        self.d = desktop
        self.scaler = scaler
        self.stop = stop
        self.prefer_jpeg = prefer_jpeg
        self._held_buttons: set[str] = set()
        self.deadline: float | None = None     # oturumun bitiş anı (clock.monotonic)

    # ---------- yardımcılar ----------
    def _check_stop(self) -> None:
        if self.stop.is_set():
            raise Interrupted("stop")
        if self.deadline is not None and self.d.clock.monotonic() >= self.deadline:  # type: ignore[union-attr]
            raise Interrupted("time")

    def _sleep(self, seconds: float) -> None:
        """Durdurulabilir bekleme (0,1 sn dilimlerle)."""
        clock = self.d.clock
        assert clock is not None
        end = clock.monotonic() + seconds
        while True:
            self._check_stop()
            left = end - clock.monotonic()
            if left <= 0:
                return
            clock.sleep(min(0.1, left))

    def foreground(self) -> ForegroundInfo:
        try:
            return self.d.probe.foreground()
        except Exception:  # noqa: BLE001
            log.debug("Ön plan penceresi okunamadı", exc_info=True)
            return ForegroundInfo()

    def guard_foreground(self) -> ForegroundInfo:
        fg = self.foreground()
        reason = sensitive_reason(fg)
        if reason is None:
            try:
                if self.d.probe.process_running("consent.exe"):
                    reason = SENSITIVE_UAC
            except Exception:  # noqa: BLE001
                log.debug("İşlem listesi okunamadı", exc_info=True)
        if reason:
            raise SafetyStop(f"Ekranda {reason} açık. Güvenlik nedeniyle burada işlem yapmıyorum ve durdum; "
                             "bu adımı sen tamamlamalısın.")
        return fg

    def _move(self, point: object, name: str = "coordinate") -> tuple[int, int]:
        x, y = self.scaler.to_physical(point, name)
        self.d.input.move(x, y)
        return x, y

    def _with_modifiers(self, text: object, fn) -> None:  # noqa: ANN001
        mods = parse_modifiers(text if isinstance(text, str) else None)
        pressed: list[int] = []
        try:
            for vk in mods:
                self.d.input.key(vk, True, vk in EXTENDED_VK)
                pressed.append(vk)
            fn()
        finally:
            for vk in reversed(pressed):
                self.d.input.key(vk, False, vk in EXTENDED_VK)

    def _press_combo(self, combo: Combo) -> None:
        inp = self.d.input
        pressed: list[int] = []
        try:
            for vk in combo.modifiers:
                inp.key(vk, True, vk in EXTENDED_VK)
                pressed.append(vk)
            if combo.key is not None:
                if combo.key.vk:
                    if combo.key.vk == VK_ESCAPE and self.d.kill_switch is not None:
                        self.d.kill_switch.note_injected_escape()
                    inp.key(combo.key.vk, True, combo.key.extended)
                    inp.key(combo.key.vk, False, combo.key.extended)
                else:
                    inp.unicode_char(combo.key.char)
        finally:
            for vk in reversed(pressed):
                inp.key(vk, False, vk in EXTENDED_VK)

    def _combo(self, text: object) -> Combo:
        if not isinstance(text, str):
            raise ActionError("text bir tuş adı olmalı (örn. 'Return', 'ctrl+s').")
        try:
            combo = parse_combo(text, self.d.input.char_vk)
        except KeyError_ as e:
            raise ActionError(str(e)) from e
        reason = key_reason(combo)
        if reason:
            raise ActionError(f"Bu kısayolu kullanmıyorum: {reason}.")
        return combo

    def _int(self, args: dict, name: str, lo: int, hi: int, default: int | None = None) -> int:
        v = args.get(name, default)
        if v is None:
            raise ActionError(f"'{name}' gerekli.")
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ActionError(f"'{name}' bir sayı olmalı.")
        v = int(v)
        if not lo <= v <= hi:
            raise ActionError(f"'{name}' {lo}-{hi} arasında olmalı.")
        return v

    def capture_frame(self) -> np.ndarray:
        return imaging.as_frame(self.d.screen.capture(self.scaler.monitor))

    def _image(self, frame: np.ndarray) -> tuple[str, str]:
        media, data = imaging.encode(frame, self.prefer_jpeg)
        return media, base64.b64encode(data).decode("ascii")

    # ---------- yürütme ----------
    def run(self, member: str, args: dict, fg: ForegroundInfo | None = None) -> ActionResult:
        """Tek bir üye çağrısı. ActionError / SafetyStop / Interrupted fırlatabilir."""
        self._check_stop()
        if member not in MEMBERS:
            raise ActionError(f"Bilinmeyen bilgisayar eylemi: {member}")
        if not isinstance(args, dict):
            raise ActionError("Eylem girdisi bir nesne olmalı.")
        fg = fg if fg is not None else self.guard_foreground()
        combo = self._combo(args.get("text")) if member in ("key", "hold_key") else None
        reason = game_action_reason(member, fg, combo)
        if reason:
            raise ActionError(reason)
        try:
            return getattr(self, "_do_" + member)(args, fg, combo)
        except CoordinateError as e:
            raise ActionError(str(e)) from e
        except KeyError_ as e:
            raise ActionError(str(e)) from e

    def _do_screenshot(self, args: dict, fg: ForegroundInfo, combo: Combo | None) -> ActionResult:
        frame = self.capture_frame()
        small = imaging.resize(frame, self.scaler.width, self.scaler.height)
        note = ""
        if int(frame.max(initial=0)) < 8:
            note = ("Ekran görüntüsü tamamen siyah. Oyun özel (exclusive) tam ekran modunda olabilir; bu modda "
                    "ekran yakalanamaz. Kullanıcıdan oyunu pencereli tam ekran (borderless) moduna almasını iste.")
        return ActionResult(image=self._image(small), note=note, thumb=imaging.thumbnail(small))

    def _do_zoom(self, args: dict, fg: ForegroundInfo, combo: Combo | None) -> ActionResult:
        region = self.scaler.region_to_physical(args.get("region"))
        frame = self.capture_frame()
        part = imaging.crop(frame, region)
        # Kırpılan bölge görüntü bütçesini dolduracak şekilde ölçeklenir (büyütme serbest)
        s = min(self.scaler.width / region.width, self.scaler.height / region.height)
        w, h = fit_size(max(1, int(region.width * s)), max(1, int(region.height * s)),
                        max(self.scaler.width, self.scaler.height))
        return ActionResult(image=self._image(imaging.resize(part, w, h)))

    def _click_ratio(self, args: dict) -> tuple[float, float] | None:
        c = args.get("coordinate")
        if isinstance(c, (list, tuple)) and len(c) == 2:
            return float(c[0]) / self.scaler.width, float(c[1]) / self.scaler.height
        try:
            px, py = self.d.input.cursor()
            x, y = self.scaler.to_screenshot(px, py)
            return x / self.scaler.width, y / self.scaler.height
        except Exception:  # noqa: BLE001
            return None

    def _do_click(self, member: str, args: dict) -> ActionResult:
        button, count = CLICKS[member]

        def act() -> None:
            if args.get("coordinate") is not None:
                self._move(args["coordinate"])
                self.d.clock.sleep(0.03)  # type: ignore[union-attr]  # hover etkisi için kısa an
            for i in range(count):
                self.d.input.button(button, True)
                self.d.input.button(button, False)
                if i + 1 < count:
                    self.d.clock.sleep(0.05)  # type: ignore[union-attr]

        ratio = self._click_ratio(args)
        self._with_modifiers(args.get("text"), act)
        return ActionResult(clicked_at=ratio)

    def _do_left_click(self, a, fg, c):  # noqa: ANN001, ANN202
        return self._do_click("left_click", a)

    def _do_right_click(self, a, fg, c):  # noqa: ANN001, ANN202
        return self._do_click("right_click", a)

    def _do_middle_click(self, a, fg, c):  # noqa: ANN001, ANN202
        return self._do_click("middle_click", a)

    def _do_double_click(self, a, fg, c):  # noqa: ANN001, ANN202
        return self._do_click("double_click", a)

    def _do_triple_click(self, a, fg, c):  # noqa: ANN001, ANN202
        return self._do_click("triple_click", a)

    def _do_left_click_drag(self, args: dict, fg: ForegroundInfo, combo: Combo | None) -> ActionResult:
        sx, sy = self.scaler.to_physical(args.get("start_coordinate"), "start_coordinate")
        ex, ey = self.scaler.to_physical(args.get("coordinate"), "coordinate")

        def act() -> None:
            self.d.input.move(sx, sy)
            self.d.input.button("left", True)
            try:
                for i in range(1, 11):
                    self._check_stop()
                    self.d.input.move(sx + (ex - sx) * i // 10, sy + (ey - sy) * i // 10)
                    self.d.clock.sleep(0.02)  # type: ignore[union-attr]
            finally:
                self.d.input.button("left", False)

        self._with_modifiers(args.get("text"), act)
        return ActionResult()

    def _do_mouse_move(self, args: dict, fg: ForegroundInfo, combo: Combo | None) -> ActionResult:
        self._move(args.get("coordinate"))
        return ActionResult()

    def _do_left_mouse_down(self, args: dict, fg: ForegroundInfo, combo: Combo | None) -> ActionResult:
        self.d.input.button("left", True)
        self._held_buttons.add("left")
        return ActionResult()

    def _do_left_mouse_up(self, args: dict, fg: ForegroundInfo, combo: Combo | None) -> ActionResult:
        self.d.input.button("left", False)
        self._held_buttons.discard("left")
        return ActionResult()

    def _do_cursor_position(self, args: dict, fg: ForegroundInfo, combo: Combo | None) -> ActionResult:
        px, py = self.d.input.cursor()
        x, y = self.scaler.to_screenshot(px, py)
        return ActionResult(text=f"X={x},Y={y}")

    def _do_scroll(self, args: dict, fg: ForegroundInfo, combo: Combo | None) -> ActionResult:
        direction = args.get("scroll_direction")
        if direction not in ("up", "down", "left", "right"):
            raise ActionError("scroll_direction up/down/left/right olmalı.")
        amount = self._int(args, "scroll_amount", 0, MAX_SCROLL, 3)
        if args.get("coordinate") is not None:
            self._move(args["coordinate"])
        v = {"up": amount, "down": -amount}.get(direction, 0)
        h = {"right": amount, "left": -amount}.get(direction, 0)
        self._with_modifiers(args.get("text"), lambda: self.d.input.wheel(v, h))
        return ActionResult()

    def _do_type(self, args: dict, fg: ForegroundInfo, combo: Combo | None) -> ActionResult:
        text = args.get("text")
        if not isinstance(text, str) or not text:
            raise ActionError("Yazılacak metin boş olamaz.")
        if len(text) > MAX_TYPE_CHARS:
            raise ActionError(f"Bir seferde en çok {MAX_TYPE_CHARS} karakter yazabilirim.")
        reason = secret_reason(text, fg)
        if reason:
            raise ActionError(f"Bunu yazmıyorum: {reason}. Gizli bilgileri kullanıcı kendisi girmeli.")
        inp = self.d.input
        for i, ch in enumerate(text.replace("\r\n", "\n")):
            if i % 20 == 0:
                self._check_stop()
            if ch == "\n":
                inp.key(VK_RETURN, True)
                inp.key(VK_RETURN, False)
            elif ch == "\t":
                inp.key(VK_TAB, True)
                inp.key(VK_TAB, False)
            else:
                inp.unicode_char(ch)
            if i % 20 == 19:
                self.d.clock.sleep(0.01)  # type: ignore[union-attr]  # bazı uygulamalar hızlı girdiyi düşürür
        return ActionResult()

    def _do_key(self, args: dict, fg: ForegroundInfo, combo: Combo | None) -> ActionResult:
        assert combo is not None
        repeat = self._int(args, "repeat", 1, 100, 1)
        for i in range(repeat):
            self._check_stop()
            self._press_combo(combo)
            if i + 1 < repeat:
                self.d.clock.sleep(0.03)  # type: ignore[union-attr]
        return ActionResult()

    def _do_hold_key(self, args: dict, fg: ForegroundInfo, combo: Combo | None) -> ActionResult:
        assert combo is not None
        duration = self._int(args, "duration", 0, MAX_WAIT_S)
        inp = self.d.input
        vks = list(combo.modifiers) + ([combo.key.vk] if combo.key is not None and combo.key.vk else [])
        if not vks:
            raise ActionError("Basılı tutulacak tuş bulunamadı.")
        pressed: list[int] = []
        try:
            for vk in vks:
                inp.key(vk, True, vk in EXTENDED_VK)
                pressed.append(vk)
            self._sleep(duration)
        finally:
            for vk in reversed(pressed):
                inp.key(vk, False, vk in EXTENDED_VK)
        return ActionResult()

    def _do_wait(self, args: dict, fg: ForegroundInfo, combo: Combo | None) -> ActionResult:
        self._sleep(self._int(args, "duration", 0, MAX_WAIT_S))
        return ActionResult()

    def release_all(self) -> None:
        """Oturum biterken basılı kalmış fare düğmesi varsa bırak."""
        for b in list(self._held_buttons):
            try:
                self.d.input.button(b, False)
            except Exception:  # noqa: BLE001
                log.debug("Düğme bırakılamadı", exc_info=True)
        self._held_buttons.clear()


SENSITIVE_UAC = "Kullanıcı Hesabı Denetimi (UAC) penceresi"
