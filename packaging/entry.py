"""JARVIS giriş betiği.

İki yerde kullanılır:
* PyInstaller bunu exe'nin giriş noktası yapar (`packaging/jarvis.spec`).
* Kaynaktan çalışırken "Windows açılınca başlat" bu betiği `pythonw.exe` ile
  çağırır; bu yüzden depo kökünü kendisi `sys.path`'e ekler (çalışma klasörü
  ne olursa olsun `jarvis` paketi bulunur).
"""

import sys
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
