"""Sistem özellikleri için yerel Türkçe komutlar (Claude'suz çalışır).

`intents.parse` bu modülü, "sonra"/"ve" içeren cümleleri eleyen çok adımlı komut denetiminden
ÖNCE çağırır; çünkü "1 saat sonra bilgisayarı kapat" tek bir komuttur. Bütün kalıplar tam eşleşmedir
(`fullmatch`): emin olunmayan cümle Claude'a bırakılır. Girdi `fold` edilmiş metindir
(küçük harf, ı->i, ş->s ...; kesme işareti korunur).
"""

from __future__ import annotations

import re
from collections.abc import Callable

# ---------- süre ayrıştırma ----------
_HALF = {"yarim saat": 1800, "ceyrek saat": 900, "bucuk saat": 5400, "bir bucuk saat": 5400,
         "iki bucuk saat": 9000, "uc bucuk saat": 12600}
_UNIT_S = {"saat": 3600, "sa": 3600, "dakika": 60, "dk": 60, "dakka": 60, "saniye": 1, "sn": 1}
DELAY_RE = (r"(?P<delay>(?:yarim saat|ceyrek saat|(?:bir |iki |uc )?bucuk saat|"
            r"\d{1,3}(?: bucuk)? (?:saat|sa|dakika|dakka|dk|saniye|sn)(?: \d{1,3} (?:dakika|dakka|dk|saniye|sn))?))")


