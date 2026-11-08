# Aşama 5 Test Raporu: Windows Araçları ve Güvenlik Katmanı

**Tarih:** 2026-11-07
**Ortam:** Bulut Linux konteyneri, Python 3.13.18, PySide6 6.12.0 (ekransız), pypdf 7.0.0, python-docx 1.2.0, send2trash 2.1.0
**Sonuç:** 377 test geçti (Aşama 5'ten 274 yeni test), art arda 3 tam çalıştırmada da. 10 gerçek Windows testi bu ortamda
atlandı; GitHub Actions `windows-latest` ya da kullanıcının bilgisayarında `python -m pytest -m windows` ile çalışır.

## Kapsam

| Bileşen | Dosya |
|---|---|
| Araç kaydı, strict JSON şema, sonuç boyutu sınırı | `jarvis/tools/registry.py`, `jarvis/tools/schema.py` |
| İzin seviyeleri ve onay isteği | `jarvis/security/permission_manager.py` |
| Yol korumalı alanı, URL/ana makine doğrulama | `jarvis/security/action_validator.py` |
| Denetim kaydı (SQLite `audit_log`) | `jarvis/security/audit_logger.py` |
| Uygulamalar, oyunlar (Steam), başlangıç uygulamaları | `jarvis/tools/applications.py` |
| İşlemler, donanım, sistem bilgisi | `jarvis/tools/processes.py`, `hardware.py`, `system_info.py` |
| Dosyalar | `jarvis/tools/files.py` |
| Ses, Ayarlar, web, kilit, kapatma, onaylı komutlar | `jarvis/tools/windows_actions.py`, `winapi.py` |
| Uygulama içi zamanlayıcı | `jarvis/tools/scheduler.py` |
| Yerel Türkçe komut ayrıştırıcı | `jarvis/tools/intents.py` |
| Asistana bağlantı | `jarvis/core/tool_service.py` |
| Sistem ekranı ve onay penceresi | `jarvis/interface/system_view.py`, `jarvis/interface/dialogs.py` |

## Nasıl test edildi?

Linux'ta Windows'a özgü arka uçlar (pencereler, ses, açıcı, kayıt defteri) sahteleriyle değiştirildi (`tests/tool_fakes.py`);
araçların izin, doğrulama, onay ve denetim mantığı gerçek kodla sınandı. Dosya işlemleri gerçek geçici klasörlerde,
işlem sonlandırma gerçek alt işlemlerle, PDF/Word okuma gerçek dosyalarla yapıldı. Geri Dönüşüm Kutusu taklit edildi.

## Kabul testleri

| # | Test | Sonuç |
|---|---|---|
| 1 | "Jarvis, Notepad'i aç": Claude API'ye hiç istek gitmeden Not Defteri açılır, sohbette "Yerel komut, $0" | ✅ (sahte açıcıyla; gerçek Windows'ta `test_launch_and_close_notepad`) |
| 2 | 77 Türkçe komut kalıbı doğru araca çevrilir; 21 belirsiz/çok adımlı/açıklama isteyen cümle Claude'a bırakılır | ✅ |
| 3 | "not defterini", "hesap makinesini", "tarayıcıyı", "discordu", "spotifyı" gibi ekli adlar doğru uygulamayı bulur | ✅ |
| 4 | Yol kaçışları (`..`, UNC, `\\?\`, sembolik bağ, NTFS veri akışı, CON/NUL, sistem klasörleri, AppData) reddedilir | ✅ |
| 5 | Geçersiz girdi (eksik/fazla alan, sınır dışı sayı, bool yerine sayı...) işleyiciye hiç ulaşmaz | ✅ |
| 6 | CONFIRM araçları: onay yoksa/reddedilirse/arayüz çökerse çalışmaz; onaylanırsa çalışır; hepsi denetim kaydında | ✅ |
| 7 | Kalıcı silme kırmızı uyarı ve "Kalıcı olarak sil" düğmesiyle sorulur; varsayılan düğme "Vazgeç" | ✅ |
| 8 | Silme varsayılan olarak Geri Dönüşüm Kutusu'na gider; kökler ve kök dışı hiçbir zaman silinmez | ✅ |
| 9 | Klasör düzenleme önce önizleme verir, hiçbir şey taşımaz; uygulama onayla, çakışmada yeni ad verilir | ✅ |
| 10 | Kritik işlemler (lsass, csrss, Defender...), PID 0-4, JARVIS'in kendisi, başka kullanıcının işlemleri sonlandırılamaz; PID ile ad uyuşmazsa sonlandırılmaz | ✅ |
| 11 | Onaylı komutlar: yalnızca listedeki adlar; ping hedefine `&&`, `;`, `$()` eklenemez | ✅ |
| 12 | Salt okunur modda yalnızca okuma araçları çalışır | ✅ |
| 13 | Zamanlanmış görev onay gerektiren bir şeyi asla çalıştırmaz | ✅ |
| 14 | Dosya/komut çıktısı Claude'a "güvenilmeyen içerik" notuyla sarılı gider; sonuç 12.000 karakteri aşmaz | ✅ |
| 15 | Denetim kaydında parola/anahtar/token görünmez, belge içeriği yalnızca uzunluk olarak yazılır | ✅ |
| 16 | Komut satırındaki API anahtarı işlem ayrıntısında maskelenir | ✅ |
| 17 | Windows dışında Windows araçları "bu işletim sisteminde desteklenmiyor" der; uydurma uygulama listesi yok | ✅ |
| 18 | GPU fanı okunamıyorsa "okunamıyor", CPU sıcaklığı güvenilir sensör yoksa "desteklenmiyor" | ✅ |
| 19 | Sistem ekranı: işlem tablosu sayısal sıralanır, "Sonlandır" onay penceresi açar, Vazgeç'te işlem yaşar, onayda kapanır | ✅ |
| 20 | Tüm 44 aracın şeması strict uyumlu; Claude listesinde sınır anahtarları yok, sıra sabit | ✅ |

## Windows'ta bekleyen testler (`pytest -m windows`)

Bilinen klasörler, uygulama keşfi (Not Defteri ve Hesap Makinesi bulunur), Not Defteri'ni açıp nazikçe kapatma,
ses düzeyi gidiş-dönüş (ses aygıtı yoksa nedeniyle atlanır), Windows Credential Manager, işlem listesi ve sistem
bilgisi, Sistem olay günlüğü, başlangıç uygulamaları ve Steam, `ipconfig /all`, pencere listesi.

## Bilinen sınırlar

- Mağaza (UWP) uygulamalarının pencereleri `ApplicationFrameHost` işlemine ait olduğundan (örn. Hesap Makinesi)
  nazik kapatma pencere bulamayabilir; bu durumda JARVIS zorla kapatmayı yalnızca onayla önerir.
- Oyun keşfi Steam ve Başlat menüsü kısayollarıyla sınırlı (Epic, Xbox uygulaması ayrıca taranmıyor).
- Zamanlayıcı yalnızca JARVIS açıkken çalışır, görevler diske yazılmaz.
- "Bluetooth'u aç" Ayarlar sayfasını açar; radyo düğmesini JARVIS değiştirmez.
- pywin32/pycaw/comtypes Windows tekerlekleri (py3.12 win_amd64) doğrulandı ama bu ortamda çalıştırılamadı.
