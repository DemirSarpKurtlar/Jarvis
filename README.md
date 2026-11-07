# JARVIS

Türkçe konuşan, Claude API ile düşünen, Windows'u güvenli şekilde yönetebilen kişisel masaüstü asistanı.

> **Durum:** Aşama 1 (temel iskelet) tamamlandı. Yazılı sohbet Aşama 2'de, sesli konuşma Aşama 3'te geliyor.
> Kararlar için `docs/KARARLAR.md`, testler için `docs/ASAMA1-TEST-RAPORU.md`.

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
| `python -m pytest` | Tüm testleri çalıştırır |

Kısayol: **Ctrl+Shift+M** mikrofonu anında kapatır ya da açar.

## Dosyalar nerede?

| Ne | Windows'ta |
|---|---|
| Ayarlar | `%APPDATA%\JARVIS\settings.json` |
| Loglar | `%LOCALAPPDATA%\JARVIS\Logs\jarvis.log` |
| Veri (ileride hafıza veritabanı) | `%LOCALAPPDATA%\JARVIS\` |

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
