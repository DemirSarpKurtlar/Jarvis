"""Sesli konuşma döngüsü.

    BEKLEMEDE --(uyandırma)--> UYANDI --> DİNLİYOR --(söz bitti)--> DÜŞÜNÜYOR (STT + Claude)
        ^                                    ^                          |
        |                                    | (sözü kesme)             v
        +----(takip süresi doldu)---- DİNLİYOR <------------------- KONUŞUYOR

Thread düzeni:
* **Ses thread'i** (`FrameProcessor.run`): mikrofondan 80 ms'lik kareleri alır; VAD, uyandırma
  kelimesi, söz sonu ve sözü kesme kararlarını verir. Hafif işler (birkaç ms/kare).
* **asyncio (Qt) thread'i** (`VoiceManager`): durum makinesini yönetir, olayları yayınlar,
  Claude'a yalnızca tanınan METNİ gönderir (`ChatService.send`). Hiçbir zaman beklemez.
* **Executor thread'leri**: model indirme/yükleme, konuşma tanıma, konuşma sentezi.

Mikrofon sesi hiçbir yere gönderilmez: ses kareleri yalnızca bu süreçteki yerel modellere gider.
"""

from __future__ import annotations

import asyncio
import dataclasses
import enum
import logging
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import numpy as np

from jarvis.config.settings import SettingsStore, VoiceSettings
from jarvis.core.errors import JarvisError
from jarvis.core.events import EventBus, Topics
from jarvis.core.state import AssistantState, StateChange, StateMachine
from jarvis.voice.audio_io import Frame, MicrophoneStream, make_chime, volume_to_gain
from jarvis.voice.components import ComponentLoader, RealComponentLoader, VoiceComponents, VoiceDisabled
from jarvis.voice.echo import EchoGuard
from jarvis.voice.models import DownloadProgress
from jarvis.voice.stt import Transcript, clean_transcript
from jarvis.voice.tts import speakable_text, split_sentences
from jarvis.voice.vad import (EndpointConfig, Endpointer, EndpointKind, EnergyVad, NoiseProfile, VadModel,
                              measure_noise, speech_threshold_for_noise)
from jarvis.voice.wake_word import WakeDetection, WakeWordDetector, hey_threshold_from_sensitivity

log = logging.getLogger(__name__)
S = AssistantState


# ---------- dışarıya yayınlanan veriler ----------
class MicState(str, enum.Enum):
    OFF = "Kapalı"                       # kullanıcı kapattı: akış kapalı
    IDLE = "Dinlemiyor"                  # ses motoru hazır değil ya da düşünüyor
    WAKE = "Uyandırma bekleniyor"
    LISTENING = "Dinliyor"


@dataclass(frozen=True)
class VoiceStatus:
    mic: MicState
    ready: bool            # sesli komut verilebilir mi (mikrofon + konuşma tanıma)
    wake: bool             # uyandırma kelimesi çalışıyor mu
    tts: bool              # sesli yanıt var mı
    detail: str            # kullanıcıya açıklama
    stt_label: str = ""
    starting: bool = False


@dataclass(frozen=True)
class VoiceLevel:
    source: str            # "mic" | "tts"
    level: float           # 0..1


# ---------- ses thread'inden gelen olaylar ----------
@dataclass(frozen=True)
class WakeEvent:
    epoch: int
    detection: WakeDetection


@dataclass(frozen=True)
class SpeechStartEvent:
    epoch: int


@dataclass(frozen=True)
class UtteranceEvent:
    epoch: int
    audio: np.ndarray
    truncated: bool = False


@dataclass(frozen=True)
class NoSpeechEvent:
    epoch: int


@dataclass(frozen=True)
class BargeInEvent:
    epoch: int
    reason: str            # "speech" | "wake"


@dataclass(frozen=True)
class ProcessorError:
    epoch: int
    message: str


class Mode(enum.Enum):
    PAUSED = "paused"      # kareler atılır (düşünürken)
    WAKE = "wake"          # uyandırma kelimesi bekleniyor
    LISTEN = "listen"      # söz sonu bekleniyor
    BARGE = "barge"        # JARVIS konuşuyor, sözü kesme bekleniyor


