"""Medya ve ses için yerel Türkçe komutlar (Claude'suz).

`intents.parse` bunu genel kurallardan ÖNCE çağırır: "şarkıyı geç" pencereye geçme, "Discord'da
mikrofonumu kapat" uygulama kapatma sanılmasın. Girdi `fold()` edilmiş metindir (küçük harf,
Türkçe harfler ASCII, "%" ve "-" boşluk). Emin olunmayan cümlede `None` döner.
"""

from __future__ import annotations

import re

from jarvis.tools.intents import Intent, _number_words
from jarvis.tools.text import fold, tr_lower

# ---------- oynat / duraklat / sonraki / önceki ----------
_MEDIA_OBJ = r"(?:muzigi|muzik|sarkiyi|sarki|videoyu|video|medyayi|parcayi|calani|spotifyi|spotify)"
_POLITE = r"(?:\s+(?:lutfen|misin|musun))?"
_PAUSE = re.compile(r"(?:" + _MEDIA_OBJ + r"\s+)?(?:durdur|duraklat|beklet)(?:[iu]r m[iu]s[iu]n|abilir misin|sana)?" + _POLITE
                    + r"|" + _MEDIA_OBJ + r"\s+(?:kapat|kapa|sustur)(?:ir misin)?" + r"|(?:muzige|sarkiya) ara ver")
_PLAY = re.compile(r"(?:(?:" + _MEDIA_OBJ + r"|kaldigi yerden)\s+)?(?:devam et(?:tir)?|devam ettir|oynat|"
                   r"kaldigi yerden (?:devam et|oynat|cal))(?:ir misin|sana)?" + _POLITE +
                   r"|(?:muzigi|muzik|sarkiyi|videoyu)\s+(?:ac|baslat|cal|calistir)(?:ir misin)?|muzik cal(?:sana)?")
_NEXT = re.compile(r"(?:bir\s+)?(?:sonraki|siradaki)(?:ne|ye)?(?:\s+(?:sarki|sarkiya|sarkiyi|parca|parcaya|video|videoya))?"
                   r"(?:\s+(?:gec|ac|cal|lutfen|gecer misin|gecsene))?" + _POLITE +
                   r"|(?:sarkiyi|parcayi|videoyu|bu sarkiyi|bunu)\s+(?:gec|atla|degistir)(?:ir misin|sene|sana)?" + _POLITE
                   + r"|(?:sarki|parca)\s+(?:gec|atla|degistir)|ileri gec")
_PREV = re.compile(r"(?:bir\s+)?onceki(?:ne|ye)?(?:\s+(?:sarki|sarkiya|sarkiyi|parca|parcaya|video|videoya))?"
                   r"(?:\s+(?:gec|ac|cal|don|lutfen|gecer misin|doner misin))?" + _POLITE +
                   r"|(?:sarkiyi|parcayi)\s+basa sar|geri gec")
_WHAT = re.compile(r"(?:su an\s+|simdi\s+)?(?:ne caliyor|hangi sarki caliyor|calan sarki ne|calan ne|"
                   r"bu (?:sarki|parca) ne|bu hangi sarki|bu sarkinin adi ne|bu kimin sarkisi|ne dinliyorum)(?:\s+su an)?")

# ---------- Spotify / YouTube ----------
_SPOTIFY = r"(?:spotify|spotifay|spotfy|spotifi)"
_YOUTUBE = r"(?:youtube|yutub|yutup|youtub|you tube)"
_LOC = r"(?:'?(?:da|de|ta|te|dan|den))?"
_PLAY_VERB = r"(cal|calsana|calar misin|calabilir misin|ac|acar misin|acsana|oynat|oynatir misin|dinlet|dinle|izle|" \
             r"izlet|ac lutfen|cal lutfen|baslat)"


def _span(cleaned: str, head: int, tail: int) -> str:
    words = tr_lower(cleaned).split()
    if head + tail >= len(words):
        return ""
    return " ".join(words[head: len(words) - tail]).strip(" '\"")


def _strip_obj_suffix(q: str) -> str:
    """"Tarkan'ı" -> "Tarkan" (son kelimedeki kesmeli ek)."""
    words = q.split()
    if words and "'" in words[-1]:
        words[-1] = words[-1].split("'", 1)[0]
    return " ".join(words).strip()


