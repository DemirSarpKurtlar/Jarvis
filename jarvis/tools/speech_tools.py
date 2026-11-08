"""Sesli okuma araçları: seçili metni / panodakini oku, başka dilde söyle.

* `read_text_aloud(source="selection")`: öndeki pencereye "kopyala" kısayolu gönderir (terminallerde
  Ctrl+Insert: Ctrl+C orada çalışan programı durdurur), panodaki metni okur, panonun önceki
  içeriğini geri yükler ve metni Türkçe sesle okur. Panoya bir şey gelmezse "seçili metin yok" der.
* `read_text_aloud(source="clipboard")`: panodaki metni okur (tuş göndermez).
* `speak_in_language(text, language)`: metni o dilin sesiyle okur. İngilizce ses ilk ihtiyaçta
  indirilir (jarvis/voice/languages.py). Çeviriyi Claude yapar; bu araç yalnızca seslendirir.

Parola yöneticilerinin "pano geçmişine alma" işaretli içeriği okunmaz. Okunan metin güvenilmeyen
içerik olarak işaretlenir. Seslendirme ses köprüsü (`ctx.extra["voice"]`) üzerinden yapılır.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from jarvis.core.errors import JarvisError
from jarvis.security.permission_manager import Permission
from jarvis.tools import schema as js
from jarvis.tools.registry import WINDOWS_ONLY, ToolContext, ToolError, ToolOutput, ToolRegistry, ToolSpec
from jarvis.tools.win_input import ClipboardBackend, KeyboardBackend, default_clipboard, default_keyboard

log = logging.getLogger(__name__)

MAX_SPOKEN = 1500          # sesli okunacak en çok karakter (~1,5 dakika)
MAX_RETURNED = 4000        # Claude'a/ekrana dönen en çok karakter
SHOWN_IN_SUMMARY = 600
COPY_WAIT_S = 0.8
LANGUAGES = {"en": "İngilizce", "tr": "Türkçe"}


class SpeechTools:
    def __init__(self, ctx: ToolContext, keyboard: KeyboardBackend | None = None,
                 clipboard: ClipboardBackend | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        self.ctx = ctx
        self._keyboard = keyboard
        self._clipboard = clipboard
        self.sleep = sleep

    @property
    def keyboard(self) -> KeyboardBackend:
        if self._keyboard is None:
            self._keyboard = default_keyboard()
        return self._keyboard

    @property
    def clipboard(self) -> ClipboardBackend:
        if self._clipboard is None:
            self._clipboard = default_clipboard()
        return self._clipboard

    def _bridge(self):  # noqa: ANN202
        return self.ctx.extra.get("voice")

    # ---------- metni al ----------
    def _selected_text(self) -> tuple[str, bool]:
        """(metin, pano tamamen geri yüklendi mi)."""
        fg = self.keyboard.foreground()
        if fg is None:
            raise ToolError("öndeki pencere yok", "Öndeki pencereyi bulamadım.")
        if fg.is_own:
            raise ToolError("odak JARVIS'te", "Odak şu an JARVIS'te. Metni seçtiğin pencereye geçip sesle söyle ya da "
                                              "metni kopyalayıp 'panodakini oku' de.")
        clip = self.clipboard
        snap = clip.snapshot()
        before = clip.sequence()
        self.keyboard.copy_selection(use_insert=fg.is_terminal)
        end = time.monotonic() + COPY_WAIT_S
        while clip.sequence() == before and time.monotonic() < end:
            self.sleep(0.03)
        if clip.sequence() == before:
            raise ToolError("seçim yok", f"'{fg.title[:60]}' penceresinde seçili bir metin bulamadım.")
        try:
            sensitive = clip.is_sensitive()
            text = clip.get_text()
        finally:
            clip.restore(snap)      # kullanıcının panosu her durumda eski hâline döner
        if sensitive:
            raise ToolError("hassas", "Seçili içerik parola gibi korunan bir içerik; okumuyorum.")
        if not text or not text.strip():
            raise ToolError("metin yok", "Seçili içerik metin değil.")
        return text, not snap.lost

    def _clipboard_text(self) -> str:
        clip = self.clipboard
        if clip.is_sensitive():
            raise ToolError("hassas", "Panodaki içerik bir parola yöneticisinden geliyor gibi; okumuyorum.")
        text = clip.get_text()
        if not text or not text.strip():
            raise ToolError("pano boş", "Panoda okunacak bir metin yok.")
        return text

    # ---------- işleyiciler ----------
    def read_text_aloud(self, args: dict) -> ToolOutput:
        restored = True
        if args["source"] == "selection":
            text, restored = self._selected_text()
            what = "Seçili metni"
        else:
            text = self._clipboard_text()
            what = "Panodaki metni"
        text = text.strip()
        bridge = self._bridge()
        spoken = False
        reason = ""
        if bridge is not None and bridge.can_speak:
            try:
                spoken = bridge.speak(text[:MAX_SPOKEN], "tr", max_chars=MAX_SPOKEN + 1)
            except Exception as e:  # noqa: BLE001 - metin yine de gösterilsin
                log.warning("Sesli okuma başarısız: %s", e)
                reason = e.user_message if isinstance(e, JarvisError) else "seslendirme hatası"
        else:
            reason = bridge.unavailable_reason if bridge is not None else "Ses motoru hazır değil."
        shown = text if len(text) <= SHOWN_IN_SUMMARY else text[:SHOWN_IN_SUMMARY] + "…"
        if spoken:
            more = " Uzun olduğu için yalnızca başını okudum." if len(text) > MAX_SPOKEN else ""
            summary = f"{what} okudum ({len(text)} karakter).{more}"
        else:
            summary = f"{what} sesli okuyamadım ({reason.rstrip('.') or 'seslendirme yarıda kaldı'}). Metin: {shown}"
        if not restored:
            summary += " Panonun önceki içeriğinin bir kısmı geri yüklenemedi."
        log.info("Sesli okuma: %s, %d karakter, okundu=%s", args["source"], len(text), spoken)
        return ToolOutput(summary, {"text": text[:MAX_RETURNED], "chars": len(text), "spoken": spoken},
                          untrusted=True)

    def speak_in_language(self, args: dict) -> ToolOutput:
        text, lang = args["text"].strip(), args["language"]
        if not text:
            raise ToolError("boş", "Söylenecek bir metin yok.")
        bridge = self._bridge()
        if bridge is None or not bridge.can_speak:
            why = bridge.unavailable_reason if bridge is not None else "Ses motoru hazır değil."
            raise ToolError("ses yok", f"Sesli söyleyemiyorum: {why}")
        try:
            spoken = bridge.speak(text, lang, max_chars=len(text) + 1)
        except JarvisError as e:
            raise ToolError(f"ses: {e}", e.user_message) from e
        if not spoken:
            return ToolOutput("Seslendirme yarıda kesildi.", {"spoken": False})
        return ToolOutput(f"{LANGUAGES[lang]} olarak söyledim: {text}", {"spoken": True, "language": lang})


def register(reg: ToolRegistry, ctx: ToolContext, keyboard: KeyboardBackend | None = None,
             clipboard: ClipboardBackend | None = None, sleep: Callable[[float], None] = time.sleep) -> SpeechTools:
    t = SpeechTools(ctx, keyboard, clipboard, sleep)
    reg.register(ToolSpec(
        name="read_text_aloud", title="Metni sesli oku", category="ekran", platforms=WINDOWS_ONLY,
        description=("Öndeki penceredeki seçili metni (kopyalayıp panoyu geri yükleyerek) ya da panodaki metni sesli "
                     "okur. / Reads the selected text (copies it, restores the clipboard) or the clipboard aloud."),
        input_schema=js.obj(source=js.string("selection = seçili metin, clipboard = pano", enum=["selection",
                                                                                                 "clipboard"])),
        permission=Permission.SAFE, handler=t.read_text_aloud, timeout_s=150))
    reg.register(ToolSpec(
        name="speak_in_language", title="Başka dilde söyle", category="ekran",
        description=("Metni o dilin yerel sesiyle sesli söyler (Türkçe ses İngilizceyi kötü okur). Kullanıcı bir şeyi "
                     "İngilizce söylemeni/telaffuz etmeni isterse çeviriyi kendin yap, sonra bunu çağır; cevabında "
                     "metni yine yaz. / Speaks text aloud with a native voice for that language (English voice is "
                     "downloaded on first use)."),
        input_schema=js.obj(text=js.string("Söylenecek metin (o dilde) / text in that language", 600, 1),
                            language=js.string("Dil / language", enum=list(LANGUAGES))),
        permission=Permission.SAFE, handler=t.speak_in_language, timeout_s=300))
    return t
