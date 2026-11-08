"""Ekran kartı denemesi (ayrı süreç): başarı, çökme, takılma (zaman aşımı), iptal, işaret dosyası
ve ses motoruna bağlanması (takılan deneme işlemciye düşer, ses motoru asılı kalmaz)."""

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from jarvis.config.settings import SettingsStore
from jarvis.voice import cuda_libs, gpu_probe
from jarvis.voice import stt as stt_mod
from jarvis.voice.components import GpuCheckProgress, RealComponentLoader
from jarvis.voice.gpu_probe import ProbeCancel, ProbeResult, run_probe
from jarvis.voice.models import ModelManager


def script_command(body: str):  # noqa: ANN201
    """Alt süreçte verilen Python kodunu çalıştıran komut (OUT değişkeni sonuç dosyası)."""
    def command(model_dir: Path, out: Path) -> list[str]:
        code = f"import json, sys, time\nOUT = {str(out)!r}\n" + body
        return [sys.executable, "-c", code]
    return command


def test_success_reports_ok():
    res = run_probe(Path("model"), command=script_command("open(OUT,'w').write(json.dumps({'ok': True}))"),
                    poll_s=0.05)
    assert res.ok and not res.timed_out and res.detail == ""


def test_error_message_is_carried_back():
    body = "open(OUT,'w').write(json.dumps({'ok': False, 'error': 'RuntimeError: no kernel image'}))\nsys.exit(1)"
    res = run_probe(Path("model"), command=script_command(body), poll_s=0.05)
    assert not res.ok and "no kernel image" in res.detail


def test_crash_without_result_is_honest():
    res = run_probe(Path("model"), command=script_command("import os; os._exit(-1073741819 & 0xFF)"), poll_s=0.05)
    assert not res.ok and "çıkış kodu" in res.detail


def test_hang_is_killed_after_timeout_and_ticks_are_reported():
    ticks: list[float] = []
    t0 = time.monotonic()
    res = run_probe(Path("model"), command=script_command("time.sleep(60)"), timeout_s=1.0, poll_s=0.05,
                    on_tick=ticks.append)
    assert res.timed_out and not res.ok and time.monotonic() - t0 < 15
    assert ticks and ticks[-1] < 1.1


def test_cancel_kills_probe():
    ev = threading.Event()
    threading.Timer(0.3, ev.set).start()
    res = run_probe(Path("model"), command=script_command("time.sleep(60)"), cancel=ProbeCancel(None, ev),
                    poll_s=0.05)
    assert res.cancelled and not res.ok


def test_probe_main_writes_failure_instead_of_crashing(tmp_path):
    out = tmp_path / "r.json"
    code = gpu_probe.probe_main([str(tmp_path / "olmayan-model"), str(out)])
    data = json.loads(out.read_text(encoding="utf-8"))
    assert code == 1 and data["ok"] is False and data["error"]


def test_known_good_marker_roundtrip(tmp_path):
    model = tmp_path / "large-v3-turbo"
    model.mkdir()
    assert not gpu_probe.known_good(model, tmp_path / "cuda")
    gpu_probe.remember_good(model, tmp_path / "cuda")
    assert gpu_probe.known_good(model, tmp_path / "cuda")
    assert not gpu_probe.known_good(tmp_path / "small", tmp_path / "cuda")   # başka model: yeniden dene


def test_cuda_cache_is_enlarged_but_user_value_kept():
    env: dict = {}
    gpu_probe.tune_cuda_cache(env)
    assert int(env["CUDA_CACHE_MAXSIZE"]) >= 2**31
    env = {"CUDA_CACHE_MAXSIZE": "123"}
    gpu_probe.tune_cuda_cache(env)
    assert env["CUDA_CACHE_MAXSIZE"] == "123"


