"""Gerçek model dosyası gerektiren testler (`@pytest.mark.models`) için yardımcılar.

Modeller `JARVIS_TEST_MODELS_DIR`, yoksa `JARVIS_MODELS_DIR` (CI), yoksa depo kökünde `.test-models/`
altında aranır. Yoksa test atlanır. Önceden indirmek için `python scripts/fetch_models.py` (CI böyle yapar),
ya da `JARVIS_DOWNLOAD_MODELS=1` ile test sırasında indirilir:

    JARVIS_DOWNLOAD_MODELS=1 python -m pytest -m models -v

Kaynaklar: openWakeWord ve İngilizce Piper sesi GitHub'dan, Türkçe Piper sesi ve
faster-whisper modelleri Hugging Face'ten.
"""

from __future__ import annotations

import os
import tarfile
from pathlib import Path

import pytest

from jarvis.voice.models import ModelError, ModelFile, ModelManager

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = Path(os.environ.get("JARVIS_TEST_MODELS_DIR") or os.environ.get("JARVIS_MODELS_DIR")
                  or ROOT / ".test-models")
DOWNLOAD = os.environ.get("JARVIS_DOWNLOAD_MODELS") == "1"

# "hey jarvis" sesini üretmek için İngilizce erkek Piper sesi (GitHub, piper v0.0.2 sürümü)
EN_VOICE_TARBALL = ModelFile(
    "test-piper-en-ryan", "test/voice-en-us-ryan-medium.tar.gz",
    "https://github.com/rhasspy/piper/releases/download/v0.0.2/voice-en-us-ryan-medium.tar.gz",
    "İngilizce test sesi (ryan)", 58389017, "2e00d747eaed6ce9f63f4991921ef3bb2bbfbc7f28cde4f14eb7048960f928d8")


def manager() -> ModelManager:
    return ModelManager(MODELS_DIR, timeout=60)


def _skip_or_download(ready: bool, what: str) -> None:
    if ready:
        return
    if not DOWNLOAD:
        pytest.skip(f"{what} yok ({MODELS_DIR}); indirmek için JARVIS_DOWNLOAD_MODELS=1")


def need_files(*files: ModelFile) -> list[Path]:
    m = manager()
    _skip_or_download(all(m.is_ready(f) for f in files), ", ".join(f.label for f in files))
    try:
        return m.ensure_all(list(files))
    except ModelError as e:
        pytest.skip(f"model indirilemedi: {e.user_message}")


def english_voice() -> Path:
    tar_path = need_files(EN_VOICE_TARBALL)[0]
    dest = MODELS_DIR / "test" / "en-us-ryan-medium"
    onnx = dest / "en-us-ryan-medium.onnx"
    if not onnx.is_file():
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(tar_path) as tf:
            tf.extractall(dest, filter="data")
    return onnx


def turkish_voice(key: str = "tr_TR-dfki-medium") -> Path:
    m = manager()
    onnx, cfg = m.piper_files(key)
    _skip_or_download(m.is_ready(onnx) and m.is_ready(cfg), f"Türkçe Piper sesi ({key})")
    try:
        return m.ensure_piper(key)
    except ModelError as e:
        pytest.skip(f"model indirilemedi: {e.user_message}")


def whisper(name: str | None = None) -> Path:
    name = name or os.environ.get("JARVIS_TEST_STT_MODEL", "small")
    m = manager()
    _skip_or_download(m.whisper_ready(name), f"faster-whisper ({name})")
    try:
        return m.ensure_whisper(name)
    except ModelError as e:
        pytest.skip(f"model indirilemedi: {e.user_message}")
