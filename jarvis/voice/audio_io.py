"""Mikrofon ve hoparlör: aygıt listesi, 16 kHz mono giriş akışı, anında durdurulabilen çıkış.

Ses kütüphanesi (`sounddevice`, PortAudio) yalnızca `load_backend()` çağrıldığında yüklenir;
yoksa ya da hiç ses aygıtı yoksa `AudioUnavailable` fırlatılır ve yazılı sohbet etkilenmez.
Testler aynı arayüzü taklit eden sahte bir arka uç verebilir (`tests/voice_fakes.py`).

Kural: PortAudio geri çağrıları (callback) ses thread'inde çalışır. Burada yalnızca
kopyalama ve kuyruğa koyma yapılır; ağır iş (model, ağ) asla yapılmaz.
"""

from __future__ import annotations

import collections
import logging
import math
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from jarvis.core.errors import JarvisError

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000          # tüm tanıma modelleri 16 kHz mono ister
FRAME_SAMPLES = 1280         # 80 ms: openWakeWord'ün doğal adımı
FRAME_S = FRAME_SAMPLES / SAMPLE_RATE


class AudioUnavailable(JarvisError):
    """Ses sistemi ya da aygıt kullanılamıyor. Yazılı sohbet çalışmaya devam eder."""


# ---------- yardımcılar ----------
def rms_dbfs(x: np.ndarray) -> float:
    """Sinyalin RMS seviyesi (dBFS). int16 ya da [-1, 1] float kabul eder."""
    if x.size == 0:
        return -120.0
    f = x.astype(np.float32)
    if x.dtype == np.int16:
        f = f / 32768.0
    rms = float(np.sqrt(np.mean(f * f)))
    return 20.0 * math.log10(rms) if rms > 1e-6 else -120.0


def level_from_dbfs(db: float) -> float:
    """dBFS'i görselleştirici için 0..1 aralığına çevirir (-60 dB sessiz, -10 dB tam)."""
    return max(0.0, min(1.0, (db + 60.0) / 50.0))


def to_float(x: np.ndarray) -> np.ndarray:
    return x.astype(np.float32) / 32768.0 if x.dtype == np.int16 else x.astype(np.float32)


def to_int16(x: np.ndarray) -> np.ndarray:
    return np.clip(x * 32768.0, -32768, 32767).astype(np.int16)


def load_backend() -> Any:
    """`sounddevice` modülünü yükler. PortAudio yoksa anlaşılır bir hata verir."""
    try:
        import sounddevice
    except (ImportError, OSError) as e:
        raise AudioUnavailable(
            f"sounddevice yüklenemedi: {e}",
            user_message="Ses sistemi başlatılamadı (PortAudio bulunamadı). Sesli konuşma kapalı; "
                         "yazılı sohbet çalışmaya devam ediyor.",
        ) from e
    return sounddevice


# ---------- aygıtlar ----------
@dataclass(frozen=True)
class AudioDevice:
    index: int
    name: str
    hostapi: str
    max_input: int
    max_output: int
    default_samplerate: float
    is_default_input: bool = False
    is_default_output: bool = False


def _default_indices(backend: Any) -> tuple[int, int]:
    try:
        d = backend.default.device
        return int(d[0]) if d[0] is not None else -1, int(d[1]) if d[1] is not None else -1
    except Exception:  # noqa: BLE001
        return -1, -1


