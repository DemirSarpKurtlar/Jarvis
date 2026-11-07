"""Uygulamanın dosya yolları.

Varsayılan olarak işletim sisteminin standart klasörleri kullanılır
(Windows'ta %APPDATA%\\JARVIS ve %LOCALAPPDATA%\\JARVIS). `JARVIS_HOME`
ortam değişkeni verilirse her şey o klasörün altına toplanır; testler ve
taşınabilir kurulum bunu kullanır.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_config_dir, user_data_dir, user_log_dir

from jarvis import APP_NAME

ASSETS_DIR = Path(__file__).resolve().parent / "assets"


@dataclass(frozen=True)
class AppPaths:
    config_dir: Path
    data_dir: Path
    log_dir: Path

    @property
    def settings_file(self) -> Path:
        return self.config_dir / "settings.json"

    @property
    def log_file(self) -> Path:
        return self.log_dir / "jarvis.log"

    @classmethod
    def resolve(cls) -> "AppPaths":
        home = os.environ.get("JARVIS_HOME")
        if home:
            root = Path(home).expanduser().resolve()
            paths = cls(root / "config", root / "data", root / "logs")
        else:
            paths = cls(
                Path(user_config_dir(APP_NAME, appauthor=False, roaming=True)),
                Path(user_data_dir(APP_NAME, appauthor=False)),
                Path(user_log_dir(APP_NAME, appauthor=False)),
            )
        paths.ensure()
        return paths

    def ensure(self) -> None:
        for d in (self.config_dir, self.data_dir, self.log_dir):
            d.mkdir(parents=True, exist_ok=True)
