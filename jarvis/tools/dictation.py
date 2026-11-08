"""Dikte modu: söylenenleri odaktaki pencereye yazar.

"dikte modu" / "yazmaya başla" ile açılır, "dikte bitti" / "yazmayı bitir" ile kapanır. Açıkken
ses motoru tanıdığı metni Claude'a göndermez; `DictationSession.handle` ile biçimleyip
`KeyboardBackend.type_text` (SendInput, KEYEVENTF_UNICODE) ile yazar.

Onay: başka bir programa tuş göndermek onay ister. Onay, dikteyi başlatmaktır; her cümle için ayrıca
sorulmaz:
* Kullanıcı "dikte modu"nu **sesle** söylerse bu, açık onaydır: VoiceManager aracı onay penceresi
  açmadan çalıştırır (kaynak "ses", denetim kaydına yazılır).
* Yazılı komutla ya da Claude başlatmak isterse (`action="start"`) onay penceresi açılır (CONFIRM);
  kullanıcı onaylamadan dikte başlamaz. Durdurmak (`stop`) her zaman serbesttir.

Noktalama sözcükleri: "virgül", "nokta", "soru işareti", "ünlem", "iki nokta", "noktalı virgül",
"yeni satır", "yeni paragraf", "tırnak aç/kapat", "parantez aç/kapat", "tire".
Konuşma tanımanın her sözün sonuna kendiliğinden koyduğu nokta atılır (nokta için "nokta" denir);
soru/ünlem işaretleri ve cümle içi virgüller korunur.
"""

from __future__ import annotations

import logging
import re

from jarvis.security.permission_manager import ConfirmPrompt, Permission
from jarvis.tools import schema as js
from jarvis.tools.registry import WINDOWS_ONLY, ToolContext, ToolError, ToolOutput, ToolRegistry, ToolSpec
from jarvis.tools.text import fold
from jarvis.tools.win_input import KeyboardBackend, default_keyboard

log = logging.getLogger(__name__)

MAX_UTTERANCE_CHARS = 2000

# (katlanmış sözcükler, yazılacak işaret). Uzun ifadeler önce denenir.
PUNCTUATION: list[tuple[tuple[str, ...], str]] = sorted([
    (("iki", "nokta", "ust", "uste"), ":"), (("iki", "nokta"), ":"), (("noktali", "virgul"), ";"),
    (("soru", "isareti"), "?"), (("unlem", "isareti"), "!"), (("unlem",), "!"), (("uc", "nokta"), "…"),
    (("virgul",), ","), (("nokta",), "."),
    (("yeni", "satir"), "\n"), (("alt", "satir"), "\n"), (("satir", "basi"), "\n"), (("yeni", "paragraf"), "\n\n"),
    (("tirnak", "ac"), "«"), (("tirnak", "kapat"), "»"), (("parantez", "ac"), "("), (("parantez", "kapat"), ")"),
    (("tire",), "-"),
], key=lambda p: -len(p[0]))
_CLOSING = set(",.;:?!…)»")
_OPENING = set("(«")
_SENTENCE_END = set(".?!…\n")

_STOP = re.compile(r"(?:(?:hey )?jarvis )?(?:dikte(?:yi)? (?:bitti|bitir|kapat|durdur|sonlandir)|"
                   r"dikte modu(?:nu)? (?:bitti|bitir|kapat|durdur)|dikte modundan cik|"
                   r"yazma(?:yi)? (?:bitir|bitti|durdur|kes|birak)|yazmayi sonlandir)(?: lutfen)?")


def is_stop_command(text: str) -> bool:
    return bool(_STOP.fullmatch(fold(text).strip()))


def _cap(word: str) -> str:
    """Türkçe büyük harf: i -> İ, ı -> I."""
    if not word:
        return word
    first = {"i": "İ", "ı": "I"}.get(word[0], word[0].upper())
    return first + word[1:]


