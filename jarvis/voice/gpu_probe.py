"""Ekran kartında konuşma tanımayı AYRI bir süreçte dener (zaman aşımlı, iptal edilebilir).

Neden ayrı süreç: CUDA sürücüsü/kütüphanesi yeni bir kartta (ör. RTX 50 serisi) ilk açılışta
dakikalarca derleme yapabilir, takılabilir ya da süreci çökertebilir. Bu, JARVIS'in kendi
içinde olursa ses motoru "yeniden başlatılıyor…" durumunda sonsuza kadar asılı kalır.
Deneme ayrı süreçte yapılır: başarılıysa ana süreç modeli ekran kartına yükler; takılır ya da
çökerse süreç öldürülür ve dürüstçe işlemciye dönülür.

Başarılı deneme bir işaret dosyasına yazılır (model + ctranslate2 + kütüphane sürümleri);
aynı yapılandırmada bir daha denenmez.

Paketlenmiş uygulamada `-m` yoktur: `JARVIS.exe --gpu-probe MODEL SONUC.json` ile çağrılır
(bkz. `jarvis.app.main`). Kaynaktan: `python -m jarvis.voice.gpu_probe MODEL SONUC.json`.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

PROBE_FLAG = "--gpu-probe"
DEFAULT_TIMEOUT_S = 240.0
MARKER = "gpu-deneme-basarili.json"
# Yeni kartlarda CUDA çekirdekleri ilk seferde derlenip önbelleğe yazılır. Varsayılan önbellek
# (256 MB) büyük modellere yetmeyebilir; yetmezse her açılışta yeniden derlenir.
CUDA_CACHE_BYTES = str(4 * 1024**3)


def tune_cuda_cache(environ=None) -> None:  # noqa: ANN001
    """CUDA JIT önbelleğini büyütür (CUDA başlamadan önce çağrılmalı; kullanıcı ayarladıysa dokunmaz)."""
    env = os.environ if environ is None else environ
    env.setdefault("CUDA_CACHE_MAXSIZE", CUDA_CACHE_BYTES)


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    detail: str = ""
    seconds: float = 0.0
    timed_out: bool = False
    cancelled: bool = False
    cached: bool = False


def probe_command(model_dir: Path, out_file: Path) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, PROBE_FLAG, str(model_dir), str(out_file)]
    return [sys.executable, "-m", "jarvis.voice.gpu_probe", str(model_dir), str(out_file)]


def _fingerprint(model_dir: Path, cuda_root: Path | None) -> dict:
    from jarvis.voice import cuda_libs

    try:
        import ctranslate2

        ct2 = getattr(ctranslate2, "__version__", "?")
    except Exception:  # noqa: BLE001
        ct2 = "yok"
    manifest = cuda_libs.read_manifest(cuda_root) or {}
    pkgs = {k: (v or {}).get("version") for k, v in (manifest.get("packages") or {}).items()}
    return {"model": model_dir.name, "ctranslate2": ct2, "libs": pkgs}


def _marker_path(cuda_root: Path | None) -> Path:
    from jarvis.voice import cuda_libs

    return cuda_libs.bin_dir(cuda_root) / MARKER


def known_good(model_dir: Path, cuda_root: Path | None = None) -> bool:
    try:
        data = json.loads(_marker_path(cuda_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return data == _fingerprint(model_dir, cuda_root)


def remember_good(model_dir: Path, cuda_root: Path | None = None) -> None:
    try:
        p = _marker_path(cuda_root)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(_fingerprint(model_dir, cuda_root)), encoding="utf-8")
    except OSError as e:
        log.warning("Ekran kartı deneme işareti yazılamadı: %s", e)


def run_probe(model_dir: Path, *, timeout_s: float = DEFAULT_TIMEOUT_S,
              cancel: Callable[[], bool] = lambda: False, on_tick: Callable[[float], None] | None = None,
              command: Callable[[Path, Path], list[str]] = probe_command, poll_s: float = 0.5) -> ProbeResult:
    """Denemeyi alt süreçte çalıştırır; bitene, zaman aşımına ya da iptale kadar bekler (engelleyici)."""
    tune_cuda_cache()
    fd, name = tempfile.mkstemp(prefix="jarvis-gpu-", suffix=".json")
    os.close(fd)
    out = Path(name)
    out.unlink(missing_ok=True)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(command(model_dir, out), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, creationflags=flags)
    except OSError as e:
        out.unlink(missing_ok=True)
        return ProbeResult(False, f"deneme süreci başlatılamadı: {e}")
    try:
        while True:
            code = proc.poll()
            elapsed = time.monotonic() - t0
            if code is not None:
                break
            if cancel():
                proc.kill()
                proc.wait(10)
                return ProbeResult(False, "iptal edildi", elapsed, cancelled=True)
            if elapsed >= timeout_s:
                proc.kill()
                proc.wait(10)
                return ProbeResult(False, f"ekran kartı {timeout_s / 60:.0f} dakikada hazır olmadı (takıldı)",
                                   elapsed, timed_out=True)
            if on_tick is not None:
                on_tick(elapsed)
            time.sleep(poll_s)
        try:
            data = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if code == 0 and data.get("ok"):
            return ProbeResult(True, "", elapsed)
        detail = str(data.get("error") or f"deneme süreci beklenmedik şekilde kapandı (çıkış kodu {code})")
        return ProbeResult(False, detail, elapsed)
    finally:
        out.unlink(missing_ok=True)


def probe_main(argv: list[str]) -> int:
    """Alt süreç: modeli ekran kartına yükler, 1 sn sessizliği çözer, sonucu dosyaya yazar."""
    if len(argv) != 2:
        return 2
    model_dir, out = Path(argv[0]), Path(argv[1])
    result: dict = {"ok": False}
    try:
        tune_cuda_cache()
        from jarvis.voice import cuda_libs

        cuda_libs.activate()
        err = cuda_libs.preload()
        if err:
            result["preload"] = err
        import numpy as np
        from faster_whisper import WhisperModel

        model = WhisperModel(str(model_dir), device="cuda", compute_type="float16")
        segments, _ = model.transcribe(np.zeros(16000, np.float32), language="tr", beam_size=1,
                                       without_timestamps=True, condition_on_previous_text=False)
        list(segments)
        result["ok"] = True
    except BaseException as e:  # noqa: BLE001 - her hatayı ana sürece taşı
        result["error"] = f"{type(e).__name__}: {e}"[:2000]
    try:
        out.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    except OSError:
        return 3
    return 0 if result["ok"] else 1


class ProbeCancel:
    """Birden çok iptal kaynağını tek çağrılabilirde birleştirir."""

    def __init__(self, *events: threading.Event | None) -> None:
        self.events = [e for e in events if e is not None]

    def __call__(self) -> bool:
        return any(e.is_set() for e in self.events)


if __name__ == "__main__":
    sys.exit(probe_main(sys.argv[1:]))
