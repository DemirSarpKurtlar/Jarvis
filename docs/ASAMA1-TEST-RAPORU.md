# Aşama 1 Test Raporu: Uygulama Temeli

**Tarih:** 2026-11-07
**Ortam:** Bulut Linux konteyneri, Python 3.13.18, PySide6 6.12.0, Qt `offscreen` platformu (ekransız)
**Sonuç:** 41/41 otomatik test geçti (art arda 4 çalıştırmada da). Windows üzerinde doğrulama **henüz yapılmadı** (aşağıya bak).

## Aşama 1 kapsamı

| Bileşen | Dosya | Durum |
|---|---|---|
| Proje yapısı, paketleme | `pyproject.toml`, `requirements*.txt` | ✅ `pip install -e .` çalışıyor, `jarvis` komutu oluşuyor |
| Ayar yönetimi | `jarvis/config/settings.py` | ✅ |
| Loglama + gizli bilgi maskeleme | `jarvis/core/logging_setup.py` | ✅ |
| Hata yakalama (ana thread, yan thread, asyncio) | `jarvis/core/errors.py` | ✅ |
| Olay veriyolu | `jarvis/core/events.py` | ✅ |
| 7 durumlu durum makinesi | `jarvis/core/state.py` | ✅ |
| Gerçek sistem izleme | `jarvis/system/monitor.py` | ✅ (GPU: bu ortamda NVIDIA yok, nedeni gösteriliyor) |
| PySide6 arayüzü (taslağa sadık) | `jarvis/interface/` | ✅ |
| Temiz başlatma/kapatma | `jarvis/app.py` | ✅ |

## Kabul testleri

| # | Test | Beklenen | Gerçekleşen | Sonuç |
|---|---|---|---|---|
| 1 | Uygulama açılışı | Pencere açılır, log'a "Arayüz hazır" yazılır | Açılış 159 ms | ✅ |
| 2 | Temiz kapanış | Görevler durur, ayarlar kaydedilir, çıkış kodu 0 | Çıkış kodu 0, log'da hata yok | ✅ (ilk denemede ❌, düzeltildi: 1. hata) |
| 3 | Bozuk ayar dosyası | Çökmez, dosyayı yedekler, kullanıcıya söyler | `settings.bozuk-<zaman>.json` oluştu, sarı uyarı bandı göründü | ✅ |
| 4 | API anahtarı loga sızmaz | Hata izi dahil maskelenir | `sk-ant-***` olarak yazıldı | ✅ |
| 5 | Yan thread çökmesi | Gizlenmez; loglanır, arayüz Hata durumuna geçer | Kırmızı bant + Hata durumu | ✅ |
| 6 | 7 durumun hepsi çizilir | Her durum hatasız çizilir | Hepsi çizildi | ✅ |
| 7 | Geçersiz durum geçişi | Hata fırlatır, sessizce geçmez | `InvalidStateTransition` | ✅ |
| 8 | Mikrofon kapatma kalıcı | Ayar diske yazılır, yeniden açılışta korunur | Korundu | ✅ |
| 9 | Arayüz akıcılığı | Sistem izlenirken döngü gecikmesi < 150 ms | Test geçti | ✅ |
| 10 | Boşta CPU | Düşük | %16,4 → **%2,7** (tek çekirdek ölçeği) | ✅ (ilk ölçüm yüksekti, düzeltildi: 3. hata) |
| 11 | Boşta RAM | Makul | ~96 MB | ✅ |
| 12 | Pencere gizliyken animasyon | Zamanlayıcı durur | Durdu | ✅ |

Ölçümler yazılımsal (offscreen) çizimle alındı. Windows'taki değerler farklı olabilir ve orada ayrıca ölçülecek.

## Bulunan ve düzeltilen hatalar

1. **Kapanış yarım kalıyordu.** Qt, son pencere kapanınca olay döngüsünü asistan kapanmadan durduruyordu (`RuntimeError: Event loop stopped before Future completed`).
   *Düzeltme:* `setQuitOnLastWindowClosed(False)` ve `close_requested` sinyali eklendi. Artık önce asistan kapanıyor, sonra uygulama çıkıyor. Uygulama ve testler aynı `configure_application()` fonksiyonunu kullanıyor.
2. **Değişken fontlar en ince ağırlıkta açılıyordu.**
   *Düzeltme:* Değişken fontlardan sabit ağırlıklı dosyalar üretildi (`jarvis/assets/fonts/README.md`).
3. **Boşta CPU yüksekti (%16).**
   *Düzeltme:* Sabit katmanlar (ölçek, halka, parıltı, yazı) önbelleğe alındı, beklemede kare hızı 8 fps'e indirildi.
4. **Dinleme animasyonu halkadan taşıyordu.**
   *Düzeltme:* Gürültü fonksiyonu 0–1 aralığına sınırlandı.
5. **`QApplication` olmadan font yükleme segfault veriyordu** (testte bulundu).
   *Düzeltme:* Çökme yerine anlaşılır bir `RuntimeError` veriliyor.

## Dürüstlük notları

- Arayüzde henüz çalışmayan özellikler (yazılı sohbet, Konuş düğmesi, Hafıza/Ayarlar ekranları) **sahte içerik göstermiyor**. Hangi aşamada geleceklerini açıkça yazıyor ve devre dışı duruyorlar.
- "Mikrofon: izinli" yalnızca bir tercih. Ses motoru Aşama 3'te eklenecek; şu an mikrofon **dinlenmiyor**.
- Claude paneli "Kurulmadı" gösteriyor. Token ve maliyet sayıları Aşama 2'de gerçek verilerle gelecek.

## Windows'ta yapılacak doğrulama (bekliyor)

- [ ] `python -m pytest` Windows'ta 41/41 geçiyor mu?
- [ ] Pencere gerçek ekranda doğru ölçekleniyor mu (yüksek DPI)?
- [ ] NVIDIA GPU sıcaklığı, kullanımı ve belleği gerçek değerlerle görünüyor mu?
- [ ] Disk satırı `C:` olarak görünüyor mu?
- [ ] Boşta CPU ve RAM ölçümü
- [ ] Ayar ve log dosyaları `%APPDATA%` ve `%LOCALAPPDATA%` altında oluşuyor mu?
