"""Ayarlar ekranının "Ses" bölümü: aygıt seçimi, mikrofon testi, kalibrasyon, ses denemesi, hız.

Ayrı bir widget'tır; `SettingsView` içine yerleştirilir. Aygıt listesi arka planda okunur
(PortAudio sorgusu arayüzü bekletmesin). Aygıt, ses ya da model değişikliği "Ses motorunu
yeniden başlat" ile uygulanır; eşikler ve süreler anında uygulanır.
"""

from __future__ import annotations

import asyncio
import logging

from PySide6.QtCore import QLocale, Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                               QProgressBar, QPushButton, QSlider, QVBoxLayout, QWidget)

from jarvis.core.assistant import Assistant
from jarvis.core.errors import JarvisError
from jarvis.core.events import Topics
from jarvis.interface.theme import C
from jarvis.voice.audio_io import AudioDevice, AudioUnavailable, list_devices, load_backend
from jarvis.voice.cuda_libs import default_cuda_dir, size_summary
from jarvis.voice.models import PIPER_VOICES, default_models_dir

log = logging.getLogger(__name__)

STT_CHOICES = [
    ("auto", "Otomatik (ekran kartı varsa large-v3-turbo, yoksa small)"),
    ("small", "small (işlemci için dengeli, ~460 MB)"),
    ("medium", "medium (daha doğru, daha yavaş, ~1,5 GB)"),
    ("large-v3-turbo", "large-v3-turbo (en doğru; ekran kartı önerilir, ~1,6 GB)"),
]
DEVICE_CHOICES = [("auto", "Otomatik"), ("cpu", "İşlemci (CPU)"), ("cuda", "NVIDIA ekran kartı (CUDA)")]
DEFAULT_DEVICE = "Sistem varsayılanı"


def _tr_spin(value: float, lo: float, hi: float, step: float, suffix: str) -> QDoubleSpinBox:
    sb = QDoubleSpinBox()
    sb.setLocale(QLocale(QLocale.Language.Turkish, QLocale.Country.Turkey))
    sb.setRange(lo, hi)
    sb.setSingleStep(step)
    sb.setDecimals(1)
    sb.setSuffix(suffix)
    sb.setValue(value)
    sb.setMaximumWidth(140)
    return sb


