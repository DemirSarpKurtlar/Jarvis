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

## 2026-11-07 — Aşama 4 teknik kararları (hafıza)

| Konu | Karar |
|---|---|
| Depo | Aynı SQLite veritabanı, 2. şema geçişi: `memories` tablosu + FTS5 dizini (`unicode61 remove_diacritics 2`) + tetikleyiciler. |
| Türkçe arama | Dizin ve sorgu aynı sadeleştirmeden geçer (Türkçe küçük harf, ı/İ/ş/ğ/ü/ö/ç aksansız). Ekler için kelimenin ilk 5 harfiyle önek araması. |
| Puan | %60 ilgi (bm25) + %20 önem + %10 yakınlık (60 gün yarı ömür) + %10 kullanım sıklığı; en çok 5 kayıt. Eşleşme azsa yüksek önemli tercih/kişisel bilgiler eklenir. |
| Bağlama ekleme | Kayıtlar sistem isteminin **değişken** bloğuna eklenir; önbelleklenen sabit blok bayt bayt aynı kalır. Her kayıt "doğrulanmış" ya da "doğrulanmamış çıkarım" diye etiketlenir. |
| Bilgi çıkarma | Önce yerel süzgeç (ücretsiz); işaret varsa Haiku 5.5, effort `low`, düşünme kapalı, yapılandırılmış JSON çıktısı (`output_config.format`). Arka planda, cevabı geciktirmez. Maliyet `usage` tablosuna "hafıza" nedeniyle yazılır. |
| Güvenlik kuralları (kodda) | Parola/anahtar/kart numarası asla kaydedilmez. Çıkarım asla doğrulanmış bilgi olarak kaydedilmez (onay bekler). Hassas bilgi (sağlık, para, kesin konum, kimlik, inanç, siyaset, cinsel yaşam, hukuki durum) kullanıcı Hafıza ekranında "Hatırla" demeden kullanılmaz; açık "hatırla" komutunda da. |
| Tekrar | Kök kümesi örtüşmesi ≥ %75 ya da karakter benzerliği ≥ %92 ise yeni kayıt eklenmez, mevcut kayıt güncellenir (kullanıcının en yeni ifadesi geçerli). Model bir düzeltmeyi `replaces_id` ile bildirebilir. |
| "unut ..." | Tamamen yerel, istekten **önce** çalışır; bütün kelimeleri geçen kayıtlar silinir (en çok 5), Claude'a ne silindiği söylenir. "bunu unut" bu konuşmanın son kaydını siler. |
| Özet | Konuşma bittiğinde (30 dk sessizlik sonrası yeni konuşma, kapanış ya da önceki oturumdan kalan) Haiku ile 1-2 cümle. 4 mesajdan kısa konuşmalar özetlenmez. Özetler 90 gün (önemliyse 365 gün) sonra silinir; silinen özet yeniden üretilmez. |
| Bütçe | Bütçe dolmuşsa ve politika "durdur" ya da "sor" ise hafıza çağrıları sessizce atlanır (log satırı yazılır). |
| Hafıza kapalı | Kayıt kullanılmaz, bilgi çıkarılmaz, özet yapılmaz (API çağrısı yok). Mevcut kayıtlar silinmez; silme ve "unut" her zaman çalışır. |

## Güncel aşama sırası

0. Ortam ve mimari ✅
1. Temel: yapı, ayarlar, loglama, PySide6 iskeleti, durum makinesi ✅ (Linux'ta test edildi, Windows doğrulaması bekliyor: `docs/ASAMA1-TEST-RAPORU.md`)
2. Claude entegrasyonu: metin sohbeti, yönlendirici, token ve maliyet takibi, bütçe ✅ (sahte API ile test edildi; canlı API testi Windows'ta bekliyor: `docs/ASAMA2-TEST-RAPORU.md`)
3. Ses motoru: uyandırma kelimesi, STT, TTS, sözü kesme, bekleme
4. Hafıza ✅ (sahte API ile test edildi; canlı test Windows'ta bekliyor: `docs/ASAMA4-TEST-RAPORU.md`)
5. Windows araçları ve izin sistemi
6. Orkestrasyon
7. Profesyonel arayüz
8. Kalite güvencesi
9. Paketleme ve kurulum ✅ (Windows'ta CI ile doğrulanıyor: `.github/workflows/windows.yml`)

## Arayüz taslağı

`design/taslaklar/jarvis-arayuz-v1.html`, tarayıcıda açılan etkileşimli bir taslak.
Üstteki taslak çubuğundan 7 ses durumu ve silme onayı penceresi denenebilir.
Son uygulama PySide6 ile yazılacak. Görselleştirici QPainter ile aynı şekilde çizilecek,
fontlar (Saira ve IBM Plex Sans, OFL lisanslı) uygulamayla birlikte paketlenecek.