def test_frozen_app_uses_flag(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    cmd = gpu_probe.probe_command(Path("m"), Path("o.json"))
    assert cmd[1] == gpu_probe.PROBE_FLAG and cmd[0] == sys.executable


def test_app_main_dispatches_probe_without_qt(monkeypatch, tmp_path):
    from jarvis import app

    seen = []
    monkeypatch.setattr(gpu_probe, "probe_main", lambda argv: seen.append(argv) or 0)
    assert app.main(["--gpu-probe", "M", "O"]) == 0 and seen == [["M", "O"]]


# ---------- ses motoruna bağlanma ----------
class FakeWhisper:
    loads: list[str] = []

    def __init__(self, model_dir, choice, *a, **k) -> None:  # noqa: ANN001
        self.choice = choice

    @property
    def label(self) -> str:
        return self.choice.label

    def load(self) -> None:
        FakeWhisper.loads.append(self.choice.label)


def make_loader(tmp_path, monkeypatch, probe_result: ProbeResult | None, known: bool = False):  # noqa: ANN001, ANN201
    FakeWhisper.loads = []
    monkeypatch.setattr("jarvis.voice.components.WhisperSTT", FakeWhisper)
    monkeypatch.setattr(stt_mod, "cuda_available", lambda: True)
    monkeypatch.setattr(ModelManager, "ensure_whisper", lambda self, name, progress=None, cancel=None: tmp_path / name)
    monkeypatch.setattr(cuda_libs, "activate", lambda root=None, *a, **k: True)
    monkeypatch.setattr(cuda_libs, "preload", lambda root=None, *a, **k: None)
    monkeypatch.setattr(cuda_libs, "is_installed", lambda root=None, *a, **k: True)
    monkeypatch.setattr(gpu_probe, "known_good", lambda model_dir, root=None: known)
    remembered = []
    monkeypatch.setattr(gpu_probe, "remember_good", lambda model_dir, root=None: remembered.append(model_dir.name))
    loader = RealComponentLoader(ModelManager(tmp_path), gpu_probe=lambda: "NVIDIA GeForce RTX 5070 Ti")
    calls = []

    def fake_check(model_dir, *, cancel, on_tick):  # noqa: ANN001, ANN202
        calls.append(model_dir.name)
        on_tick(12.0)
        if probe_result is None:
            pytest.fail("bilinen iyi yapılandırmada deneme yapılmamalı")
        return probe_result

    loader.gpu_check = fake_check
    return loader, calls, remembered


def run_stt(loader, tmp_path):  # noqa: ANN001, ANN201
    settings = SettingsStore(tmp_path / "s.json").load().voice
    progress: list = []
    notes: list[str] = []
    stt, label, report = loader._load_stt(settings, ModelManager(tmp_path), progress.append, threading.Event(), notes)
    return label, report, notes, progress


def test_probe_ok_loads_on_gpu_and_remembers(tmp_path, monkeypatch):
    loader, calls, remembered = make_loader(tmp_path, monkeypatch, ProbeResult(True, seconds=40))
    label, report, notes, progress = run_stt(loader, tmp_path)
    assert label == "large-v3-turbo (GPU, float16)" and report.on_gpu
    assert calls == ["large-v3-turbo"] and remembered == ["large-v3-turbo"]
    tick = [p for p in progress if isinstance(p, GpuCheckProgress)][0]
    assert "Ekran kartı deneniyor (0:12)" in tick.text()


def test_hanging_gpu_falls_back_to_cpu_without_loading_cuda_in_process(tmp_path, monkeypatch):
    loader, calls, remembered = make_loader(
        tmp_path, monkeypatch, ProbeResult(False, "ekran kartı 4 dakikada hazır olmadı (takıldı)", 240, timed_out=True))
    label, report, notes, _ = run_stt(loader, tmp_path)
    assert label == "small (CPU, int8)" and not report.on_gpu and report.issue == "gpu_failed"
    assert "takıldı" in report.detail and remembered == []
    assert FakeWhisper.loads == ["small (CPU, int8)"]          # GPU modeli süreç içinde hiç yüklenmedi


def test_known_good_skips_probe(tmp_path, monkeypatch):
    loader, calls, _ = make_loader(tmp_path, monkeypatch, None, known=True)
    label, report, _, _ = run_stt(loader, tmp_path)
    assert label == "large-v3-turbo (GPU, float16)" and calls == []


def test_gpu_check_progress_is_not_shown_as_download():
    p = GpuCheckProgress(done=75)
    assert "1:15" in p.text() and "indiriliyor" not in p.text() and p.fraction is None
