"""Ekran kartı hızlandırması: cuBLAS indirme/doğrulama/çıkarma, DLL arama yolu ve GPU raporu.

Ağ yerine sahte bir HTTP sunucusu (opener) ve içinde `nvidia/cublas/bin/cublas64_12.dll` bulunan
küçük sahte bir wheel (zip) kullanılır. Gerçek PyPI dosyası bu testlerde indirilmez.
"""

import base64
import hashlib
import io
import json
import os
import sys
import threading
import urllib.error
import zipfile
from pathlib import Path

import numpy as np
import pytest

from jarvis.config.settings import SettingsStore
from jarvis.voice import cuda_libs, stt as stt_mod
from jarvis.voice.components import (CUDA_GENERIC_NOTE, CUDA_MISSING_NOTE, CUDA_STILL_FAILING_NOTE,
                                     RealComponentLoader)
from jarvis.voice.cuda_libs import (CUBLAS, CudaLibCancelled, CudaLibError, CudaLibInstaller, CudaPackage,
                                    GpuReport, activate, extract_dlls, is_installed, is_missing_library_error,
                                    parse_wheel_info, preload)
from jarvis.voice.models import ModelManager
from jarvis.voice.stt import STTError, Transcript

REAL_LOG_ERROR = ("Whisper yüklenemedi (large-v3-turbo (GPU, float16)): Library cublas64_12.dll is not found "
                  "or cannot be loaded")
WHEEL_NAME = "nvidia_cublas_cu12-12.8.4.1-py3-none-win_amd64.whl"
WHEEL_URL = "https://files.example/" + WHEEL_NAME
JSON_URL = "https://pypi.org/pypi/nvidia-cublas-cu12/12.8.4.1/json"
DLLS = {"cublasLt64_12.dll": b"L" * 300_000, "cublas64_12.dll": b"C" * 120_000}


def _rec_hash(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def make_wheel(dlls: dict[str, bytes] = DLLS, *, bad_record: bool = False, record: bool = True) -> bytes:
    buf = io.BytesIO()
    lines = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        members = {f"nvidia/cublas/bin/{n}": d for n, d in dlls.items()}
        members.update({
            "nvidia/cublas/bin/nvblas64_12.dll": b"N" * 500,          # ctranslate2 kullanmaz: çıkarılmamalı
            "nvidia/cublas/include/cublas_v2.h": b"// header",
            "nvidia/cublas/lib/x64/cublas.lib": b"lib",
            "nvidia/__init__.py": b"",
            "nvidia/cublas/bin/../../../evil.dll": b"x",              # zip-slip denemesi
        })
        for name, data in members.items():
            z.writestr(name, data)
            digest = _rec_hash(b"bozuk" if bad_record and name.endswith("cublas64_12.dll") else data)
            lines.append(f"{name},{digest},{len(data)}")
        if record:
            lines.append("nvidia_cublas_cu12-12.8.4.1.dist-info/RECORD,,")
            z.writestr("nvidia_cublas_cu12-12.8.4.1.dist-info/RECORD", "\n".join(lines) + "\n")
    return buf.getvalue()


def package_for(wheel: bytes, dlls: dict[str, bytes] = DLLS) -> CudaPackage:
    return CudaPackage("nvidia-cublas-cu12", "12.8.4.1", tuple(dlls), len(wheel),
                       hashlib.sha256(wheel).hexdigest(), sum(len(d) for d in dlls.values()))


def pypi_json(wheel: bytes, sha: str | None = None, size: int | None = None) -> dict:
    return {"info": {"name": "nvidia-cublas-cu12", "version": "12.8.4.1"}, "urls": [
        {"filename": "nvidia_cublas_cu12-12.8.4.1-py3-none-manylinux_2_27_x86_64.whl", "packagetype": "bdist_wheel",
         "url": "https://files.example/linux.whl", "size": 1, "digests": {"sha256": "0" * 64}},
        {"filename": WHEEL_NAME, "packagetype": "bdist_wheel", "url": WHEEL_URL,
         "size": len(wheel) if size is None else size,
         "digests": {"sha256": sha or hashlib.sha256(wheel).hexdigest()}},
    ]}


class FakeResp:
    def __init__(self, data: bytes, status: int = 200, chunk_hook=None) -> None:  # noqa: ANN001
        self._buf = io.BytesIO(data)
        self.status = status
        self.headers = {"Content-Length": str(len(data))}
        self.chunk_hook = chunk_hook

    def read(self, n: int = -1) -> bytes:
        chunk = self._buf.read(n)
        if chunk and self.chunk_hook:
            self.chunk_hook()
        return chunk

    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, *a) -> None:  # noqa: ANN002
        pass