def list_devices(backend: Any, kind: str | None = None) -> list[AudioDevice]:
    """Ses aygıtları. Windows'ta aynı aygıt her ses sürücüsünde (MME, WASAPI...) ayrı görünür;
    tekrarları önlemek için yalnızca varsayılan ses sürücüsünün (genelde MME) aygıtları döner.
    MME, 16 kHz'e dönüştürmeyi kendisi yapar."""
    try:
        raw = list(backend.query_devices())
        apis = list(backend.query_hostapis())
    except Exception as e:  # noqa: BLE001
        log.warning("Ses aygıtları okunamadı: %s", e)
        return []
    din, dout = _default_indices(backend)
    default_api = None
    for idx in (din, dout):
        if 0 <= idx < len(raw):
            default_api = raw[idx].get("hostapi")
            break
    out: list[AudioDevice] = []
    for i, d in enumerate(raw):
        if default_api is not None and d.get("hostapi") != default_api:
            continue
        api = apis[d["hostapi"]]["name"] if 0 <= d.get("hostapi", -1) < len(apis) else "?"
        dev = AudioDevice(i, str(d.get("name", f"Aygıt {i}")), api, int(d.get("max_input_channels", 0)),
                          int(d.get("max_output_channels", 0)), float(d.get("default_samplerate", 0) or 0),
                          i == din, i == dout)
        if kind == "input" and dev.max_input < 1:
            continue
        if kind == "output" and dev.max_output < 1:
            continue
        out.append(dev)
    return out


def find_device(devices: list[AudioDevice], name: str | None) -> int | None:
    """Ayarlarda saklanan ada göre aygıt numarası. Ad yoksa ya da bulunamazsa None (sistem varsayılanı)."""
    if not name:
        return None
    for d in devices:
        if d.name == name:
            return d.index
    for d in devices:  # aygıt adı sürücü güncellemesiyle hafif değişmiş olabilir
        if d.name.startswith(name) or name.startswith(d.name):
            return d.index
    log.warning("Kayıtlı ses aygıtı bulunamadı, varsayılan kullanılacak: %s", name)
    return None


def has_input_device(backend: Any) -> bool:
    return bool(list_devices(backend, "input")) and _default_indices(backend)[0] >= 0


# ---------- yeniden örnekleme ----------
class StreamResampler:
    """Parça parça gelen sesi kesintisiz yeniden örnekler (alçak geçiren FIR + doğrusal ara değer).

    Aygıt 16 kHz açılamadığında (ör. yalnızca 48 kHz destekleyen sürücü) kullanılır.
    """

    def __init__(self, src_sr: float, dst_sr: float, taps: int = 63) -> None:
        self.src_sr, self.dst_sr = float(src_sr), float(dst_sr)
        self.ratio = self.src_sr / self.dst_sr      # bir çıkış örneği başına giriş örneği
        self._h: np.ndarray | None = None
        if self.src_sr > self.dst_sr:
            fc = 0.5 * self.dst_sr / self.src_sr * 0.9   # örnek başına çevrim
            n = np.arange(taps) - (taps - 1) / 2
            h = 2 * fc * np.sinc(2 * fc * n) * np.hamming(taps)
            self._h = (h / h.sum()).astype(np.float32)
            self._fir_state = np.zeros(taps - 1, np.float32)
        self._prev: float | None = None
        self._pos = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        x = x.astype(np.float32)
        if self.src_sr == self.dst_sr or x.size == 0:
            return x
        if self._h is not None:
            ext = np.concatenate((self._fir_state, x))
            y = np.convolve(ext, self._h, mode="valid").astype(np.float32)
            self._fir_state = ext[-(len(self._h) - 1):]
        else:
            y = x
        buf = y if self._prev is None else np.concatenate(([self._prev], y)).astype(np.float32)
        last = len(buf) - 1
        if self._pos > last:
            n = 0
        else:
            n = int(math.floor((last - self._pos) / self.ratio)) + 1
        positions = self._pos + np.arange(n) * self.ratio
        out = np.interp(positions, np.arange(len(buf)), buf).astype(np.float32)
        self._pos = self._pos + n * self.ratio - last
        self._prev = float(buf[-1])
        return out


# ---------- mikrofon ----------
@dataclass(frozen=True)
class Frame:
    samples: np.ndarray   # int16, FRAME_SAMPLES uzunluğunda, 16 kHz mono
    t: float              # yakalanma anı (time.monotonic)


