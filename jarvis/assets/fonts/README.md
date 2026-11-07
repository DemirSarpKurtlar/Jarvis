# Fontlar

| Dosya | Kaynak | Lisans |
|---|---|---|
| `Saira-*.ttf` | [Saira](https://github.com/Omnibus-Type/Saira), Google Fonts | SIL Open Font License 1.1 (`OFL-Saira.txt`) |
| `IBMPlexSans-*.ttf` | [IBM Plex Sans](https://github.com/IBM/plex), Google Fonts | SIL Open Font License 1.1 (`OFL-IBMPlexSans.txt`) |

Google Fonts deposundaki değişken (variable) fontlardan `fontTools.varLib.instancer`
ile sabit ağırlıklar üretildi (genişlik 100): Saira 300/400/500, IBM Plex Sans 400/500/600.
Sebep: Qt değişken fontlarda ağırlık eksenini her platformda güvenilir seçmiyor.
Ad tablosunda tipografik aile (ID 16) ortak aile adına, alt aile (ID 17) ağırlık adına ayarlandı.
