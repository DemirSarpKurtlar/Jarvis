"""Yerel Türkçe komut ayrıştırıcı: Claude'a gitmeden araç çağrısına çevrilen komutlar."""

import pytest

from jarvis.tools.applications import AppIndex
from jarvis.tools.intents import parse
from tests.tool_fakes import FAKE_APPS

# (cümle, beklenen araç, beklenen girdi alt kümesi)
CASES = [
    # uygulama açma
    ("Jarvis, Notepad'i aç", "open_app", {"name": "notepad"}),
    ("Notepad'i aç", "open_app", {"name": "notepad"}),
    ("jarvis notepadi aç", "open_app", {"name": "notepadi"}),
    ("Hey Jarvis, not defterini aç.", "open_app", {"name": "not defterini"}),
    ("Not Defteri'ni açar mısın?", "open_app", {"name": "not defteri"}),
    ("hesap makinesini aç lütfen", "open_app", {"name": "hesap makinesini"}),
    ("Spotify'ı başlat", "open_app", {"name": "spotify"}),
    ("discordu aç", "open_app", {"name": "discordu"}),
    ("Steam'i çalıştır", "open_app", {"name": "steam"}),
    ("tarayıcıyı aç", "open_app", {"name": "tarayiciyi"}),
    ("Chrome’u açabilir misin", "open_app", {"name": "chrome"}),
    ("görev yöneticisini aç", "open_app", {"name": "gorev yoneticisini"}),
    ("VS Code'u aç", "open_app", {"name": "vs code"}),
    # kapatma
    ("Chrome'u kapat", "close_app", {"name": "chrome", "force": False}),
    ("Jarvis, Spotify'ı kapatır mısın", "close_app", {"name": "spotify", "force": False}),
    ("discordu zorla kapat", "close_app", {"name": "discordu", "force": True}),
    ("not defterini kapat", "close_app", {"name": "not defterini"}),
    # pencere değiştirme
    ("Chrome'a geç", "switch_to_app", {"name": "chrome"}),
    ("Spotify penceresine geç", "switch_to_app", {"name": "spotify"}),
    ("Word'ü öne getir", "switch_to_app", {"name": "word"}),
    # oyun
    ("Counter-Strike 2 oyna", "launch_game", {"name": "counter strike 2"}),
    ("Hades oyununu başlat", "launch_game", {"name": "hades"}),
    # ses
    ("sesi yüzde 30 yap", "set_volume", {"level": 30}),
    ("Jarvis sesi %45 yap", "set_volume", {"level": 45}),
    ("ses seviyesini 70'e ayarla", "set_volume", {"level": 70}),
    ("sesi 0 yap", "set_volume", {"level": 0}),
    ("sesi kapat", "set_mute", {"muted": True}),
    ("sessize al", "set_mute", {"muted": True}),
    ("sesi aç", "set_mute", {"muted": False}),
    ("sesi biraz kıs", "change_volume", {"delta": -10}),
    ("sesi 20 artır", "change_volume", {"delta": 20}),
    ("ses kaç", "get_volume", {}),
    ("ses seviyesi ne", "get_volume", {}),
    # ayarlar
    ("ayarlardan bluetooth'u aç", "open_settings", {"page": "bluetooth"}),
    ("Bluetooth ayarlarını aç", "open_settings", {"page": "bluetooth"}),
    ("wifi ayarlarını aç", "open_settings", {"page": "wifi"}),
    ("ayarlardan ekranı aç", "open_settings", {"page": "ekran"}),
    ("Windows Update ayarları", "open_settings", {"page": "guncelleme"}),
    ("ayarları aç", "open_settings", {"page": "ana_sayfa"}),
    # SISTEM: radyo artık gerçekten açılıp kapatılıyor (radio_control); ayar sayfası için "bluetooth ayarlarını aç"
    ("bluetooth'u aç", "radio_control", {"radio": "bluetooth", "action": "ac"}),
    ("ses ayarlarını açar mısın", "open_settings", {"page": "ses"}),
    # donanım ve sistem
    ("ekran kartı sıcaklığı kaç", "gpu_status", {}),
    ("Jarvis, ekran kartının sıcaklığı ne?", "gpu_status", {}),
    ("GPU kullanımı", "gpu_status", {}),
    ("işlemci kullanımı", "cpu_status", {}),
    ("CPU yüzde kaç", "cpu_status", {}),
    ("işlemci sıcaklığı", "cpu_status", {}),
    ("RAM kullanımı ne kadar", "memory_status", {}),
    ("bellek durumu", "memory_status", {}),
    ("en çok RAM kullanan uygulamalar", "list_processes", {"sort": "memory"}),
    ("en çok işlemci kullanan programlar hangileri", "list_processes", {"sort": "cpu"}),
    ("hangi uygulama kasıyor", "list_processes", {"sort": "cpu"}),
    ("diskte ne kadar yer kaldı", "disk_info", {}),
    ("boş alan ne kadar", "disk_info", {}),
    ("sistem durumu", "performance_report", {}),
    ("performans nasıl", "performance_report", {}),
    ("bilgisayar ne zamandır açık", "system_overview", {}),
    ("IP adresim ne", "network_info", {}),
    ("pil durumu", "battery_status", {}),
    ("son sistem hataları", "recent_system_events", {"hours": 24}),
    # dosyalar
    ("Downloads klasöründe pdf ara", "search_files", {"folder": "downloads", "extension": "pdf", "pattern": ""}),
    ("indirilenlerde fatura ara", "search_files", {"folder": "indirilenler", "pattern": "fatura"}),
    ("masaüstünde rapor bul", "search_files", {"folder": "masaustu", "pattern": "rapor"}),
    ("Belgelerim klasöründe word dosyalarını ara", "search_files", {"folder": "belgelerim", "extension": "docx"}),
    ("Downloads'da fatura pdf ara", "search_files", {"pattern": "fatura", "extension": "pdf"}),
    ("pdf dosyalarını ara", "search_files", {"folder": "", "extension": "pdf"}),
    ("İndirilenler klasörünü aç", "open_path", {"path": "indirilenler"}),
    ("masaüstünü aç", "open_path", {"path": "masaustu"}),
    # web
    ("youtube'u aç", "open_url", {"url": "https://www.youtube.com"}),
    ("google'da hava durumu ara", "web_search_in_browser", {"engine": "google", "query": "hava durumu"}),
    ("YouTube'da lo-fi müzik ara", "web_search_in_browser", {"engine": "youtube", "query": "lo-fi müzik"}),
    ("internette Türkçe sözlük ara", "web_search_in_browser", {"engine": "google", "query": "türkçe sözlük"}),
    # güç
    ("ekranı kilitle", "lock_screen", {}),
    ("bilgisayarı kilitle", "lock_screen", {}),
    ("bilgisayarı kapat", "power_action", {"action": "shutdown"}),
    ("bilgisayarı yeniden başlat", "power_action", {"action": "restart"}),
    ("kapatmayı iptal et", "cancel_power_action", {}),
]