class MicrophoneStream:
    """16 kHz mono int16 mikrofon akışı. Ses thread'i kareleri kuyruğa koyar, `read()` alır.

    `stop()` akışı gerçekten kapatır: kapalıyken işletim sistemi mikrofonu kullanılıyor göstermez.
    """

    def __init__(self, backend: Any, device: int | None = None, frame_samples: int = FRAME_SAMPLES,
                 max_queue: int = 64) -> None:
        self.backend = backend
        self.device = device
        self.frame_samples = frame_samples
        self._q: queue.Queue[Frame] = queue.Queue(maxsize=max_queue)
        self._stream: Any = None
        self._resampler: StreamResampler | None = None
        self._pending = np.zeros(0, np.int16)
        self._lock = threading.Lock()
        self.dropped = 0
        self.overflows = 0
        self.level = 0.0
        self.samplerate = SAMPLE_RATE

    @property
    def active(self) -> bool:
        return self._stream is not None

    def start(self) -> None:
        with self._lock:
            if self._stream is not None:
                return
            self._pending = np.zeros(0, np.int16)
            try:
                self._stream = self._open(SAMPLE_RATE)
                self._resampler = None
            except Exception as first:  # noqa: BLE001 - PortAudioError dahil
                native = self._native_rate()
                if not native or native == SAMPLE_RATE:
                    raise self._error(first) from first
                log.info("Mikrofon 16 kHz açılamadı (%s); %d Hz açılıp dönüştürülecek", first, native)
                try:
                    self._stream = self._open(native)
                    self._resampler = StreamResampler(native, SAMPLE_RATE)
                except Exception as second:  # noqa: BLE001
                    raise self._error(second) from second
            self._stream.start()
            log.info("Mikrofon açıldı (aygıt=%s, %d Hz%s)", self.device if self.device is not None else "varsayılan",
                     int(self.samplerate), ", dönüştürülüyor" if self._resampler else "")

    def _native_rate(self) -> int | None:
        try:
            info = self.backend.query_devices(self.device, "input")
            return int(info["default_samplerate"])
        except Exception:  # noqa: BLE001
            return None

    def _open(self, rate: int) -> Any:
        block = self.frame_samples if rate == SAMPLE_RATE else int(round(self.frame_samples * rate / SAMPLE_RATE))
        stream = self.backend.InputStream(samplerate=rate, channels=1, dtype="int16", blocksize=block,
                                          device=self.device, callback=self._callback)
        self.samplerate = rate
        return stream

    @staticmethod
    def _error(e: Exception) -> AudioUnavailable:
        return AudioUnavailable(f"Mikrofon açılamadı: {e}",
                                user_message="Mikrofon açılamadı. Başka bir uygulama kullanıyor olabilir ya da "
                                             "Windows mikrofon izni kapalı olabilir (Ayarlar > Gizlilik > Mikrofon).")

    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ANN001, ARG002
        if status:
            self.overflows += 1
        mono = indata[:, 0] if indata.ndim == 2 else indata
        if self._resampler is not None:
            mono = to_int16(self._resampler.process(mono.astype(np.float32) / 32768.0))
        buf = np.concatenate((self._pending, mono.astype(np.int16)))
        now = time.monotonic()
        n = self.frame_samples
        while len(buf) >= n:
            frame = Frame(buf[:n].copy(), now)
            buf = buf[n:]
            self._put(frame)
        self._pending = buf

    def _put(self, frame: Frame) -> None:
        try:
            self._q.put_nowait(frame)
        except queue.Full:
            # İşleyici geride kaldı: en eski kareyi at, gecikme birikmesin
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
            self.dropped += 1
            try:
                self._q.put_nowait(frame)
            except queue.Full:
                pass

    def read(self, timeout: float = 0.2) -> Frame | None:
        try:
            frame = self._q.get(timeout=timeout)
        except queue.Empty:
            return None
        self.level = level_from_dbfs(rms_dbfs(frame.samples))
        return frame

    def drain(self) -> int:
        n = 0
        while True:
            try:
                self._q.get_nowait()
                n += 1
            except queue.Empty:
                return n

    def stop(self) -> None:
        with self._lock:
            stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
            except Exception:  # noqa: BLE001
                log.debug("Mikrofon durdurulurken hata", exc_info=True)
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                log.debug("Mikrofon kapatılırken hata", exc_info=True)
            log.info("Mikrofon kapatıldı")
        self.drain()
        self.level = 0.0