class FakeServer:
    """PyPI JSON + wheel. Range başlığını destekler (istenirse yok sayar)."""

    def __init__(self, wheel: bytes, *, serve: bytes | None = None, json_data: dict | None = None,
                 honor_range: bool = True, chunk_hook=None, fail_after: int | None = None) -> None:  # noqa: ANN001
        self.wheel = wheel
        self.serve = wheel if serve is None else serve
        self.json = json_data or pypi_json(wheel)
        self.honor_range = honor_range
        self.chunk_hook = chunk_hook
        self.fail_after = fail_after
        self.requests: list[tuple[str, dict]] = []

    def __call__(self, req, timeout=None):  # noqa: ANN001, ANN204
        url = req.full_url
        headers = {k.lower(): v for k, v in req.header_items()}
        self.requests.append((url, headers))
        if url.startswith("https://pypi.org/pypi/") and url.endswith("/json"):
            return FakeResp(json.dumps(self.json).encode())
        if url == WHEEL_URL:
            data = self.serve
            rng = headers.get("range")
            if rng and self.honor_range:
                start = int(rng.split("=")[1].rstrip("-"))
                return FakeResp(data[start:], 206, self.chunk_hook)
            if self.fail_after is not None:
                return FakeResp(data[: self.fail_after], 200, self.chunk_hook)   # bağlantı yarıda kesildi
            return FakeResp(data, 200, self.chunk_hook)
        raise urllib.error.HTTPError(url, 404, "yok", {}, None)

    def wheel_requests(self) -> list[dict]:
        return [h for u, h in self.requests if u == WHEEL_URL]


@pytest.fixture
def wheel() -> bytes:
    return make_wheel()


def installer(tmp_path: Path, wheel: bytes, server: FakeServer, **kw) -> CudaLibInstaller:  # noqa: ANN003
    kw.setdefault("disk_free", lambda p: 10 * 1024**3)
    return CudaLibInstaller(tmp_path / "cuda", (package_for(wheel),), opener=server, **kw)


# ---------- sabitlenmiş gerçek değerler ----------
def test_pinned_cublas_is_cuda_12_8_and_sizes_are_shown_before_download():
    assert CUBLAS.name == "nvidia-cublas-cu12" and CUBLAS.version.startswith("12.8.")   # Blackwell: cuBLAS >= 12.8
    assert CUBLAS.dlls == ("cublasLt64_12.dll", "cublas64_12.dll")                    # bağımlılık önce
    assert CUBLAS.wheel_size == 567_544_208 and len(CUBLAS.wheel_sha256) == 64
    text = cuda_libs.size_summary()
    assert "541 MB" in text and "752 MB" in text and "1,3 GB" in text
    assert cuda_libs.default_cuda_dir() == Path(os.environ["JARVIS_HOME"]).resolve() / "data" / "cuda"


# ---------- PyPI üst verisi ----------
def test_parse_wheel_info_picks_windows_wheel_and_checks_pin(wheel):
    pkg = package_for(wheel)
    info = parse_wheel_info(pypi_json(wheel), pkg)
    assert info.filename == WHEEL_NAME and info.url == WHEEL_URL and info.size == len(wheel)
    with pytest.raises(CudaLibError) as e:
        parse_wheel_info(pypi_json(wheel, sha="a" * 64), pkg)
    assert "uyuşmuyor" in e.value.user_message
    with pytest.raises(CudaLibError):
        parse_wheel_info(pypi_json(wheel, size=5), pkg)
    only_linux = pypi_json(wheel)
    only_linux["urls"] = only_linux["urls"][:1]
    with pytest.raises(CudaLibError) as e:
        parse_wheel_info(only_linux, pkg)
    assert "Windows paketi" in e.value.user_message
    with pytest.raises(CudaLibError):
        parse_wheel_info({"info": {}}, pkg)


