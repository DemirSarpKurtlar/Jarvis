# JARVIS v1.0 — Alınan Kararlar

Bu dosya, Aşama 0 analizinden sonra kullanıcının onayladığı kararları kaydeder.
Yeni bir karar alındığında buraya tarihle eklenir.

## 2026-11-07 — Aşama 0 kararları

| Konu | Karar |
|---|---|
| Geliştirme ortamı | Windows PC'ye Claude masaüstü uygulamasındaki **Remote Control** ile bağlanılacak. Mikrofon, GPU ve arayüz testleri gerçek Windows'ta yapılacak. |
| Başlangıç noktası | Sıfırdan. Proje JARVIS asistanı tarafından tasarlanıp yazılacak, önce arayüz taslakları hazırlanacak. |
| Yapay zekâ | Beyin olarak yalnızca **Claude API** kullanılacak (resmi `anthropic` Python SDK). |
| Model yönlendirme | Yerel komutlar API'siz çalışır. Varsayılan model `claude-haiku-5-5`, gerektiğinde `claude-sonnet-5-5`, açık talepte `claude-opus-5-5`. Model ID'leri ayar dosyasında tutulur. |
| Bütçe politikası | Günlük ve aylık limit ayarlanabilir (varsayılan: $5/ay). Limit dolunca **Haiku'ya düşülür**, kullanıcı uyarılır. |
| Uyandırma kelimesi | **İkisi de**: "Hey Jarvis" (openWakeWord hazır model) ve tek başına "Jarvis" (gerekirse özel model eğitilir). Tamamen yerel. |
| Ses (STT/TTS) maliyeti | **$0**. STT için yerel faster-whisper, TTS için yerel Piper ya da XTTS-v2. Ücretli ses sağlayıcısı kullanılmayacak. |
| İnternet araması | Claude `web_search` aracı **açık** (her 1.000 aramada $10, bütçe limitine dahil). `web_fetch` aracı token dışında ücretsiz. |
| Aşama sırası | Claude entegrasyonu ses motorundan **önce** yapılacak. |

## 2026-11-07 — Aşama 1 teknik kararları

| Konu | Karar |
|---|---|
| Paket yapısı | Önerilen klasörler tek bir `jarvis/` Python paketinin altında. Paketleme (PyInstaller) ve importlar için daha sağlam. |
| Async + Qt | `qasync`: asyncio ve Qt aynı döngüde çalışır. Engelleyici işler `ThreadPoolExecutor`'da yürür. |
| Ayar dosyası | JSON + pydantic doğrulama, atomik kayıt. Bozuk dosya sessizce yutulmaz, yedeklenip kullanıcıya bildirilir. |
| Fontlar | Değişken fontlardan üretilmiş sabit ağırlıklar (Qt uyumluluğu için). |
| Görselleştirici performansı | Sabit katmanlar önbellekte. Beklemede 8 fps, etkin durumlarda 30 fps, pencere gizliyken 0. |

## 2026-11-07 — Aşama 2 teknik kararları

| Konu | Karar |
|---|---|
| SDK | Resmi `anthropic` Python SDK 2.x, async istemci, akışlı yanıt. |
| API adresi | Her zaman açıkça `https://api.anthropic.com`; ortam değişkeni ile yönlendirilemez. |
| Effort | Haiku `low`, Sonnet `medium`, Opus `high` (model kaydında). |
| Yedek model | Sonnet 5.5 ve Opus 5.5'ta `fallbacks: "default"`; Haiku 5.5'ta desteklenmediği için yok. |
| Web araması | `web_search_20260209`, istek başına en çok 3 arama, konum TR. Yalnızca Sonnet/Opus'ta. |
| Konuşma geçmişi | Son 16 mesaj, yalnızca metin (düşünme blokları geri gönderilmez, gerekçe test raporunda). |
| Önbellek | Sabit sistem istemi `cache_control` ile; tarih ayrı blokta, önbelleği bozmaz. |
| Bütçe | Varsayılan günlük $0,50, aylık $5,00, %80'de uyarı, dolunca Haiku. Ayarlar'dan değişir. |
| Anahtar | Windows Credential Manager. Kaydetmeden önce ücretsiz model sorgusuyla doğrulanır. |
| Canlı testler | `pytest -m live`, varsayılan çalıştırmada atlanır (ücretli). |

## 2026-11-07 — Aşama 9 teknik kararları (paketleme ve kurulum)

| Konu | Karar |
|---|---|
| Paketleme | PyInstaller, tek klasör (onedir), konsolsuz. Tek dosya (onefile) değil: her açılışta geçici klasöre açma yavaş ve antivirüslere takılıyor. |
| Kurulum | Inno Setup 6, kullanıcıya özel (`PrivilegesRequired=lowest`), `%LOCALAPPDATA%\Programs\JARVIS`. Sabit AppId ile yükseltme. Türkçe arayüz. |
| Kaldırma | Program dosyaları ve başlangıç kaydı her zaman silinir; kullanıcı verisi yalnızca kullanıcı "Evet" derse (varsayılan: Hayır). |
| Windows ile başlatma | `HKCU\...\Run` (yönetici izni yok). Gerçek durumun kaynağı kayıt defteri; ayar açılışta eşitlenir. Görev Yöneticisi'nin "devre dışı" işareti dikkate alınır. |
| Tek örnek | `QLockFile` (veri klasöründe) + `QLocalServer`. İkinci açılış çalışan pencereyi öne getirir. Kurulum/kaldırma çalışan JARVIS'i `--kapat` ile temizce kapatır. |
| Bildirim alanı | Simge her zaman var (sistem destekliyorsa). Pencereyi kapatmak varsayılan olarak uygulamayı kapatır; "kapatınca bildirim alanına in" ayarla açılır. |
| Simge | `scripts/make_icon.py` ile QPainter'la çizilir (Pillow gerekmez), 16–256 px çok boyutlu ICO. |
| CI | GitHub Actions: Windows'ta test, PyInstaller, kurulum, sessiz kurma/yükseltme/kaldırma doğrulaması; Linux'ta test. Ses modelleri `actions/cache` ile önbellekte. |

## Güncel aşama sırası

0. Ortam ve mimari ✅
1. Temel: yapı, ayarlar, loglama, PySide6 iskeleti, durum makinesi ✅ (Linux'ta test edildi, Windows doğrulaması bekliyor: `docs/ASAMA1-TEST-RAPORU.md`)
2. Claude entegrasyonu: metin sohbeti, yönlendirici, token ve maliyet takibi, bütçe ✅ (sahte API ile test edildi; canlı API testi Windows'ta bekliyor: `docs/ASAMA2-TEST-RAPORU.md`)
3. Ses motoru: uyandırma kelimesi, STT, TTS, sözü kesme, bekleme
4. Hafıza
5. Windows araçları ve izin sistemi
6. Orkestrasyon
7. Profesyonel arayüz
8. Kalite güvencesi
9. Paketleme ve kurulum

## Arayüz taslağı

`design/taslaklar/jarvis-arayuz-v1.html`, tarayıcıda açılan etkileşimli bir taslak.
Üstteki taslak çubuğundan 7 ses durumu ve silme onayı penceresi denenebilir.
Son uygulama PySide6 ile yazılacak. Görselleştirici QPainter ile aynı şekilde çizilecek,
fontlar (Saira ve IBM Plex Sans, OFL lisanslı) uygulamayla birlikte paketlenecek.
