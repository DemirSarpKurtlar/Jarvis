"""Ekran kartı hızlandırması: konuşma tanımanın (ctranslate2) ihtiyaç duyduğu NVIDIA kütüphaneleri.

Sorun: ctranslate2'nin Windows paketi CUDA çalışma zamanını içinde taşır (statik cudart) ama
cuBLAS'ı taşımaz; ilk GPU işleminde `LoadLibraryA("cublas64_12.dll")` ile arar. Bilgisayarda
CUDA Toolkit kurulu değilse "Library cublas64_12.dll is not found or cannot be loaded" hatası
alınır ve JARVIS işlemciye (CPU, `small`) düşer.

Çözüm: kullanıcı tek tıkla NVIDIA'nın PyPI'deki resmi `nvidia-cublas-cu12` paketini (wheel = zip)
indirir; yalnızca gereken DLL'ler (`nvidia/cublas/bin/cublas64_12.dll`, `cublasLt64_12.dll`)
JARVIS'in veri klasörüne (`%LOCALAPPDATA%\\JARVIS\\cuda\\bin`) çıkarılır. Yönetici izni gerekmez,
sisteme hiçbir şey kurulmaz. Kaldırmak için o klasörü silmek yeter.

Sürüm seçimi (doğrulandı, bkz. docs/KURULUM.md):
* ctranslate2 4.8.5'in Windows paketi CUDA 12.8.1 ile, `-DWITH_CUDNN=OFF` ve
  `-DCUDA_DYNAMIC_LOADING=ON` ile derlenmiş (depodaki `python/tools/prepare_build_environment_windows.sh`).
  `ctranslate2.dll` içindeki DLL adlarından yalnızca `cublas64_12.dll` ve `nvcuda.dll` (sürücü) geçiyor;
  cuDNN gerekmiyor (4.6.3'ten beri Conv1D saf CUDA; pakette gelen `cudnn64_9.dll` yalnızca bir kabuk).
* cuBLAS 12.8.4.1 = CUDA 12.8 Update 1'in cuBLAS'ı: ctranslate2'nin derlendiği araç takımıyla aynı ve
  Blackwell (RTX 50xx, sm_120) için yerel çekirdekler içeren ilk cuBLAS serisi (12.8).
* ctranslate2.dll'deki kendi CUDA çekirdekleri sm_53…sm_86 için derlenmiş, ayrıca compute_86 PTX var.
  sm_120 kartlarda sürücü bu PTX'i ilk açılışta derler (JIT): ilk GPU başlatması uzun sürebilir.

Yükleme yolu (Windows): ctranslate2 düz `LoadLibraryA` kullandığı için `os.add_dll_directory`
TEK BAŞINA yetmez (o yalnızca `LOAD_LIBRARY_SEARCH_*` bayraklı yüklemelerde geçerli); bu yüzden
klasör PATH'in başına da eklenir ve GPU denemesinden hemen önce DLL'ler tam yollarıyla önceden
yüklenir (aynı adlı modül zaten yüklüyse Windows onu kullanır).

Bu modüldeki indirme/çıkarma işlevleri engelleyicidir: her zaman arka plan thread'inde çağrılır.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jarvis import __version__
from jarvis.core.errors import JarvisError
from jarvis.paths import AppPaths

log = logging.getLogger(__name__)

PYPI_JSON = "https://pypi.org/pypi/{name}/{version}/json"
MANIFEST = "jarvis-cuda.json"
_MB = 1_048_576
_CHUNK = 1024 * 1024
_MARGIN = 64 * _MB          # disk kontrolünde pay


class CudaLibError(JarvisError):
    """Kütüphaneler indirilemedi, doğrulanamadı ya da kurulamadı."""


class CudaLibCancelled(CudaLibError):
    """Kullanıcı ya da uygulama kapanışı indirmeyi iptal etti (yarım dosya devam için saklanır)."""


@dataclass(frozen=True)
class CudaPackage:
    """PyPI'deki bir NVIDIA wheel'i. Boyut ve sha256 PyPI JSON API'sinden alınıp burada sabitlendi;
    çalışma zamanında PyPI'nin söylediğiyle karşılaştırılır (sessizce başka dosya inemez)."""

    name: str
    version: str
    dlls: tuple[str, ...]          # wheel içinde nvidia/*/bin/ altında; yükleme sırası (bağımlılık önce)
    wheel_size: int                # indirilecek bayt
    wheel_sha256: str
    installed_size: int            # çıkarılan DLL'lerin toplamı

    @property
    def title(self) -> str:
        return f"{self.name} {self.version}"

    @property
    def wheel_filename(self) -> str:
        return f"{self.name.replace('-', '_')}-{self.version}-py3-none-win_amd64.whl"


