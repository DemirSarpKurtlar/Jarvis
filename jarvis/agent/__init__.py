"""Bilgisayar kullanan ajan: Claude ekrana bakarak fare ve klavyeyle uygulamaları kullanır.

- `geometry`: ekran görüntüsü <-> fiziksel piksel ölçekleme (çoklu ekran, DPI)
- `imaging`: yakalanan kareyi küçültme, kırpma (zoom), JPEG/PNG kodlama, değişim ölçümü
- `keys`: "ctrl+shift+Escape" gibi tuş adlarını Windows sanal tuş kodlarına çevirme
- `win32`: Windows arka uçları (GDI yakalama, SendInput, ön plan penceresi, Esc Esc acil durdurma)
- `safety`: eylem korumaları (UAC/güvenlik/banka pencereleri, gizli bilgi yazma, oyun içi oynama yasağı)
- `actions`: Claude'un bilgisayar araç seti üyelerini (screenshot, left_click, type...) yürütür
- `session`: tek bir hedef için Claude ile ekran-eylem döngüsü (bütçe, adım ve süre sınırı)
- `service`: oturumu sohbete ve araç kaydına bağlar (`use_computer` aracı, olaylar, durdurma)
"""
