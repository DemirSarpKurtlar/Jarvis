"""Konuşma tanımaya ipucu: Türkçe komut örnekleri ve bu bilgisayardaki uygulama/oyun adları.

Whisper, çözümlemeye başlamadan önce kendisine verilen metni "az önce söylenenler" gibi okur
(`initial_prompt`). Bu metin hem dili (Türkçe, komut üslubu) hem de yabancı özel adların
yazılışını ("Rocket League", "Discord") modele hatırlatır. Örnek: ipucu olmadan "Not defterini
aç" -> "Not after night." gibi İngilizce uydurmalar çıkabiliyor.

Sınırlar:
* faster-whisper ipucunun en fazla `max_length // 2 - 1` = 223 belirtecini kullanır (fazlası
  baştan kesilir). Uzun ipucu işlemcide çözümlemeyi de yavaşlatır; bu yüzden bütçe varsayılan
  olarak bunun epey altındadır ve adlar öncelik sırasıyla, bütçe dolana kadar eklenir.
* Bu modül Qt ya da araç paketini içe aktarmaz: ad listesi dışarıdan bir çağrılabilirle gelir
  (`Assistant` uygulama dizinini bağlar).
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable, Iterable, Sequence

log = logging.getLogger(__name__)

# Tipik JARVIS komutları: Türkçe ekler, kesme işaretleri ve sık geçen uygulama adları
BASE_PHRASES: tuple[str, ...] = (
    "Jarvis, Not Defteri'ni aç.",
    "Hesap makinesini aç.",
    "Sesi yüzde 30 yap.",
    "Chrome'u kapat.",
    "Discord'u aç.",
    "Spotify'ı kapat.",
)
VOCAB_INTRO = "Bilgisayardaki uygulamalar ve oyunlar:"

# Whisper'ın kullandığı üst sınır 223 belirteç; işlemci hızı ve uydurma riski için daha azı yeter
DEFAULT_MAX_TOKENS = 150
MAX_NAME_CHARS = 40
MAX_NAMES = 60

VocabularyFn = Callable[[], Sequence[str]]
TokenCounter = Callable[[str], int]

_TRADEMARKS = re.compile(r"[™®©]")


def estimate_tokens(text: str) -> int:
    """Belirteç sayısı tahmini (tokenizer yokken). Türkçe/özel adlarda ~2,5 karakter/belirteç: temkinli."""
    return math.ceil(len(text.encode("utf-8")) / 2.5) if text else 0


def clean_name(name: str) -> str:
    """Ad listesine girecek biçim: ™/® yok, tek boşluk, virgül/nokta ayıracı karıştırmayacak."""
    n = _TRADEMARKS.sub("", name)
    n = re.sub(r"\s+", " ", n.replace(",", " ")).strip(" .;:-")
    return n


def build_prompt(vocabulary: Iterable[str] = (), max_tokens: int = DEFAULT_MAX_TOKENS,
                 count_tokens: TokenCounter | None = None, phrases: Sequence[str] = BASE_PHRASES) -> str:
    """Whisper `initial_prompt` metni.

    Biçim: "Bilgisayardaki uygulamalar ve oyunlar: Rocket League, Discord. Jarvis, Not Defteri'ni aç. ..."
    Komut cümleleri sonda durur (modele en yakın bağlam). Adlar verilen sırayla, bütçe dolana kadar
    eklenir; tekrarlar (büyük/küçük harf farkı dahil) atlanır.
    """
    count = count_tokens or estimate_tokens
    tail = " ".join(phrases)
    budget = max_tokens - count(" " + tail)
    names: list[str] = []
    seen: set[str] = set()
    used = count(VOCAB_INTRO + ".")
    for raw in vocabulary:
        if len(names) >= MAX_NAMES:
            break
        name = clean_name(str(raw))
        key = name.casefold()
        if not name or len(name) > MAX_NAME_CHARS or key in seen:
            continue
        cost = count((", " if names else " ") + name)
        if used + cost > budget:
            continue    # sonraki daha kısa bir ad hâlâ sığabilir
        seen.add(key)
        names.append(name)
        used += cost
    if not names:
        return tail
    return f"{VOCAB_INTRO} {', '.join(names)}. {tail}"


def _fold_words(text: str) -> list[str]:
    return re.findall(r"\w+", text.casefold())


def is_prompt_echo(text: str, prompt: str, min_words: int = 6) -> bool:
    """Whisper bazen sessizlikte/gürültüde ipucunu aynen tekrarlar. Uzun (>= min_words kelime) ve
    ipucunda birebir geçen bir çıktı ya da ad listesinin kendisi yankı sayılır. Kısa komutlar
    ("Not Defteri'ni aç") ipucunda geçse de geçerlidir."""
    words = _fold_words(text)
    if not words or not prompt:
        return False
    p = _fold_words(prompt)
    if " ".join(_fold_words(VOCAB_INTRO)) in " ".join(words):
        return True
    if len(words) < min_words:
        return False
    joined, pj = " ".join(words), " " + " ".join(p) + " "
    return f" {joined} " in pj


class PromptCache:
    """Ad listesi değişmedikçe ipucunu yeniden kurmaz. Ad kaynağı hata verirse temel cümlelerle devam eder."""

    def __init__(self, max_tokens: int = DEFAULT_MAX_TOKENS) -> None:
        self.max_tokens = max_tokens
        self._key: tuple[str, ...] | None = None
        self._prompt = ""

    def get(self, vocabulary: VocabularyFn | None, count_tokens: TokenCounter | None = None) -> str:
        names: tuple[str, ...] = ()
        if vocabulary is not None:
            try:
                names = tuple(str(n) for n in vocabulary())
            except Exception:  # noqa: BLE001 - ipucu yalnızca yardımcı; tanımayı asla bozmasın
                log.warning("Konuşma tanıma ipucu için uygulama adları alınamadı", exc_info=True)
        if names != self._key or not self._prompt:
            self._prompt = build_prompt(names, self.max_tokens, count_tokens)
            self._key = names
            log.debug("Konuşma tanıma ipucu güncellendi: %d ad, %d karakter", len(names), len(self._prompt))
        return self._prompt