def format_dictation(text: str, tail: str = "") -> str:
    """Tanınan metni yazılacak hâle getirir. `tail`: daha önce yazılanın sonu (boşluk/büyük harf kararı)."""
    t = re.sub(r"\s+", " ", text).strip()
    t = re.sub(r"\.+$", "", t).strip()       # konuşma tanımanın otomatik son noktası
    tokens = t.split(" ") if t else []
    out = tail
    start = len(tail)
    quote_open = False
    i = 0
    while i < len(tokens):
        # noktalama sözcüğü mü? (ekli noktalama: "virgül," -> "virgul")
        folded = [fold(x).strip(".,!?;:…") for x in tokens[i:i + 4]]
        hit = None
        for words, sym in PUNCTUATION:
            if tuple(folded[:len(words)]) == words:
                hit = (len(words), sym)
                break
        if hit is not None:
            n, sym = hit
            i += n
            if sym in ("«", "»"):
                sym = '"'
                closing = quote_open
                quote_open = not quote_open
                if closing:
                    out = out.rstrip(" ") + sym
                else:
                    out += ("" if not out or out[-1] in " \n(" else " ") + sym
                continue
            if sym in _OPENING:
                out += ("" if not out or out[-1] in " \n(\"" else " ") + sym
            elif sym.startswith("\n"):
                out = out.rstrip(" ") + sym
            elif out.rstrip(" ")[-1:] == sym:
                continue                        # "Merhaba, virgül" -> tek virgül
            else:
                out = out.rstrip(" ") + sym
            continue
        word = tokens[i]
        i += 1
        prev = out.rstrip(" \"(")
        if not prev or prev[-1] in _SENTENCE_END:
            word = _cap(word)
        if out and out[-1] not in " \n(" and not (out[-1] == '"' and quote_open):
            out += " "
        out += word
    return out[start:]


class DictationSession:
    """Bir dikte oturumu: biçimleme + yazma. Ses motoru `handle()`'ı arka plan thread'inde çağırır."""

    def __init__(self, keyboard: KeyboardBackend) -> None:
        self.keyboard = keyboard
        self.tail = ""
        self.typed_chars = 0
        self.utterances = 0

    def handle(self, text: str) -> str:
        """"stop" (bitirme komutu), "typed" ya da "empty" döndürür. Yazamazsa ToolError."""
        if is_stop_command(text):
            return "stop"
        piece = format_dictation(text[:MAX_UTTERANCE_CHARS], self.tail)
        if not piece.strip():
            return "empty"
        self.keyboard.type_text(piece)
        self.tail = (self.tail + piece)[-200:]
        self.typed_chars += len(piece)
        self.utterances += 1
        # Gizlilik: metnin kendisi loglanmaz
        log.info("Dikte: %d karakter yazıldı", len(piece))
        return "typed"


class DictationTools:
    def __init__(self, ctx: ToolContext, keyboard: KeyboardBackend | None = None) -> None:
        self.ctx = ctx
        self._keyboard = keyboard

    @property
    def keyboard(self) -> KeyboardBackend:
        if self._keyboard is None:
            self._keyboard = default_keyboard()
        return self._keyboard

    def _bridge(self):  # noqa: ANN202
        bridge = self.ctx.extra.get("voice")
        if bridge is None:
            raise ToolError("ses köprüsü yok", "Dikte için ses motorunun çalışıyor olması gerekiyor.")
        return bridge

    def dictation(self, args: dict) -> ToolOutput:
        bridge = self._bridge()
        if args["action"] == "stop":
            if bridge.stop_dictation():
                return ToolOutput("Dikte modu kapandı.", {"dictation": False})
            return ToolOutput("Dikte modu zaten kapalı.", {"dictation": False})
        if bridge.dictation_active:
            return ToolOutput("Dikte modu zaten açık. Bitirmek için 'dikte bitti' de.", {"dictation": True})
        session = DictationSession(self.keyboard)
        bridge.start_dictation(session)
        return ToolOutput("Dikte modu açık. Söylediklerini odaktaki pencereye yazıyorum. Bitirmek için "
                          "'dikte bitti' de.", {"dictation": True})


def register(reg: ToolRegistry, ctx: ToolContext, keyboard: KeyboardBackend | None = None) -> DictationTools:
    t = DictationTools(ctx, keyboard)
    reg.register(ToolSpec(
        name="dictation", title="Dikte modu", category="ekran", platforms=WINDOWS_ONLY,
        description=("Dikte modunu açar/kapatır: açıkken kullanıcının söyledikleri odaktaki pencereye yazılır. "
                     "Yalnızca kullanıcı açıkça isterse başlat; başlatma onay ister. / Starts or stops dictation into "
                     "the focused window (start requires user confirmation)."),
        input_schema=js.obj(action=js.string("start | stop", enum=["start", "stop"])),
        permission=Permission.SAFE, handler=t.dictation,
        escalate=lambda a: Permission.CONFIRM if a.get("action") == "start" else None,
        confirm=lambda a: ConfirmPrompt(
            "dictation", "Dikte modu",
            "Dikte modu açılsın mı? Açıkken söylediklerin klavyeden yazılmış gibi odaktaki pencereye yazılır "
            "(Claude'a gönderilmez). Bitirmek için \"dikte bitti\" de.", confirm_label="Dikteyi başlat")))
    return t