def _service(f: str, cleaned: str) -> Intent | None:
    for svc, tool, label in ((_SPOTIFY, "spotify_play", "Spotify"), (_YOUTUBE, "youtube_play", "YouTube")):
        m = re.fullmatch(svc + _LOC + r"\s+(.+?)\s+" + _PLAY_VERB, f)
        if m:
            head = len(f[: m.start(1)].split())
            q = _span(cleaned, head, len(m.group(2).split()))
            if q:
                return Intent(tool, {"query": q[:150]}, label)
        m = re.fullmatch(r"(.+?)\s+" + svc + _LOC + r"\s+" + _PLAY_VERB, f)
        if m:
            tail = len(f[m.end(1):].split())
            q = _strip_obj_suffix(_span(cleaned, 0, tail))
            if q:
                return Intent(tool, {"query": q[:150]}, label)
    return None


# ---------- ses çıkış aygıtı ----------
_DEVICE_WORD = r"(kulaklig\w*|kulaklik\w*|hoparlor\w*|headset\w*|speaker\w*)"
_SWITCH = r"(?:gec|gecir|gecis yap|al|ver|aktar|cevir|yonlendir|ayarla|yap)(?:ir misin|sene|sana)?"


def _device_name(word: str) -> str:
    return "kulaklık" if word.startswith(("kulakl", "headset")) else "hoparlör"


def _output(f: str, cleaned: str) -> Intent | None:
    m = re.fullmatch(r"(?:(?:sesi|ses cikisini|ses cikisi|cikisi|sesleri)\s+)?" + _DEVICE_WORD + r"\s+" + _SWITCH +
                     _POLITE, f)
    if m:
        return Intent("set_audio_output", {"device": _device_name(m.group(1))}, "Ses çıkışı")
    # "sesi monitöre ver", "ses çıkışını Realtek yap" (ses düzeyi komutlarıyla karışmasın: sayı yok)
    m = re.fullmatch(r"(?:sesi|sesleri)\s+(.+?)\s+(?:ver|aktar|gecir|yonlendir|cevir)(?:ir misin|sene|sana)?" + _POLITE,
                     f) or re.fullmatch(r"ses cikisini\s+(.+?)\s+(?:yap|ayarla|olarak ayarla|sec|gecir)"
                                        r"(?:ir misin|sene|sana)?" + _POLITE, f)
    if m:
        target = m.group(1)
        if re.search(r"\d|\byuzde\b|\bbiraz\b|\btamamen\b|\b(?:kis|ac|kapat)\b", _number_words(target)):
            return None
        head = len(f[: m.start(1)].split())
        tail = len(f[m.end(1):].split())
        q = _strip_obj_suffix(_span(cleaned, head, tail))
        q = re.sub(r"(?<=[^\W\d_])(?:'?(?:ye|ya|e|a))$", "", q) if "'" not in q else q
        return Intent("set_audio_output", {"device": q[:100]}, "Ses çıkışı") if q else None
    if re.fullmatch(r"(?:ses\s+(?:aygitlarini|aygitlari|cihazlarini|cihazlari|cikislarini|cikislari)|"
                    r"hangi ses (?:aygitlari|cihazlari|cikislari) var|uygulama seslerini)"
                    r"(?:\s+(?:listele|goster|say|neler))?", f):
        return Intent("list_audio", {}, "Ses aygıtları")
    return None


# ---------- uygulama sesi ----------
_NOT_APP = {"sistem", "sistemin", "bilgisayar", "bilgisayarin", "pc", "pcnin", "windows", "windowsun", "genel",
            "ana", "hoparlor", "hoparlorun", "kulaklik", "kulakligin", "benim", "senin", "bunun", "onun", "muzik",
            "muzigin", "sarkinin", "videonun"}


def _app(name: str) -> str | None:
    n = name.split("'", 1)[0].strip()
    if not n or fold(n) in _NOT_APP or len(n) > 40 or len(n.split()) > 3:
        return None
    return n


