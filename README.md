# JARVIS

Türkçe konuşan, Claude API ile düşünen, Windows'u güvenli şekilde yönetebilen kişisel masaüstü asistanı.

> **Durum:** Aşama 2 (Claude ile yazılı sohbet, model seçimi, maliyet ve bütçe takibi) tamamlandı. Sesli konuşma Aşama 3'te geliyor.
> Kararlar için `docs/KARARLAR.md`, testler için `docs/ASAMA1-TEST-RAPORU.md` ve `docs/ASAMA2-TEST-RAPORU.md`.

## Kurulum (Windows)

Hazır kurulum programı (yönetici izni gerekmez), Windows ile başlatma, yükseltme, kaldırma ve
kaynaktan exe/kurulum derleme için: **`docs/KURULUM.md`**.

Kaynaktan çalıştırmak için Python **3.12** önerilir.

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

Kısayol: **Ctrl+Shift+M** mikrofonu anında kapatır ya da açar.

## Claude API anahtarı

JARVIS'i aç, **Ayarlar > Claude bağlantısı > Anahtarı gir**. Anahtar önce Anthropic'e ücretsiz bir istekle doğrulanır,
sonra Windows Credential Manager'a kaydedilir. Anahtarı [Anthropic Console](https://platform.claude.com) > API Keys'ten alabilirsin.
Claude Pro/Max aboneliği API kullanımını kapsamaz; API ayrı faturalanır.

## Hafıza

JARVIS konuşmalardan yalnızca işe yarayacak bilgileri (tercihler, kendi isteğinle verdiğin kişisel bilgiler,
görevler, rutinler, kararlar, teknik sorunlar ve konuşma özetleri) yerel veritabanında saklar. Her cümleyi kaydetmez.
"Bunu hatırla: ..." ve "... unut" komutları çalışır. Sağlık, para, kesin konum gibi hassas bilgiler ve JARVIS'in
tahmin ettiği şeyler **Hafıza** ekranında onaylamadan kullanılmaz; şifre ve API anahtarı hiçbir zaman kaydedilmez.
Hafıza ekranından kayıtları görebilir, düzeltebilir, silebilir ya da hafızayı tamamen kapatabilirsin.
Ayrıntı: `docs/KARARLAR.md` (Aşama 4) ve `docs/ASAMA4-TEST-RAPORU.md`.

## Dosyalar nerede?

| Ne | Windows'ta |
|---|---|
| Ayarlar | `%APPDATA%\JARVIS\settings.json` |
| Loglar | `%LOCALAPPDATA%\JARVIS\Logs\jarvis.log` |
| Veri (konuşmalar, maliyet, hafıza: `jarvis.db`) | `%LOCALAPPDATA%\JARVIS\` |

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
  interface/          PySide6 arayüzü: tema, reaktör görselleştirici, paneller
  assets/fonts/       Saira ve IBM Plex Sans (OFL)
design/taslaklar/     onaylanan arayüz taslağı
docs/                 kararlar ve test raporları
tests/                pytest + pytest-qt
```
