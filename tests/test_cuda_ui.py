"""Ekran kartı hızlandırması arayüzü (ekransız Qt): bant düğmesi, onay penceresi (boyutla),
Ayarlar > Ses bölümündeki düğme/ilerleme, indirme sonrası ses motorunun yeniden başlatılması,
iptal ve "kütüphaneler kurulu ama GPU yine çalışmıyor" durumunda dürüst mesaj (döngü yok)."""

import asyncio
import dataclasses
import threading
import time
from pathlib import Path

import pytest
import qasync

from tests.voice_fakes import FakeBackend, FakeSTT, FakeTTS
from jarvis.app import configure_application
from jarvis.config.settings import SettingsStore
from jarvis.core.assistant import Assistant
from jarvis.core.events import Topics
from jarvis.interface import theme
from jarvis.interface.main_window import MainWindow
from jarvis.paths import AppPaths
from jarvis.voice.audio_io import AudioPlayer, MicrophoneStream
from jarvis.voice.components import CUDA_MISSING_NOTE, CUDA_STILL_FAILING_NOTE, VoiceComponents
from jarvis.voice.cuda_libs import CudaLibCancelled, CudaLibError, CudaProgress, GpuReport
from jarvis.voice.vad import EnergyVad

RTX = "NVIDIA GeForce RTX 5070 Ti"
MISSING = GpuReport(RTX, issue="libs_missing", detail="Library cublas64_12.dll is not found or cannot be loaded")
ON_GPU = GpuReport(RTX, libs_installed=True, on_gpu=True)
STILL_FAILING = GpuReport(RTX, libs_installed=True, issue="gpu_failed", detail="no kernel image is available")


@pytest.fixture
def loop(qapp):
    configure_application(qapp)
    lp = qasync.QEventLoop(qapp, already_running=False)
    asyncio.set_event_loop(lp)
    yield lp
    lp.close()
    asyncio.set_event_loop(None)


def comps(backend: FakeBackend, gpu: GpuReport | None, label: str, notes: list[str] | None = None) -> VoiceComponents:
    return VoiceComponents(vad=EnergyVad(-70), mic_factory=lambda: MicrophoneStream(backend),
                           player=AudioPlayer(backend), stt=FakeSTT([]), tts=FakeTTS(0.1),
                           notes=list(notes or []), stt_label=label, gpu=gpu)


class SeqLoader:
    """Her yüklemede sıradaki parçaları verir (ilk açılış, indirme sonrası yeniden başlatma…)."""

    def __init__(self, *items: VoiceComponents) -> None:
        self.items = list(items)
        self.calls = 0

    def load(self, settings, progress, cancel, gain):  # noqa: ANN001, ANN201
        self.calls += 1
        return self.items[min(self.calls, len(self.items)) - 1]


class FakeInstaller:
    def __init__(self, fail: str | None = None, block: bool = False) -> None:
        self.fail, self.block = fail, block
        self.calls = 0
        self.cancel: threading.Event | None = None
        self.started = threading.Event()

    def install(self, progress, cancel):  # noqa: ANN001, ANN201
        self.calls += 1
        self.cancel = cancel
        progress(CudaProgress("download", 0, 100 * 2**20))
        progress(CudaProgress("download", 50 * 2**20, 100 * 2**20))
        self.started.set()
        if self.block:
            if not cancel.wait(5):
                raise AssertionError("iptal gelmedi")
            raise CudaLibCancelled("iptal", user_message="İndirme iptal edildi. Tekrar başlatınca kaldığı yerden "
                                                         "devam eder.")
        if self.fail:
            raise CudaLibError("x", user_message=self.fail)
        progress(CudaProgress("verify", 100 * 2**20, 100 * 2**20))
        return Path("cuda/bin")


def make(loader, installer):  # noqa: ANN001, ANN201
    store = SettingsStore(AppPaths.resolve().settings_file)
    store.load()
    a = Assistant(store, sample_interval_s=0.5, voice_loader=loader)
    a.voice.cuda_installer = installer
    w = MainWindow(a, theme.load_fonts())
    return a, w


async def until(cond, timeout=5.0):  # noqa: ANN001, ANN201
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("koşul sağlanmadı")