def parse_delay(text: str, number_words: Callable[[str], str] | None = None) -> int | None:
    """Katlanmış Türkçe süreyi saniyeye çevirir: "1 saat" -> 3600, "yarim saat" -> 1800,
    "2 bucuk saat" -> 9000, "1 saat 15 dakika" -> 4500, "otuz dakika" -> 1800 (number_words ile)."""
    t = " ".join(text.split())
    if number_words is not None:
        t = number_words(t)
    if t in _HALF:
        return _HALF[t]
    total, matched = 0, False
    for m in re.finditer(r"(\d{1,3})( bucuk)? (saat|sa|dakika|dakka|dk|saniye|sn)\b", t):
        unit = _UNIT_S[m.group(3)]
        total += int(m.group(1)) * unit + (unit // 2 if m.group(2) else 0)
        matched = True
    return total if matched and total > 0 else None


_PC = r"(?:(?:bilgisayari|bilgisayarimi|pcyi|pc'yi|sistemi|windowsu)\s+)"
_SLEEP = r"(?:uykuya al|uyku moduna al|uyku moduna gec|uyut|uykuya gec|uykuya alir misin)"
_HIBER = r"(?:hazirda beklet|hazirda beklemeye al|hazirda bekletmeye al)"
_RADIO = {"bluetooth": "bluetooth", "blutut": "bluetooth", "bluetut": "bluetooth", "wifi": "wifi", "wi fi": "wifi",
          "kablosuz": "wifi", "kablosuz ag": "wifi", "wlan": "wifi", "kablosuz agi": "wifi"}
# "alarm kur", "baglanti kur" gibi kur'lar program kurulumu değildir
_NOT_PACKAGES = {"alarm", "hatirlatma", "hatirlatici", "zamanlayici", "toplanti", "baglanti", "cumle", "plan",
                 "kurulum", "saat", "dugun", "masa", "sofra", "takim", "ekip", "sistem", "windows", "guncelleme",
                 "guncellemeleri", "guncellemeler"}


def parse(f: str, make: Callable[..., object], number_words: Callable[[str], str] | None = None) -> object | None:
    """`f`: katlanmış metin. `make(tool, args, label)` bir Intent üretir. Eşleşme yoksa None."""
    nw = number_words or (lambda s: s)
    g = nw(f)

    # ----- güç: gecikmeli kapat / yeniden başlat / uyku / hazırda beklet -----
    m = re.fullmatch(DELAY_RE + r" sonra " + _PC + r"?(kapat|kapatir misin|kapa|yeniden baslat|" + _SLEEP[3:-1] +
                     "|" + _HIBER[3:-1] + r")", g)
    if m:
        delay = parse_delay(m.group("delay"))
        verb = m.group(2)
        if delay is None:
            return None
        if verb.startswith("kapa"):
            if not re.search(_PC, g):
                return None    # "1 saat sonra kapat": neyin kapanacağı belirsiz
            return make("power_action", {"action": "shutdown", "delay_seconds": max(30, min(delay, 86400))},
                        "Bilgisayarı kapatma")
        if verb.startswith("yeniden"):
            return make("power_action", {"action": "restart", "delay_seconds": max(30, min(delay, 86400))},
                        "Yeniden başlatma")
        action = "hibernate" if verb.startswith("hazirda") else "sleep"
        return make("power_action", {"action": action, "delay_seconds": min(delay, 86400)},
                    "Hazırda bekletme" if action == "hibernate" else "Uyku modu")
    if re.fullmatch(_PC + "?" + _SLEEP, g):
        return make("power_action", {"action": "sleep", "delay_seconds": 0}, "Uyku modu")
    if re.fullmatch(_PC + "?" + _HIBER, g):
        return make("power_action", {"action": "hibernate", "delay_seconds": 0}, "Hazırda bekletme")
    if re.fullmatch(r"(?:uykuya almayi|uyku modunu|uykuyu|hazirda bekletmeyi|kapatma zamanlayicisini|"
                    r"zamanlanmis kapatmayi)\s+iptal et", g):
        return make("cancel_power_action", {}, "Kapatmayı iptal")

    # ----- radyolar -----
    m = re.fullmatch(r"(bluetooth|blutut|bluetut|wi ?fi|kablosuz(?: agi?)?|wlan)(?:'?(?:u|yu|i|yi|ı|yı|nu|nı|ni))?"
                     r"\s+(ac|acar misin|acsana|kapat|kapa|kapatir misin|kapatsana)", f)
    if m:
        radio = _RADIO.get(m.group(1).replace("wi fi", "wifi"), _RADIO.get(m.group(1)))
        if radio:
            action = "ac" if m.group(2).startswith("ac") else "kapat"
            return make("radio_control", {"radio": radio, "action": action},
                        ("Bluetooth" if radio == "bluetooth" else "Wi-Fi") + (" açma" if action == "ac" else " kapatma"))
    m = re.fullmatch(r"(bluetooth|blutut|wi ?fi|kablosuz(?: ag)?)\s+(?:acik mi|kapali mi|durumu(?: ne)?|calisiyor mu)",
                     g)
    if m:
        radio = _RADIO.get(m.group(1).replace("wi fi", "wifi"))
        if radio:
            return make("radio_control", {"radio": radio, "action": "durum"}, "Radyo durumu")

    # ----- internet hız testi -----
    if re.fullmatch(r"(?:internet\s+)?hiz testi(?:\s+(?:yap|yapar misin|baslat))?|internet hizi(?:mi|ni)?\s+"
                    r"(?:test et|olc|olcer misin|test eder misin|olcsene)|internetim ne kadar hizli|"
                    r"internet ne kadar hizli|internet hizim ne kadar", f):
        return make("internet_speed_test", {}, "Hız testi")
    if re.fullmatch(r"(?:hiz testini|taramayi|temizligi|islemi)\s+(?:iptal et|durdur)", f):
        return make("cancel_long_operation", {}, "İşlemi durdurma")

    # ----- yer açma / büyük dosyalar -----
    if re.fullmatch(r"(?:en\s+)?buyuk dosyalar(?:i|imi)?\s+(?:bul|goster|listele|bulur musun)", f):
        return make("find_space_hogs", {"kind": "buyuk_dosyalar", "scope": "izinli_klasorler", "folder": ""},
                    "Büyük dosyalar")
    if re.fullmatch(r"(?:en\s+)?buyuk klasorler(?:i|imi)?\s+(?:bul|goster|listele)", f):
        return make("find_space_hogs", {"kind": "buyuk_klasorler", "scope": "izinli_klasorler", "folder": ""},
                    "Büyük klasörler")
    if re.fullmatch(r"(?:kopya|ayni|cift|tekrarlanan) dosyalar(?:i)?\s+(?:bul|goster|listele)", f):
        return make("find_space_hogs", {"kind": "kopyalar", "scope": "izinli_klasorler", "folder": ""},
                    "Kopya dosyalar")
    if re.fullmatch(r"(?:diskimde|diskte|bilgisayarimda|bilgisayarda|c diskinde)?\s*(?:biraz\s+)?"
                    r"(?:yer ac|yer bosalt|alan ac|alan bosalt)(?:\s*(?:mak istiyorum|alim|abilir misin|ir misin))?|"
                    r"neler yer kapliyor|ne cok yer kapliyor|diskimi ne dolduruyor", f):
        return make("find_space_hogs", {"kind": "hepsi", "scope": "izinli_klasorler", "folder": ""}, "Yer açma")

    # ----- geçici dosyalar -----
    tmp = r"(?:gecici dosyalar(?:i)?|temp(?: klasorunu| dosyalarini)?|%temp%)"
    if re.fullmatch(tmp + r"\s+(?:temizle|sil|temizler misin|bosalt)", f):
        return make("clean_temp_files", {"apply": True}, "Geçici dosya temizliği")
    if re.fullmatch(r"gecici dosyalar\s+(?:ne kadar yer kapliyor|ne kadar|kac gb)", f):
        return make("clean_temp_files", {"apply": False}, "Geçici dosyalar")

    # ----- İndirilenler rutini -----
    dl = r"(?:indirilenler(?:i|in)?|indirilenler klasorunu|indirmeler(?:i)?)"
    if re.fullmatch(dl + r"\s+(?:her gun|otomatik(?: olarak)?|gunluk)\s+(?:duzenle|duzenlemeye basla)", f):
        return make("downloads_routine", {"action": "ac"}, "İndirilenler rutini")
    if re.fullmatch(r"(?:indirilenler(?:i)?\s+)?(?:otomatik duzenlemeyi|duzenleme rutinini|gunluk duzenlemeyi)\s+"
                    r"(?:kapat|durdur|iptal et)|indirilenler rutinini kapat", f):
        return make("downloads_routine", {"action": "kapat"}, "İndirilenler rutini")
    if re.fullmatch(r"(?:indirilenler(?:in)?\s+)?(?:son\s+)?(?:duzenlemey?i|duzenlemesini)\s+geri al", f):
        return make("downloads_routine", {"action": "geri_al"}, "Düzenlemeyi geri al")
    if re.fullmatch(dl + r"\s+(?:simdi\s+)?(?:duzenle|duzenler misin|topla)", f):
        return make("downloads_routine", {"action": "simdi_calistir"}, "İndirilenler'i düzenle")

    # ----- Windows Update -----
    if re.fullmatch(r"(?:windows\s+)?guncellemeler(?:ini|i)?\s+(?:kontrol et|denetle|kontrol eder misin)|"
                    r"(?:windows\s+)?guncelleme(?:si)? var mi|bekleyen guncellemeler(?: var mi)?|"
                    r"windows guncel mi", f):
        return make("windows_update_check", {}, "Windows Update denetimi")
    if re.fullmatch(r"(?:windows\s+)?guncellemeler(?:ini|i)?\s+(?:yukle|kur|yap)", f):
        return make("open_settings", {"page": "guncelleme"}, "Windows Update")

    # ----- winget -----
    if re.fullmatch(r"(?:neler|hangi programlar|hangi uygulamalar)\s+guncellenebilir|guncellenebilir "
                    r"(?:programlar|uygulamalar)(?:\s+neler)?|guncel olmayan programlar(?:i goster)?", f):
        return make("winget_packages", {"action": "guncellenebilirler", "query": ""}, "Güncellenebilir programlar")
    if re.fullmatch(r"(?:tum\s+|butun\s+)?(?:programlari|uygulamalari)\s+(?:guncelle|guncelleyebilir misin|"
                    r"guncellesene)", f):
        return make("winget_packages", {"action": "tumunu_guncelle", "query": ""}, "Programları güncelle")
    m = re.fullmatch(r"(.+?)\s+(?:programini|uygulamasini)\s+guncelle", f)
    if m and _pkg_name_ok(m.group(1)):
        return make("winget_packages", {"action": "guncelle", "query": _pkg(m.group(1))}, "Program güncelleme")
    m = re.fullmatch(r"(.+?)\s+(?:(?:programini|uygulamasini)\s+)?(?:kur|kurar misin|kursana|yukle|yukler misin)", f)
    if m and _pkg_name_ok(m.group(1)) and (re.search(r"\b(kur|kurar misin|kursana)$", f)
                                           or re.search(r"(programini|uygulamasini)\s+yukle", f)):
        return make("winget_packages", {"action": "kur", "query": _pkg(m.group(1))}, "Program kurma")
    return None


def _pkg(name: str) -> str:
    return re.sub(r"'.*$", "", name).strip()


def _pkg_name_ok(name: str) -> bool:
    n = _pkg(name)
    words = n.split()
    return bool(n) and len(words) <= 4 and not (set(words) & _NOT_PACKAGES) and len(n) <= 60