# ---------- kurulum ----------
def test_install_extracts_only_wanted_dlls_atomically(tmp_path, wheel):
    server = FakeServer(wheel)
    inst = installer(tmp_path, wheel, server)
    events = []
    out = inst.install(events.append)
    assert out == tmp_path / "cuda" / "bin" and inst.installed()
    assert sorted(p.name for p in out.iterdir()) == sorted([*DLLS, cuda_libs.MANIFEST])
    for name, data in DLLS.items():
        assert (out / name).read_bytes() == data
    assert not (tmp_path / "evil.dll").exists() and not list(tmp_path.rglob("evil.dll"))
    assert not list(tmp_path.rglob("nvblas64_12.dll")) and not list(tmp_path.rglob("*.h"))
    assert list((tmp_path / "cuda" / "downloads").iterdir()) == []         # wheel silindi
    assert not (tmp_path / "cuda" / ".staging").exists()
    man = cuda_libs.read_manifest(tmp_path / "cuda")
    entry = man["packages"]["nvidia-cublas-cu12"]
    assert entry["version"] == "12.8.4.1" and entry["files"] == {n: len(d) for n, d in DLLS.items()}
    phases = [e.phase for e in events]
    assert "verify" in phases and "extract" in phases and events[-1].finished and events[-1].error is None


def test_install_is_idempotent_no_network_when_installed(tmp_path, wheel):
    server = FakeServer(wheel)
    inst = installer(tmp_path, wheel, server)
    inst.install()
    n = len(server.requests)

    def no_net(req, timeout=None):  # noqa: ANN001, ANN202
        raise AssertionError("kuruluyken ağa çıkılmamalı")

    inst.opener = no_net
    inst.install()
    assert n == 2   # JSON + wheel


def test_sha256_mismatch_is_rejected_and_partial_removed(tmp_path, wheel):
    corrupted = bytearray(wheel)
    corrupted[100] ^= 0xFF
    server = FakeServer(wheel, serve=bytes(corrupted))
    inst = installer(tmp_path, wheel, server)
    with pytest.raises(CudaLibError) as e:
        inst.install()
    assert "sha256" in e.value.user_message and "doğrulanamadı" in e.value.user_message
    assert not inst.installed() and not (tmp_path / "cuda" / "bin").exists()
    assert not list((tmp_path / "cuda" / "downloads").glob("*.part"))


def test_record_hash_mismatch_and_missing_dll_are_errors(tmp_path):
    bad = make_wheel(bad_record=True)
    with pytest.raises(CudaLibError) as e:
        installer(tmp_path / "a", bad, FakeServer(bad)).install()
    assert "doğrulanamadı" in e.value.user_message
    assert not (tmp_path / "a" / "cuda" / "bin").exists()

    only_one = make_wheel({"cublas64_12.dll": b"C" * 10})
    pkg = CudaPackage("nvidia-cublas-cu12", "12.8.4.1", ("cublasLt64_12.dll", "cublas64_12.dll"), len(only_one),
                      hashlib.sha256(only_one).hexdigest(), 10)
    inst = CudaLibInstaller(tmp_path / "b", (pkg,), opener=FakeServer(only_one), disk_free=lambda p: 1 << 40)
    with pytest.raises(CudaLibError) as e:
        inst.install()
    assert "cublasLt64_12.dll" in e.value.user_message


def test_extract_rejects_non_zip(tmp_path):
    f = tmp_path / "x.whl"
    f.write_bytes(b"zip degil")
    with pytest.raises(CudaLibError) as e:
        extract_dlls(f, tmp_path, ("cublas64_12.dll",))
    assert "bozuk" in e.value.user_message


def test_download_resumes_with_range(tmp_path, wheel):
    server = FakeServer(wheel)
    inst = installer(tmp_path, wheel, server)
    part = tmp_path / "cuda" / "downloads" / (WHEEL_NAME + ".part")
    part.parent.mkdir(parents=True)
    part.write_bytes(wheel[:1000])
    inst.install()
    assert server.wheel_requests()[0]["range"] == "bytes=1000-"
    assert inst.installed()


