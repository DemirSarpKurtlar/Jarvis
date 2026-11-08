# JARVIS Kurulum Rehberi

Bu belge JARVIS'in kurulum programıyla kurulmasını, ilk çalıştırmayı, verilerin nerede durduğunu,
Windows ile başlatmayı, yükseltmeyi, kaldırmayı ve kaynaktan derlemeyi anlatır.

## 1. Kurulum programıyla kurulum

1. `JARVIS-Kurulum-<sürüm>.exe` dosyasını çalıştır (nereden alınacağı için bkz. [CI yapıtları](#8-ci-yapıtları)).
2. **Yönetici izni istenmez.** JARVIS yalnızca senin kullanıcı hesabına kurulur:
   `%LOCALAPPDATA%\Programs\JARVIS` (ör. `C:\Users\demir\AppData\Local\Programs\JARVIS`).
3. Kurulum sırasında iki isteğe bağlı seçenek var (ikisi de varsayılan olarak kapalı):
   * **Masaüstü kısayolu oluştur**
   * **Windows açılınca JARVIS'i başlat** (sonradan Ayarlar'dan da açılıp kapatılabilir)
4. Başlat menüsüne her zaman bir **JARVIS** kısayolu eklenir.

İmzasız bir kurulum dosyası olduğu için Windows SmartScreen "Windows bilgisayarınızı korudu" diyebilir:
**Ek bilgi > Yine de çalıştır**.

Sessiz kurulum (yöneticiler ve otomasyon için):

```powershell
JARVIS-Kurulum-1.0.0.exe /VERYSILENT /SUPPRESSMSGBOXES /CURRENTUSER /NORESTART
# Windows açılınca başlatma görevini de seçmek için:
JARVIS-Kurulum-1.0.0.exe /VERYSILENT /SUPPRESSMSGBOXES /CURRENTUSER /TASKS=autostart
```

## 2. İlk çalıştırma: Claude API anahtarı

JARVIS ilk açıldığında üstte "Claude API anahtarı girilmedi" uyarısı çıkar.

1. **Anahtarı gir** düğmesine ya da **Ayarlar > Claude bağlantısı > Anahtarı gir**'e tıkla.
2. Anahtarı [Anthropic Console](https://platform.claude.com) > **API Keys** bölümünden al (`sk-ant-` ile başlar).
3. Anahtar önce Anthropic'e ücretsiz bir istekle doğrulanır, sonra **Windows Credential Manager**'a kaydedilir.
   Ayar dosyasına, loglara ya da veritabanına asla yazılmaz.

Claude Pro/Max aboneliği API kullanımını kapsamaz; API ayrı faturalanır. Bütçe limitleri **Ayarlar > Bütçe**'de.

## 3. Veriler nerede?

| Ne | Konum |
|---|---|
| Program dosyaları | `%LOCALAPPDATA%\Programs\JARVIS\` |
| Ayarlar | `%APPDATA%\JARVIS\settings.json` |
| Veritabanı (konuşmalar, kullanım/maliyet kayıtları) | `%LOCALAPPDATA%\JARVIS\jarvis.db` |
| Loglar | `%LOCALAPPDATA%\JARVIS\Logs\jarvis.log` |
| Claude API anahtarı | Windows Credential Manager > Windows Kimlik Bilgileri > `JARVIS` |
| "Windows açılınca başlat" kaydı | `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` > `JARVIS` |

Bu klasörleri **Ayarlar > Dosyalar** bölümünde de görebilirsin. `JARVIS_HOME` ortam değişkeni verilirse
ayarlar, veri ve loglar o klasörün altında toplanır (taşınabilir kullanım).

## 4. Windows açılınca başlatma ve bildirim alanı

* **Ayarlar > Başlangıç ve pencere > Windows açılınca başlat**: işaretlediğin anda geçerli olur, yeniden
  başlatma gerekmez. Yönetici izni gerekmez; yalnızca senin hesabını etkiler.
* Windows açılışında JARVIS pencere açmadan **bildirim alanında** (saatin yanındaki simgeler) başlar.
  Simgeye tıklayınca pencere açılır.
* Bildirim alanı simgesinin menüsü: **Pencereyi göster/gizle**, **Mikrofonu kapat**, **Tamamen kapat**.
* **Pencereyi kapatınca bildirim alanında çalışmaya devam et** seçeneği varsayılan olarak kapalıdır:
  pencereyi kapatmak JARVIS'i kapatır. Seçeneği açarsan pencere kapatılınca JARVIS arka planda çalışır;
  tamamen kapatmak için soldaki **Kapat** düğmesini ya da simge menüsündeki **Tamamen kapat**'ı kullan.
* Görev Yöneticisi > Başlangıç uygulamaları'ndan devre dışı bırakırsan JARVIS bunu görür ve ayarı kapalı
  gösterir. JARVIS ayarlarından yeniden açarsan Görev Yöneticisi'ndeki işaret de kaldırılır.
* JARVIS ikinci kez başlatılırsa yeni bir kopya açılmaz; çalışan pencere öne gelir.

## 5. Yükseltme

Yeni sürümün kurulum programını çalıştırman yeterli. Aynı klasöre, ayarlarına ve verilerine dokunmadan kurulur.
JARVIS açıksa kurulum programı onu önce temizce kapatır (`JARVIS.exe --kapat`). Eski sürümden kalan
kütüphaneler (`_internal` klasörü) silinip yenileri kopyalanır. Önceki kurulumdaki seçimlerin
(masaüstü kısayolu, Windows ile başlat) hatırlanır.

## 6. Kaldırma

**Ayarlar > Uygulamalar > Yüklü uygulamalar > JARVIS > Kaldır** (ya da Denetim Masası > Programlar).

* Program dosyaları, Başlat menüsü/masaüstü kısayolları ve "Windows açılınca başlat" kaydı her zaman silinir.
* Kaldırıcı **"JARVIS'in kişisel verileri de silinsin mi?"** diye sorar. Varsayılan cevap **Hayır**:
  ayarların, konuşma geçmişin, logların ve API anahtarın bilgisayarda kalır; yeniden kurunca kaldığın
  yerden devam edersin. **Evet** dersen `%APPDATA%\JARVIS`, `%LOCALAPPDATA%\JARVIS` ve Credential
  Manager'daki anahtar da silinir.
* Sessiz kaldırma verileri korur; verileri de silmek için `/DELETEUSERDATA=1` ver:

```powershell
& "$env:LOCALAPPDATA\Programs\JARVIS\unins000.exe" /VERYSILENT /SUPPRESSMSGBOXES
& "$env:LOCALAPPDATA\Programs\JARVIS\unins000.exe" /VERYSILENT /SUPPRESSMSGBOXES /DELETEUSERDATA=1
```

## 7. Kaynaktan derleme

Gerekenler: Windows 10/11 (64 bit), Python **3.12**, [Inno Setup 6](https://jrsoftware.org/isdl.php)
(`choco install innosetup -y` ile de kurulabilir).

```powershell
cd C:\Users\<adın>\Projeler\Jarvis
py -3.12 -m venv .venv
.venv\Scripts\activate
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Install
```

`build.ps1` sırasıyla: bağımlılıkları kurar (`-Install`), testleri çalıştırır (`-SkipTests` ile atlanır),
PyInstaller ile `dist\JARVIS\JARVIS.exe`'yi üretir, exe'yi 5 saniyelik duman testinden geçirir ve Inno Setup ile
`dist\installer\JARVIS-Kurulum-<sürüm>.exe`'yi derler (`-SkipInstaller` ile atlanır).

Parçalar:

| Dosya | Görevi |
|---|---|
| `packaging/jarvis.spec` | PyInstaller tarifi (tek klasör, konsolsuz, simge, sürüm bilgisi). Ses ve Windows aracı paketleri kuruluysa veri dosyalarını ve DLL'lerini otomatik ekler. |
| `packaging/entry.py` | exe'nin giriş noktası; kaynaktan otomatik başlatmada da kullanılır |
| `packaging/jarvis.iss` | Inno Setup betiği (UTF-8 BOM ile kaydedilmeli) |
| `packaging/requirements-build.txt` | PyInstaller |
| `scripts/make_icon.py` | `jarvis/assets/icon/jarvis.ico` ve `jarvis.png`'yi yeniden üretir |
| `scripts/version_info.py` | Sürümü `jarvis/__init__.py`'den okur (kurulum ve exe sürüm bilgisi buradan gelir) |
| `scripts/smoke_run.py` | Uygulamayı kısa süre çalıştırıp logda hata olmadığını doğrular |
| `scripts/instance_check.py` | İkinci açılışın çalışan pencereyi öne getirdiğini ve `--kapat`'ı doğrular |

Sürümü değiştirmek için yalnızca `jarvis/__init__.py` içindeki `__version__` güncellenir.

Komut satırı seçenekleri:

| Seçenek | Ne yapar |
|---|---|
| `--baslangic` | Windows açılışında kullanılır: pencereyi göstermeden bildirim alanında başlar |
| `--kapat` | Çalışan JARVIS'i temizce kapatır ve kapanmasını bekler (kurulum/kaldırma programı kullanır) |
| `--anahtari-sil` | Credential Manager'daki Claude API anahtarını siler (kaldırıcı, "verileri sil" seçilince kullanır) |
| `--smoke-test SANİYE` | Verilen süre sonra kendini kapatır (otomatik testler) |

## 8. CI yapıtları

`main` ve `claude/**` dallarına her gönderimde GitHub Actions (`.github/workflows/windows.yml`) çalışır:

* **Linux**: tüm testler.
* **Windows**: tüm testler (`windows` işaretliler dahil), ses modeli testleri (`-m models`, modeller önbellekten),
  kaynaktan ve paketlenmiş exe'den duman testi ve tek örnek testi, kurulum programının derlenmesi, sessiz kurulum,
  kurulu exe'nin çalıştırılması, JARVIS açıkken yükseltme, kaldırma (verilerin kaldığı doğrulanır), yeniden
  kurulum ve verilerle birlikte kaldırma.

Çalıştırma bitince sayfanın altındaki **Artifacts** bölümünden indirilebilir:

| Yapıt | İçerik |
|---|---|
| `JARVIS-kurulum` | `JARVIS-Kurulum-<sürüm>.exe` |
| `JARVIS-pyinstaller` | Kurulumsuz çalışan `JARVIS\` klasörü (`JARVIS.exe` + `_internal\`) |
| `ci-loglari` | pytest sonuçları (JUnit XML), uygulama logları, kurulum/kaldırma logları, PyInstaller uyarıları |

Yapıtlar 14 gün saklanır. Elle çalıştırmak için: GitHub > Actions > CI > **Run workflow**.
