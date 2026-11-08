"""Kullanıcı ayarları.

Ayarlar JSON olarak saklanır ve pydantic ile doğrulanır. Dosya bozuksa ya da
geçersiz değer içeriyorsa sessizce yutulmaz: dosya `settings.bozuk-<zaman>.json`
adıyla yedeklenir, varsayılanlar yüklenir ve `SettingsStore.load_warning`
doldurulur ki arayüz kullanıcıya söyleyebilsin.

Gizli bilgiler (API anahtarı vb.) ASLA bu dosyada tutulmaz; onlar Windows
Credential Manager'a gider (bkz. Aşama 2, `security/credentials.py`).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jarvis.config.constants import (
    DEFAULT_MODEL_ADVANCED,
    DEFAULT_MODEL_LIGHT,
    DEFAULT_MODEL_MAIN,
)

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


class _Section(BaseModel):
    # Bilinmeyen anahtarlar (eski/yeni sürümden kalma) yok sayılır, hata vermez.
    model_config = ConfigDict(extra="ignore", validate_assignment=True)


class GeneralSettings(_Section):
    autostart: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


class UISettings(_Section):
    start_minimized: bool = False
    reduced_motion: bool = False
    # Pencereyi kapatınca bildirim alanında çalışmaya devam et (varsayılan: kapat = çık)
    close_to_tray: bool = False
    # Dinlerken ekranın üstünde gösterge (interface/overlay.py)
    overlay_enabled: bool = True


class AISettings(_Section):
    model_light: str = Field(DEFAULT_MODEL_LIGHT, min_length=1)
    model_main: str = Field(DEFAULT_MODEL_MAIN, min_length=1)
    model_advanced: str = Field(DEFAULT_MODEL_ADVANCED, min_length=1)
    preferred_model: Literal["auto", "light", "main", "advanced"] = "auto"
    max_retries: int = Field(2, ge=0, le=5)
    web_search_enabled: bool = True


class BudgetSettings(_Section):
    daily_limit_usd: float = Field(0.50, ge=0)
    monthly_limit_usd: float = Field(5.00, ge=0)
    warn_at_ratio: float = Field(0.8, gt=0, le=1)
    # Limit dolunca: ekonomik modele düş / tamamen durdur / her seferinde sor
    on_limit: Literal["fallback_light", "block", "ask"] = "fallback_light"


class VoiceSettings(_Section):
    microphone_enabled: bool = True
    input_device: str | None = None
    output_device: str | None = None
    volume: int = Field(70, ge=0, le=100)
    speech_rate: float = Field(1.0, ge=0.5, le=2.0)
    wake_words: list[str] = Field(default_factory=lambda: ["hey_jarvis", "jarvis"])
    wake_sensitivity: float = Field(0.5, ge=0.0, le=1.0)
    # --- Aşama 3: ses motoru (yalnızca alan eklendi; hepsinin güvenli varsayılanı var) ---
    # Tek başına "Jarvis" için hazır modelin (daha düşük) eşiği; özel model verilirse ona da uygulanır
    jarvis_threshold: float = Field(0.35, ge=0.05, le=1.0)
    # Kullanıcının eğittiği openWakeWord .onnx modeli (docs/UYANDIRMA-KELIMESI.md)
    custom_wake_model_path: str | None = None
    # "auto": CUDA varsa large-v3-turbo, yoksa small. Ya da faster-whisper model adı
    stt_model: str = Field("auto", min_length=1)
    stt_device: Literal["auto", "cpu", "cuda"] = "auto"
    tts_voice: str = Field("tr_TR-dfki-medium", min_length=1)
    silence_timeout_s: float = Field(0.8, ge=0.3, le=3.0)
    max_utterance_s: float = Field(20.0, ge=3.0, le=60.0)
    no_speech_timeout_s: float = Field(6.0, ge=2.0, le=30.0)
    follow_up_s: float = Field(8.0, ge=0.0, le=30.0)   # 0: konuşma modu kapalı
    barge_in: bool = True                               # konuşurken söz kesilebilsin
    wake_chime: bool = True                             # uyanınca kısa bir ses çal
    noise_floor_dbfs: float | None = Field(None, ge=-120.0, le=0.0)  # ortam gürültüsü ölçümü
    # Türkçe telaffuz ("Carvis"): uyandırma skoru düşük kalan kısa konuşmaları konuşma tanımayla doğrula
    wake_verify: bool = True
    wake_verify_floor: float = Field(0.03, ge=0.001, le=0.9)


class MemorySettings(_Section):
    enabled: bool = True


# --- Aşama 5: araçlar ---
class ToolSettings(_Section):
    # Dosya araçlarının varsayılan kullanıcı klasörlerine ek olarak erişebileceği klasörler
    extra_allowed_roots: list[str] = Field(default_factory=list)
    # Açıkken yalnızca okuma (READ) araçları çalışır
    read_only: bool = False
    # Onay penceresi bu süre içinde yanıtlanmazsa işlem yapılmaz
    confirm_timeout_s: int = Field(120, ge=10, le=600)
    # Aşama 6: Claude bu araçları kendi kararıyla çağırabilir (onay gerektirenler yine sorulur)
    claude_can_use_tools: bool = True
# --- Aşama 5 sonu ---


# --- RUTIN ---
class BriefingSettings(_Section):
    """Sabah brifingi (jarvis/tools/briefing.py)."""

    # Hava durumu için şehir (open-meteo, anahtarsız). Boşsa hava durumu atlanır ve kullanıcıya söylenir.
    city: str = Field("", max_length=80)
    # Günün ilk açılışında / ilk etkinliğinde brifingi kendiliğinden ver (varsayılan kapalı)
    auto_enabled: bool = False
    # Haber başlıkları (ücretsiz RSS, yalnızca başlıklar)
    news_enabled: bool = True
    news_source: Literal["aa", "bbc", "trt"] = "aa"
    # Otomatik brifingin en son verildiği gün (YYYY-MM-DD); günde bir kez
    last_auto_day: str = ""
# --- RUTIN sonu ---
# --- SISTEM ---
class SistemSettings(_Section):
    # Oyun sırasında ekran kartı sıcaklık uyarısı (jarvis/system/gpu_watch.py)
    gpu_alert_enabled: bool = True
    gpu_alert_threshold_c: int = Field(85, ge=50, le=110)
    gpu_alert_duration_s: int = Field(30, ge=0, le=600)     # eşiğin üstünde bu kadar kalırsa uyar
    gpu_alert_cooldown_min: int = Field(10, ge=1, le=240)   # iki uyarı arası en az
    gpu_alert_speak: bool = True                            # sesli de söyle (JARVIS boştaysa)
# --- SISTEM sonu ---


class Settings(_Section):
    schema_version: int = SCHEMA_VERSION
    general: GeneralSettings = Field(default_factory=GeneralSettings)
    ui: UISettings = Field(default_factory=UISettings)
    ai: AISettings = Field(default_factory=AISettings)
    budget: BudgetSettings = Field(default_factory=BudgetSettings)
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    tools: ToolSettings = Field(default_factory=ToolSettings)  # Aşama 5
    briefing: BriefingSettings = Field(default_factory=BriefingSettings)  # --- RUTIN --- (tek satır)
    sistem: SistemSettings = Field(default_factory=SistemSettings)  # SISTEM


class SettingsStore:
    """Ayarların diske okunup yazılmasından sorumlu."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.settings = Settings()
        self.load_warning: str | None = None

    def load(self) -> Settings:
        self.load_warning = None
        if not self.path.exists():
            log.info("Ayar dosyası yok, varsayılanlar oluşturuluyor: %s", self.path)
            self.settings = Settings()
            self.save()
            return self.settings
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.settings = Settings.model_validate(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as exc:
            backup = self._backup_corrupt()
            self.load_warning = (
                "Ayar dosyası okunamadı, varsayılan ayarlar yüklendi. "
                f"Eski dosya şuraya yedeklendi: {backup.name}"
            )
            log.warning("Ayarlar geçersiz (%s). Yedek: %s", exc.__class__.__name__, backup)
            log.debug("Ayar doğrulama ayrıntısı: %s", exc)
            self.settings = Settings()
            self.save()
        return self.settings

    def save(self) -> None:
        """Atomik yazma: önce geçici dosya, sonra yer değiştirme."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = self.settings.model_dump_json(indent=2)
        fd, tmp = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        log.debug("Ayarlar kaydedildi: %s", self.path)

    def _backup_corrupt(self) -> Path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = self.path.with_name(f"settings.bozuk-{stamp}.json")
        os.replace(self.path, backup)
        return backup