def test_download_restarts_when_server_ignores_range(tmp_path, wheel):
    server = FakeServer(wheel, honor_range=False)
    inst = installer(tmp_path, wheel, server)
    part = tmp_path / "cuda" / "downloads" / (WHEEL_NAME + ".part")
    part.parent.mkdir(parents=True)
    part.write_bytes(b"\0" * 1000)        # çöp önek: baştan inmezse sha256 tutmaz
    inst.install()
    assert inst.installed()


def test_interrupted_download_keeps_part_and_next_try_continues(tmp_path, wheel):
    server = FakeServer(wheel, fail_after=len(wheel) // 2)
    inst = installer(tmp_path, wheel, server)
    with pytest.raises(CudaLibError) as e:
        inst.install()
    assert "kaldığı yerden" in e.value.user_message
    part = tmp_path / "cuda" / "downloads" / (WHEEL_NAME + ".part")
    assert part.stat().st_size == len(wheel) // 2
    server.fail_after = None
    inst.install()
    assert server.wheel_requests()[-1]["range"] == f"bytes={len(wheel) // 2}-" and inst.installed()


def test_cancel_stops_download_and_resume_completes(tmp_path):
    big = make_wheel({"cublasLt64_12.dll": os.urandom(3_000_000), "cublas64_12.dll": os.urandom(1_000_000)})
    cancel = threading.Event()
    server = FakeServer(big, chunk_hook=cancel.set)        # ilk 1 MB'den sonra iptal
    inst = installer(tmp_path, big, server)
    with pytest.raises(CudaLibCancelled) as e:
        inst.install(cancel=cancel)
    assert "iptal" in e.value.user_message
    part = tmp_path / "cuda" / "downloads" / (WHEEL_NAME + ".part")
    assert 0 < part.stat().st_size < len(big) and not inst.installed()
    server.chunk_hook = None
    inst.install(cancel=threading.Event())
    assert inst.installed()


def test_failed_reinstall_keeps_previous_install(tmp_path, wheel):
    inst = installer(tmp_path, wheel, FakeServer(wheel))
    inst.install()
    before = sorted(p.name for p in inst.bin.iterdir())
    # Yeni bir sürüm isteniyor ama indirilen dosya bozuk: eski kurulum yerinde kalmalı
    newer = CudaPackage("nvidia-cublas-cu12", "12.9.9.9", tuple(DLLS), len(wheel), "f" * 64, 1)
    bad = CudaLibInstaller(tmp_path / "cuda", (newer,), opener=FakeServer(wheel, json_data=pypi_json(wheel, "f" * 64)),
                           disk_free=lambda p: 1 << 40)
    with pytest.raises(CudaLibError):
        bad.install()
    assert sorted(p.name for p in inst.bin.iterdir()) == before and inst.installed()


def test_finalize_replaces_older_install(tmp_path, wheel):
    root = tmp_path / "cuda"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / cuda_libs.MANIFEST).write_text(json.dumps({"packages": {"nvidia-cublas-cu12": {
        "version": "12.4.0", "files": {}}}}), encoding="utf-8")
    (root / "bin" / "eski.dll").write_bytes(b"x")
    inst = installer(tmp_path, wheel, FakeServer(wheel))
    assert not inst.installed()
    inst.install()
    assert inst.installed() and not (root / "bin" / "eski.dll").exists()
    assert not list(root.glob("bin.old-*"))


def test_not_enough_disk_space(tmp_path, wheel):
    server = FakeServer(wheel)
    inst = installer(tmp_path, wheel, server, disk_free=lambda p: 1000)
    with pytest.raises(CudaLibError) as e:
        inst.install()
    assert "Diskte yeterli yer yok" in e.value.user_message and server.requests == []


def test_offline_metadata_error_is_turkish(tmp_path, wheel):
    def offline(req, timeout=None):  # noqa: ANN001, ANN202
        raise urllib.error.URLError("çevrimdışı")

    inst = CudaLibInstaller(tmp_path, (package_for(wheel),), opener=offline, disk_free=lambda p: 1 << 40)
    events = []
    with pytest.raises(CudaLibError) as e:
        inst.install(events.append)
    assert "internet bağlantısı yok" in e.value.user_message
    assert events[-1].error and events[-1].text().startswith("Ekran kartı hızlandırması kurulamadı")


