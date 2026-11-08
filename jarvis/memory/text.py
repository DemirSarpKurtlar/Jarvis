"""Hafıza için Türkçe metin araçları: sadeleştirme, benzerlik, gizli ve hassas bilgi tespiti.

Hepsi yereldir ve ücretsizdir. Sadeleştirme hem arama dizininde hem de sorguda
aynı şekilde uygulanır; böylece "IŞIK", "ışık" ve "isik" aynı kelime sayılır.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from jarvis.ai.router import tr_lower

_FOLD = str.maketrans({"ç": "c", "ğ": "g", "ı": "i", "ö": "o", "ş": "s", "ü": "u",
                       "â": "a", "î": "i", "û": "u", "é": "e"})
_NON_WORD = re.compile(r"[^a-z0-9]+")

STEM_LEN = 5

# Aramada ve tekrar tespitinde anlam taşımayan sık kelimeler (sadeleştirilmiş halleriyle)
STOPWORDS = frozenset("""
ve veya ya da de ki mi mu bir bu su o ben sen biz siz onlar beni seni bana sana benim senin
icin ile gibi kadar daha cok az en ne neden nasil hangi kim kac ama fakat ancak her hep sey
seyi bunu sunu onu simdi sonra once olan olarak oldu olur var yok mi mı acaba lutfen tamam
evet hayir merhaba selam kullanici kullanicinin kullaniciya kullanicin jarvis
""".split())


TR_MONTHS = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim",
             "Kasım", "Aralık"]


def tr_date(dt, year: bool = False) -> str:  # noqa: ANN001
    """Yerel saatle "15 Kasım" (ya da "15 Kasım 2026")."""
    d = dt.astimezone()
    return f"{d.day} {TR_MONTHS[d.month - 1]}" + (f" {d.year}" if year else "")


def fold(text: str) -> str:
    """Türkçe küçük harf + aksan/şapka kaldırma."""
    return tr_lower(text).translate(_FOLD)


def normalize(text: str) -> str:
    """Arama ve karşılaştırma için: küçük harf, aksansız, noktalama yok, tek boşluk."""
    return _NON_WORD.sub(" ", fold(text)).strip()


def tokens(text: str) -> list[str]:
    return [t for t in normalize(text).split() if t]


def content_tokens(text: str) -> list[str]:
    """Anlamlı kelimeler (sık kelimeler ve tek harfler hariç)."""
    return [t for t in tokens(text) if t not in STOPWORDS and (len(t) > 1 or t.isdigit())]


def stem(token: str) -> str:
    """Kaba Türkçe kök: ekler çoğunlukla sondadır, ilk 5 harf yeterli bir yaklaşım."""
    return token[:STEM_LEN]


def stems(text: str) -> set[str]:
    return {stem(t) for t in content_tokens(text)}


def similarity(a: str, b: str) -> float:
    """0..1 arası benzerlik: kök kümesi örtüşmesi ile karakter dizisi benzerliğinin büyüğü.

    Karakter benzerliği tek başına yanıltabilir ("annesinin doğum günü" / "babasının doğum günü"),
    bu yüzden ona daha yüksek bir eşik uygulanır (bkz. `is_near_duplicate`).
    """
    sa, sb = stems(a), stems(b)
    jac = len(sa & sb) / len(sa | sb) if sa and sb else 0.0
    seq = SequenceMatcher(None, normalize(a), normalize(b)).ratio()
    return max(jac, seq)


def is_near_duplicate(a: str, b: str) -> bool:
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    sa, sb = stems(a), stems(b)
    jac = len(sa & sb) / len(sa | sb) if sa and sb else 0.0
    seq = SequenceMatcher(None, na, nb).ratio()
    return jac >= 0.75 or seq >= 0.92


# ---------------- gizli bilgi (asla kaydedilmez) ----------------

_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\."),
    # harf ve rakam karışık 32+ karakterlik dizi (anahtar, belirteç)
    re.compile(r"\b(?=[A-Za-z0-9_\-]*\d)(?=[A-Za-z0-9_\-]*[A-Za-z])[A-Za-z0-9_\-]{32,}\b"),
]
# "şifre", "parola", "password" vb. ve ardından değer gibi görünen bir şey
_SECRET_WORDS = (r"sifre\w*|parola\w*|password\w*|passwd|pass|pin|pin kodu\w*|cvv|cvc|api anahtar\w*|"
                 r"api key\w*|apikey|token\w*|dogrulama kod\w*|onay kod\w*|sms kod\w*|guvenlik kod\w*|"
                 r"gizli anahtar\w*|secret\w*|kurtarma kod\w*|recovery")
_SECRET_TOPIC = re.compile(rf"\b({_SECRET_WORDS})\b")
_SECRET_WITH_VALUE = re.compile(
    rf"\b(?:{_SECRET_WORDS})\b\s*(?:[:=]|\bsu\b|\bboyle\b|\bdir\b|\bdur\b)?\s*(\S+)")
_CARD = re.compile(r"(?<!\d)(?:\d[ \-]?){13,19}(?!\d)")


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def contains_secret(text: str) -> bool:
    """Metinde parola, API anahtarı, kart numarası gibi bir gizli değer var mı?"""
    if any(p.search(text) for p in _SECRET_PATTERNS):
        return True
    for m in _CARD.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return True
    raw_folded = fold(text)
    for m in _SECRET_WITH_VALUE.finditer(raw_folded):
        value = m.group(1)
        if any(c.isdigit() for c in value) or re.search(r"[^\w\s]", value[:-1] if len(value) > 1 else ""):
            return True
    return False


def mentions_secret_topic(text: str) -> bool:
    """Parola/anahtar konusunu anıyor mu? (Hafıza kaydı için: bunlarla ilgili hiçbir şey saklanmaz.)"""
    return bool(_SECRET_TOPIC.search(normalize(text)))


def is_secret_memory(text: str) -> bool:
    return contains_secret(text) or mentions_secret_topic(text)


# ---------------- hassas bilgi (kaydetmeden önce kullanıcıya sorulur) ----------------

SENSITIVE_KINDS: dict[str, str] = {
    # tür -> "Bu ... ilgili bir bilgi" cümlesindeki hali
    "health": "sağlıkla",
    "finance": "parayla",
    "location": "konumunla",
    "identity": "kimlik bilgileriyle",
    "religion": "inançla",
    "politics": "siyasi görüşle",
    "sexuality": "özel yaşamla",
    "legal": "hukuki durumla",
    "other": "özel bir konuyla",
}

_SENSITIVE_LOCAL: list[tuple[str, re.Pattern]] = [
    ("health", re.compile(
        r"\b(hastal\w*|hastay\w*|ilac\w*|tanisi\w*|teshis\w*|kan grub\w*|diyabet\w*|seker hasta\w*|tansiyon\w*|"
        r"kanser\w*|depresyon\w*|anksiyete\w*|panik atak\w*|terapi\w*|psikiyatr\w*|psikolog\w*|alerji\w*|"
        r"ameliyat\w*|hamile\w*|gebe\w*|engelli\w*|kronik\w*|astim\w*|migren\w*|doktor\w*|hastane\w*|"
        r"recete\w*|tedavi\w*|bipolar\w*|adhd|dehb|otizm\w*|hiv|epilepsi\w*)\b")),
    ("finance", re.compile(
        r"\b(maas\w*|borc\w*|kredi\w*|banka\w*|iban\w*|hesap numara\w*|aylik gelir\w*|bakiye\w*|yatirim\w*|"
        r"birikim\w*|servet\w*|vergi\w*|icra\w*|haciz\w*|sigorta\w*|kira(?:m|mi|si|yi|ya|da)?)\b")),
    ("location", re.compile(
        r"\b(adres\w*|sokak\w*|sok|cadde\w*|cad|mahalle\w*|mah|apartman\w*|apt|daire\w*|posta kod\w*|"
        r"koordinat\w*|kapi no\w*|bina no\w*)\b")),
    ("identity", re.compile(
        r"\b(tc kimlik\w*|kimlik no\w*|kimlik numara\w*|tckn|pasaport\w*|ehliyet no\w*|"
        r"sosyal guvenlik\w*|dogum yeri\w*)\b|(?<!\d)\d{11}(?!\d)")),
    ("religion", re.compile(
        r"\b(dini|dinim\w*|dinin\w*|inanc\w*|namaz\w*|oruc\w*|ibadet\w*|musluman\w*|hristiyan\w*|yahudi\w*|"
        r"ateist\w*|agnostik\w*|deist\w*|alevi\w*|sunni\w*|mezhep\w*|cami\w*|kilise\w*)\b")),
    ("politics", re.compile(
        r"\b(siyasi\w*|siyaset\w*|partili\w*|milletvekil\w*|oy ver\w*|oyumu\w*|secimde\w*)\b")),
    ("sexuality", re.compile(
        r"\b(cinsel\w*|escinsel\w*|gay|lezbiyen\w*|biseksuel\w*|transseksuel\w*|trans birey\w*|lgbt\w*)\b")),
    ("legal", re.compile(
        r"\b(sabika\w*|mahkum\w*|dava\w*|ceza(?:si|m|mi|ya|lar\w*)?|hapis\w*|tutukl\w*|gozalti\w*)\b")),
]


def local_sensitive_kind(text: str) -> str | None:
    """Yerel anahtar kelimelerle hassas bilgi türünü tahmin eder (modelin kararına ek güvence)."""
    n = normalize(text)
    for kind, pat in _SENSITIVE_LOCAL:
        if pat.search(n):
            return kind
    return None
