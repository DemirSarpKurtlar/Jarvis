"""Ses motorunun ve `models` işaretli testlerin ihtiyaç duyduğu tüm modelleri indirir.

Kullanım (argümansız):

    python scripts/fetch_models.py

Hedef klasör `JARVIS_MODELS_DIR` (yoksa depo kökünde `.test-models/`). Hugging Face önbelleği
`HF_HOME` verilmemişse bu klasörün altındaki `huggingface/` olur. CI bu klasörü önbelleğe alır.

İndirilenler (zaten varsa ve doğrulanıyorsa atlanır):
* openWakeWord v0.5.1 ONNX dosyaları (GitHub, sha256)
* Türkçe erkek Piper sesi tr_TR-dfki-medium (Hugging Face, md5 + boyut)
* faster-whisper `small` (CPU varsayılanı; `JARVIS_TEST_STT_MODEL` ile değişir)
* "Hey Jarvis" testi için İngilizce erkek Piper sesi (GitHub, sha256)

Ağ hatasında anlaşılır bir mesajla 1 koduyla çıkar.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    target = Path(os.environ.get("JARVIS_MODELS_DIR") or ROOT / ".test-models").expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    os.environ["JARVIS_MODELS_DIR"] = str(target)
    os.environ.setdefault("HF_HOME", str(target / "huggingface"))

    from jarvis.voice.models import WAKE_FILES, DownloadProgress, ModelError, ModelManager
    from tests import model_assets

    model_assets.MODELS_DIR = target
    manager = ModelManager(target, timeout=60)
    last = [0.0]

    def progress(p: DownloadProgress) -> None:
        now = time.monotonic()
        if p.finished or p.error or now - last[0] > 5:
            last[0] = now
            print("  " + p.text(), flush=True)

    whisper = os.environ.get("JARVIS_TEST_STT_MODEL", "small")
    steps = [
        ("openWakeWord", lambda: manager.ensure_all(list(WAKE_FILES), progress, label="uyandırma kelimesi")),
        ("Türkçe ses (tr_TR-dfki-medium)", lambda: manager.ensure_piper("tr_TR-dfki-medium", progress)),
        (f"faster-whisper ({whisper})", lambda: manager.ensure_whisper(whisper, progress)),
        ("İngilizce test sesi (ryan)", lambda: manager.ensure(model_assets.EN_VOICE_TARBALL, progress)),
    ]
    print(f"Model klasörü: {target}")
    failed = []
    for title, step in steps:
        print(f"- {title}", flush=True)
        try:
            step()
        except ModelError as e:
            print(f"  HATA: {e.user_message}", flush=True)
            failed.append(title)
    if not failed:
        model_assets.english_voice()   # tar arşivini aç
    total = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
    print(f"Toplam: {total / 1_048_576:.0f} MB")
    if failed:
        print("İndirilemeyenler: " + ", ".join(failed))
        return 1
    print("Tüm modeller hazır.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