def test_is_installed_detects_tampering(tmp_path, wheel):
    inst = installer(tmp_path, wheel, FakeServer(wheel))
    inst.install()
    (inst.bin / "cublas64_12.dll").write_bytes(b"kisa")
    assert not inst.installed()


# ---------- DLL arama yolu ----------
def _installed_root(tmp_path: Path, wheel: bytes) -> Path:
    installer(tmp_path, wheel, FakeServer(wheel)).install()
    return tmp_path / "cuda"


def test_activate_adds_dll_directory_and_path_once(tmp_path, wheel):
    cuda_libs.reset_for_tests()
    root = _installed_root(tmp_path, wheel)
    pkgs = (package_for(wheel),)
    added = []
    env = {"PATH": r"C:\Windows\system32;C:\Windows"}
    assert activate(root, pkgs, platform="win32", environ=env, add_dll_directory=added.append)
    assert activate(root, pkgs, platform="win32", environ=env, add_dll_directory=added.append)   # idempotent
    d = str(root / "bin")
    assert added == [d]
    assert env["PATH"].split(";") == [d, r"C:\Windows\system32", r"C:\Windows"]
    cuda_libs.reset_for_tests()


def test_activate_noop_off_windows_or_when_missing(tmp_path, wheel):
    cuda_libs.reset_for_tests()
    env = {"PATH": "/usr/bin"}
    calls = []
    assert not activate(tmp_path / "yok", platform="win32", environ=env, add_dll_directory=calls.append)
    root = _installed_root(tmp_path, wheel)
    assert not activate(root, (package_for(wheel),), platform="linux", environ=env, add_dll_directory=calls.append)
    assert env == {"PATH": "/usr/bin"} and calls == []
    # Gerçek ortam (Linux CI): hiçbir şeye dokunmaz
    if sys.platform != "win32":
        before = os.environ.get("PATH")
        assert not activate(root, (package_for(wheel),))
        assert os.environ.get("PATH") == before


def test_preload_loads_full_paths_dependency_first(tmp_path, wheel):
    cuda_libs.reset_for_tests()
    root = _installed_root(tmp_path, wheel)
    pkgs = (package_for(wheel),)
    loaded = []
    assert preload(root, pkgs, platform="win32", loader=loaded.append) is None
    assert loaded == [str(root / "bin" / "cublasLt64_12.dll"), str(root / "bin" / "cublas64_12.dll")]
    assert preload(root, pkgs, platform="win32", loader=loaded.append) is None and len(loaded) == 2
    cuda_libs.reset_for_tests()

    def broken(path):  # noqa: ANN001, ANN202
        raise OSError("[WinError 126] modül bulunamadı")

    err = preload(root, pkgs, platform="win32", loader=broken)
    assert err and "cublasLt64_12.dll" in err
    assert preload(root, pkgs, platform="linux", loader=broken) is None
    cuda_libs.reset_for_tests()


# ---------- tanılama ----------
def test_missing_library_error_classification():
    assert is_missing_library_error(REAL_LOG_ERROR)
    assert is_missing_library_error("Could not locate cudnn_ops64_9.dll. Please make sure it is in your library path!")
    assert is_missing_library_error("libcublas.so.12: cannot open shared object file")
    assert not is_missing_library_error("CUDA failed with error out of memory")
    assert not is_missing_library_error("no kernel image is available for execution on the device")


def test_gpu_report_offer_logic_and_honest_texts():
    rtx = "NVIDIA GeForce RTX 5070 Ti"
    assert GpuReport(rtx, issue="libs_missing").offer_download
    assert not GpuReport("", issue="libs_missing").offer_download                 # NVIDIA yok
    assert not GpuReport(rtx, libs_installed=True, issue="gpu_failed").offer_download   # döngü yok
    assert not GpuReport(rtx, on_gpu=True).offer_download
    assert not GpuReport(rtx, issue="libs_missing", device_setting="cpu").offer_download
    assert not GpuReport(rtx, issue="gpu_failed").offer_download                  # başka bir GPU hatası
    assert "cuBLAS" in GpuReport(rtx, issue="libs_missing").summary()
    failed = GpuReport(rtx, libs_installed=True, issue="gpu_failed").summary()
    assert "kurulu ama" in failed and "işlemci" in failed
    assert "ekran kartında çalışıyor" in GpuReport(rtx, libs_installed=True, on_gpu=True).summary()
    assert "bulunamadı" in GpuReport().summary()