class FrameProcessor:
    """Ses thread'inde çalışan karar verici. `process()` thread'den bağımsız test edilebilir."""

    def __init__(self, vad: VadModel, wake: WakeWordDetector | None, echo: EchoGuard | None,
                 endpoint: EndpointConfig, post: Callable[[object], None], barge_threshold: float = 0.8,
                 barge_frames: int = 3, preroll_frames: int = 6) -> None:
        self.vad = vad
        self.wake = wake
        self.echo = echo
        self.endpointer = Endpointer(endpoint)
        self.post = post
        self.barge_threshold = barge_threshold
        self.barge_frames = barge_frames
        self._ring: deque[np.ndarray] = deque(maxlen=preroll_frames)
        self._lock = threading.RLock()
        self._mode = Mode.PAUSED
        self._epoch = 0
        self._barge_run = 0
        self._end_requested = False
        self._taps: list[Callable[[Frame], bool]] = []
        self.frames = 0
        self.last_prob = 0.0
        self._errors = 0

    # ---- denetim (asyncio thread'inden) ----
    @property
    def mode(self) -> Mode:
        return self._mode

    @property
    def epoch(self) -> int:
        return self._epoch

    def set_mode(self, mode: Mode, no_speech_timeout_s: float | None = None) -> int:
        with self._lock:
            self._epoch += 1
            self._mode = mode
            self._barge_run = 0
            self._end_requested = False
            if mode is Mode.LISTEN:
                self.endpointer.reset(no_speech_timeout_s)
            return self._epoch

    def request_end(self) -> None:
        """"Konuş" düğmesine dinlerken basıldı: konuşma varsa şimdi bitir, yoksa iptal et."""
        with self._lock:
            self._end_requested = True

    def add_tap(self, fn: Callable[[Frame], bool]) -> None:
        """Her karede çağrılır; True döndürünce kaldırılır (kalibrasyon için)."""
        with self._lock:
            self._taps.append(fn)

    # ---- ses thread'i ----
    def run(self, mic: MicrophoneStream, stop: threading.Event) -> None:
        while not stop.is_set():
            frame = mic.read(timeout=0.1)
            if frame is None:
                continue
            try:
                self.process(frame)
                self._errors = 0
            except Exception as e:  # noqa: BLE001 - ses thread'i çökmesin
                self._errors += 1
                if self._errors <= 3:
                    log.exception("Ses karesi işlenemedi")
                if self._errors == 3:
                    self.post(ProcessorError(self._epoch, f"Ses işlenirken hata: {type(e).__name__}"))

    def process(self, frame: Frame) -> None:
        samples = frame.samples
        self.frames += 1
        with self._lock:
            taps = list(self._taps)
        for tap in taps:
            if tap(frame):
                with self._lock:
                    if tap in self._taps:
                        self._taps.remove(tap)
        prob = self.vad.frame_prob(samples)
        self.last_prob = prob
        # Uyandırma modeli her karede beslenir: 1,3 sn'lik bağlamı hep sıcak kalsın
        det = self.wake.feed(samples, frame.t) if self.wake is not None else None
        # Mod kararları kilit altında: denetleyici aynı anda modu değiştiremesin
        with self._lock:
            mode, end_requested = self._mode, self._end_requested
            self._end_requested = False
            self._ring.append(samples)
            if mode is Mode.WAKE:
                if det is not None:
                    epoch = self._switch_to_listen()
                    self.post(WakeEvent(epoch, det))
            elif mode is Mode.LISTEN:
                self._listen(samples, prob, end_requested)
            elif mode is Mode.BARGE:
                self._barge(frame, prob, det)

    def _listen(self, samples: np.ndarray, prob: float, end_requested: bool) -> None:
        if end_requested:
            self._force_end()
            return
        ev = self.endpointer.feed(samples, prob)
        if ev is None:
            return
        if ev.kind is EndpointKind.SPEECH_START:
            self.post(SpeechStartEvent(self._epoch))
        elif ev.kind in (EndpointKind.UTTERANCE, EndpointKind.MAX_LENGTH):
            epoch = self.set_mode(Mode.PAUSED)
            self.post(UtteranceEvent(epoch, ev.audio, ev.kind is EndpointKind.MAX_LENGTH))  # type: ignore[arg-type]
        elif ev.kind is EndpointKind.NO_SPEECH:
            epoch = self.set_mode(Mode.PAUSED)
            self.post(NoSpeechEvent(epoch))

    def _barge(self, frame: Frame, prob: float, det: WakeDetection | None) -> None:
        user = True
        if self.echo is not None:
            user = self.echo.assess(frame.samples, frame.t).user_likely
        if det is not None and user:
            epoch = self._switch_to_listen()
            self.post(BargeInEvent(epoch, "wake"))
            return
        self._barge_run = self._barge_run + 1 if (prob >= self.barge_threshold and user) else 0
        if self._barge_run >= self.barge_frames:
            self._epoch += 1
            self._mode = Mode.LISTEN
            self._barge_run = 0
            # Kullanıcı zaten konuşuyor: son kareler sözün başı
            self.endpointer.reset(None, preroll=list(self._ring), speech_started=True)
            self.post(BargeInEvent(self._epoch, "speech"))

    def _switch_to_listen(self) -> int:
        with self._lock:
            self._epoch += 1
            self._mode = Mode.LISTEN
            self._barge_run = 0
            self.endpointer.reset(None)
            return self._epoch

    def _force_end(self) -> None:
        ep = self.endpointer
        audio = ep.collected()
        if audio is not None and ep.speech_s > 0.2:
            epoch = self.set_mode(Mode.PAUSED)
            self.post(UtteranceEvent(epoch, audio))
        else:
            epoch = self.set_mode(Mode.PAUSED)
            self.post(NoSpeechEvent(epoch))