def test_banner_action_confirms_size_downloads_and_switches_to_gpu(qapp, loop):
    backend = FakeBackend()
    loader = SeqLoader(comps(backend, MISSING, "small (CPU, int8)", [CUDA_MISSING_NOTE]),
                       comps(backend, ON_GPU, "large-v3-turbo (GPU, float16)"))
    inst = FakeInstaller()
    a, w = make(loader, inst)
    w.show()
    panel = w.settings_view.voice_panel
    banner = w.panel.banner
    progress: list[CudaProgress] = []
    a.bus.subscribe(Topics.VOICE_GPU, progress.append)
    # Bandın, not geldiği andaki hâli (sonra Claude anahtarı uyarısı gibi başka bir bant üstüne yazabilir)
    seen = []
    a.bus.subscribe(Topics.NOTICE, lambda msg: seen.append(
        (banner.isVisible(), banner.text.text(), banner.property("seviye"), banner.action.text(),
         not banner.action.isHidden(), banner._action_cb)))

    async def go():
        await a.start()
        await until(lambda: bool(seen))
        # Amber bant: CPU'ya düşüldü + tek tık düğmesi
        visible, text, level, action, action_shown, callback = seen[0]
        assert visible and CUDA_MISSING_NOTE in text and level == "uyari"
        assert action == "Ekran kartı hızlandırmasını indir" and action_shown
        # Ayarlar > Ses: düğme görünür, boyut düğmeden ÖNCE yazıyor
        await until(lambda: panel.gpu_btn.isVisibleTo(panel))
        assert "541 MB" in panel.gpu_info.text()
        assert "cuBLAS" in panel.gpu_info.text() and not panel.gpu_bar.isVisibleTo(panel)
        callback()                                              # = bant düğmesine tıklama
        box = w._gpu_box
        assert box.isVisible() and "541 MB" in box.informativeText() and "752 MB" in box.informativeText()
        assert inst.calls == 0                                    # onaylanmadan indirme yok
        next(b for b in box.buttons() if b.text() == "İndir").click()
        await until(lambda: inst.calls == 1)
        await until(lambda: progress and progress[-1].finished)
        assert loader.calls == 2                                  # ses motoru yeniden başlatıldı
        await a.shutdown()

    loop.run_until_complete(go())
    phases = [p.phase for p in progress]
    assert phases[0] == "download" and "verify" in phases and "restart" in phases
    final = progress[-1]
    assert final.ok and final.text() == ("Ekran kartı hızlandırması etkin: konuşma tanıma "
                                         "large-v3-turbo (GPU, float16).")
    assert banner.text.text() == final.text() and banner.property("seviye") == "bilgi"
    assert a.voice.status.stt_label == "large-v3-turbo (GPU, float16)"
    assert not panel.gpu_btn.isVisibleTo(panel) and "ekran kartında çalışıyor" in panel.gpu_info.text()
    assert "large-v3-turbo (GPU, float16)" in panel.gpu_result.text()


def test_panel_button_and_honest_message_when_gpu_still_fails(qapp, loop):
    backend = FakeBackend()
    loader = SeqLoader(comps(backend, MISSING, "small (CPU, int8)", [CUDA_MISSING_NOTE]),
                       comps(backend, STILL_FAILING, "small (CPU, int8)", [CUDA_STILL_FAILING_NOTE]))
    inst = FakeInstaller()
    a, w = make(loader, inst)
    panel = w.settings_view.voice_panel
    banner = w.panel.banner

    async def go():
        await a.start()
        await until(lambda: panel.gpu_btn.isVisibleTo(panel))
        panel.gpu_btn.click()
        assert not panel.gpu_btn.isVisibleTo(panel)
        await until(lambda: a.voice.gpu_progress is not None and a.voice.gpu_progress.finished)
        await a.shutdown()

    loop.run_until_complete(go())
    final = a.voice.gpu_progress
    assert not final.ok and "kurulu ama" in final.text() and "small (CPU, int8)" in final.text()
    assert banner.property("seviye") == "uyari" and banner.text.text() == final.text()
    assert banner.action.text() != "Ekran kartı hızlandırmasını indir" or banner.action.isHidden()
    # Döngü yok: tek indirme, tek yeniden başlatma; düğme artık teklif edilmiyor
    assert inst.calls == 1 and loader.calls == 2
    assert not a.voice.gpu_offer and not panel.gpu_btn.isVisibleTo(panel)
    assert "kurulu ama" in panel.gpu_info.text()


