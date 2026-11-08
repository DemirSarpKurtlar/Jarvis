# Aşama 3 Test Raporu: Ses Motoru

**Tarih:** 2026-11-08
**Ortam:** Bulut Linux konteyneri, Python 3.13, 4 çekirdek CPU, GPU yok, ses aygıtı yok.
**Sonuç:** Tüm ücretsiz testler geçiyor; gerçek modellerle (`-m models`) 6 testin hepsi geçiyor. Gerçek mikrofon ve
hoparlör ile deneme, GPU (`large-v3-turbo`) ve Windows ses sürücüleri **Windows'ta** doğrulanacak.

## Kapsam

| Bileşen | Dosya |
|---|---|
| Mikrofon (16 kHz mono), hoparlör (anında durdurma), aygıtlar, yeniden örnekleme | `jarvis/voice/audio_io.py` |
| Model indirme/önbellek (sha256/md5/boyut, atomik taşıma, Türkçe hatalar) | `jarvis/voice/models.py` |
| Uyandırma kelimesi (openWakeWord ONNX akış hattı) | `jarvis/voice/wake_word.py` |
| VAD (Silero v6) ve söz sonu tespiti, gürültü kalibrasyonu | `jarvis/voice/vad.py` |
| Yankı koruması (enerji kapısı + korelasyon) | `jarvis/voice/echo.py` |
| Konuşma tanıma (faster-whisper) | `jarvis/voice/stt.py` |
| Konuşma sentezi (Piper, cümle cümle) | `jarvis/voice/tts.py` |
| Parçaların hazırlanması, eksik parçada zarif düşüş | `jarvis/voice/components.py` |
| Konuşma döngüsü, sözü kesme, konuşma modu | `jarvis/voice/voice_manager.py` |
| Arayüz: Konuş düğmesi, mikrofon işareti, indirme bandı, Ayarlar > Ses | `jarvis/interface/main_window.py`, `jarvis/interface/voice_panel.py` |

## Burada gerçekten doğrulananlar

| Test | Nasıl | Sonuç |
|---|---|---|
| "Hey Jarvis" algılama | Piper İngilizce erkek sesiyle (GitHub'dan) üretilen gerçek konuşma + gerçek openWakeWord modelleri | ✅ "Hey Jarvis" ve "Jarvis" uyandırıyor, "What time is it…" ve "Please open the calculator" uyandırmıyor |
| openWakeWord eşdeğerliği | Kendi ONNX hattımız ile `openwakeword` 0.6.0 paketi aynı seslerde | ✅ en büyük skor farkı 0,00005 (tek seferlik betik) |
| Silero VAD | Gerçek konuşma / sessizlik / düz gürültü | ✅ |
| Piper Python API'si (piper-tts 1.8.0) | İngilizce ses, hız ayarı | ✅ hız 1,5 → süre < 0,8 kat |
| Gerçek sounddevice/PortAudio | ALSA "null" aygıtında MicrophoneStream ve AudioPlayer (tek seferlik betik) | ✅ 1280 örneklik int16 kareler, durdurma 0,3 ms |
| Kare başına işlem süresi | Uyandırma + VAD, CPU | ~3-6 ms / 80 ms kare (tek çekirdeğin %5'i civarı) |
| Model indirme | Gerçek GitHub indirmesi (openWakeWord, test sesi) ModelManager ile | ✅ sha256 tuttu |
| Türkçe ses + konuşma tanıma | Gerçek `tr_TR-dfki-medium` (Hugging Face'ten, md5 ve boyut tuttu) ve Whisper `small` CPU int8 | ✅ "Merhaba, bugün hava çok güzel." aynen geri yazıldı; "Not Defteri'ni aç." → "Not defterini aç." (yerel komut ayrıştırıcısı bunu da tanıyor); "Yarın saat dokuzda toplantım var, bana hatırlat." → "Yarın saat 9'da toplantım var bana hatırlat." |
| Hız (CPU) | Piper cümle sentezi / Whisper small çözümleme | Sentez 0,07-0,13 sn (2-3 sn'lik ses için), tanıma 1,5-1,9 sn |
| Tam döngü, gerçek modellerle | Mikrofona Piper'ın "Hey Jarvis." + Türkçe "Bugün hava nasıl?" sesi verildi; gerçek uyandırma, Silero, Whisper, Piper, VoiceManager, ChatService (sahte API) | ✅ uyandı, tanıdı, Claude'a yalnızca metin gitti ("jarvis" ayıklanmış), Türkçe cevap çalındı |
| Windows bağımlılıkları | `pip install --dry-run --platform win_amd64 --python-version 3.12 --only-binary=:all:` | ✅ hepsinin cp312 win_amd64 tekerleği var |

## Sahte parçalarla uçtan uca (gerçek VoiceManager + gerçek ChatService/sahte API)

Uyandırma → dinleme → STT → Claude → seslendirme → konuşma modu → bekleme; uyandırmasız takip sorusu;
sözü kesme (ses < 0,8 sn içinde susuyor, durduktan sonra tek örnek çalınmıyor); kendi yankısıyla kesilmeme
(yankı seviyesi -9 dB olarak öğreniliyor); Konuş düğmesi (uyandırma modeli yokken de); dinlerken Konuş = hemen gönder;
mikrofon kapatma (akış gerçekten kapanıyor, kapalıyken söylenen hiçbir şey yakalanmıyor); anlaşılamayan sözde döngüye girmeme;
anahtar yokken hatanın sesli söylenmesi; yazılı sohbetin seslendirilmemesi; kalibrasyon; ses aygıtı/model yokken
Türkçe açıklama ve yazılı sohbetin çalışması; açılışı bekletmeme. Bu testlerde soket bağlantısı yasak;
Claude'a giden istek gövdesinde yalnızca tanınan metin var.

## Windows'ta / CI'da doğrulanacaklar

1. `python scripts/fetch_models.py` (~650 MB, `JARVIS_MODELS_DIR` altına), sonra `python -m pytest -m "models or windows" -v`.
   CI (`.github/workflows/windows.yml`) bunu zaten yapıyor.
2. Elle: gerçek mikrofonla "Hey Jarvis, saat kaç?", konuşurken araya girme (hoparlörle ve kulaklıkla),
   Ctrl+Shift+M ile kapatınca Windows'un mikrofon simgesinin kaybolması, NVIDIA kartta `large-v3-turbo`.

## Bilinen sınırlar

* Yankı koruması sezgiseldir, AEC değildir. Yüksek sesli hoparlör mikrofona çok yakınsa yanlış kesme olabilir; kulaklık önerilir.
* Hazır uyandırma modeli İngilizce eğitildi; Türkçe söyleyişte skor düşebilir, benzer kelimeler ("Hey Travis") uyandırabilir.
* `tr_TR-dfki-medium` lisansı CC BY-NC-SA 4.0 (ticari olmayan). fahrettin/fettah seslerinin cinsiyeti model kartıyla doğrulanamadı.
* Sesli komut yazılı metinle aynı yoldan gider (`Assistant._handle_text`): "Not defterini aç" gibi yerel komutlar
  Claude'suz çalışır ve sonucu sesli söylenir (testle doğrulandı, sahte Windows arka ucuyla).
* Kapanışta Whisper yüklemesi sürüyorsa çıkış birkaç saniye gecikebilir (yükleme kesilemiyor; indirme kesilebiliyor).