def _app_volume(f: str) -> Intent | None:
    if " sesini " not in f and not re.search(r"\s(?:sessize al|sustur)", f):
        return None
    g = _number_words(f)
    m = re.fullmatch(r"(.+?)\s+sesini\s+(?:yuzde\s*)?(\d{1,3})\s*(?:'?(?:e|a|ye|ya))?\s*"
                     r"(?:yap|ayarla|getir|olsun|cek|dusur|indir|yukselt|al|olarak ayarla)?" + _POLITE, g)
    if m:
        app, level = _app(m.group(1)), int(m.group(2))
        return Intent("set_app_volume", {"app": app, "mode": "set", "value": level}, "Uygulama sesi") \
            if app and level <= 100 else None
    m = re.fullmatch(r"(.+?)\s+sesini\s+(?:biraz\s+)?(?:(\d{1,3})\s+)?(?:biraz\s+)?"
                     r"(kis|azalt|dusur|artir|arttir|yukselt)(?:ir misin|sana)?" + _POLITE, g)
    if m:
        app = _app(m.group(1))
        step = int(m.group(2)) if m.group(2) else 20
        sign = -1 if m.group(3) in ("kis", "azalt", "dusur") else 1
        return Intent("set_app_volume", {"app": app, "mode": "change", "value": sign * step}, "Uygulama sesi") \
            if app and 0 < step <= 100 else None
    m = re.fullmatch(r"(.+?)\s+(?:sesini\s+(?:kapat|kapa|sustur|tamamen kis|sessize al)|sessize al|sustur)"
                     r"(?:ir misin|sana)?" + _POLITE, f)
    if m:
        app = _app(m.group(1))
        return Intent("set_app_volume", {"app": app, "mode": "mute", "value": 0}, "Uygulama sesi") if app else None
    m = re.fullmatch(r"(.+?)\s+sesini\s+(?:ac|geri ac)(?:ar misin|sana)?" + _POLITE, f)
    if m:
        app = _app(m.group(1))
        return Intent("set_app_volume", {"app": app, "mode": "unmute", "value": 0}, "Uygulama sesi") if app else None
    return None


# ---------- Discord ----------
_DISCORD = r"(?:discord|diskort|diskord|discort)"


def _discord(f: str) -> Intent | None:
    if re.fullmatch(r"(?:" + _DISCORD + r"'?(?:da|ta|de|te|u|un)?\s+)?(?:beni\s+)?(?:sagirlastir|sagir yap|sagirlas|"
                    r"sagirlastirmayi (?:kaldir|ac|kapat))(?:ir misin|sana)?" + _POLITE, f):
        return Intent("discord_voice", {"action": "deafen"}, "Discord sağırlaştır")
    if re.fullmatch(_DISCORD + r"'?(?:da|ta|de|te)?\s+(?:kulakligi\w*|hoparloru|herkesi|gelen sesi)\s+(?:kapat|kapa|"
                    r"ac|sustur)(?:ir misin|sana)?" + _POLITE, f):
        return Intent("discord_voice", {"action": "deafen"}, "Discord sağırlaştır")
    mic = (_DISCORD + r"'?(?:da|ta|de|te)?\s+(?:mikrofon\w*|mik\w*|sesimi|beni)\s+(?:kapat|kapa|ac|sustur|sessize al|"
           r"kis)(?:ir misin|sana)?" + _POLITE,
           r"(?:mikrofon\w*)\s+" + _DISCORD + r"'?(?:da|ta|de|te)\s+(?:kapat|kapa|ac|sustur)" + _POLITE,
           _DISCORD + r"'?(?:da|ta|de|te)\s+(?:sustur|sessize al|mute)(?:\s+beni)?" + _POLITE)
    if any(re.fullmatch(p, f) for p in mic):
        return Intent("discord_voice", {"action": "mute"}, "Discord mikrofon")
    return None


def parse_media(f: str, cleaned: str) -> Intent | None:
    """Katlanmış metin `f` ve temizlenmiş özgün metin `cleaned` ile medya komutları."""
    for rule in (_discord, _app_volume):
        hit = rule(f)
        if hit:
            return hit
    hit = _service(f, cleaned) or _output(f, cleaned)
    if hit:
        return hit
    if _WHAT.fullmatch(f):
        return Intent("now_playing", {}, "Ne çalıyor")
    if _PAUSE.fullmatch(f):
        return Intent("media_control", {"action": "pause"}, "Medya duraklatma")
    if _NEXT.fullmatch(f):
        return Intent("media_control", {"action": "next"}, "Sonraki parça")
    if _PREV.fullmatch(f):
        return Intent("media_control", {"action": "previous"}, "Önceki parça")
    if _PLAY.fullmatch(f):
        return Intent("media_control", {"action": "play"}, "Medya oynatma")
    return None