# Ölçüldü (PyPI, 2026-11-08): wheel 567.544.208 bayt; cublas64_12.dll 113.716.224 + cublasLt64_12.dll
# 674.667.520 bayt. nvblas64_12.dll (335 KB) ctranslate2 tarafından kullanılmaz, çıkarılmaz.
CUBLAS = CudaPackage(
    name="nvidia-cublas-cu12",
    version="12.8.4.1",
    dlls=("cublasLt64_12.dll", "cublas64_12.dll"),
    wheel_size=567_544_208,
    wheel_sha256="47e9b82132fa8d2b4944e708049229601448aaad7e6f296f630f2d1a32de35af",
    installed_size=113_716_224 + 674_667_520,
)
PACKAGES: tuple[CudaPackage, ...] = (CUBLAS,)


def download_bytes(packages: tuple[CudaPackage, ...] = PACKAGES) -> int:
    return sum(p.wheel_size for p in packages)


def installed_bytes(packages: tuple[CudaPackage, ...] = PACKAGES) -> int:
    return sum(p.installed_size for p in packages)


def _mb(n: int) -> str:
    return f"{n / _MB:,.0f} MB".replace(",", ".")


def size_summary(packages: tuple[CudaPackage, ...] = PACKAGES) -> str:
    """Kullanıcıya indirmeden ÖNCE gösterilen boyut özeti."""
    peak = download_bytes(packages) + installed_bytes(packages)
    return (f"İndirilecek: {_mb(download_bytes(packages))} (NVIDIA cuBLAS {packages[0].version}, PyPI). "
            f"Kurulunca diskte {_mb(installed_bytes(packages))} yer kaplar; indirme sırasında "
            f"yaklaşık {_gb(peak)} boş yer gerekir.")


def _gb(n: int) -> str:
    return f"{n / 1024 / _MB:.1f} GB".replace(".", ",")


# ---------- klasörler ve kurulum durumu ----------
def default_cuda_dir() -> Path:
    """`%LOCALAPPDATA%\\JARVIS\\cuda` (JARVIS_HOME verilirse onun altındaki data/cuda)."""
    return AppPaths.resolve().data_dir / "cuda"


def bin_dir(root: Path | None = None) -> Path:
    return (root or default_cuda_dir()) / "bin"


