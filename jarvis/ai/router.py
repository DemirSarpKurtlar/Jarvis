"""Model yönlendirici.

Her isteği en ucuz yeterli modele gönderir. Kararlar yerel ve anlıktır
(yönlendirme için ayrıca API çağrısı yapılmaz, bedavadır). Sırayla:

1. Bütçe aşıldıysa kullanıcının politikası uygulanır (Haiku'ya düş / durdur / sor).
2. Kullanıcı ayarlardan sabit bir model seçtiyse o kullanılır.
3. Açık güçlü model talebi ("derin analiz", "Opus kullan" ...) -> Opus 5.5
4. Güncel bilgi / araştırma gerektiren istek -> Sonnet 5.5 + web araması
5. Karmaşık istek (açıklama, analiz, kod, sorun giderme, uzun metin) -> Sonnet 5.5
6. Geri kalan her şey -> Haiku 5.5

Başarısız bir istek otomatik olarak pahalı modele yükseltilmez.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from jarvis.ai.model_registry import Tier
from jarvis.ai.usage import BudgetStatus
from jarvis.config.settings import Settings


def tr_lower(text: str) -> str:
    """Türkçe'ye uygun küçük harf: I->ı, İ->i."""
    return text.replace("I", "ı").replace("İ", "i").lower()


ADVANCED_TRIGGERS = (
    "derin analiz", "derinlemesine", "çok detaylı analiz", "kapsamlı analiz", "en güçlü model",
    "opus kullan", "opus ile", "opus'la", "en iyi modelle", "çok dikkatli düşün",
)
RESEARCH_TRIGGERS = (
    "araştır", "internette", "internetten", "webde", "web'de", "google", "güncel", "son dakika",
    "haber", "bugün", "dün", "bu hafta", "şu an", "şu anda", "fiyat", "kaç para", "kaç tl", "döviz",
    "dolar kuru", "euro kuru", "borsa", "hava durumu", "maç", "skor", "kim kazandı", "en son",
    "en yeni", "yeni çıkan", "kaynak göster", "kaynaklarıyla", "sürümü ne", "son sürüm",
)
COMPLEX_TRIGGERS = (
    "neden", "niçin", "nasıl çalışır", "açıkla", "karşılaştır", "farkı ne", "analiz", "değerlendir",
    "plan", "adım adım", "strateji", "öner", "tavsiye", "hata", "sorun", "çalışmıyor", "bozuldu",
    "çöktü", "kod", "python", "program", "script", "fonksiyon", "algoritma", "hesapla", "özetle",
    "yaz bana", "makale", "mail yaz", "e-posta yaz", "çevir", "matematik", "kanıtla",
)
# Bilgisayarda çok adımlı iş isteyen ifadeler: araç planlaması için dengeli model (Sonnet)
COMPUTER_TRIGGERS = (
    "klasör", "dosya", "taşı", "düzenle", "topla", "sil", "yedekle", "indirilenler", "masaüst",
    "belgeler", "işlem", "program", "uygulama", "kapat", "hatırlat", "zamanla", "dakika sonra",
    "saat sonra", "pdf", "word", "belge oluştur", "not oluştur",
)
_YEAR_RE = re.compile(r"\b20[2-9]\d\b")


@dataclass(frozen=True)
class Route:
    tier: Tier
    reason: str
    web_search: bool = False
    needs_confirmation: bool = False   # bütçe "sor" politikası
    blocked: bool = False              # bütçe "durdur" politikası
    budget_note: str | None = None     # kullanıcıya gösterilecek bütçe uyarısı


def _has(text: str, words: tuple[str, ...]) -> bool:
    return any(w in text for w in words)


def classify(text: str) -> tuple[Tier, bool, str]:
    t = tr_lower(text)
    if _has(t, ADVANCED_TRIGGERS):
        return "advanced", _has(t, RESEARCH_TRIGGERS), "güçlü model istendi"
    if _has(t, RESEARCH_TRIGGERS) or _YEAR_RE.search(t):
        return "main", True, "güncel bilgi gerekiyor"
    if _has(t, COMPUTER_TRIGGERS):
        return "main", False, "bilgisayarda işlem"
    if _has(t, COMPLEX_TRIGGERS) or len(t) > 280:
        return "main", False, "karmaşık istek"
    return "light", False, "basit istek"


def route(text: str, settings: Settings, budget: BudgetStatus) -> Route:
    tier, research, reason = classify(text)
    web = research and settings.ai.web_search_enabled

    pref = settings.ai.preferred_model
    if pref != "auto":
        tier, reason = pref, "elle seçilen model"  # type: ignore[assignment]

    level = budget.level
    if level == "exceeded":
        which = budget.exceeded_which
        policy = settings.budget.on_limit
        if policy == "block":
            return Route(tier, reason, blocked=True,
                         budget_note=f"{which.capitalize()} Claude bütçesi doldu, istek gönderilmedi.")
        if policy == "ask":
            return Route(tier, reason, web, needs_confirmation=True,
                         budget_note=f"{which.capitalize()} Claude bütçesi doldu. Yine de gönderilsin mi?")
        # fallback_light: en ucuz modele düş, ücretli web aramasını kapat
        return Route("light", "bütçe doldu, ekonomik model", False,
                     budget_note=f"{which.capitalize()} bütçe doldu, Haiku 5.5 kullanılıyor ve web araması kapalı.")
    note = None
    if level == "warn":
        note = "Claude bütçesinin büyük kısmı kullanıldı."
    return Route(tier, reason, web, budget_note=note)