@pytest.mark.parametrize("text,tool,args", CASES, ids=[c[0] for c in CASES])
def test_parse(text, tool, args):
    intent = parse(text)
    assert intent is not None, text
    assert intent.tool == tool
    for k, v in args.items():
        assert intent.args.get(k) == v, (k, intent.args)
    assert intent.label


def test_at_least_forty_phrasings():
    assert len(CASES) >= 40


@pytest.mark.parametrize("text", [
    "", "Jarvis", "Merhaba, nasılsın?", "bana bir şiir yaz", "bugün hava nasıl", "Notepad'i aç ve bir not yaz",
    "Chrome'u kapat, sonra Spotify'ı aç", "ekran kartı sıcaklığı kaç olmalı", "işlemcimi nasıl hızlandırırım",
    "pencereyi kapat", "sesi kapat ve müziği durdur", "kapıyı aç", "ışıkları kapat", "bunu aç",
    "neden bilgisayarım yavaş", "RAM nedir", "bir dosya oluştur", "Python'da liste nasıl sıralanır",
    "evde ne var ara", "sesi 150 yap", "x" * 400,
])
def test_returns_none_when_unsure(text):
    assert parse(text) is None


@pytest.mark.parametrize("text,expected", [
    ("Jarvis, Notepad'i aç", "Not Defteri"), ("not defterini aç", "Not Defteri"),
    ("hesap makinesini aç", "Hesap Makinesi"), ("tarayıcıyı aç", "Google Chrome"), ("chrome'u aç", "Google Chrome"),
    ("spotifyı aç", "Spotify"), ("discordu aç", "Discord"), ("steami aç", "Steam"), ("vs code'u aç", "Visual Studio Code"),
    ("görev yöneticisini aç", "Görev Yöneticisi"), ("exceli aç", "Excel"), ("word'ü aç", "Word"),
    ("firefoxu aç", "Mozilla Firefox"), ("whatsapp'ı aç", "WhatsApp"), ("dosya gezginini aç", "Dosya Gezgini"),
    ("calculator'ı aç", "Hesap Makinesi"), ("paint'i aç", "Paint"), ("kamerayı aç", "Kamera"),
])
def test_parsed_names_resolve_in_fake_index(text, expected):
    intent = parse(text)
    assert intent is not None and intent.tool == "open_app"
    entry = AppIndex(FAKE_APPS).best(intent.args["name"])
    assert entry is not None and entry.name == expected