# ---------- çıkış geçmişi (yankı önleme için) ----------
class OutputHistory:
    """Hoparlöre son gönderilen sesi zaman damgasıyla tutar. Yankı koruması mikrofon karesini
    bununla karşılaştırır. Thread güvenlidir."""

    def __init__(self, keep_s: float = 3.0) -> None:
        self.keep_s = keep_s
        self._blocks: collections.deque[tuple[float, float, np.ndarray]] = collections.deque()
        self._lock = threading.Lock()

    def push(self, block: np.ndarray, sr: float, t_end: float) -> None:
        with self._lock:
            self._blocks.append((t_end, float(sr), block.astype(np.float32, copy=True)))
            while self._blocks and self._blocks[0][0] < t_end - self.keep_s:
                self._blocks.popleft()

    def clear(self) -> None:
        with self._lock:
            self._blocks.clear()

    def last_time(self) -> float | None:
        with self._lock:
            return self._blocks[-1][0] if self._blocks else None

    def segment(self, t_end: float, duration_s: float, sr: int = SAMPLE_RATE) -> np.ndarray:
        """[t_end - duration, t_end] aralığında çalınan ses (sr örnekleme), çalınmayan yerler sıfır."""
        n = int(round(duration_s * sr))
        out = np.zeros(n, np.float32)
        t0 = t_end - duration_s
        with self._lock:
            blocks = list(self._blocks)
        for b_end, b_sr, b in blocks:
            b_start = b_end - len(b) / b_sr
            if b_end <= t0 or b_start >= t_end:
                continue
            m = max(1, int(round(len(b) * sr / b_sr)))
            res = b if b_sr == sr else np.interp(np.linspace(0, len(b) - 1, m), np.arange(len(b)), b)
            i0 = int(round((b_start - t0) * sr))
            lo, hi = max(0, i0), min(n, i0 + len(res))
            if hi > lo:
                out[lo:hi] = res[lo - i0:hi - i0]
        return out


