# Uyandırma kelimesi: "Hey Jarvis" ve "Jarvis"

JARVIS'i uyandırmak için iki yol var, ikisi de tamamen bilgisayarında çalışır:

| Söylediğin | Nasıl algılanır | Eşik (Ayarlar > Ses) |
|---|---|---|
| "Hey Jarvis" | openWakeWord'ün hazır `hey_jarvis` modeli | Uyandırma hassasiyeti (varsayılan 0,5 hassasiyet = 0,5 eşik) |
| Tek başına "Jarvis" | Aynı model, "Jarvis" kelimesinde de skor üretir ama genelde daha düşük. İkinci, daha düşük bir eşik art arda **en az 2 karede** (160 ms) aşılırsa uyanır | `jarvis_threshold` (varsayılan 0,35) |

Uyandıktan sonra 1,5 saniye yeni bir uyandırma kabul edilmez (aynı sözün kuyruğu tekrar tetiklemesin).
Hiçbiri çalışmazsa **Konuş** düğmesi her zaman çalışır.

## Bilinen davranışlar

* Hazır model İngilizce eğitildi. Türkçe konuşurken "Carvis" ya da "Cervis" gibi söylenişler daha düşük skor alabilir.
  Hassasiyeti biraz artırmak ya da aşağıdaki gibi kendi modelini eğitmek işe yarar.
* Benzer sesli kelimeler ("Hey Travis" gibi) bazen uyandırabilir. Yanlış uyanmalar artarsa hassasiyeti düşür.
* Model dosyaları ilk açılışta GitHub'dan indirilir (yaklaşık 3,7 MB) ve
  `%LOCALAPPDATA%\JARVIS\models\openwakeword\` altında tutulur.

## Kendi "Jarvis" modelini eğitmek

openWakeWord, gerçek ses kaydı gerektirmeden, metinden üretilen sentetik seslerle yeni bir kelime modeli eğitebilir.
Eğitim JARVIS'in içinde değil, ücretsiz bir Google Colab defterinde yapılır; sonuçta tek bir `.onnx` dosyası çıkar.

1. openWakeWord deposundaki eğitim bağlantısını aç: README dosyasının "Training New Models" bölümündeki
   **Google Colab** defteri (hızlı, kolay) ya da daha ayrıntılı `notebooks/automatic_model_training.ipynb`.
2. Hedef kelime olarak `jarvis` yaz. Türkçe söyleyişi de yakalamak için, defter izin veriyorsa
   ek yazımlar ekle: `carvis`, `cervis`, `jarviss`.
3. Defteri sonuna kadar çalıştır (Colab'ın ücretsiz GPU'suyla genelde 1 saatten kısa sürer).
   Sonunda indirilecek dosyalardan **`.onnx` uzantılı** olanı al (`.tflite` değil; Windows'ta tflite çalışmaz).
4. JARVIS'te **Ayarlar > Ses > Özel uyandırma modeli > Özel model seç…** ile bu dosyayı göster,
   sonra **Ses motorunu yeniden başlat**'a bas.

Özel model, hazır "Hey Jarvis" modelinin **yanında** çalışır (onu kapatmaz). Özel modelin skoru hassasiyet
eşiğini tek karede ya da `jarvis_threshold` eşiğini art arda 2 karede aşınca uyanır.

### Teknik ayrıntılar

* JARVIS, openWakeWord'ün akış hattını onnxruntime ile birebir uygular (`jarvis/voice/wake_word.py`):
  80 ms'lik her karede melspektrogram, 96 boyutlu gömme ve son 16 gömme ile kelime modeli.
  Paketin kendisiyle karşılaştırıldığında skorlar arasındaki en büyük fark 0,00005'tir.
* Bu yüzden özel model, openWakeWord'ün ortak özellik çıkarıcısıyla (`melspectrogram.onnx` ve
  `embedding_model.onnx`, v0.5.1) eğitilmiş olmalı. Eğitim defterleri bunu zaten kullanır.
* Modelin girişi `[1, N, 96]` biçiminde olmalı (genelde N = 16). JARVIS, N'yi dosyadan okur.

## Gizlilik

Uyandırma kelimesi dinlenirken ses yalnızca bellekte, 80 ms'lik karelerle işlenir; diske yazılmaz ve
hiçbir yere gönderilmez. Mikrofon düğmesi (ya da Ctrl+Shift+M) mikrofon akışını tamamen kapatır.