class VoicePanel(QWidget):
    def __init__(self, assistant: Assistant, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.assistant = assistant
        self.voice = assistant.voice
        v = assistant.settings.voice
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        self.status = QLabel(objectName="BosMetin")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        self.input_combo = QComboBox()
        self.output_combo = QComboBox()
        for c in (self.input_combo, self.output_combo):
            c.addItem("Aygıtlar okunuyor…", None)
            c.setEnabled(False)
            c.setMinimumWidth(320)
        self.input_combo.currentIndexChanged.connect(lambda _: self._device_changed("input"))
        self.output_combo.currentIndexChanged.connect(lambda _: self._device_changed("output"))
        form.addRow("Mikrofon", self.input_combo)
        form.addRow("Hoparlör", self.output_combo)

        # Mikrofon testi: canlı seviye
        self.meter = QProgressBar(objectName="SesSeviyesi")
        self.meter.setRange(0, 100)
        self.meter.setTextVisible(False)
        self.meter.setFixedWidth(320)
        form.addRow("Mikrofon seviyesi", self.meter)

        self.voice_combo = QComboBox()
        for key, info in PIPER_VOICES.items():
            self.voice_combo.addItem(f"{key}  ({info.gender_note})", key)
        idx = self.voice_combo.findData(v.tts_voice)
        self.voice_combo.setCurrentIndex(max(0, idx))
        self.voice_combo.currentIndexChanged.connect(self._apply_restart_settings)
        form.addRow("JARVIS'in sesi", self.voice_combo)

        self.rate = QSlider(Qt.Orientation.Horizontal)
        self.rate.setRange(50, 200)
        self.rate.setValue(int(round(v.speech_rate * 100)))
        self.rate.setFixedWidth(220)
        self.rate_label = QLabel()
        self.rate.valueChanged.connect(self._apply_live_settings)
        rate_row = QHBoxLayout()
        rate_row.addWidget(self.rate)
        rate_row.addWidget(self.rate_label)
        rate_row.addStretch(1)
        form.addRow("Konuşma hızı", rate_row)

        self.sensitivity = QSlider(Qt.Orientation.Horizontal)
        self.sensitivity.setRange(0, 100)
        self.sensitivity.setValue(int(round(v.wake_sensitivity * 100)))
        self.sensitivity.setFixedWidth(220)
        self.sensitivity.setToolTip("Yüksek: daha kolay uyanır ama yanlışlıkla uyanma artar.")
        self.sensitivity.valueChanged.connect(self._apply_live_settings)
        form.addRow("Uyandırma hassasiyeti", self.sensitivity)

        words = QHBoxLayout()
        self.hey = QCheckBox("\"Hey Jarvis\"")
        self.solo = QCheckBox("Tek başına \"Jarvis\"")
        self.hey.setChecked("hey_jarvis" in v.wake_words)
        self.solo.setChecked("jarvis" in v.wake_words)
        for cb in (self.hey, self.solo):
            cb.toggled.connect(self._apply_live_settings)
            words.addWidget(cb)
        words.addStretch(1)
        form.addRow("Uyandırma kelimesi", words)

        custom_row = QHBoxLayout()
        self.custom_label = QLabel(objectName="Not")
        self.custom_btn = QPushButton("Özel model seç…", objectName="Dugme")
        self.custom_clear = QPushButton("Kaldır", objectName="Dugme")
        self.custom_btn.clicked.connect(self._pick_custom_model)
        self.custom_clear.clicked.connect(self._clear_custom_model)
        custom_row.addWidget(self.custom_btn)
        custom_row.addWidget(self.custom_clear)
        custom_row.addWidget(self.custom_label, 1)
        form.addRow("Özel uyandırma modeli", custom_row)

        self.follow = _tr_spin(v.follow_up_s, 0, 30, 1, " sn")
        self.follow.setToolTip("Cevaptan sonra uyandırma kelimesi demeden devam edebileceğin süre. 0: kapalı.")
        self.follow.valueChanged.connect(self._apply_live_settings)
        form.addRow("Konuşma modu süresi", self.follow)
        self.silence = _tr_spin(v.silence_timeout_s, 0.3, 3.0, 0.1, " sn")
        self.silence.setToolTip("Bu kadar sessizlik olunca sözünün bittiğini anlarım.")
        self.silence.valueChanged.connect(self._apply_live_settings)
        form.addRow("Söz sonu sessizliği", self.silence)

        self.barge = QCheckBox("Konuşurken sözümü kesebilirsin")
        self.barge.setChecked(v.barge_in)
        self.barge.toggled.connect(self._apply_live_settings)
        self.chime = QCheckBox("Uyanınca kısa bir ses çal")
        self.chime.setChecked(v.wake_chime)
        self.chime.toggled.connect(self._apply_live_settings)
        self.wake_verify = QCheckBox("Türkçe söylenişi de tanı (\"Carvis\", \"Cervis\")")
        self.wake_verify.setToolTip("Uyandırma modeli emin olamadığında kısa konuşma parçası yerelde yazıya "
                                    "çevrilip kontrol edilir. \"Carvis, Chrome'u aç\" gibi tek cümle de çalışır.")
        self.wake_verify.setChecked(v.wake_verify)
        self.wake_verify.toggled.connect(self._apply_live_settings)
        form.addRow("", self.barge)
        form.addRow("", self.chime)
        form.addRow("", self.wake_verify)

        self.stt_combo = QComboBox()
        for key, label in STT_CHOICES:
            self.stt_combo.addItem(label, key)
        if self.stt_combo.findData(v.stt_model) < 0:
            self.stt_combo.addItem(v.stt_model, v.stt_model)
        self.stt_combo.setCurrentIndex(self.stt_combo.findData(v.stt_model))
        self.stt_combo.currentIndexChanged.connect(self._apply_restart_settings)
        self.stt_device = QComboBox()
        for key, label in DEVICE_CHOICES:
            self.stt_device.addItem(label, key)
        self.stt_device.setCurrentIndex(self.stt_device.findData(v.stt_device))
        self.stt_device.currentIndexChanged.connect(self._apply_restart_settings)
        form.addRow("Konuşma tanıma modeli", self.stt_combo)
        form.addRow("Konuşma tanıma donanımı", self.stt_device)

        # Ekran kartı hızlandırması: NVIDIA kart var ama cuBLAS eksikse tek tıkla indirme
        gpu_box = QVBoxLayout()
        gpu_box.setSpacing(4)
        self.gpu_info = QLabel(objectName="Not")
        self.gpu_info.setWordWrap(True)
        self.gpu_info.setTextFormat(Qt.TextFormat.RichText)
        gpu_row = QHBoxLayout()
        self.gpu_btn = QPushButton("Ekran kartı hızlandırmasını indir", objectName="Dugme")
        self.gpu_btn.setToolTip(f"NVIDIA cuBLAS kütüphanesi şu klasöre kurulur: {default_cuda_dir()}")
        self.gpu_btn.clicked.connect(self.start_gpu_download)
        self.gpu_bar = QProgressBar(objectName="SesSeviyesi")
        self.gpu_bar.setRange(0, 1000)
        self.gpu_bar.setTextVisible(False)
        self.gpu_bar.setFixedWidth(220)
        self.gpu_cancel = QPushButton("İptal", objectName="Dugme")
        self.gpu_cancel.clicked.connect(self.voice.cancel_gpu_install)
        for wdg in (self.gpu_btn, self.gpu_bar, self.gpu_cancel):
            gpu_row.addWidget(wdg)
        gpu_row.addStretch(1)
        self.gpu_result = QLabel(objectName="Not")
        self.gpu_result.setWordWrap(True)
        self.gpu_result.hide()
        gpu_box.addWidget(self.gpu_info)
        gpu_box.addLayout(gpu_row)
        gpu_box.addWidget(self.gpu_result)
        form.addRow("Ekran kartı hızlandırması", gpu_box)
        lay.addLayout(form)

        buttons = QHBoxLayout()
        self.calibrate_btn = QPushButton("Ortam gürültüsünü ölç", objectName="Dugme")
        self.test_btn = QPushButton("Sesi dene", objectName="Dugme")
        self.restart_btn = QPushButton("Ses motorunu yeniden başlat", objectName="Dugme")
        self.calibrate_btn.setToolTip("2 saniye sessiz kal; ortam gürültüsü ölçülür ve eşikler ayarlanır.")
        self.calibrate_btn.clicked.connect(lambda: self.assistant.spawn(self._calibrate(), "voice-calibrate"))
        self.test_btn.clicked.connect(lambda: self.assistant.spawn(self._speak_test(), "voice-test"))
        self.restart_btn.clicked.connect(self.restart)
        for b in (self.calibrate_btn, self.test_btn, self.restart_btn):
            buttons.addWidget(b)
        buttons.addStretch(1)
        lay.addLayout(buttons)
        self.result = QLabel(objectName="Not")
        self.result.setWordWrap(True)
        lay.addWidget(self.result)
        self.restart_hint = QLabel("Aygıt, ses ya da model değişikliği yeniden başlatınca uygulanır.", objectName="Not")
        self.restart_hint.hide()
        lay.addWidget(self.restart_hint)
        privacy = QLabel("Mikrofon sesi bilgisayarından çıkmaz: uyandırma, konuşma tanıma ve sentez yereldir. "
                         f"Claude'a yalnızca tanınan metin gider. Modeller: {default_models_dir()}", objectName="Not")
        privacy.setWordWrap(True)
        privacy.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(privacy)

        self._devices_loaded = False
        self._update_labels()
        bus = assistant.bus
        self._unsubs = [bus.subscribe(Topics.VOICE_STATUS, self.show_status),
                        bus.subscribe(Topics.VOICE_LEVEL, self._on_level),
                        bus.subscribe(Topics.VOICE_GPU, self._on_gpu_progress)]
        self.show_status(self.voice.status)

    # ---------- aygıtlar ----------
    def showEvent(self, event) -> None:  # noqa: ANN001
        super().showEvent(event)
        if not self._devices_loaded:
            self._devices_loaded = True
            self.refresh_devices()

    def refresh_devices(self) -> None:
        try:
            self.assistant.spawn(self._load_devices(), "voice-devices")
        except RuntimeError:
            log.debug("Olay döngüsü yok, aygıtlar okunmadı")

    async def _load_devices(self) -> None:
        def query() -> tuple[list[AudioDevice], list[AudioDevice]]:
            backend = load_backend()
            return list_devices(backend, "input"), list_devices(backend, "output")
        try:
            ins, outs = await asyncio.get_running_loop().run_in_executor(None, query)
        except AudioUnavailable as e:
            self.fill_devices([], [], e.user_message)
            return
        self.fill_devices(ins, outs)

    def fill_devices(self, ins: list[AudioDevice], outs: list[AudioDevice], error: str | None = None) -> None:
        v = self.assistant.settings.voice
        for combo, devs, current in ((self.input_combo, ins, v.input_device),
                                     (self.output_combo, outs, v.output_device)):
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(DEFAULT_DEVICE, None)
            for d in devs:
                combo.addItem(d.name + ("  (varsayılan)" if d.is_default_input or d.is_default_output else ""), d.name)
            if current and combo.findData(current) < 0:
                combo.addItem(f"{current} (bağlı değil)", current)
            combo.setCurrentIndex(max(0, combo.findData(current)) if current else 0)
            combo.setEnabled(error is None)
            combo.blockSignals(False)
        if error:
            self.result.setText(error)

    def _device_changed(self, kind: str) -> None:
        v = self.assistant.settings.voice
        if kind == "input":
            v.input_device = self.input_combo.currentData()
        else:
            v.output_device = self.output_combo.currentData()
        self._save(restart=True)

    # ---------- ayarlar ----------
    def _update_labels(self) -> None:
        self.rate_label.setText(f"{self.rate.value() / 100:.2f}".replace(".", ",") + " kat")
        path = self.assistant.settings.voice.custom_wake_model_path
        self.custom_label.setText(path or "Yok (hazır \"Hey Jarvis\" modeli kullanılıyor)")
        self.custom_clear.setEnabled(bool(path))

    def _apply_live_settings(self) -> None:
        v = self.assistant.settings.voice
        v.speech_rate = round(self.rate.value() / 100, 2)
        v.wake_sensitivity = round(self.sensitivity.value() / 100, 2)
        words = [w for w, cb in (("hey_jarvis", self.hey), ("jarvis", self.solo)) if cb.isChecked()]
        v.wake_words = words or ["hey_jarvis"]
        if not words:
            self.hey.setChecked(True)
        v.follow_up_s = round(self.follow.value(), 1)
        v.silence_timeout_s = round(self.silence.value(), 1)
        v.barge_in = self.barge.isChecked()
        v.wake_chime = self.chime.isChecked()
        v.wake_verify = self.wake_verify.isChecked()
        self._update_labels()
        self._save(restart=False)
        self.voice.apply_settings()

    def _apply_restart_settings(self) -> None:
        v = self.assistant.settings.voice
        v.tts_voice = self.voice_combo.currentData()
        v.stt_model = self.stt_combo.currentData()
        v.stt_device = self.stt_device.currentData()
        self._save(restart=True)

    def _save(self, restart: bool) -> None:
        self.assistant.store.save()
        self.assistant.bus.publish(Topics.SETTINGS_CHANGED, self.assistant.settings)
        if restart:
            self.restart_hint.show()

    def _pick_custom_model(self) -> None:
        dlg = QFileDialog(self, "Özel uyandırma modeli (.onnx)")
        dlg.setNameFilter("openWakeWord modeli (*.onnx)")
        dlg.setFileMode(QFileDialog.FileMode.ExistingFile)
        dlg.fileSelected.connect(self._set_custom_model)
        dlg.open()   # exec() değil: asyncio + Qt döngüsü iç içe kilitlenmesin
        self._dialog = dlg

    def _set_custom_model(self, path: str) -> None:
        self.assistant.settings.voice.custom_wake_model_path = path or None
        self._update_labels()
        self._save(restart=True)

    def _clear_custom_model(self) -> None:
        self._set_custom_model("")

    # ---------- eylemler ----------
    def restart(self) -> None:
        self.restart_hint.hide()
        self.result.setText("Ses motoru yeniden başlatılıyor…")
        self.assistant.spawn(self.voice.restart(), "voice-restart")

    def start_gpu_download(self) -> None:
        """Boyut zaten düğmenin üstünde yazıyor; tıklayınca indirme arka planda başlar."""
        self.gpu_btn.hide()
        self.gpu_result.hide()
        self.assistant.spawn(self.voice.install_gpu_libs(), "voice-gpu")

    async def _calibrate(self) -> None:
        self.calibrate_btn.setEnabled(False)
        self.result.setText("Ölçüyorum, 2 saniye sessiz kal…")
        try:
            profile = await self.voice.calibrate(2.0)
            self.result.setText(profile.description + " Eşikler buna göre ayarlandı.")
        except JarvisError as e:
            self.result.setText(e.user_message)
        except asyncio.TimeoutError:
            self.result.setText("Mikrofondan ses gelmedi; aygıtı kontrol et.")
        finally:
            self.calibrate_btn.setEnabled(True)

    async def _speak_test(self) -> None:
        self.test_btn.setEnabled(False)
        try:
            await self.voice.speak_test()
            self.result.setText("")
        except JarvisError as e:
            self.result.setText(e.user_message)
        finally:
            self.test_btn.setEnabled(True)

    # ---------- olaylar ----------
    def _on_level(self, lv) -> None:  # noqa: ANN001
        if lv.source == "mic":
            self.meter.setValue(int(lv.level * 100))

    def show_status(self, status) -> None:  # noqa: ANN001
        color = C.CYAN if status.ready else (C.SIS if status.starting else C.AMBER)
        parts = [f"<span style='color:{color}'>Durum: {'hazır' if status.ready else 'hazır değil'}</span>",
                 status.detail]
        if status.ready:
            parts.append(f"Konuşma tanıma: {status.stt_label or '?'}. Uyandırma kelimesi: "
                         f"{'açık' if status.wake else 'kullanılamıyor'}. Sesli yanıt: {'açık' if status.tts else 'kapalı'}.")
        self.status.setText("<br>".join(p for p in parts if p))
        self.test_btn.setEnabled(status.tts)
        self.calibrate_btn.setEnabled(status.ready and status.mic.name != "OFF")
        self._update_gpu(status)

    # ---------- ekran kartı hızlandırması ----------
    def _update_gpu(self, status) -> None:  # noqa: ANN001
        gpu = getattr(status, "gpu", None)
        installing = self.voice.gpu_installing
        if gpu is None:
            text = ("Ses motoru hazırlanınca ekran kartı denetlenir." if status.starting
                    else "Ekran kartı durumu bilinmiyor (ses motoru hazır değil).")
        else:
            text = gpu.summary()
            if gpu.offer_download and not installing:
                text += "<br>" + size_summary()
        self.gpu_info.setText(text)
        self.gpu_btn.setVisible(bool(gpu is not None and gpu.offer_download and not installing))
        self.gpu_bar.setVisible(installing)
        self.gpu_cancel.setVisible(installing)

    def _on_gpu_progress(self, p) -> None:  # noqa: ANN001
        done = p.finished or p.error is not None
        self.gpu_bar.setVisible(not done)
        self.gpu_cancel.setVisible(not done and p.phase in ("download", "restart"))
        if not done:
            self.gpu_btn.hide()
            frac = p.fraction
            if frac is None:
                self.gpu_bar.setRange(0, 0)     # belirsiz (doğrulama, çıkarma, yeniden başlatma)
            else:
                self.gpu_bar.setRange(0, 1000)
                self.gpu_bar.setValue(int(frac * 1000))
        color = C.CYAN if p.finished and p.ok else (C.AMBER if done else C.SIS)
        self.gpu_result.setStyleSheet(f"color:{color};")
        self.gpu_result.setText(p.text())
        self.gpu_result.show()
        if done:
            self.show_status(self.voice.status)   # düğme görünürlüğünü yeni duruma göre yenile
