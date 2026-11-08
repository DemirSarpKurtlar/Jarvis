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

## 2026-11-07 — Aşama 3 teknik kararları (ses motoru)

| Konu | Karar |
|---|---|
| Uyandırma | openWakeWord v0.5.1 ONNX modelleri (GitHub sürümünden, sha256 doğrulamalı). Paketin kendisi yerine akış hattı onnxruntime ile birebir yazıldı: paket scipy/scikit-learn çekiyor, Linux'ta kurulamayan tflite-runtime'a bağlı ve pakete ~150 MB ekliyor. Skor farkı paketle karşılaştırıldı: en çok 0,00005. |
| "Hey Jarvis" / "Jarvis" | Aynı hazır model, iki eşik: hassasiyet eşiği (tek kare) ve daha düşük `jarvis_threshold` (art arda 2 kare). İsteğe bağlı özel `.onnx` model yanına yüklenir (`docs/UYANDIRMA-KELIMESI.md`). |
| VAD | faster-whisper'ın içinde gelen Silero VAD v6 (ek indirme yok), yoksa enerji tabanlı yedek. Söz sonu: 0,8 sn sessizlik, en çok 20 sn, 6 sn hiç konuşmama. |
| STT | faster-whisper, `language="tr"`. CUDA varsa `large-v3-turbo` float16, yoksa CPU `small` int8. GPU ilk çözümlemede hata verirse (eksik cuBLAS/cuDNN) CPU'ya düşülür. |
| TTS | Piper `tr_TR-dfki-medium` (erkek: MaryTTS dfki-ot verisi, `gender="male"`). Lisansı CC BY-NC-SA 4.0. Cümle cümle sentez, `speech_rate` → `length_scale`. |
| Gizlilik | Mikrofon sesi süreç dışına çıkmaz; Claude'a yalnızca tanınan metin gider (testle doğrulandı). Logda metnin kendisi değil uzunluğu yazılır. Mikrofon kapatılınca akış gerçekten kapanır. |
| Durum makinesi | Değişmedi. ChatService bitince BEKLEMEDE'ye döndüğü için seslendirme aynı döngü adımında BEKLEMEDE → DÜŞÜNÜYOR → KONUŞUYOR geçer (ekranda görünmez). |
| Sözü kesme | Konuşurken VAD eşiği 0,8'e çıkar, art arda 3 kare (240 ms) ister ve yankı korumasından geçmesi gerekir (enerji kapısı + çalınan sesle korelasyon). Tam bir AEC değildir; kulaklık en iyi sonucu verir. |
| Konuşma modu | Cevaptan sonra 8 sn uyandırma kelimesi gerekmeden dinlenir. Anlaşılamayan söz bir kez sorulur, ikincisinde sessizce beklemeye dönülür. |
| Yazılı cevaplar | Yalnızca sesle sorulan sorular seslendirilir; yazılı sohbet sessiz kalır. |

## Güncel aşama sırası

0. Ortam ve mimari ✅
1. Temel: yapı, ayarlar, loglama, PySide6 iskeleti, durum makinesi ✅ (Linux'ta test edildi, Windows doğrulaması bekliyor: `docs/ASAMA1-TEST-RAPORU.md`)
2. Claude entegrasyonu: metin sohbeti, yönlendirici, token ve maliyet takibi, bütçe ✅ (sahte API ile test edildi; canlı API testi Windows'ta bekliyor: `docs/ASAMA2-TEST-RAPORU.md`)
3. Ses motoru: uyandırma kelimesi, STT, TTS, sözü kesme, bekleme ✅ (Linux'ta sahte ses sürücüsü ve gerçek modellerle test edildi; mikrofon/hoparlör ve Türkçe modeller Windows'ta doğrulanacak: `docs/ASAMA3-TEST-RAPORU.md`)
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
