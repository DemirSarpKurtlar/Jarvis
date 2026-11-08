"""Türkçe metin yardımcıları: büyük/küçük harf, aksan katlama, ek ayıklama.

Uygulama adı eşleştirme ve yerel komut ayrıştırma aynı kuralları kullanır.
"""

from __future__ import annotations

import re
import unicodedata

_FOLD = str.maketrans({"ı": "i", "ş": "s", "ğ": "g", "ü": "u", "ö": "o", "ç": "c", "â": "a", "î": "i", "û": "u"})
_OTHER_APOSTROPHES = "’`´‘"
_VOWELS = "aeıioöuü"


def tr_lower(text: str) -> str:
    """Türkçe kurallarıyla küçük harf: I -> ı, İ -> i."""
    return text.replace("I", "ı").replace("İ", "i").lower()


def fold(text: str) -> str:
    """Karşılaştırma anahtarı: küçük harf, Türkçe karakterler ASCII'ye, fazla boşluk yok."""
    t = tr_lower(normalize_apostrophes(text)).translate(_FOLD)
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[_\-\.]+", " ", t)
    t = re.sub(r"[^\w\s\+#']", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def fold_chars(text: str) -> str:
    """Yalnızca harfleri katlar (küçük harf, Türkçe -> ASCII); noktalama ve joker karakterler kalır."""
    return tr_lower(text).translate(_FOLD)


def normalize_apostrophes(text: str) -> str:
    for a in _OTHER_APOSTROPHES:
        text = text.replace(a, "'")
    return text


def strip_apostrophe_suffix(word: str) -> str:
    """"Notepad'i" -> "Notepad", "Chrome'u" -> "Chrome"."""
    word = normalize_apostrophes(word)
    return word.split("'", 1)[0] if "'" in word else word


def suffix_variants(phrase: str) -> list[str]:
    """Kesme işaretsiz yazılmış belirtme/yönelme eklerini ayıklayan aday listesi.

    "not defterini" -> ["not defterini", "not defteri", ...], "discordu" -> ["discordu", "discord"],
    "spotifyı" -> ["spotifyı", "spotify"]. Hangisinin doğru olduğuna çağıran (eşleştirme) karar verir.
    """
    phrase = normalize_apostrophes(phrase).strip()
    if "'" in phrase:
        return [strip_apostrophe_suffix(phrase).strip()]
    out = [phrase]
    low = tr_lower(phrase)
    # -nı/-ni/-nu/-nü (tamlama sonrası belirtme), -yı/-yi (ünlüden sonra), -na/-ne/-ya/-ye (yönelme)
    for suf in ("nı", "ni", "nu", "nü", "yı", "yi", "yu", "yü", "na", "ne", "ya", "ye"):
        if low.endswith(suf) and len(low) > len(suf) + 1:
            stem = phrase[: -len(suf)]
            if suf[0] == "n" and tr_lower(stem)[-1:] not in _VOWELS:
                continue
            out.append(stem)
    # -ı/-i/-u/-ü/-a/-e (ünsüzden sonra)
    for suf in ("ı", "i", "u", "ü", "a", "e"):
        if low.endswith(suf) and len(low) > 3 and tr_lower(phrase[:-1])[-1:] not in _VOWELS:
            out.append(phrase[:-1])
    seen: list[str] = []
    for c in out:
        c = c.strip()
        if c and c not in seen:
            seen.append(c)
    return seen