# ---------- denetleyici ----------
class VoiceManager:
    LEVEL_INTERVAL_S = 0.05
    WAKE_TO_LISTEN_S = 0.3

    def __init__(self, store: SettingsStore, bus: EventBus, state: StateMachine, chat: Any,
                 loader: ComponentLoader | None = None, sleep: Callable[[float], None] = time.sleep,
                 send: Callable[[str], Awaitable[Any]] | None = None) -> None:
        self.store = store
        self.bus = bus
        self.state = state
        self.chat = chat
        # Tanınan metnin gideceği yol. Assistant burada `_handle_text` verir: yazılı metinle aynı
        # (önce yerel komutlar, örn. "Not Defteri'ni aç", Claude'suz; sonra Claude).
        self._send = send or chat.send
        self.loader = loader or RealComponentLoader()
        self._sleep = sleep
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="jarvis-voice")
        self._loop: asyncio.AbstractEventLoop | None = None
        self._cancel = threading.Event()
        self.components: VoiceComponents | None = None
        self.processor: FrameProcessor | None = None
        self.mic: MicrophoneStream | None = None
        self._audio_thread: threading.Thread | None = None
        self._audio_stop = threading.Event()
        self._speak_cancel = threading.Event()
        # "iptal mi?" kontrolü ile çalmaya başlama arasına durdurma giremesin (yarış: kısa ses sızması)
        self._speak_lock = threading.Lock()
        self._speaking = False
        self._busy = False                # bir sesli istek işleniyor
        self._empty_streak = 0
        self._tasks: set[asyncio.Task] = set()
        self._error: str | None = None
        self._starting = False
        self.running = False
        self.status = VoiceStatus(MicState.IDLE, False, False, False, "Ses motoru henüz başlatılmadı.")
        self._unsubs = [
            bus.subscribe(Topics.MICROPHONE_TOGGLED, self._on_mic_toggled),
            bus.subscribe(Topics.STATE_CHANGED, self._on_state_changed),
        ]

    @property
    def settings(self) -> VoiceSettings:
        return self.store.settings.voice

    @property
    def ready(self) -> bool:
        return self.components is not None and self.components.stt is not None

    # ---------- yaşam döngüsü ----------
    async def start(self) -> None:
        """Arka planda çağrılır; pencere açılışını asla bekletmez. Hata fırlatmaz, durum yayınlar."""
        if self.running or self._starting:
            return
        self._loop = asyncio.get_running_loop()
        self._starting = True
        self._cancel.clear()
        self._error = None
        self._publish_status()
        try:
            comps = await self._loop.run_in_executor(
                self._executor, self.loader.load, self.settings.model_copy(deep=True),
                self._progress_threadsafe, self._cancel, self._gain)
        except VoiceDisabled as e:
            self._error = e.user_message
            log.info("Ses motoru başlatılmadı: %s", e)
        except JarvisError as e:
            self._error = e.user_message
            log.warning("Ses motoru başlatılamadı: %s", e)
            self.bus.publish(Topics.VOICE_ERROR, e.user_message)
        except Exception:  # noqa: BLE001
            self._error = "Ses motoru beklenmeyen bir hata nedeniyle başlatılamadı. Ayrıntılar log dosyasında."
            log.exception("Ses motoru başlatılamadı")
            self.bus.publish(Topics.VOICE_ERROR, self._error)
        else:
            if self._cancel.is_set():   # yüklenirken kapatıldı ya da yeniden başlatıldı
                return
            self.components = comps
            echo = EchoGuard(comps.player.history) if comps.player is not None else None
            self.processor = FrameProcessor(comps.vad, comps.wake, echo, self._endpoint_config(), self._post)
            self.running = True
            if comps.stt is None:
                self._error = next((n for n in comps.notes if n.startswith("Sesli komut kapalı")),
                                   "Konuşma tanıma hazır değil.")
            if comps.notes:   # tek bant: art arda uyarılar birbirinin üstüne yazmasın
                self.bus.publish(Topics.NOTICE, " ".join(comps.notes))
            if self.ready and self.settings.microphone_enabled:
                await self._open_mic()
            self._sync_mode()
            self._spawn(self._level_pump(), "voice-level")
            log.info("Ses motoru hazır (uyandırma=%s, sentez=%s, tanıma=%s)", comps.wake is not None,
                     comps.tts is not None, comps.stt_label or "yok")
        finally:
            self._starting = False
            self._publish_status()

    async def shutdown(self) -> None:
        self._cancel.set()
        self.running = False
        self._stop_speaking()
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._close_mic()
        if self.components is not None and self.components.player is not None:
            self.components.player.close()
        for u in self._unsubs:
            u()
        self._unsubs.clear()
        self._executor.shutdown(wait=False, cancel_futures=True)
        log.info("Ses motoru kapatıldı")

    async def restart(self) -> None:
        """Aygıt ya da model ayarı değişince: her şeyi kapatıp yeniden hazırlar."""
        self._cancel.set()
        while self._starting:   # süren indirme/yükleme iptali görsün
            await asyncio.sleep(0.05)
        self._stop_speaking()
        await self._close_mic()
        if self.components is not None and self.components.player is not None:
            self.components.player.close()
        self.components = None
        self.processor = None
        self.running = False
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.state.state not in (S.STANDBY, S.ERROR):
            self.state.reset()
        await self.start()

    def _endpoint_config(self) -> EndpointConfig:
        v = self.settings
        return EndpointConfig(threshold=speech_threshold_for_noise(0.5, v.noise_floor_dbfs),
                              silence_timeout_s=v.silence_timeout_s, max_utterance_s=v.max_utterance_s,
                              no_speech_timeout_s=v.no_speech_timeout_s)

    def apply_settings(self) -> None:
        """Yeniden başlatma gerektirmeyen ayarlar (sessizlik süresi, eşikler) hemen uygulanır."""
        if self.processor is not None:
            self.processor.endpointer.config = self._endpoint_config()
        comps = self.components
        if comps is not None and comps.wake is not None:
            words = set(self.settings.wake_words)
            comps.wake.hey_threshold = hey_threshold_from_sensitivity(self.settings.wake_sensitivity)
            comps.wake.hey_enabled = "hey_jarvis" in words or not words
            comps.wake.jarvis_threshold = (self.settings.jarvis_threshold
                                           if ("jarvis" in words or comps.wake.custom_labels) else None)
        self._publish_status()

    # ---------- mikrofon ----------
    async def _open_mic(self) -> None:
        if self.mic is not None or self.components is None or self.processor is None:
            return
        mic = self.components.mic_factory()
        try:
            await asyncio.get_running_loop().run_in_executor(self._executor, mic.start)
        except JarvisError as e:
            self._error = e.user_message
            self.bus.publish(Topics.VOICE_ERROR, e.user_message)
            return
        except Exception as e:  # noqa: BLE001
            self._error = "Mikrofon açılamadı."
            log.exception("Mikrofon açılamadı")
            self.bus.publish(Topics.VOICE_ERROR, f"Mikrofon açılamadı ({type(e).__name__}).")
            return
        self._error = None
        self.mic = mic
        self._audio_stop = threading.Event()
        proc, stop = self.processor, self._audio_stop
        self._audio_thread = threading.Thread(target=proc.run, args=(mic, stop), name="jarvis-voice-audio",
                                              daemon=True)
        self._audio_thread.start()

    async def _close_mic(self) -> None:
        mic, thread = self.mic, self._audio_thread
        self.mic, self._audio_thread = None, None
        self._audio_stop.set()
        if thread is not None:
            await asyncio.get_running_loop().run_in_executor(None, thread.join, 1.0)
        if mic is not None:
            await asyncio.get_running_loop().run_in_executor(None, mic.stop)

    def _on_mic_toggled(self, enabled: bool) -> None:
        if self._loop is None:
            return
        if not enabled:
            if self.state.state in (S.WAKE_DETECTED, S.LISTENING):
                self.state.reset()
            # Kapatma gecikmesin: akış hemen kapanır (thread birleşmesi arka planda)
            self._spawn(self._close_and_publish(), "mic-close")
        elif self.ready and self.running:
            self._spawn(self._open_and_sync(), "mic-open")
        self._publish_status()

    async def _close_and_publish(self) -> None:
        await self._close_mic()
        self._publish_status()

    async def _open_and_sync(self) -> None:
        await self._open_mic()
        self._sync_mode()
        self._publish_status()

    # ---------- olaylar ----------
    def _post(self, ev: object) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._on_event, ev)
        except RuntimeError:
            pass  # döngü kapanıyor

    def _on_event(self, ev: object) -> None:
        proc = self.processor
        if proc is None or not self.running:
            return
        if isinstance(ev, ProcessorError):
            self.bus.publish(Topics.VOICE_ERROR, ev.message)
            return
        if getattr(ev, "epoch", None) != proc.epoch:
            log.debug("Eski ses olayı atlandı: %r", type(ev).__name__)
            return
        if isinstance(ev, WakeEvent):
            self._on_wake(ev.detection)
        elif isinstance(ev, SpeechStartEvent):
            if self.state.state is S.WAKE_DETECTED:
                self.state.transition(S.LISTENING, "Dinliyorum")
        elif isinstance(ev, UtteranceEvent):
            self._spawn(self._handle_utterance(ev.audio, ev.truncated), "voice-utterance")
        elif isinstance(ev, NoSpeechEvent):
            if self.state.state in (S.WAKE_DETECTED, S.LISTENING):
                self.state.reset()
        elif isinstance(ev, BargeInEvent):
            self._on_barge_in(ev.reason)

    def _on_wake(self, det: WakeDetection) -> None:
        if self.state.state is S.ERROR:
            self.state.reset()
        if self.state.state is not S.STANDBY or self._busy or getattr(self.chat, "busy", False):
            self._sync_mode(force=True)
            return
        self.state.transition(S.WAKE_DETECTED, "Hey Jarvis" if det.label == "hey_jarvis" else "Jarvis")
        if self.settings.wake_chime:
            self._play_chime()
        assert self._loop is not None
        self._loop.call_later(self.WAKE_TO_LISTEN_S, self._wake_to_listening)

    def _wake_to_listening(self) -> None:
        if self.state.state is S.WAKE_DETECTED:
            self.state.transition(S.LISTENING, "Dinliyorum")

    def _on_barge_in(self, reason: str) -> None:
        if self.state.state is not S.SPEAKING:
            self._sync_mode(force=True)
            return
        log.info("Sözüm kesildi (%s)", "uyandırma kelimesi" if reason == "wake" else "konuşma")
        self._stop_speaking()
        self.state.transition(S.LISTENING, "Dinliyorum")

    def _on_state_changed(self, change: StateChange) -> None:
        self._sync_mode()
        self._publish_status()

    def _sync_mode(self, force: bool = False) -> None:
        """İşleyicinin modunu durum makinesine uydurur. Zaten doğru moddaysa dokunmaz
        (yoksa ses thread'inin başlattığı dinleme sıfırlanırdı)."""
        proc = self.processor
        if proc is None:
            return
        st = self.state.state
        if st in (S.STANDBY, S.ERROR):
            desired = Mode.WAKE if self.components and self.components.wake is not None and not self._busy else Mode.PAUSED
        elif st in (S.WAKE_DETECTED, S.LISTENING):
            desired = Mode.LISTEN
        elif st is S.SPEAKING:
            desired = Mode.BARGE if (self._speaking and self.settings.barge_in) else Mode.PAUSED
        else:
            desired = Mode.PAUSED
        if force or proc.mode is not desired:
            proc.set_mode(desired)

    # ---------- Konuş düğmesi ----------
    def activate(self) -> None:
        """Elle başlatma: uyandırma kelimesi çalışmasa da sesli komut verilebilir."""
        if not self.ready:
            self.bus.publish(Topics.NOTICE, self.status.detail or "Ses motoru hazır değil.")
            return
        if not self.settings.microphone_enabled or self.mic is None:
            self.bus.publish(Topics.NOTICE, "Mikrofon kapalı. Konuşmak için önce mikrofonu aç.")
            return
        st = self.state.state
        assert self.processor is not None
        if st is S.SPEAKING:
            self._stop_speaking()
            self.processor.set_mode(Mode.LISTEN)
            self.state.transition(S.LISTENING, "Dinliyorum")
        elif st in (S.WAKE_DETECTED, S.LISTENING):
            self.processor.request_end()
        elif st in (S.STANDBY, S.ERROR):
            if self._busy or getattr(self.chat, "busy", False):
                self.bus.publish(Topics.NOTICE, "Önce şu anki isteği bitiriyorum.")
                return
            if st is S.ERROR:
                self.state.reset()
            self.processor.set_mode(Mode.LISTEN)
            self.state.transition(S.LISTENING, "Dinliyorum")
        else:
            self.bus.publish(Topics.NOTICE, "Önce şu anki isteği bitiriyorum.")

    # ---------- bir sesli isteğin işlenmesi ----------
    async def _handle_utterance(self, audio: np.ndarray, truncated: bool) -> None:
        comps = self.components
        if comps is None or comps.stt is None:
            return
        if self.state.state is S.WAKE_DETECTED:
            self.state.transition(S.LISTENING, "Dinliyorum")
        if self.state.state is not S.LISTENING:
            self._sync_mode(force=True)
            return
        if truncated:
            log.info("Konuşma en uzun süreye ulaştı, olduğu kadarı işleniyor")
        self._busy = True
        try:
            self.state.transition(S.PROCESSING, "Söylediğini yazıya çeviriyorum")
            try:
                tr: Transcript = await asyncio.get_running_loop().run_in_executor(
                    self._executor, comps.stt.transcribe, audio)
            except Exception as e:  # noqa: BLE001
                log.exception("Konuşma tanıma hatası")
                msg = e.user_message if isinstance(e, JarvisError) else "Söylediğini yazıya çeviremedim."
                self.bus.publish(Topics.VOICE_ERROR, msg)
                self.state.reset()
                return
            tr = dataclasses.replace(tr, text=clean_transcript(tr.text))
            # Gizlilik: logda metnin kendisi değil yalnızca uzunluğu
            log.info("Konuşma tanındı: %d karakter, %.1f sn ses, %.2f sn işlem", len(tr.text), tr.duration_s,
                     tr.seconds)
            self.bus.publish(Topics.VOICE_TRANSCRIPT, tr)
            if not tr.text:
                # Gürültüde "anlayamadım" döngüsüne girme: art arda ikincisinde sessizce beklemeye dön
                self._empty_streak += 1
                if self._empty_streak == 1:
                    await self._speak("Anlayamadım, tekrar söyler misin?", follow_up=True)
                else:
                    self._empty_streak = 0
                    self.state.reset()
                return
            self._empty_streak = 0
            failed: list[str] = []
            answers: list[Any] = []   # yerel komutlar sonucu CHAT_DONE ile duyurur, değer döndürmez
            unsubs = [self.bus.subscribe(Topics.CHAT_FAILED, failed.append),
                      self.bus.subscribe(Topics.CHAT_DONE, answers.append)]
            try:
                done = await self._send(tr.text)
            except Exception:  # noqa: BLE001
                log.exception("Sesli istek Claude'a iletilemedi")
                self.bus.publish(Topics.VOICE_ERROR, "İsteğini iletirken beklenmeyen bir hata oldu.")
                done = None
            finally:
                for u in unsubs:
                    u()
            if self.state.state not in (S.STANDBY, S.ERROR):
                self.state.reset()   # send erken döndüyse (meşgul, anahtar yok) durum kalmış olabilir
            if done is None and answers and hasattr(answers[-1], "text"):
                done = answers[-1]
            if done is not None:
                await self._speak(done.text, follow_up=True)
            elif failed and self.state.state is not S.ERROR:
                await self._speak(failed[-1], follow_up=False)
        finally:
            self._busy = False
            self._sync_mode()
            self._publish_status()

    # ---------- konuşma ----------
    def _enter_speaking(self) -> bool:
        st = self.state.state
        if st is S.ERROR:
            return False
        if not self.state.can_transition(S.SPEAKING) and st is not S.SPEAKING:
            # ChatService bitince durumu BEKLEMEDE'ye alır; BEKLEMEDE -> KONUŞUYOR geçişi tanımlı
            # olmadığı için aynı döngü adımında DÜŞÜNÜYOR üzerinden geçilir (ekranda görünmez).
            self.state.transition(S.PROCESSING, "Cevabı seslendiriyorum")
        self.state.transition(S.SPEAKING, "")
        return True

    async def _speak(self, text: str, follow_up: bool) -> bool:
        comps = self.components
        if comps is None or comps.tts is None or comps.player is None or not text.strip():
            if self.state.state not in (S.STANDBY, S.ERROR):
                self.state.reset()
            return False
        self._speak_cancel = cancel = threading.Event()
        self._speaking = True
        if not self._enter_speaking():
            self._speaking = False
            return False
        try:
            completed = await asyncio.get_running_loop().run_in_executor(
                self._executor, self._speak_blocking, text, self.settings.speech_rate, cancel)
        except Exception as e:  # noqa: BLE001
            completed = False
            log.exception("Konuşma sentezi hatası")
            self.bus.publish(Topics.VOICE_ERROR, e.user_message if isinstance(e, JarvisError)
                             else "Cevabı seslendiremedim; ekranda okuyabilirsin.")
            if self.state.state is S.SPEAKING:
                self.state.reset()
            return False
        finally:
            self._speaking = False
        if cancel.is_set():
            return False   # sözü kesildi: dinlemeye zaten geçildi
        if self.state.state is not S.SPEAKING:
            return completed
        if (follow_up and self.settings.follow_up_s > 0 and self.mic is not None and self.ready
                and self.processor is not None):
            self.processor.set_mode(Mode.LISTEN, no_speech_timeout_s=self.settings.follow_up_s)
            self.state.transition(S.LISTENING, "Devam edebilirsin")
        else:
            self.state.reset()
        return completed

    def _speak_blocking(self, text: str, rate: float, cancel: threading.Event) -> bool:
        comps = self.components
        assert comps is not None and comps.tts is not None and comps.player is not None
        tts, player = comps.tts, comps.player
        try:
            for sentence in split_sentences(speakable_text(text)):
                if cancel.is_set():
                    return False
                audio = tts.synthesize(sentence, rate)
                with self._speak_lock:
                    if cancel.is_set():
                        return False
                    player.play(audio, tts.sample_rate)
            player.finish()
            while not player.wait(0.05):
                if cancel.is_set():
                    return False
            # Hoparlör tamponundaki son parça da çalınsın; kuyruğu takip dinlemesine karışmasın
            if not cancel.is_set():
                self._sleep(0.15)
            return not cancel.is_set()
        finally:
            if cancel.is_set():
                player.stop()

    def _stop_speaking(self) -> None:
        comps = self.components
        with self._speak_lock:
            self._speak_cancel.set()
            if comps is not None and comps.player is not None:
                comps.player.stop()

    def _play_chime(self) -> None:
        comps = self.components
        if comps is None or comps.player is None:
            return
        player = comps.player

        def play() -> None:
            try:
                player.play(make_chime(22050), 22050)
                player.finish()
            except Exception:  # noqa: BLE001
                log.debug("Uyandırma sesi çalınamadı", exc_info=True)
        self._executor.submit(play)

    # ---------- ayarlar ekranı için ----------
    async def speak_test(self, text: str = "Merhaba, ben JARVIS. Sesimi rahat duyabiliyor musun?") -> None:
        if self.components is None or self.components.tts is None:
            raise JarvisError("tts yok", user_message=self.status.detail or "Konuşma sentezi hazır değil.")
        if self.state.state is not S.STANDBY:
            raise JarvisError("meşgul", user_message="Şu an meşgulüm, birazdan tekrar dene.")
        await self._speak(text, follow_up=False)

    async def calibrate(self, seconds: float = 2.0) -> NoiseProfile:
        """Ortam gürültüsünü ölçer (bu sürede konuşma). Sonuç ayarlara kaydedilir."""
        if self.mic is None or self.processor is None:
            raise JarvisError("mikrofon kapalı", user_message="Kalibrasyon için mikrofonun açık olması gerekiyor.")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        frames: list[np.ndarray] = []
        need = max(5, int(seconds / 0.08))

        def tap(frame: Frame) -> bool:
            frames.append(frame.samples)
            if len(frames) >= need:
                loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(None))
                return True
            return False

        self.processor.add_tap(tap)
        await asyncio.wait_for(fut, timeout=seconds + 3.0)
        profile = measure_noise(frames)
        self.settings.noise_floor_dbfs = round(profile.floor_dbfs, 1)
        self.store.save()
        if isinstance(self.components.vad if self.components else None, EnergyVad):
            self.components.vad.noise_floor_dbfs = profile.floor_dbfs  # type: ignore[union-attr]
        self.apply_settings()
        log.info("Ortam gürültüsü ölçüldü: %.1f dBFS (p90 %.1f)", profile.floor_dbfs, profile.p90_dbfs)
        return profile

    @property
    def mic_level(self) -> float:
        return self.mic.level if self.mic is not None else 0.0

    # ---------- yardımcılar ----------
    def _gain(self) -> float:
        return volume_to_gain(self.settings.volume)

    def _progress_threadsafe(self, p: DownloadProgress) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self.bus.publish, Topics.VOICE_DOWNLOAD, p)
            except RuntimeError:
                pass

    def _spawn(self, coro, name: str) -> asyncio.Task:  # noqa: ANN001
        task = asyncio.ensure_future(coro)
        task.set_name(name)
        self._tasks.add(task)

        def done(t: asyncio.Task) -> None:
            self._tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                log.error("Ses görevi başarısız (%s)", name, exc_info=t.exception())
        task.add_done_callback(done)
        return task

    async def _level_pump(self) -> None:
        last: tuple[str, float] | None = None
        while True:
            await asyncio.sleep(self.LEVEL_INTERVAL_S)
            comps = self.components
            if self.state.state is S.SPEAKING and comps is not None and comps.player is not None:
                cur = ("tts", round(comps.player.level, 3))
            elif self.mic is not None:
                cur = ("mic", round(self.mic.level, 3))
            else:
                cur = ("mic", 0.0)
            if cur != last:
                last = cur
                self.bus.publish(Topics.VOICE_LEVEL, VoiceLevel(*cur))

    def _compute_status(self) -> VoiceStatus:
        comps = self.components
        wake = comps is not None and comps.wake is not None
        tts = comps is not None and comps.tts is not None
        label = comps.stt_label if comps else ""
        muted = not self.settings.microphone_enabled
        if self._starting:
            return VoiceStatus(MicState.OFF if muted else MicState.IDLE, False, wake, tts,
                               "Ses motoru hazırlanıyor…", label, starting=True)
        if not self.ready:
            return VoiceStatus(MicState.OFF if muted else MicState.IDLE, False, wake, tts,
                               self._error or "Ses motoru hazır değil.", label)
        if muted:
            return VoiceStatus(MicState.OFF, True, wake, tts, "Mikrofon kapalı; hiçbir şey dinlenmiyor.", label)
        if self.mic is None:
            return VoiceStatus(MicState.IDLE, True, wake, tts, self._error or "Mikrofon açılıyor…", label)
        st = self.state.state
        if st in (S.WAKE_DETECTED, S.LISTENING):
            return VoiceStatus(MicState.LISTENING, True, wake, tts, "Dinliyorum.", label)
        if st is S.SPEAKING and self._speaking and self.settings.barge_in:
            return VoiceStatus(MicState.LISTENING, True, wake, tts, "Konuşurken sözümü kesebilirsin.", label)
        if st in (S.STANDBY, S.ERROR):
            if wake:
                return VoiceStatus(MicState.WAKE, True, wake, tts, "\"Hey Jarvis\" ya da \"Jarvis\" de.", label)
            return VoiceStatus(MicState.IDLE, True, wake, tts,
                               "Uyandırma kelimesi kullanılamıyor; \"Konuş\" düğmesine bas.", label)
        return VoiceStatus(MicState.IDLE, True, wake, tts, "Şu an dinlemiyorum.", label)

    def _publish_status(self) -> None:
        st = self._compute_status()
        if st != self.status:
            self.status = st
            self.bus.publish(Topics.VOICE_STATUS, st)
