# Aşama 2 Test Raporu: Claude Entegrasyonu

**Tarih:** 2026-11-07
**Ortam:** Bulut Linux konteyneri, Python 3.13.18, anthropic SDK 2.6.0, PySide6 6.12.0 (ekransız)
**Sonuç:** 103/103 otomatik test geçti, art arda 14 tam çalıştırmada da. Gerçek API ile ücretli testler **kullanıcının bilgisayarında yapılacak** (aşağıya bak).

## Kapsam

| Bileşen | Dosya |
|---|---|
| Claude istemcisi (resmi SDK, akışlı yanıt) | `jarvis/ai/claude_client.py` |
| Model kayıt defteri ve doğrulanmış fiyatlar | `jarvis/ai/model_registry.py` |
| Model yönlendirici (yerel, ücretsiz) | `jarvis/ai/router.py` |
| Token, maliyet ve bütçe takibi (SQLite) | `jarvis/ai/usage.py`, `jarvis/storage/database.py` |
| Sistem istemi (önbelleklenen sabit blok + tarih) | `jarvis/ai/prompts.py` |
| API anahtarı (Windows Credential Manager) | `jarvis/security/credentials.py` |
| Sohbet akışı ve kısa süreli bağlam | `jarvis/core/chat_service.py`, `jarvis/core/conversation.py` |
| Arayüz: sohbet, Ayarlar, Konuşmalar, maliyet paneli | `jarvis/interface/*` |

## Nasıl test edildi?

Ücretsiz testler **gerçek anthropic SDK'sını** kullanıyor; yalnızca ağ katmanı sahte (`tests/fake_api.py`).
Böylece SDK'nın akış olayları, kullanım alanları ve hata sınıfları birebir sınanıyor, para harcanmıyor.

## Kabul testleri

