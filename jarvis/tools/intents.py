"""Yerel Türkçe komut ayrıştırıcı.

Basit, tek adımlı komutları Claude'a gitmeden araç çağrısına çevirir:
"Jarvis, Notepad'i aç" -> open_app(name="notepad"). Emin olmadığında `None`
döner ve istek Claude'a gider. Kural: yanlış bir eylem yapmaktansa hiç
eşleşmemek daha iyidir (birden çok adımlı ya da açıklama isteyen cümleler
yerel olarak işlenmez).

Eşleştirme katlanmış metin üzerinde yapılır (küçük harf, ı->i, ş->s ...).
Kesme işaretli ve işaretsiz ekler desteklenir: "Chrome'u kapat", "discordu kapat",
"not defterini aç".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from jarvis.security.action_validator import FOLDER_ALIASES
from jarvis.tools.text import fold, normalize_apostrophes, strip_apostrophe_suffix, suffix_variants, tr_lower


@dataclass(frozen=True)
class Intent:
    tool: str
    args: dict = field(default_factory=dict)
    label: str = ""     # kısa Türkçe açıklama ("Uygulama açma")


# ---------- sözlükler ----------
SETTINGS_WORDS: dict[str, str] = {
    "bluetooth": "bluetooth", "blutut": "bluetooth",
    "wifi": "wifi", "wi fi": "wifi", "kablosuz": "wifi", "kablosuz ag": "wifi", "wlan": "wifi",
    "ag": "ag", "internet": "ag", "ag ve internet": "ag", "vpn": "vpn",
    "ses": "ses", "hoparlor": "ses", "ekran": "ekran", "goruntu": "ekran", "parlaklik": "ekran",
    "cozunurluk": "ekran", "gece isigi": "gece_isigi", "bildirim": "bildirimler", "bildirimler": "bildirimler",
    "odak": "odak", "rahatsiz etmeyin": "odak", "guc": "guc", "uyku": "guc", "guc ve uyku": "guc",
    "pil": "pil", "batarya": "pil", "depolama": "depolama", "uygulamalar": "uygulamalar",
    "varsayilan uygulamalar": "varsayilan_uygulamalar", "baslangic": "baslangic",
    "baslangic uygulamalari": "baslangic", "kisisellestirme": "kisisellestirme", "arka plan": "arka_plan",
    "duvar kagidi": "arka_plan", "renk": "renkler", "renkler": "renkler", "tema": "temalar", "temalar": "temalar",
    "kilit ekrani": "kilit_ekrani", "gorev cubugu": "gorev_cubugu", "fare": "fare", "mouse": "fare",
    "klavye": "klavye", "yazici": "yazicilar", "yazicilar": "yazicilar", "tarih": "tarih_saat", "saat": "tarih_saat",
    "tarih ve saat": "tarih_saat", "dil": "dil", "bolge": "dil", "hesap": "hesaplar", "hesaplar": "hesaplar",
    "oturum acma": "oturum_acma", "gizlilik": "gizlilik", "mikrofon": "mikrofon_izni", "kamera": "kamera_izni",
    "konum": "konum", "guncelleme": "guncelleme", "guncellemeler": "guncelleme", "windows update": "guncelleme",
    "windows guncelleme": "guncelleme", "guvenlik": "windows_guvenligi", "windows guvenligi": "windows_guvenligi",
    "virus": "windows_guvenligi", "oyun": "oyun", "erisilebilirlik": "erisilebilirlik", "hakkinda": "hakkinda",
    "ucak modu": "ucak_modu", "etkin nokta": "mobil_erisim_noktasi", "hotspot": "mobil_erisim_noktasi",
}

WEBSITES = {
    "youtube": "https://www.youtube.com", "google": "https://www.google.com", "gmail": "https://mail.google.com",
    "github": "https://github.com", "netflix": "https://www.netflix.com", "twitter": "https://x.com",
    "instagram": "https://www.instagram.com", "vikipedi": "https://tr.wikipedia.org",
    "wikipedia": "https://www.wikipedia.org", "linkedin": "https://www.linkedin.com", "reddit": "https://www.reddit.com",
    "haritalar": "https://www.google.com/maps", "google haritalar": "https://www.google.com/maps",
}

SEARCH_ENGINES = {"google": "google", "youtube": "youtube", "bing": "bing", "internet": "google", "web": "google",
                  "vikipedi": "vikipedi", "wikipedia": "vikipedi"}

EXTENSION_WORDS = {"pdf": "pdf", "word": "docx", "docx": "docx", "doc": "doc", "excel": "xlsx", "xlsx": "xlsx",
                   "powerpoint": "pptx", "pptx": "pptx", "sunum": "pptx", "metin": "txt", "txt": "txt",
                   "markdown": "md", "md": "md", "mp3": "mp3", "mp4": "mp4", "zip": "zip", "rar": "rar",
                   "png": "png", "jpg": "jpg", "jpeg": "jpg", "csv": "csv", "json": "json"}

# Bu adlar "X'i kapat/aç" kalıbında uygulama sayılmaz (başka kurallara aittir)
_NOT_APPS = {"ses", "sesi", "sesler", "sesleri", "bilgisayar", "bilgisayari", "pc", "pcyi", "ekran", "ekrani",
             "pencere", "pencereyi", "bunu", "bu", "su", "sunu", "onu", "hepsini", "her seyi", "isik", "isiklari",
             "kapi", "kapiyi", "mikrofon", "mikrofonu", "ayar", "ayarlar", "ayarlari"}

_OPEN = r"(?:ac|acar misin|acabilir misin|acar misiniz|acin|acsana|acar mısın|baslat|baslatir misin|calistir|" \
        r"calistirir misin)"
_CLOSE = r"(?:kapat|kapa|kapatir misin|kapatabilir misin|kapatin|kapatsana)"
_SEARCH = r"(?:ara|bul|arar misin|bulur musun|arat|aratir misin|ara bakalim)"
_EXPLAIN = re.compile(r"\b(nasil\s+\S|hizlandir|neden|niye|ne yapmali|ne yapmam|oner|tavsiye|anlat|acikla|olmali|normal mi|"
                      r"yuksek mi|dusuk mu|nedir|ne demek|nasil duzel|nasil artir|nasil dusur)\w*")
_MULTI = re.compile(r"(,|;|\bve\b|\bsonra\b|\bardindan\b|\bayrica\b|\bonu da\b)")
# "bluetooth'u aç" gibi: radyo düğmesi olan ayarlar için ilgili Ayarlar sayfası açılır
_RADIO_PAGES = {"bluetooth", "wifi", "ucak_modu", "mobil_erisim_noktasi", "gece_isigi", "vpn"}


def _clean(text: str) -> str:
    t = normalize_apostrophes(text).strip()
    t = re.sub(r"^\s*(?:hey\s+)?jarvis\b[\s,.:;!]*", "", t, flags=re.I)
    t = re.sub(r"[\s.!?…]+$", "", t)
    t = re.sub(r"^(?:lutfen|lütfen)\s+|\s+(?:lutfen|lütfen)$", "", t, flags=re.I)
    return t.strip()


def _name(phrase: str) -> str:
    """Kesme işaretinden sonrasını at; işaretsiz ekler eşleştirmede (suffix_variants) çözülür."""
    return strip_apostrophe_suffix(phrase).strip(" '\"")


def _lookup_key(phrase: str, table: dict[str, str]) -> str | None:
    """Tablodaki anahtarlardan eşleşeni (ekleri ayıklanmış, katlanmış) döndürür."""
    for cand in suffix_variants(_name(phrase)):
        key = fold(cand)
        if key in table:
            return key
    return None


def _lookup(phrase: str, table: dict[str, str]) -> str | None:
    key = _lookup_key(phrase, table)
    return table[key] if key is not None else None


def _folder(phrase: str) -> str | None:
    """Klasör takma adı ("indirilenler", "masaüstü"...) ya da None."""
    p = re.sub(r"\s*(klasoru|klasorunu|klasorum|klasorumu|dizini|dizinini)$", "", fold(_name(phrase))).strip()
    return _lookup_key(p, FOLDER_ALIASES)


def _search_args(folder: str, query: str) -> dict | None:
    q = fold(_name(query)).strip(" '\"")
    q = re.sub(r"\s*(dosyalari|dosyalarini|dosyasi|dosyasini|dosyalar|dosya|belgeleri|belgelerini|belgesi|"
               r"belgesini|belge)$", "", q).strip()
    if not q:
        return None
    ext, pattern = "", q
    m = re.fullmatch(r"\*?\.?([a-z0-9]{2,5})", q)
    words = q.split()
    if q in EXTENSION_WORDS:
        ext, pattern = EXTENSION_WORDS[q], ""
    elif m and q.startswith(("*.", ".")):
        ext, pattern = m.group(1), ""
    elif len(words) >= 2 and words[-1] in EXTENSION_WORDS:
        ext, pattern = EXTENSION_WORDS[words[-1]], " ".join(words[:-1])
    return {"folder": folder, "pattern": pattern, "extension": ext, "max_results": 25, "max_depth": 5}


# Konuşma tanıma sayıları çoğu zaman yazıyla verir: "sesi yüzde elli yap", "sesi otuza çek"
_UNITS = {"sifir": 0, "bir": 1, "iki": 2, "uc": 3, "dort": 4, "dord": 4, "bes": 5, "alti": 6, "yedi": 7,
          "sekiz": 8, "dokuz": 9}
_TENS = {"on": 10, "yirmi": 20, "otuz": 30, "kirk": 40, "elli": 50, "altmis": 60, "yetmis": 70, "seksen": 80,
         "doksan": 90, "yuz": 100}
_NUM_WORD = re.compile(r"^(" + "|".join(sorted({*_UNITS, *_TENS}, key=len, reverse=True)) + r")('?(?:ye|ya|e|a))?$")


def _number_words(f: str) -> str:
    """Katlanmış metindeki yazıyla sayıları rakama çevirir: "yuzde yetmis bes" -> "yuzde 75", "elliye" -> "50ye"."""
    out: list[str] = []
    words = f.split()
    i = 0
    while i < len(words):
        m = _NUM_WORD.match(words[i])
        if not m:
            out.append(words[i])
            i += 1
            continue
        word, suffix = m.group(1), m.group(2) or ""
        value = _TENS.get(word, _UNITS.get(word, 0))
        if word in _TENS and word != "yuz" and not suffix and i + 1 < len(words):
            n = _NUM_WORD.match(words[i + 1])
            if n and n.group(1) in _UNITS and n.group(1) != "sifir":
                value += _UNITS[n.group(1)]
                suffix = n.group(2) or ""
                i += 1
        out.append(f"{value}{suffix}")
        i += 1
    return " ".join(out)


def _volume(f: str) -> Intent | None:
    vol = r"(?:sesi|ses|ses seviyesini|ses seviyesi|ses duzeyini|sistem sesini|sesin seviyesini)"
    if re.match(vol + r"\s", f):
        f = _number_words(f)
    m = re.fullmatch(vol + r"\s+(?:yuzde\s*)?(\d{1,3})\s*(?:'?(?:e|a|ye|ya|'e|'a))?\s*"
                     r"(?:yap|ayarla|getir|olsun|olarak ayarla|cek|dusur|yukselt|al)?", f)
    if m:
        level = int(m.group(1))
        return Intent("set_volume", {"level": level}, "Ses ayarı") if level <= 100 else None
    m = re.fullmatch(vol + r"\s+(?:(\d{1,3})\s+)?(?:biraz\s+)?(artir|arttir|yukselt|ac biraz|azalt|kis|dusur)", f)
    if m:
        step = int(m.group(1)) if m.group(1) else 10
        sign = -1 if m.group(2) in ("azalt", "kis", "dusur") else 1
        return Intent("change_volume", {"delta": max(-100, min(100, sign * step))}, "Ses ayarı") \
            if step <= 100 else None
    if re.fullmatch(r"(?:sesi|sesleri|hoparloru|sistem sesini)\s+(?:kapat|kapa|sustur|tamamen kis)|sessize al|"
                    r"sesi sessize al", f):
        return Intent("set_mute", {"muted": True}, "Sesi kapatma")
    if re.fullmatch(r"(?:sesi|sesleri|hoparloru)\s+(?:ac|geri ac|ac lutfen)", f):
        return Intent("set_mute", {"muted": False}, "Sesi açma")
    if re.fullmatch(r"(?:ses|ses seviyesi|ses duzeyi|sesin seviyesi)\s+(?:kac|ne|nedir|ne kadar|yuzde kac)", f):
        return Intent("get_volume", {}, "Ses seviyesi")
    return None


def _settings(f: str) -> Intent | None:
    m = re.fullmatch(r"ayarlar(?:dan|da|i|in)?\s+(.+?)\s+(?:" + _OPEN[3:-1] + r"|ayarlarini ac|sayfasini ac)", f) \
        or re.fullmatch(r"(.+?)\s+(?:ayarlarini|ayarlari|ayarini|ayari)(?:\s+(?:ac|goster|acar misin))?", f)
    if m:
        page = _lookup(m.group(1), SETTINGS_WORDS)
        return Intent("open_settings", {"page": page}, "Ayarlar") if page else None
    if re.fullmatch(r"(?:windows\s+)?ayarlar(?:i|ini)?\s*(?:ac|goster|acar misin)?", f):
        return Intent("open_settings", {"page": "ana_sayfa"}, "Ayarlar")
    return None


def _system_query(f: str) -> Intent | None:
    if _EXPLAIN.search(f) or len(f.split()) > 9:
        return None
    if "en cok" in f or re.search(r"hangi (uygulama|program|islem)\w*\s+(kasiyor|yavaslatiyor|en cok)", f):
        if re.search(r"\b(ram|bellek|hafiza)\w*", f):
            return Intent("list_processes", {"sort": "memory", "limit": 10}, "İşlemler")
        if re.search(r"\b(islemci|cpu)\w*|kasiyor|yavaslatiyor", f):
            return Intent("list_processes", {"sort": "cpu", "limit": 10}, "İşlemler")
        return None
    if re.search(r"\b(ekran karti|ekran kartin|ekran kartim|grafik karti|gpu|vram|ekran kartinin)\w*", f):
        return Intent("gpu_status", {}, "Ekran kartı")
    if re.search(r"\b(islemci|cpu)\w*", f):
        return Intent("cpu_status", {}, "İşlemci")
    # "ram"/"disk"/"pil" yalnızca kendi ekleriyle: "Diskordu aç", "Pillars'ı aç", "Rampage" uygulama adıdır
    if re.search(r"\b(?:ram(?:im|in|i|de|da|e)?\b|bellek\w*)", f) and not re.search(r"\b(temizle|bosalt)", f):
        return Intent("memory_status", {}, "Bellek")
    if re.search(r"\b(performans\w*|sistem durumu|bilgisayarin durumu|sistem nasil|bilgisayar nasil|"
                 r"bilgisayar yavas\w*)", f):
        return Intent("performance_report", {}, "Performans")
    if re.search(r"\b(?:disk(?:ler\w*|im\w*|in\w*|te\w*|de\w*|i|e)?\b|bos alan\w*|ne kadar yer|depolama alani)", f) \
            and not re.search(r"temizle", f):
        return Intent("disk_info", {}, "Diskler")
    if re.search(r"(ne zamandir acik|calisma suresi|acik kalma suresi|uptime|ne zaman acildi|kac saattir acik)", f):
        return Intent("system_overview", {}, "Sistem özeti")
    if re.search(r"\b(ip adres\w*|ip m\b|ipim|ag bilgi\w*|internet baglanti\w*|ag baglanti\w*|internet hiz\w*)", f):
        return Intent("network_info", {}, "Ağ")
    if re.search(r"\b(?:pil(?:im|in|i|de|e)?\b|sarj\w*|batarya\w*)", f):
        return Intent("battery_status", {}, "Pil")
    if re.search(r"(sistem hatalari|son hatalar|olay gunlugu|sistem olaylari)", f):
        return Intent("recent_system_events", {"hours": 24, "limit": 10}, "Sistem olayları")
    if re.search(r"\b(sistem bilgi\w*|bilgisayarin ozellik\w*|windows surum\w*)", f):
        return Intent("system_overview", {}, "Sistem özeti")
    return None


def _power(f: str) -> Intent | None:
    if re.fullmatch(r"(?:ekrani|bilgisayari|pcyi|oturumu)?\s*kilitle(?:r misin|yebilir misin)?", f):
        return Intent("lock_screen", {}, "Ekran kilidi")
    if re.fullmatch(r"(?:bilgisayari|pcyi|windowsu|sistemi)\s+(?:kapat|kapatir misin|kapa)", f):
        return Intent("power_action", {"action": "shutdown", "delay_seconds": 60}, "Bilgisayarı kapatma")
    if re.fullmatch(r"(?:bilgisayari|pcyi|windowsu|sistemi)?\s*yeniden baslat(?:ir misin)?", f):
        return Intent("power_action", {"action": "restart", "delay_seconds": 60}, "Yeniden başlatma")
    if re.fullmatch(r"(?:kapatmayi|kapanmayi|yeniden baslatmayi)\s+iptal et", f):
        return Intent("cancel_power_action", {}, "Kapatmayı iptal")
    return None


# --- BILGISAYAR: "bilgisayarı sen kullan: ...", "Valorant'ı aç ve takımlı ölüm kalım maçı başlat" ---
_COMPUTER_PREFIX = re.compile(
    r"^(?:bilgisayar[ıi](?:m[ıi])?\s+(?:sen\s+)?(?:kullan(?:arak|[ıi]p)?|y[öo]net(?:erek)?)|"
    r"ekran[ıi]\s+(?:sen\s+)?kullanarak|ekrana\s+bakarak|"
    r"fare(?:yi)?\s+(?:ve|ile)\s+klavye(?:yi)?\s+(?:sen\s+)?kullanarak)\s*[:,\-–]?\s+(.{3,})$")
_QUEUE_GOAL = re.compile(r"(ma[cç]\S*\s+(?:ba[sş]lat|bul|ara)|s[ıi]raya\s+gir|kuyru[gğ]a\s+gir|e[sş]le[sş]me\s+ara|"
                         r"mod(?:unu|u|una)?\s+se[cç])")
_OPEN_AND = re.compile(r"\b(?:a[cç]|ba[sş]lat|[cç]al[ıi][sş]t[ıi]r)(?:[ıi]p\s+|\s*(?:ve|,|sonra|ard[ıi]ndan)\s+)")


def _computer_use(cleaned: str) -> Intent | None:
    low = tr_lower(cleaned)
    m = _COMPUTER_PREFIX.match(low)
    if m:
        goal = cleaned[len(cleaned) - len(m.group(1)):].strip() if len(low) == len(cleaned) else m.group(1)
        return Intent("use_computer", {"goal": goal[:500]}, "Bilgisayarı kullanma")
    if _OPEN_AND.search(low) and _QUEUE_GOAL.search(low):
        return Intent("use_computer", {"goal": cleaned[:500]}, "Bilgisayarı kullanma")
    return None
# --- BILGISAYAR sonu ---


def parse(text: str) -> Intent | None:
    if not isinstance(text, str) or len(text) > 300:
        return None
    cleaned = _clean(text)
    if not cleaned:
        return None
    # --- BILGISAYAR: çok adımlı olsa da ekranı kullanma isteği (çok adım kuralından önce) ---
    hit = _computer_use(cleaned)
    if hit:
        return hit
    # --- BILGISAYAR sonu ---
    f = fold(cleaned)
    if not f or _MULTI.search(f) or re.search(r"[,;]", cleaned):
        return None

    for rule in (_volume, _power, _settings):
        hit = rule(f)
        if hit:
            return hit

    # web araması: "google'da kedi videoları ara", "internette hava durumu ara"
    m = re.fullmatch(r"(google|youtube|bing|internet|web|vikipedi|wikipedia)'?(?:da|de|ta|te|dan|den|ten)?\s+(.+?)\s+"
                     + _SEARCH, f)
    if m:
        low_words = tr_lower(cleaned).split()
        query = " ".join(low_words[1:-1]) if len(low_words) >= 3 else m.group(2)
        return Intent("web_search_in_browser", {"query": query[:300], "engine": SEARCH_ENGINES[m.group(1)]},
                      "Web araması")

    # dosya araması
    m = re.fullmatch(r"(.+?)\s+(?:klasorunde|klasorumde|klasorunun icinde|klasoru icinde|icinde)\s+(.+?)\s+" + _SEARCH,
                     f)
    if m and _folder(m.group(1)):
        args = _search_args(_folder(m.group(1)) or "", m.group(2))
        return Intent("search_files", args, "Dosya arama") if args else None
    m = re.fullmatch(r"(\S+?)'?(?:de|da|te|ta|nde|nda)\s+(.+?)\s+" + _SEARCH, f)
    if m:
        word = m.group(1)
        folder = next((k for k in (_lookup_key(c, FOLDER_ALIASES) for c in (word, word.removesuffix("n"))) if k),
                      None)
        if folder:
            args = _search_args(folder, m.group(2))
            return Intent("search_files", args, "Dosya arama") if args else None
        return None
    m = re.fullmatch(r"(?:bilgisayarda|dosyalarda|tum dosyalarda)?\s*(.+?)\s+(?:dosyalarini|dosyasini|belgelerini|"
                     r"belgesini)\s+" + _SEARCH, f)
    if m:
        args = _search_args("", m.group(1) + " dosyalari")
        return Intent("search_files", args, "Dosya arama") if args else None

    hit = _system_query(f)
    if hit:
        return hit

    # oyun
    m = re.fullmatch(r"(.+?)\s+(?:oyununu|oyunu)\s+" + _OPEN, f) or \
        re.fullmatch(r"(.+?)\s+oyna(?:yalim|mak istiyorum|yayim)?", f)
    if m:
        return Intent("launch_game", {"name": _name(m.group(1))}, "Oyun başlatma")

    # kapat
    m = re.fullmatch(r"(.+?)\s+(zorla\s+)?" + _CLOSE, f)
    if m:
        name = _name(m.group(1))
        if fold(name) in _NOT_APPS or not name:
            return None
        return Intent("close_app", {"name": name, "force": bool(m.group(2))}, "Uygulama kapatma")

    # pencereye geç
    m = re.fullmatch(r"(.+?)\s+(?:penceresine\s+)?(?:gec|gecer misin|gecebilir misin|gecis yap)", f) or \
        re.fullmatch(r"(.+?)\s+(?:one getir|one al|on plana getir)", f)
    if m:
        name = _name(m.group(1))
        if fold(name) in _NOT_APPS or not name:
            return None
        return Intent("switch_to_app", {"name": name}, "Pencereye geçme")

    # aç
    m = re.fullmatch(r"(.+?)\s+" + _OPEN, f)
    if m:
        phrase = m.group(1)
        folder = _folder(phrase)
        if folder or re.search(r"klasor", phrase):
            return Intent("open_path", {"path": folder}, "Klasör açma") if folder else None
        page = _lookup(phrase, SETTINGS_WORDS)
        if page in _RADIO_PAGES:
            return Intent("open_settings", {"page": page}, "Ayarlar")
        site = _lookup(phrase, WEBSITES)
        if site:
            return Intent("open_url", {"url": site}, "Web sitesi açma")
        name = _name(phrase)
        if fold(name) in _NOT_APPS or not name or len(name) > 60:
            return None
        return Intent("open_app", {"name": name}, "Uygulama açma")
    return None