def read_manifest(root: Path | None = None) -> dict[str, Any] | None:
    try:
        data = json.loads((bin_dir(root) / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def is_installed(root: Path | None = None, packages: tuple[CudaPackage, ...] = PACKAGES) -> bool:
    """Bildirim dosyası var, sürümler tutuyor ve her DLL kayıtlı boyutunda mı?"""
    data = read_manifest(root)
    if not data:
        return False
    pkgs = data.get("packages") or {}
    d = bin_dir(root)
    for p in packages:
        entry = pkgs.get(p.name)
        if not isinstance(entry, dict) or entry.get("version") != p.version:
            return False
        files = entry.get("files") or {}
        for dll in p.dlls:
            f = d / dll
            if dll not in files or not f.is_file() or f.stat().st_size != files[dll]:
                return False
    return True


# ---------- ilerleme ----------
@dataclass(frozen=True)
class CudaProgress:
    phase: str                     # "download" | "verify" | "extract" | "restart" | "done"
    done: int = 0
    total: int | None = None
    finished: bool = False
    error: str | None = None
    message: str = ""              # bitince gösterilecek sonuç (başarılı ya da dürüst başarısızlık)
    ok: bool = True                # bitti ve ekran kartı gerçekten kullanılıyor mu

    @property
    def fraction(self) -> float | None:
        if not self.total:
            return None
        return max(0.0, min(1.0, self.done / self.total))

    def text(self) -> str:
        if self.error:
            return f"Ekran kartı hızlandırması kurulamadı: {self.error}"
        if self.finished:
            return self.message or "Ekran kartı kütüphaneleri hazır."
        if self.phase == "download":
            if self.total:
                return (f"Ekran kartı kütüphaneleri indiriliyor: %{(self.fraction or 0) * 100:.0f} "
                        f"({self.done / _MB:.0f} / {self.total / _MB:.0f} MB).")
            return f"Ekran kartı kütüphaneleri indiriliyor ({self.done / _MB:.0f} MB)."
        if self.phase == "verify":
            return "İndirilen dosya doğrulanıyor…"
        if self.phase == "extract":
            return "Kütüphaneler çıkarılıyor…"
        if self.phase == "restart":
            return "Ses motoru ekran kartıyla yeniden başlatılıyor (ilk açılış bir-iki dakika sürebilir)…"
        return "Ekran kartı kütüphaneleri hazırlanıyor…"


CudaProgressFn = Callable[[CudaProgress], None]


class _Throttle:
    def __init__(self, fn: CudaProgressFn | None, interval: float = 0.25) -> None:
        self.fn, self.interval, self._last = fn, interval, 0.0

    def __call__(self, p: CudaProgress, force: bool = False) -> None:
        if self.fn is None:
            return
        now = time.monotonic()
        if force or now - self._last >= self.interval:
            self._last = now
            try:
                self.fn(p)
            except Exception:  # noqa: BLE001
                log.debug("İlerleme dinleyicisi hata verdi", exc_info=True)


# ---------- PyPI üst verisi ----------
@dataclass(frozen=True)
class WheelInfo:
    filename: str
    url: str
    size: int
    sha256: str


def parse_wheel_info(data: dict[str, Any], package: CudaPackage, platform_tag: str = "win_amd64") -> WheelInfo:
    """PyPI JSON yanıtından Windows wheel'ini seçer ve sabitlenmiş boyut/özetle karşılaştırır."""
    urls = data.get("urls")
    if not isinstance(urls, list):
        raise CudaLibError(f"{package.title}: PyPI yanıtında 'urls' yok",
                           user_message="PyPI'den gelen paket bilgisi okunamadı.")
    for u in urls:
        if not isinstance(u, dict):
            continue
        name = str(u.get("filename", ""))
        if u.get("packagetype") not in (None, "bdist_wheel") or not name.endswith(f"-{platform_tag}.whl"):
            continue
        sha = str((u.get("digests") or {}).get("sha256", "")).lower()
        try:
            size = int(u.get("size"))
        except (TypeError, ValueError):
            size = -1
        url = str(u.get("url", ""))
        if not url.startswith("https://") or len(sha) != 64:
            raise CudaLibError(f"{name}: url/sha256 eksik", user_message="PyPI'den gelen paket bilgisi eksik.")
        if sha != package.wheel_sha256 or size != package.wheel_size:
            raise CudaLibError(f"{name}: PyPI sha256/boyut sabitlenen değerle uyuşmuyor ({sha}, {size})",
                               user_message="PyPI'deki dosya beklenen dosyayla uyuşmuyor; güvenlik için "
                                            "indirilmedi.")
        return WheelInfo(name, url, size, sha)
    raise CudaLibError(f"{package.title}: {platform_tag} wheel yok",
                       user_message=f"{package.title} için Windows paketi PyPI'de bulunamadı.")


def _friendly(e: BaseException) -> str:
    if isinstance(e, urllib.error.HTTPError):
        if e.code == 404:
            return "dosya sunucuda bulunamadı (HTTP 404)"
        return f"sunucu hata verdi (HTTP {e.code})"
    if isinstance(e, (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError)):
        return "internet bağlantısı yok ya da sunucuya ulaşılamadı"
    if isinstance(e, OSError) and getattr(e, "errno", None) == 28:
        return "diskte yeterli yer yok"
    if isinstance(e, PermissionError):
        return "dosyaya yazılamadı (izin yok ya da dosya kullanımda)"
    return f"{type(e).__name__}: {e}"


# ---------- kurulum ----------
class CudaLibInstaller:
    """İndirir, doğrular, yalnızca DLL'leri çıkarır ve atomik olarak `cuda/bin` klasörüne koyar.

    * İdempotent: zaten kuruluysa ağa hiç çıkmaz.
    * Devam ettirilebilir: yarım indirme `cuda/downloads/*.part` olarak kalır; sonraki denemede
      HTTP Range ile kaldığı yerden sürer (sunucu desteklemezse baştan iner).
    * İptal edilebilir: `cancel` her 1 MB'de kontrol edilir.
    """

    def __init__(self, root: Path | None = None, packages: tuple[CudaPackage, ...] = PACKAGES,
                 opener: Callable[..., Any] = urllib.request.urlopen, timeout: float = 30.0,
                 disk_free: Callable[[Path], int] | None = None) -> None:
        self.root = root or default_cuda_dir()
        self.packages = packages
        self.opener = opener
        self.timeout = timeout
        self.disk_free = disk_free or (lambda p: shutil.disk_usage(p).free)

    @property
    def bin(self) -> Path:
        return bin_dir(self.root)

    def installed(self) -> bool:
        return is_installed(self.root, self.packages)

    def install(self, progress: CudaProgressFn | None = None, cancel: threading.Event | None = None) -> Path:
        notify = _Throttle(progress)
        if self.installed():
            log.info("Ekran kartı kütüphaneleri zaten kurulu: %s", self.bin)
            notify(CudaProgress("done", finished=True), force=True)
            return self.bin
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self._check_disk()
            staging = self.root / ".staging"
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True)
            manifest: dict[str, Any] = {"jarvis": __version__, "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                        "packages": {}}
            total = download_bytes(self.packages)
            offset = 0
            wheels: list[Path] = []
            for pkg in self.packages:
                info = self.fetch_info(pkg)
                wheel = self.download(info, notify, cancel, offset, total)
                wheels.append(wheel)
                offset += info.size
                notify(CudaProgress("extract", offset, total), force=True)
                files = extract_dlls(wheel, staging, pkg.dlls)
                manifest["packages"][pkg.name] = {"version": pkg.version, "wheel": info.filename,
                                                  "sha256": info.sha256, "files": files}
            (staging / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            self._finalize(staging)
            for w in wheels:
                w.unlink(missing_ok=True)
        except CudaLibCancelled as e:
            notify(CudaProgress("download", error=e.user_message), force=True)
            raise
        except CudaLibError as e:
            log.warning("Ekran kartı kütüphaneleri kurulamadı: %s", e)
            notify(CudaProgress("download", error=e.user_message), force=True)
            raise
        except Exception as e:  # noqa: BLE001 - ağ/disk hataları
            log.warning("Ekran kartı kütüphaneleri kurulamadı: %s", e, exc_info=True)
            msg = _friendly(e)
            notify(CudaProgress("download", error=msg), force=True)
            raise CudaLibError(str(e), user_message=f"{msg}.") from e
        log.info("Ekran kartı kütüphaneleri kuruldu: %s", self.bin)
        notify(CudaProgress("done", finished=True), force=True)
        return self.bin

    # ---- adımlar ----
    def _check_disk(self) -> None:
        need = installed_bytes(self.packages) + _MARGIN
        for pkg in self.packages:
            part = self.root / "downloads" / f"{pkg.wheel_filename}.part"
            have = part.stat().st_size if part.exists() else 0
            need += max(0, pkg.wheel_size - have)
        free = self.disk_free(self.root)
        if free < need:
            raise CudaLibError(f"disk: {free} < {need}",
                               user_message=f"Diskte yeterli yer yok: {_mb(need)} gerekiyor, {_mb(free)} boş.")

    def fetch_info(self, pkg: CudaPackage) -> WheelInfo:
        url = PYPI_JSON.format(name=pkg.name, version=pkg.version)
        req = urllib.request.Request(url, headers={"User-Agent": f"JARVIS/{__version__}",
                                                   "Accept": "application/json"})
        try:
            with self.opener(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except CudaLibError:
            raise
        except ValueError as e:
            raise CudaLibError(f"PyPI JSON okunamadı: {e}", user_message="PyPI'den gelen paket bilgisi okunamadı.") from e
        except Exception as e:  # noqa: BLE001
            raise CudaLibError(f"PyPI'ye ulaşılamadı: {e}",
                               user_message=f"Paket bilgisi alınamadı: {_friendly(e)}.") from e
        return parse_wheel_info(data, pkg)

    def download(self, info: WheelInfo, notify: _Throttle | CudaProgressFn | None = None,
                 cancel: threading.Event | None = None, offset: int = 0, total: int | None = None) -> Path:
        """Wheel'i `.part` dosyasına indirir (devam ettirerek), sha256 doğrular, `.whl` olarak bırakır."""
        if notify is not None and not isinstance(notify, _Throttle):
            notify = _Throttle(notify)
        total = total or info.size
        dest = self.root / "downloads" / info.filename
        part = dest.with_name(dest.name + ".part")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_file() and dest.stat().st_size == info.size and sha256_file(dest) == info.sha256:
            return dest            # önceki denemede indi ama çıkarılamadı
        have = part.stat().st_size if part.exists() else 0
        if have > info.size:
            part.unlink()
            have = 0
        if have < info.size:
            headers = {"User-Agent": f"JARVIS/{__version__}"}
            if have:
                headers["Range"] = f"bytes={have}-"
            log.info("İndiriliyor: %s (%d/%d bayt hazır) <- %s", info.filename, have, info.size, info.url)
            req = urllib.request.Request(info.url, headers=headers)
            with self.opener(req, timeout=self.timeout) as resp:
                status = getattr(resp, "status", None) or (resp.getcode() if hasattr(resp, "getcode") else 200)
                if have and status != 206:
                    log.info("Sunucu kaldığı yerden devam etmeyi desteklemedi; baştan indiriliyor")
                    have = 0
                with open(part, "ab" if have else "wb") as out:
                    received = have
                    while True:
                        if cancel is not None and cancel.is_set():
                            raise CudaLibCancelled("iptal", user_message="İndirme iptal edildi. Tekrar başlatınca "
                                                                         "kaldığı yerden devam eder.")
                        chunk = resp.read(_CHUNK)
                        if not chunk:
                            break
                        out.write(chunk)
                        received += len(chunk)
                        if received > info.size:
                            break
                        if notify is not None:
                            notify(CudaProgress("download", offset + received, total))
                    out.flush()
                    os.fsync(out.fileno())
        size = part.stat().st_size
        if size != info.size:
            if size > info.size:
                part.unlink(missing_ok=True)
            raise CudaLibError(f"{info.filename}: boyut {size} != {info.size}",
                               user_message="Dosya eksik indi (bağlantı kesilmiş olabilir). Tekrar dene; "
                                            "kaldığı yerden devam eder.")
        if notify is not None:
            notify(CudaProgress("verify", offset + size, total), force=True)
        digest = sha256_file(part, cancel)
        if digest != info.sha256:
            part.unlink(missing_ok=True)
            raise CudaLibError(f"{info.filename}: sha256 tutmadı ({digest})",
                               user_message="İndirilen dosya doğrulanamadı (sha256 tutmadı): dosya bozuk ya da "
                                            "değiştirilmiş. Silindi; tekrar dene.")
        os.replace(part, dest)
        return dest

    def _finalize(self, staging: Path) -> None:
        """`.staging` -> `bin`: tek bir yeniden adlandırma; yarım kurulum asla `bin` olarak görünmez."""
        final = self.bin
        old = None
        if final.exists():
            old = self.root / f"bin.old-{int(time.time())}"
            try:
                os.replace(final, old)
            except OSError as e:
                raise CudaLibError(f"eski kurulum taşınamadı: {e}",
                                   user_message="Eski kütüphaneler kullanımda. JARVIS'i kapatıp açtıktan sonra "
                                                "tekrar dene.") from e
        os.replace(staging, final)
        if old is not None:
            shutil.rmtree(old, ignore_errors=True)


def sha256_file(path: Path, cancel: threading.Event | None = None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            if cancel is not None and cancel.is_set():
                raise CudaLibCancelled("iptal", user_message="İndirme iptal edildi.")
            chunk = f.read(_CHUNK * 4)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


_DLL_MEMBER = re.compile(r"^nvidia/[^/]+/bin/([^/]+\.dll)$", re.IGNORECASE)


def _record_hashes(z: zipfile.ZipFile) -> dict[str, str]:
    """wheel RECORD dosyasındaki `yol,sha256=<urlsafe b64>,boyut` satırlarını okur."""
    rec = next((n for n in z.namelist() if n.endswith(".dist-info/RECORD")), None)
    if rec is None:
        return {}
    out: dict[str, str] = {}
    for line in z.read(rec).decode("utf-8", "replace").splitlines():
        parts = line.rsplit(",", 2)
        if len(parts) == 3 and parts[1].startswith("sha256="):
            out[parts[0]] = parts[1][len("sha256="):]
    return out


def extract_dlls(wheel: Path, out_dir: Path, wanted: tuple[str, ...]) -> dict[str, int]:
    """Wheel'den YALNIZCA `nvidia/*/bin/<istenen>.dll` dosyalarını `out_dir`e (düz) çıkarır.

    Başka hiçbir dosya (başlıklar, .lib, betik) yazılmaz; yol bileşenleri atılır (zip-slip olmaz).
    RECORD'da özet varsa her DLL ayrıca doğrulanır. Dönüş: {dll adı: bayt}.
    """
    want = {w.lower(): w for w in wanted}
    found: dict[str, int] = {}
    try:
        with zipfile.ZipFile(wheel) as z:
            hashes = _record_hashes(z)
            for info in z.infolist():
                m = _DLL_MEMBER.match(info.filename)
                if not m or m.group(1).lower() not in want:
                    continue
                name = want[m.group(1).lower()]
                target = out_dir / name
                h = hashlib.sha256()
                with z.open(info) as src, open(target, "wb") as dst:
                    while True:
                        chunk = src.read(_CHUNK)
                        if not chunk:
                            break
                        h.update(chunk)
                        dst.write(chunk)
                expected = hashes.get(info.filename)
                if expected:
                    got = base64.urlsafe_b64encode(h.digest()).rstrip(b"=").decode()
                    if got != expected.rstrip("="):
                        target.unlink(missing_ok=True)
                        raise CudaLibError(f"{info.filename}: RECORD özeti tutmadı",
                                           user_message=f"{name} doğrulanamadı: paket bozuk.")
                found[name] = target.stat().st_size
    except zipfile.BadZipFile as e:
        raise CudaLibError(f"{wheel.name}: zip değil: {e}", user_message="İndirilen paket açılamadı (bozuk).") from e
    missing = [w for w in wanted if w not in found]
    if missing:
        raise CudaLibError(f"{wheel.name}: eksik {missing}",
                           user_message=f"Pakette beklenen dosya yok: {', '.join(missing)}.")
    return found


# ---------- Windows DLL arama yolu ----------
_dll_dir_handles: dict[str, Any] = {}
_preloaded: dict[str, Any] = {}


def activate(root: Path | None = None, packages: tuple[CudaPackage, ...] = PACKAGES, *,
             platform: str | None = None, environ: Any = None,
             add_dll_directory: Callable[[str], Any] | None = None) -> bool:
    """İndirilmiş DLL klasörünü bu sürecin DLL arama yoluna ekler (yalnızca Windows; idempotent).

    * `os.add_dll_directory`: Python/ctypes ve `LOAD_LIBRARY_SEARCH_*` kullanan yüklemeler için.
    * PATH'in başı: ctranslate2'nin düz `LoadLibraryA("cublas64_12.dll")` çağrısı ve cublas'ın
      kendi bağımlılığı (cublasLt64_12.dll) standart arama sırasında PATH'e bakar.
    Kurulu değilse hiçbir şey yapmaz ve False döner.
    """
    platform = platform or sys.platform
    if platform != "win32":
        return False
    if not is_installed(root, packages):
        return False
    environ = os.environ if environ is None else environ
    d = str(bin_dir(root))
    adder = add_dll_directory or getattr(os, "add_dll_directory", None)
    if adder is not None and d not in _dll_dir_handles:
        try:
            _dll_dir_handles[d] = adder(d)
        except OSError as e:
            log.warning("DLL klasörü eklenemedi (%s): %s", d, e)
    sep = ";"
    current = environ.get("PATH", "")
    norm = os.path.normcase(os.path.normpath(d))
    if not any(os.path.normcase(os.path.normpath(p)) == norm for p in current.split(sep) if p):
        environ["PATH"] = d + (sep + current if current else "")
        log.info("Ekran kartı kütüphaneleri arama yoluna eklendi: %s", d)
    return True


def preload(root: Path | None = None, packages: tuple[CudaPackage, ...] = PACKAGES, *,
            platform: str | None = None, loader: Callable[[str], Any] | None = None) -> str | None:
    """DLL'leri tam yollarıyla (bağımlılık sırasıyla) yükler. Hata varsa açıklamasını döndürür."""
    platform = platform or sys.platform
    if platform != "win32" or not is_installed(root, packages):
        return None
    if loader is None:
        import ctypes

        loader = ctypes.WinDLL  # type: ignore[attr-defined]
    d = bin_dir(root)
    for pkg in packages:
        for dll in pkg.dlls:
            path = str(d / dll)
            if path in _preloaded:
                continue
            try:
                _preloaded[path] = loader(path)
            except OSError as e:
                log.warning("%s yüklenemedi: %s", dll, e)
                return f"{dll} yüklenemedi: {e}"
    return None


def reset_for_tests() -> None:
    _dll_dir_handles.clear()
    _preloaded.clear()


# ---------- tanılama ----------
_MISSING_LIB = re.compile(r"(cublas|cublaslt|cudnn)\w*\.(dll|so)", re.IGNORECASE)
_MISSING_WORDS = ("not found", "cannot be loaded", "could not load", "could not locate", "unable to load",
                  "cannot open shared object")


def is_missing_library_error(text: str) -> bool:
    """"Library cublas64_12.dll is not found or cannot be loaded" türü hatalar."""
    low = text.lower()
    return bool(_MISSING_LIB.search(text)) and any(w in low for w in _MISSING_WORDS)


def probe_nvidia() -> str:
    """NVML ile ilk NVIDIA ekran kartının adı; yoksa boş metin. (Engelleyici, kısa.)"""
    try:
        import pynvml  # nvidia-ml-py

        pynvml.nvmlInit()
    except Exception:  # noqa: BLE001 - modül yok, sürücü yok, kart yok
        return ""
    try:
        if pynvml.nvmlDeviceGetCount() < 1:
            return ""
        name = pynvml.nvmlDeviceGetName(pynvml.nvmlDeviceGetHandleByIndex(0))
        return name.decode() if isinstance(name, bytes) else str(name)
    except Exception:  # noqa: BLE001
        return ""
    finally:
        try:
            pynvml.nvmlShutdown()
        except Exception:  # noqa: BLE001
            pass


@dataclass(frozen=True)
class GpuReport:
    """Ses motoru yüklenirken ekran kartı için ne olduğu (arayüz bu bilgiyle düğme gösterir)."""

    gpu_name: str = ""             # NVIDIA kartın adı; boş = NVIDIA kart yok
    libs_installed: bool = False   # JARVIS'in indirdiği cuBLAS kurulu mu
    on_gpu: bool = False           # konuşma tanıma şu an ekran kartında mı
    issue: str = ""                # "" | "libs_missing" | "gpu_failed" | "no_cuda"
    detail: str = ""               # teknik ayrıntı (loga ve ipucuna)
    device_setting: str = "auto"

    @property
    def offer_download(self) -> bool:
        """"Ekran kartı hızlandırmasını indir" düğmesi görünsün mü?

        Yalnızca NVIDIA kart varsa, GPU denemesi gerçekten eksik cuBLAS yüzünden başarısız olduysa,
        kütüphaneler kurulu değilse ve kullanıcı açıkça "İşlemci" seçmediyse. Kütüphaneler kuruluyken
        GPU yine başarısızsa ya da başka bir GPU hatası varsa düğme GÖSTERİLMEZ (indirmek işe yaramaz;
        döngü olmaz).
        """
        return (bool(self.gpu_name) and not self.libs_installed and not self.on_gpu
                and self.device_setting != "cpu" and self.issue == "libs_missing")

    def summary(self) -> str:
        """Ayarlar > Ses bölümündeki dürüst durum metni."""
        if not self.gpu_name:
            return "NVIDIA ekran kartı bulunamadı; konuşma tanıma işlemcide çalışır."
        if self.on_gpu:
            return f"{self.gpu_name}: konuşma tanıma ekran kartında çalışıyor."
        if self.device_setting == "cpu":
            return f"{self.gpu_name} bulundu; ayarlarda işlemci seçili."
        if self.issue == "gpu_failed" and self.libs_installed:
            return (f"{self.gpu_name}: ekran kartı kütüphaneleri kurulu ama konuşma tanıma ekran kartında yine "
                    "çalışmadı; işlemci kullanılıyor. Bu kart ya da sürücü sürümü bu konuşma tanıma motoruyla "
                    "henüz uyumlu olmayabilir (NVIDIA sürücüsünü güncellemek işe yarayabilir).")
        if self.issue == "gpu_failed":
            return f"{self.gpu_name}: konuşma tanıma ekran kartında çalışmadı; işlemci kullanılıyor."
        if self.issue == "no_cuda":
            return (f"{self.gpu_name} bulundu ama konuşma tanıma motoru CUDA aygıtı göremedi (NVIDIA sürücüsü "
                    "eski olabilir); konuşma tanıma işlemcide çalışıyor.")
        if not self.libs_installed:
            return (f"{self.gpu_name} bulundu ama CUDA kütüphaneleri (cuBLAS) eksik; konuşma tanıma işlemcide "
                    "(daha yavaş ve daha az doğru) çalışıyor.")
        return f"{self.gpu_name} bulundu."