def test_progress_texts():
    p = cuda_libs.CudaProgress("download", 100 * 1_048_576, 541 * 1_048_576)
    assert p.text() == "Ekran kartı kütüphaneleri indiriliyor: %18 (100 / 541 MB)."
    assert "doğrulanıyor" in cuda_libs.CudaProgress("verify").text()
    assert "yeniden başlatılıyor" in cuda_libs.CudaProgress("restart").text()


# ---------- ses motoru yükleyicisi ----------
class FakeWhisper:
    loads: list[str] = []

    def __init__(self, model_dir, choice) -> None:  # noqa: ANN001
        self.choice = choice

    @property
    def label(self) -> str:
        return self.choice.label

    def load(self) -> None:
        FakeWhisper.loads.append(self.choice.label)
        if self.choice.device == "cuda" and FakeWhisper.gpu_error:
            raise STTError(FakeWhisper.gpu_error.format(label=self.label))

    def transcribe(self, audio: np.ndarray) -> Transcript:
        return Transcript("")


def _load_stt(tmp_path, monkeypatch, gpu_error: str | None, installed: bool = False, device: str = "auto",  # noqa: ANN001, ANN201
              gpu_name: str = "NVIDIA GeForce RTX 5070 Ti"):
    FakeWhisper.loads = []
    FakeWhisper.gpu_error = gpu_error
    monkeypatch.setattr("jarvis.voice.components.WhisperSTT", FakeWhisper)
    monkeypatch.setattr(stt_mod, "cuda_available", lambda: bool(gpu_name))
    monkeypatch.setattr(ModelManager, "ensure_whisper", lambda self, name, progress=None, cancel=None: tmp_path)
    calls = []
    monkeypatch.setattr(cuda_libs, "activate", lambda root=None, *a, **k: calls.append("activate") or installed)
    monkeypatch.setattr(cuda_libs, "preload", lambda root=None, *a, **k: calls.append("preload"))
    monkeypatch.setattr(cuda_libs, "is_installed", lambda root=None, *a, **k: installed)
    settings = SettingsStore(tmp_path / "s.json").load().voice
    settings.stt_device = device
    loader = RealComponentLoader(ModelManager(tmp_path), gpu_probe=lambda: gpu_name)
    notes: list[str] = []
    stt, label, report = loader._load_stt(settings, ModelManager(tmp_path), lambda p: None, threading.Event(), notes)
    return stt, label, report, notes, calls


def test_loader_reports_missing_cublas_and_falls_back_to_cpu(tmp_path, monkeypatch):
    stt, label, report, notes, calls = _load_stt(tmp_path, monkeypatch, REAL_LOG_ERROR)
    assert label == "small (CPU, int8)" and FakeWhisper.loads == ["large-v3-turbo (GPU, float16)", "small (CPU, int8)"]
    assert notes == [CUDA_MISSING_NOTE]
    assert report.issue == "libs_missing" and report.offer_download and not report.on_gpu
    assert calls == ["activate"]          # kurulu değilken önceden yükleme denenmez


def test_loader_uses_gpu_when_libs_installed(tmp_path, monkeypatch):
    stt, label, report, notes, calls = _load_stt(tmp_path, monkeypatch, None, installed=True)
    assert label == "large-v3-turbo (GPU, float16)" and report.on_gpu and not report.offer_download
    assert notes == [] and calls == ["activate", "preload"]


def test_loader_honest_when_gpu_fails_even_with_libs(tmp_path, monkeypatch):
    err = "Whisper yüklenemedi ({label}): CUDA failed with error no kernel image is available for execution"
    stt, label, report, notes, calls = _load_stt(tmp_path, monkeypatch, err, installed=True)
    assert label == "small (CPU, int8)" and notes == [CUDA_STILL_FAILING_NOTE]
    assert report.issue == "gpu_failed" and report.libs_installed and not report.offer_download
    assert "no kernel image" in report.detail