# ---------- hoparlör ----------
class AudioPlayer:
    """Sırayla çalınan ses parçaları. `stop()` sesi anında keser (sözü kesme).

    Ses seviyesi her blokta `gain()` ile uygulanır; kaydırıcı değişince hemen duyulur.
    """

    def __init__(self, backend: Any, device: int | None = None, gain: Callable[[], float] = lambda: 1.0,
                 history: OutputHistory | None = None, blocksize: int = 1024) -> None:
        self.backend = backend
        self.device = device
        self.gain = gain
        self.history = history or OutputHistory()
        self.blocksize = blocksize
        self._stream: Any = None
        self._stream_sr: float | None = None
        self._req_sr: float | None = None
        self._resampler: StreamResampler | None = None
        self._chunks: collections.deque[np.ndarray] = collections.deque()
        self._offset = 0
        self._eos = False
        self._done = threading.Event()
        self._done.set()
        self._lock = threading.RLock()
        self.level = 0.0

    @property
    def playing(self) -> bool:
        return not self._done.is_set()

    def _ensure_stream(self, sr: float) -> None:
        if self._stream is not None and self._req_sr == sr:
            return
        self._close_stream()
        self._req_sr = sr
        try:
            self._stream = self.backend.OutputStream(samplerate=sr, channels=1, dtype="float32",
                                                     blocksize=self.blocksize, device=self.device,
                                                     callback=self._callback)
            self._stream_sr, self._resampler = sr, None
        except Exception as first:  # noqa: BLE001
            try:
                native = float(self.backend.query_devices(self.device, "output")["default_samplerate"])
            except Exception:  # noqa: BLE001
                native = None
            if not native or native == sr:
                raise AudioUnavailable(f"Hoparlör açılamadı: {first}",
                                       user_message="Hoparlör açılamadı. Ayarlar > Ses bölümünden çıkış aygıtını "
                                                    "kontrol et.") from first
            self._stream = self.backend.OutputStream(samplerate=native, channels=1, dtype="float32",
                                                     blocksize=self.blocksize, device=self.device,
                                                     callback=self._callback)
            self._stream_sr, self._resampler = native, StreamResampler(sr, native)
        self._stream.start()

    def play(self, audio: np.ndarray, sample_rate: float) -> None:
        """Parçayı sıraya ekler (bloklamaz). Akış gerekiyorsa açılır."""
        with self._lock:
            if not self.playing:
                self._eos = False
                self._chunks.clear()
                self._offset = 0
            self._ensure_stream(sample_rate)
            data = audio.astype(np.float32)
            if self._resampler is not None:
                data = self._resampler.process(data)
            if data.size:
                self._chunks.append(data)
                self._done.clear()

    def finish(self) -> None:
        """Bundan sonra parça gelmeyecek: sıra boşalınca `wait()` döner."""
        with self._lock:
            self._eos = True
            if not self._chunks:
                self._done.set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout)

    def stop(self) -> None:
        """Sesi hemen keser: sıradaki her şey atılır ve akış iptal edilir (tamponu çalmadan)."""
        with self._lock:
            self._chunks.clear()
            self._offset = 0
            self._eos = True
            self._done.set()
            self.level = 0.0
        self._close_stream(abort=True)

    def close(self) -> None:
        self.stop()

    def _close_stream(self, abort: bool = False) -> None:
        stream, self._stream = self._stream, None
        self._req_sr = None
        if stream is None:
            return
        try:
            stream.abort() if abort else stream.stop()
        except Exception:  # noqa: BLE001
            log.debug("Hoparlör durdurulurken hata", exc_info=True)
        try:
            stream.close()
        except Exception:  # noqa: BLE001
            log.debug("Hoparlör kapatılırken hata", exc_info=True)

    def _callback(self, outdata, frames, time_info, status) -> None:  # noqa: ANN001, ARG002
        out = np.zeros(frames, np.float32)
        with self._lock:
            filled = 0
            while filled < frames and self._chunks:
                cur = self._chunks[0]
                take = min(frames - filled, len(cur) - self._offset)
                out[filled:filled + take] = cur[self._offset:self._offset + take]
                filled += take
                self._offset += take
                if self._offset >= len(cur):
                    self._chunks.popleft()
                    self._offset = 0
            if not self._chunks and self._eos:
                self._done.set()
        out *= float(self.gain())
        if outdata.ndim == 2:
            outdata[:, 0] = out
            if outdata.shape[1] > 1:
                outdata[:, 1:] = out[:, None]
        else:
            outdata[:] = out
        self.level = level_from_dbfs(rms_dbfs(out)) if filled else 0.0
        if filled and self._stream_sr:
            self.history.push(out[:filled], self._stream_sr, time.monotonic())


def volume_to_gain(volume: int) -> float:
    """0..100 kaydırıcı değerini kulağa doğrusal gelen kazanca çevirir."""
    v = max(0, min(100, int(volume))) / 100.0
    return v * v


def make_chime(sr: int = 22050) -> np.ndarray:
    """Uyanınca çalan kısa, yumuşak iki notalık ses (~0,16 sn)."""
    out = []
    for f in (880.0, 1318.5):
        t = np.arange(int(sr * 0.075)) / sr
        env = np.sin(np.pi * t / t[-1]) ** 2
        out.append(0.22 * np.sin(2 * np.pi * f * t) * env)
    return np.concatenate(out).astype(np.float32)