| # | Test | Beklenen | Sonuç |
|---|---|---|---|
| 1 | Basit soru | Haiku 5.5 seçilir, cevap akışla gelir | ✅ |
| 2 | Karmaşık soru ("neden", "açıkla", kod, sorun) | Sonnet 5.5 | ✅ |
| 3 | Güncel bilgi ("bugün", "dolar kuru", "araştır") | Sonnet 5.5 + web araması, kaynaklar gösterilir | ✅ |
| 4 | "Derin analiz" | Opus 5.5 | ✅ |
| 5 | Türkçe büyük harf ("İNTERNETTEN ARAŞTIR") | Doğru tanınır | ✅ |
| 6 | Maliyet hesabı | Resmi fiyatlarla birebir (önbellek, Haiku uzun istem tarifesi, arama $0,01) | ✅ |
| 7 | Yedek model devreye girerse | Her model kendi fiyatıyla hesaplanır, kullanıcıya söylenir | ✅ |
| 8 | Bütçe aşıldı, "Haiku'ya düş" | Haiku kullanılır, ücretli web araması kapanır, uyarı gösterilir | ✅ |
| 9 | Bütçe aşıldı, "durdur" | İstek gönderilmez | ✅ |
| 10 | Bütçe aşıldı, "sor" | Onay penceresi; "Gönderme" denirse istek gitmez | ✅ |
| 11 | İnternet kesik | "İnternet yok" mesajı, Hata durumu, geçmişe yazılmaz; bağlantı dönünce kendini toparlar | ✅ |
| 12 | Hata türleri (401, 403, 404, 429, 529, 500, kredi bitti, 400) | Her biri kendi Türkçe mesajıyla | ✅ |
| 13 | Yeniden deneme | Sınırlı (1 + 2 deneme), sonsuz döngü yok | ✅ |
| 14 | Güvenlik reddi (refusal) | Kullanıcıya söylenir, faturalanan tutar kaydedilir, geçmişe eklenmez | ✅ |
| 15 | Sunucu aracı duraklaması (`pause_turn`) | Geçmiş yalnızca eklenerek sürdürülür | ✅ |
| 16 | Yanlış anahtar | Kaydedilmez, "anahtar geçersiz" denir | ✅ (sahte API'de **ve gerçek Anthropic sunucusunda**) |
| 17 | Anahtar ayar dosyasında / logda | Asla yok | ✅ |
| 18 | `ANTHROPIC_BASE_URL` ortam değişkeni | Anahtarı başka sunucuya yönlendiremez | ✅ |
| 19 | Aynı anda iki istek | İkincisi reddedilir, yazı kutusu Enter anında kilitlenir | ✅ |
| 20 | Konuşma geçmişi | Son 16 mesaj gönderilir, ilk mesaj daima kullanıcı; 30 dk sessizlikte yeni konuşma | ✅ |

## Ölçümler (Linux, ekransız)

| | Aşama 1 | Aşama 2 |
|---|---|---|
| Açılış | 159 ms | 273 ms (ilk hali 1.234 ms idi, düzeltildi: 4. hata) |
| Boşta CPU (tek çekirdek ölçeği) | %2,7 | %2,5 |
| RAM | ~96 MB | ~175 MB (anthropic SDK ~59 MB; sohbet için gerekli) |

## Bulunan ve düzeltilen hatalar

1. **Boş `anthropic-beta` başlığı.** Haiku isteklerinde boş bir başlık gidiyordu. Artık hiç gönderilmiyor.
2. **Yazı kutusu geç kilitleniyordu.** Enter ile modele gönderim arasındaki kısa sürede çift istek atılabiliyordu. Artık Enter anında kilitleniyor ve istek nasıl biterse bitsin yeniden açılıyor.
3. **Sıralama hatası.** "Cevap tamam" olayı maliyet panelinden önce yayınlanıyordu (testte ara sıra kalma). Artık önce maliyet güncelleniyor.
4. **Yavaş açılış (1,2 sn).** SDK açılışta yükleniyordu. Artık pencere açıldıktan sonra arka planda yükleniyor (0,27 sn).
5. **Bütçe onay penceresi `exec()` ile açılıyordu.** Bu, asyncio ve Qt döngüsünü iç içe kilitleyebilirdi. Artık engellemeyen `open()` kullanılıyor.
6. **Arayüz:** Sohbet alanı dar sıkışıyordu, uzun cevapta kaynak satırı görünmüyordu, para kutularında nokta kullanılıyordu, "Haiku %0" yanıltıcıydı. Hepsi düzeltildi.

**Açıkta kalan:** Oturum yeniden başlamadan önce `test_key_dialog_saves_verified_key` testi bir kez kaldı. Ayrıntısı kayboldu ve sonraki 14 tam çalıştırmada tekrar etmedi. Sebebi muhtemelen 3. hatadaki sıralama sorunuydu ama bunu kanıtlayamadım. İzlemeye devam.

## Tasarım kararı: konuşma geçmişi yalnızca metin

Claude'un yeni modellerinde (Haiku/Sonnet/Opus 5.5) düşünme blokları, onları üreten konuşmaya bağlıdır. 31 Ağustos 2026 sonrası açılan hesaplarda, geçmişi düzenlenmiş bir istek 400 hatası alır.
JARVIS geçmişi kırptığı ve modeller arasında geçtiği için düşünme bloklarını **geri göndermez**; geçmişe yalnızca metin yazar. Bedeli küçüktür: model önceki turların gizli akıl yürütmesini görmez, cevapları görür.

## Kullanıcının bilgisayarında yapılacak canlı test (bekliyor)

Bu bulut ortamı Anthropic adresleri için kullanıcı anahtarı kabul etmiyor. Gerçek API testi Windows'ta yapılacak:

1. `python -m jarvis` ile aç, **Ayarlar > Claude > Anahtarı gir**.
2. `python -m pytest -m live -v -s` (5 test, toplam yaklaşık **$0,05–0,10**).

Canlı testlerin kontrol ettikleri:
- Anahtar geçerli mi?
- Haiku Türkçe cevap veriyor mu?
- Sistem istemi ikinci istekte önbellekten okunuyor mu?
- Sonnet web'de arayıp kaynak gösteriyor mu?
- Basit bir soru uçtan uca Haiku'ya mı gidiyor?