def test_cancel_from_panel_and_retry_offer(qapp, loop):
    backend = FakeBackend()
    loader = SeqLoader(comps(backend, MISSING, "small (CPU, int8)", [CUDA_MISSING_NOTE]))
    inst = FakeInstaller(block=True)
    a, w = make(loader, inst)
    panel = w.settings_view.voice_panel
    banner = w.panel.banner

    async def go():
        await a.start()
        await until(lambda: panel.gpu_btn.isVisibleTo(panel))
        panel.gpu_btn.click()
        await until(lambda: inst.started.is_set() and panel.gpu_cancel.isVisibleTo(panel))
        await until(lambda: "%50" in panel.gpu_result.text())
        assert panel.gpu_bar.isVisibleTo(panel) and panel.gpu_bar.value() == 500
        panel.gpu_cancel.click()
        await until(lambda: a.voice.gpu_progress is not None and a.voice.gpu_progress.error)
        await a.shutdown()

    loop.run_until_complete(go())
    assert "iptal" in a.voice.gpu_progress.text() and loader.calls == 1     # yeniden başlatılmadı
    assert banner.action.text() == "Tekrar dene" and not banner.action.isHidden()
    assert panel.gpu_btn.isVisibleTo(panel) and not panel.gpu_bar.isVisibleTo(panel)


def test_download_error_is_shown_and_retry_works(qapp, loop):
    backend = FakeBackend()
    loader = SeqLoader(comps(backend, MISSING, "small (CPU, int8)", [CUDA_MISSING_NOTE]),
                       comps(backend, ON_GPU, "large-v3-turbo (GPU, float16)"))
    inst = FakeInstaller(fail="internet bağlantısı yok ya da sunucuya ulaşılamadı.")
    a, w = make(loader, inst)
    banner = w.panel.banner

    async def go():
        await a.start()
        await until(lambda: a.voice.ready)
        w.start_gpu_download()
        await until(lambda: a.voice.gpu_progress is not None and a.voice.gpu_progress.error)
        assert "internet bağlantısı yok" in banner.text.text() and banner.action.text() == "Tekrar dene"
        inst.fail = None
        banner.action.click()
        await until(lambda: a.voice.gpu_progress.finished)
        await a.shutdown()

    loop.run_until_complete(go())
    assert inst.calls == 2 and a.voice.gpu_progress.ok


def test_no_offer_without_nvidia_or_when_cpu_selected(qapp, loop):
    backend = FakeBackend()
    loader = SeqLoader(comps(backend, GpuReport("", device_setting="auto"), "small (CPU, int8)"))
    a, w = make(loader, FakeInstaller())
    panel = w.settings_view.voice_panel

    async def go():
        await a.start()
        await until(lambda: a.voice.ready)
        # Aynı not gelse bile teklif koşulu (NVIDIA kart + eksik cuBLAS) yoksa düğme eklenmez
        a.bus.publish(Topics.NOTICE, CUDA_MISSING_NOTE)
        assert w.panel.banner.action.isHidden()
        await a.shutdown()

    loop.run_until_complete(go())
    assert not panel.gpu_btn.isVisibleTo(panel) and "bulunamadı" in panel.gpu_info.text()
    cpu = dataclasses.replace(MISSING, device_setting="cpu")
    st = dataclasses.replace(a.voice.status, gpu=cpu)
    panel.show_status(st)
    assert not panel.gpu_btn.isVisibleTo(panel) and "işlemci seçili" in panel.gpu_info.text()


def test_shutdown_cancels_running_download(qapp, loop):
    backend = FakeBackend()
    loader = SeqLoader(comps(backend, MISSING, "small (CPU, int8)", [CUDA_MISSING_NOTE]))
    inst = FakeInstaller(block=True)
    a, w = make(loader, inst)

    async def go():
        await a.start()
        await until(lambda: a.voice.ready)
        w.start_gpu_download()
        await until(inst.started.is_set)
        t0 = time.perf_counter()
        await a.shutdown()
        assert time.perf_counter() - t0 < 2.0                    # kapanış indirmeyi beklemez

    loop.run_until_complete(go())
    assert inst.cancel is not None and inst.cancel.is_set()
