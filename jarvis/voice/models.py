"""Ses modellerinin indirilmesi ve önbelleği.

Modeller `AppPaths.resolve().data_dir / "models"` altında tutulur (Windows'ta
`%LOCALAPPDATA%\\JARVIS\\models`). İndirme önce `.part` dosyasına yapılır, boyut ve
özet (sha256/md5) doğrulanır, sonra atomik olarak yerine taşınır. Yarım kalan ya da
bozuk dosya asla model olarak kullanılmaz.

Kaynaklar:
* openWakeWord v0.5.1 ONNX dosyaları: GitHub sürüm sayfası (özetler bu ortamda indirilip hesaplandı).
* Piper sesleri: Hugging Face `rhasspy/piper-voices`, sabit `v1.0.0` sürümü.
* faster-whisper modelleri: Hugging Face (`huggingface_hub` ile, bayt bazında ilerleme).
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import shutil
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from jarvis import __version__
from jarvis.core.errors import JarvisError
from jarvis.paths import AppPaths

log = logging.getLogger(__name__)

OWW_BASE = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1"
PIPER_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"


class ModelError(JarvisError):
    """Model indirilemedi ya da doğrulanamadı."""


@dataclass(frozen=True)
class ModelFile:
    key: str
    relpath: str                  # models klasörüne göre yol
    url: str
    label: str                    # kullanıcıya gösterilen ad
    size: int | None = None       # bilinen bayt boyutu
    sha256: str | None = None
    md5: str | None = None
    kind: str = "binary"          # "binary" | "json"


@dataclass(frozen=True)
class DownloadProgress:
    label: str
    done: int
    total: int | None
    finished: bool = False
    error: str | None = None

    @property
    def fraction(self) -> float | None:
        if not self.total:
            return None
        return max(0.0, min(1.0, self.done / self.total))

    def text(self) -> str:
        if self.error:
            return f"Ses modeli indirilemedi ({self.label}): {self.error}"
        if self.finished:
            return f"Ses modelleri hazır ({self.label})."
        mb = self.done / 1_048_576
        if self.total:
            return (f"Ses modelleri indiriliyor: {self.label} %{self.fraction * 100:.0f} "
                    f"({mb:.0f} / {self.total / 1_048_576:.0f} MB). Bu yalnızca ilk açılışta olur.")
        return f"Ses modelleri indiriliyor: {self.label} ({mb:.0f} MB). Bu yalnızca ilk açılışta olur."


ProgressFn = Callable[[DownloadProgress], None]


# ---------- bilinen dosyalar ----------
OWW_MELSPEC = ModelFile("oww-melspec", "openwakeword/melspectrogram.onnx", f"{OWW_BASE}/melspectrogram.onnx",
                        "uyandırma kelimesi (özellik modeli)", 1087958,
                        "ba2b0e0f8b7b875369a2c89cb13360ff53bac436f2895cced9f479fa65eb176f")
OWW_EMBEDDING = ModelFile("oww-embedding", "openwakeword/embedding_model.onnx", f"{OWW_BASE}/embedding_model.onnx",
                          "uyandırma kelimesi (gömme modeli)", 1326578,
                          "70d164290c1d095d1d4ee149bc5e00543250a7316b59f31d056cff7bd3075c1f")
OWW_HEY_JARVIS = ModelFile("oww-hey-jarvis", "openwakeword/hey_jarvis_v0.1.onnx", f"{OWW_BASE}/hey_jarvis_v0.1.onnx",
                           "uyandırma kelimesi (Hey Jarvis)", 1271370,
                           "94a13cfe60075b132f6a472e7e462e8123ee70861bc3fb58434a73712ee0d2cb")
WAKE_FILES = (OWW_MELSPEC, OWW_EMBEDDING, OWW_HEY_JARVIS)


@dataclass(frozen=True)
class PiperVoiceInfo:
    key: str
    title: str
    gender_note: str
    onnx_size: int | None = None
    onnx_md5: str | None = None

    def files(self) -> tuple[ModelFile, ModelFile]:
        lang, rest = self.key.split("-", 1)
        name, quality = rest.rsplit("-", 1)
        family = lang.split("_")[0]
        base = f"{PIPER_BASE}/{family}/{lang}/{name}/{quality}/{self.key}"
        rel = f"piper/{self.key}"
        return (ModelFile(f"piper-{self.key}", f"{rel}.onnx", f"{base}.onnx?download=true",
                          f"Türkçe ses ({self.title})", self.onnx_size, md5=self.onnx_md5),
                ModelFile(f"piper-{self.key}-json", f"{rel}.onnx.json", f"{base}.onnx.json?download=true",
                          f"Türkçe ses ayarı ({self.title})", kind="json"))


# Cinsiyet: tr_TR-dfki-medium, MaryTTS'in "dfki-ot" Türkçe ses kayıtlarıyla eğitildi
# (Piper MODEL_CARD: github.com/marytts/dfki-ot-data). MaryTTS bileşen listesi bu sesi
# gender="male" olarak tanımlıyor. Boyut ve md5 rhasspy/piper-samples voices.json'dan.
# Lisans: CC BY-NC-SA 4.0 (ticari olmayan kişisel kullanım).
PIPER_VOICES: dict[str, PiperVoiceInfo] = {
    "tr_TR-dfki-medium": PiperVoiceInfo("tr_TR-dfki-medium", "dfki, erkek", "Erkek ses (MaryTTS dfki-ot verisi).",
                                        63201294, "f51287b350a042dd8d67b2b215145e5a"),
    "tr_TR-fahrettin-medium": PiperVoiceInfo("tr_TR-fahrettin-medium", "fahrettin",
                                             "Erkek adı taşıyor; model kartı doğrulanamadı."),
    "tr_TR-fettah-medium": PiperVoiceInfo("tr_TR-fettah-medium", "fettah",
                                          "Erkek adı taşıyor; model kartı doğrulanamadı."),
}

WHISPER_REPOS = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v3": "Systran/faster-whisper-large-v3",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
}
WHISPER_PATTERNS = ("config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*")
WHISPER_REQUIRED = ("config.json", "model.bin", "tokenizer.json")


def default_models_dir() -> Path:
    """`JARVIS_MODELS_DIR` verilirse o klasör (CI önbelleği, taşınabilir kurulum), yoksa veri klasörü/models."""
    env = os.environ.get("JARVIS_MODELS_DIR")
    if env:
        return Path(env).expanduser()
    return AppPaths.resolve().data_dir / "models"


def _friendly(e: BaseException) -> str:
    if isinstance(e, urllib.error.HTTPError):
        if e.code == 404:
            return "dosya sunucuda bulunamadı (HTTP 404)"
        if e.code in (401, 403):
            return f"sunucu erişime izin vermedi (HTTP {e.code})"
        return f"sunucu hata verdi (HTTP {e.code})"
    if isinstance(e, (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError)):
        return "internet bağlantısı yok ya da sunucuya ulaşılamadı"
    if isinstance(e, OSError) and getattr(e, "errno", None) == 28:
        return "diskte yeterli yer yok"
    name = type(e).__name__
    if any(k in name for k in ("Connect", "Timeout", "Offline", "Network")):
        return "internet bağlantısı yok ya da sunucuya ulaşılamadı"
    return f"{name}: {e}"


class _Throttle:
    def __init__(self, fn: ProgressFn | None, interval: float = 0.25) -> None:
        self.fn, self.interval, self._last = fn, interval, 0.0

    def __call__(self, p: DownloadProgress, force: bool = False) -> None:
        if self.fn is None:
            return
        now = time.monotonic()
        if force or now - self._last >= self.interval:
            self._last = now
            try:
                self.fn(p)
            except Exception:  # noqa: BLE001
                log.debug("İlerleme dinleyicisi hata verdi", exc_info=True)


class ModelManager:
    def __init__(self, root: Path | None = None,
                 opener: Callable[..., object] = urllib.request.urlopen, timeout: float = 30.0) -> None:
        self.root = root or default_models_dir()
        self.opener = opener
        self.timeout = timeout

    # ---------- tek dosyalar ----------
    def path(self, f: ModelFile) -> Path:
        return self.root / f.relpath

    def is_ready(self, f: ModelFile) -> bool:
        p = self.path(f)
        if not p.is_file():
            return False
        return f.size is None or p.stat().st_size == f.size

    def ensure(self, f: ModelFile, progress: ProgressFn | None = None,
               cancel: threading.Event | None = None) -> Path:
        return self.ensure_all([f], progress, cancel)[0]

    def ensure_all(self, files: list[ModelFile] | tuple[ModelFile, ...], progress: ProgressFn | None = None,
                   cancel: threading.Event | None = None, label: str | None = None) -> list[Path]:
        """Eksik dosyaları sırayla indirir; ilerleme toplam bayt üzerinden bildirilir."""
        missing = [f for f in files if not self.is_ready(f)]
        if missing:
            # Boyutu bilinmeyen dosya varsa toplam bilinmez; her dosya kendi Content-Length'ini kullanır
            total = sum(f.size for f in missing if f.size) if all(f.size for f in missing) else None
            notify = _Throttle(progress)
            done_before = 0
            for f in missing:
                title = label or f.label
                done_before += self._download(f, title, done_before, total, notify, cancel)
            notify(DownloadProgress(label or missing[-1].label, done_before, total, finished=True), force=True)
        return [self.path(f) for f in files]

    def _download(self, f: ModelFile, title: str, offset: int, total: int | None, notify: _Throttle,
                  cancel: threading.Event | None) -> int:
        dest = self.path(f)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        log.info("Model indiriliyor: %s <- %s", dest.name, f.url)
        sha, md5 = hashlib.sha256(), hashlib.md5()  # noqa: S324 - yalnızca bütünlük kontrolü
        received = 0
        try:
            req = urllib.request.Request(f.url, headers={"User-Agent": f"JARVIS/{__version__}"})
            with self.opener(req, timeout=self.timeout) as resp, open(tmp, "wb") as out:
                length = resp.headers.get("Content-Length") if hasattr(resp, "headers") else None
                expected = f.size or (int(length) if length and length.isdigit() else None)
                file_total = total if total else (offset + expected if expected else None)
                while True:
                    if cancel is not None and cancel.is_set():
                        raise ModelError("indirme iptal edildi", user_message="Model indirme iptal edildi.")
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    sha.update(chunk)
                    md5.update(chunk)
                    received += len(chunk)
                    notify(DownloadProgress(title, offset + received, file_total))
                out.flush()
                os.fsync(out.fileno())
            if expected is not None and received != expected:
                raise ModelError(f"{dest.name}: boyut {received} != {expected}",
                                 user_message=f"{f.label} eksik indi (bağlantı kesilmiş olabilir). Tekrar dene.")
            if f.sha256 and sha.hexdigest() != f.sha256:
                raise ModelError(f"{dest.name}: sha256 tutmadı",
                                 user_message=f"{f.label} doğrulanamadı: dosya bozuk ya da değiştirilmiş.")
            if f.md5 and md5.hexdigest() != f.md5:
                raise ModelError(f"{dest.name}: md5 tutmadı",
                                 user_message=f"{f.label} doğrulanamadı: dosya bozuk ya da değiştirilmiş.")
            if f.kind == "json":
                try:
                    json.loads(tmp.read_text(encoding="utf-8"))
                except (ValueError, UnicodeDecodeError) as e:
                    raise ModelError(f"{dest.name}: JSON değil", user_message=f"{f.label} bozuk indi.") from e
            os.replace(tmp, dest)
            log.info("Model hazır: %s (%d bayt)", dest.name, received)
            return received
        except ModelError as e:
            tmp.unlink(missing_ok=True)
            notify(DownloadProgress(title, offset + received, total, error=e.user_message), force=True)
            raise
        except Exception as e:  # noqa: BLE001 - ağ, disk ve HTTP hataları
            tmp.unlink(missing_ok=True)
            msg = _friendly(e)
            log.warning("Model indirilemedi (%s): %s", dest.name, e)
            notify(DownloadProgress(title, offset + received, total, error=msg), force=True)
            raise ModelError(f"{dest.name}: {e}", user_message=f"{f.label} indirilemedi: {msg}.") from e

    # ---------- Piper ----------
    def piper_files(self, voice_key: str) -> tuple[ModelFile, ModelFile]:
        info = PIPER_VOICES.get(voice_key)
        if info is None:
            if not voice_key.startswith("tr_TR-") or voice_key.count("-") < 2:
                raise ModelError(f"bilinmeyen ses: {voice_key}", user_message=f"'{voice_key}' adlı bir ses tanımıyorum.")
            info = PiperVoiceInfo(voice_key, voice_key, "Bilinmeyen ses.")
        return info.files()

    def ensure_piper(self, voice_key: str, progress: ProgressFn | None = None,
                     cancel: threading.Event | None = None) -> Path:
        onnx, cfg = self.piper_files(voice_key)
        self.ensure_all([onnx, cfg], progress, cancel, label=onnx.label)
        return self.path(onnx)

    # ---------- faster-whisper ----------
    def whisper_dir(self, name: str) -> Path:
        return self.root / "whisper" / name.replace("/", "--")

    def whisper_ready(self, name: str) -> bool:
        d = self.whisper_dir(name)
        return all((d / f).is_file() for f in WHISPER_REQUIRED)

    def ensure_whisper(self, name: str, progress: ProgressFn | None = None,
                       cancel: threading.Event | None = None) -> Path:
        """faster-whisper modelini Hugging Face'ten indirir (yoksa). Klasör yolunu döndürür."""
        d = self.whisper_dir(name)
        if self.whisper_ready(name):
            return d
        if os.path.isdir(name) and all((Path(name) / f).is_file() for f in WHISPER_REQUIRED):
            return Path(name)  # kullanıcı yerel bir klasör verdi
        repo = WHISPER_REPOS.get(name, name if "/" in name else None)
        if repo is None:
            raise ModelError(f"bilinmeyen whisper modeli: {name}",
                             user_message=f"'{name}' adlı bir konuşma tanıma modeli tanımıyorum.")
        label = f"konuşma tanıma ({name})"
        notify = _Throttle(progress)
        try:
            from huggingface_hub import HfApi, hf_hub_download

            info = HfApi().model_info(repo, files_metadata=True)
            files = [s for s in (info.siblings or [])
                     if any(fnmatch.fnmatch(s.rfilename, p) for p in WHISPER_PATTERNS)]
            if not files:
                raise ModelError(f"{repo}: dosya yok", user_message=f"{label} modelinde dosya bulunamadı.")
            total = sum(int(s.size or 0) for s in files) or None
            done = 0
            d.mkdir(parents=True, exist_ok=True)
            for s in files:
                if cancel is not None and cancel.is_set():
                    raise ModelError("iptal", user_message="Model indirme iptal edildi.")
                base = done
                bar = _bar_class(lambda n, base=base: notify(DownloadProgress(label, base + n, total)), cancel)
                hf_hub_download(repo, s.rfilename, local_dir=str(d), tqdm_class=bar)
                done += int(s.size or 0)
                notify(DownloadProgress(label, done, total))
        except ModelError as e:
            notify(DownloadProgress(label, 0, None, error=e.user_message), force=True)
            raise
        except Exception as e:  # noqa: BLE001 - huggingface_hub/httpx hata sınıfları sürüme göre değişiyor
            msg = _friendly(e)
            log.warning("Whisper modeli indirilemedi (%s): %s", repo, e)
            notify(DownloadProgress(label, 0, None, error=msg), force=True)
            raise ModelError(f"{repo}: {e}", user_message=f"Konuşma tanıma modeli ({name}) indirilemedi: {msg}.") from e
        if not self.whisper_ready(name):
            raise ModelError(f"{repo}: eksik dosya", user_message=f"Konuşma tanıma modeli ({name}) eksik indi.")
        notify(DownloadProgress(label, total or 0, total, finished=True), force=True)
        return d

    def remove(self, relpath: str) -> None:
        p = self.root / relpath
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)


def _bar_class(on_bytes: Callable[[int], None], cancel: threading.Event | None) -> type:
    """huggingface_hub'ın bayt ilerlemesini bize aktaran sessiz tqdm sınıfı."""
    from tqdm import tqdm

    class _Bar(tqdm):  # type: ignore[misc]
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            kwargs["disable"] = True
            super().__init__(*args, **kwargs)
            self._done_bytes = int(kwargs.get("initial") or 0)

        def update(self, n: float | None = 1) -> bool | None:
            if cancel is not None and cancel.is_set():
                raise ModelError("iptal", user_message="Model indirme iptal edildi.")
            self._done_bytes += int(n or 0)
            on_bytes(self._done_bytes)
            return None

    return _Bar
