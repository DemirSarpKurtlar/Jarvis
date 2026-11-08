# JARVIS

Türkçe konuşan, Claude API ile düşünen, Windows'u güvenli şekilde yönetebilen kişisel masaüstü asistanı.

> **Durum:** Aşama 3 (sesli konuşma: uyandırma kelimesi, konuşma tanıma, Türkçe ses, sözü kesme) eklendi.
> Kararlar için `docs/KARARLAR.md`, testler için `docs/ASAMA1-TEST-RAPORU.md`, `docs/ASAMA2-TEST-RAPORU.md` ve `docs/ASAMA3-TEST-RAPORU.md`.

## Kurulum (Windows)

Python **3.12** önerilir.

```powershell
cd C:\Users\<adın>\Projeler\Jarvis
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
python -m jarvis
```

Seçenekler:

| Komut | Ne yapar |
|---|---|
| `python -m jarvis --debug` | Logları konsola da yazar |
| `python -m jarvis --gelistirici` | F8 ile durum animasyonlarını sırayla gösterir, F9 deneme hatası üretir |
| `python -m pytest` | Tüm ücretsiz testleri çalıştırır |
| `python -m pytest -m live -v -s` | Gerçek Claude API testleri (ücretli, yaklaşık $0,05–0,10; önce anahtarı gir) |
| `set JARVIS_DOWNLOAD_MODELS=1` sonra `python -m pytest -m models -v` | Gerçek ses modelleriyle testler (ücretsiz; ilk seferde ~600 MB indirir) |

Kısayol: **Ctrl+Shift+M** mikrofonu anında kapatır ya da açar.

## Sesli konuşma

"**Hey Jarvis**" ya da tek başına "**Jarvis**" de, ya da **Konuş** düğmesine bas. Sözün bitince (0,8 sn sessizlik)
JARVIS anlar, Claude'a sorar ve Türkçe erkek sesle cevap verir. Cevaptan sonra 8 saniye boyunca uyandırma kelimesi
demeden devam edebilirsin. JARVIS konuşurken araya girersen susar ve seni dinler.

* Hepsi **yerel ve ücretsiz**: openWakeWord (uyandırma), faster-whisper (konuşma tanıma), Piper (ses).
  Mikrofon sesi bilgisayarından çıkmaz; Claude'a yalnızca tanınan metin gider.
* Modeller ilk açılışta arka planda indirilir (uyandırma ~4 MB, Türkçe ses ~63 MB, konuşma tanıma `small`
  ~460 MB ya da NVIDIA ekran kartında `large-v3-turbo` ~1,6 GB). İlerleme üstteki bantta görünür, bu sırada
  yazılı sohbet çalışır. Yer: `%LOCALAPPDATA%\JARVIS\models`.
* Ayarlar > Ses: mikrofon/hoparlör seçimi, canlı mikrofon seviyesi, ortam gürültüsü ölçümü, ses denemesi, hız.
* Hoparlörden çıkan ses mikrofona geri dönebilir; JARVIS bunu ayırt etmeye çalışır ama en iyi sonuç kulaklıkla alınır.
* Kendi "Jarvis" uyandırma modelini eğitmek için: `docs/UYANDIRMA-KELIMESI.md`.
* `JARVIS_VOICE=off` ortam değişkeni ses motorunu tamamen kapatır.

## Claude API anahtarı

JARVIS'i aç, **Ayarlar > Claude bağlantısı > Anahtarı gir**. Anahtar önce Anthropic'e ücretsiz bir istekle doğrulanır,
sonra Windows Credential Manager'a kaydedilir. Anahtarı [Anthropic Console](https://platform.claude.com) > API Keys'ten alabilirsin.
Claude Pro/Max aboneliği API kullanımını kapsamaz; API ayrı faturalanır.

## Dosyalar nerede?

| Ne | Windows'ta |
|---|---|
| Ayarlar | `%APPDATA%\JARVIS\settings.json` |
| Loglar | `%LOCALAPPDATA%\JARVIS\Logs\jarvis.log` |
| Veri (veritabanı, ses modelleri) | `%LOCALAPPDATA%\JARVIS\` |

`JARVIS_HOME` ortam değişkeni verilirse hepsi o klasörün altına toplanır (taşınabilir kullanım ve testler).

API anahtarı gibi gizli bilgiler **hiçbir zaman** bu dosyalarda tutulmaz; Aşama 2'de Windows Credential Manager kullanılacak.
Loglar, anahtar benzeri değerleri otomatik maskeler.

## Proje yapısı

```
jarvis/
  app.py              başlatma ve temiz kapanış
  paths.py            ayar/log/veri klasörleri
  config/             ayarlar (pydantic, atomik kayıt, bozuk dosya yedeği)
  core/               durum makinesi, olay veriyolu, hata yakalama, loglama, asistan çekirdeği
  system/             gerçek sistem verisi (CPU, RAM, disk, ağ, NVIDIA GPU)
  voice/              ses motoru: mikrofon/hoparlör, uyandırma, VAD, STT, TTS, konuşma döngüsü
  interface/          PySide6 arayüzü: tema, reaktör görselleştirici, paneller
  assets/fonts/       Saira ve IBM Plex Sans (OFL)
design/taslaklar/     onaylanan arayüz taslağı
docs/                 kararlar ve test raporları
tests/                pytest + pytest-qt
```