def test_loader_other_gpu_error_keeps_generic_note(tmp_path, monkeypatch):
    stt, label, report, notes, calls = _load_stt(tmp_path, monkeypatch, "Whisper yüklenemedi: out of memory")
    assert notes == [CUDA_GENERIC_NOTE] and not report.offer_download


def test_loader_without_nvidia_has_no_offer(tmp_path, monkeypatch):
    stt, label, report, notes, calls = _load_stt(tmp_path, monkeypatch, None, gpu_name="")
    assert label == "small (CPU, int8)" and report.gpu_name == "" and not report.offer_download and notes == []


def test_loader_nvidia_but_no_cuda_device_does_not_offer(tmp_path, monkeypatch):
    """NVML kartı görüyor ama ctranslate2 CUDA aygıtı göremiyor (ör. eski sürücü): cuBLAS indirmek işe yaramaz."""
    _load_stt(tmp_path, monkeypatch, None)            # yamaları kur
    monkeypatch.setattr(stt_mod, "cuda_available", lambda: False)
    settings = SettingsStore(tmp_path / "s2.json").load().voice
    loader = RealComponentLoader(ModelManager(tmp_path), gpu_probe=lambda: "NVIDIA GeForce RTX 5070 Ti")
    stt, label, report = loader._load_stt(settings, ModelManager(tmp_path), lambda p: None, threading.Event(), [])
    assert label == "small (CPU, int8)" and report.issue == "no_cuda" and not report.offer_download
    assert "sürücüsü" in report.summary()


# ---------- gerçek Windows (kullanıcının bilgisayarı / CI) ----------
@pytest.mark.windows
def test_windows_plain_loadlibrary_finds_dll_after_activate(tmp_path):
    """ctranslate2'nin yaptığı gibi düz LoadLibraryW("ad.dll"): PATH'e eklenen klasörde bulunmalı."""
    import ctypes

    cuda_libs.reset_for_tests()
    src = Path(sys.base_prefix) / "DLLs" / "_socket.pyd"
    if not src.is_file():
        pytest.skip("deneme için küçük bir DLL bulunamadı")
    name = "jarvis_cuda_deneme64.dll"
    root = tmp_path / "cuda"
    (root / "bin").mkdir(parents=True)
    data = src.read_bytes()
    (root / "bin" / name).write_bytes(data)
    pkg = CudaPackage("deneme", "1", (name,), 0, "0" * 64, len(data))
    (root / "bin" / cuda_libs.MANIFEST).write_text(json.dumps({"packages": {"deneme": {
        "version": "1", "files": {name: len(data)}}}}), encoding="utf-8")
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.LoadLibraryW.restype = ctypes.c_void_p
    k32.FreeLibrary.argtypes = [ctypes.c_void_p]
    old_path = os.environ.get("PATH", "")
    try:
        assert not k32.LoadLibraryW(name)                     # henüz arama yolunda değil
        assert activate(root, (pkg,))
        handle = k32.LoadLibraryW(name)
        assert handle, f"LoadLibraryW bulamadı (hata {ctypes.get_last_error()})"
        k32.FreeLibrary(handle)
    finally:
        os.environ["PATH"] = old_path
        cuda_libs.reset_for_tests()


@pytest.mark.windows
def test_windows_downloaded_cublas_is_usable_by_ctranslate2():
    """Kullanıcının bilgisayarında: cuBLAS indirildiyse ctranslate2 onu bulabilmeli. CI'da (GPU yok) atlanır."""
    if not is_installed():
        pytest.skip("ekran kartı kütüphaneleri indirilmemiş (Ayarlar > Ses > Ekran kartı hızlandırması)")
    ct2 = pytest.importorskip("ctranslate2")
    import ctypes

    assert activate() and preload() is None
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.LoadLibraryW.restype = ctypes.c_void_p
    assert k32.LoadLibraryW("cublas64_12.dll"), "cublas64_12.dll adıyla bulunamadı"
    if ct2.get_cuda_device_count() < 1:
        pytest.skip("CUDA aygıtı yok")
    assert "float16" in ct2.get_supported_compute_types("cuda")
