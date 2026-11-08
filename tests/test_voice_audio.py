"""Ses giriş/çıkışı: yeniden örnekleme, mikrofon akışı, anında durdurulabilen hoparlör, yankı koruması."""

import sys
import time

import numpy as np
import pytest

from tests.voice_fakes import FakeBackend, sine, silence, speech
from jarvis.voice.audio_io import (FRAME_SAMPLES, AudioPlayer, AudioUnavailable, MicrophoneStream, OutputHistory,
                                   StreamResampler, find_device, level_from_dbfs, list_devices, load_backend,
                                   make_chime, rms_dbfs, to_int16, volume_to_gain)
from jarvis.voice.echo import EchoGuard, max_normalized_xcorr


def dominant_freq(x: np.ndarray, sr: int) -> float:
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return float(np.fft.rfftfreq(len(x), 1 / sr)[np.argmax(spec)])


def wait_until(cond, timeout=3.0):  # noqa: ANN001, ANN201
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


# ---------- yardımcılar ----------
def test_levels():
    assert rms_dbfs(np.zeros(100, np.int16)) == -120.0
    assert rms_dbfs(np.full(100, 32767, np.int16)) == pytest.approx(0.0, abs=0.01)
    assert rms_dbfs(sine(440, 1.0, amp=1.0)) == pytest.approx(-3.01, abs=0.05)
    assert level_from_dbfs(-80) == 0.0 and level_from_dbfs(0) == 1.0
    assert 0.4 < level_from_dbfs(-35) < 0.6
    assert volume_to_gain(0) == 0.0 and volume_to_gain(100) == 1.0 and volume_to_gain(70) == pytest.approx(0.49)


def test_chime_is_short_and_quiet():
    c = make_chime(22050)
    assert 0.1 < len(c) / 22050 < 0.25
    assert np.max(np.abs(c)) < 0.3


@pytest.mark.parametrize("src", [48000, 44100, 22050])
def test_resampler_keeps_frequency_and_length_across_blocks(src):
    x = sine(1000, 2.0, sr=src)
    r = StreamResampler(src, 16000)
    out = np.concatenate([r.process(x[i:i + 997]) for i in range(0, len(x), 997)])  # tuhaf blok boyu
    assert abs(len(out) - 32000) <= 2
    assert dominant_freq(out[4000:], 16000) == pytest.approx(1000, abs=5)
    # Bloklar arası kesinti yok: türev sinüsün tepe türevini aşmıyor
    assert np.max(np.abs(np.diff(out[1000:-1000]))) < 2 * np.pi * 1000 / 16000 * 0.5 * 1.2


def test_resampler_removes_aliasing_when_downsampling():
    tone = sine(12000, 1.0, sr=48000)   # 16 kHz'de 8 kHz Nyquist üstü: süzülmeli
    out = StreamResampler(48000, 16000).process(tone)
    assert rms_dbfs(out[1000:]) < rms_dbfs(tone) - 30


def test_load_backend_reports_missing_portaudio(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):  # noqa: ANN001, ANN202
        if name == "sounddevice":
            raise OSError("PortAudio library not found")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.delitem(sys.modules, "sounddevice", raising=False)
    with pytest.raises(AudioUnavailable) as ei:
        load_backend()
    assert "PortAudio" in ei.value.user_message and "yazılı sohbet" in ei.value.user_message


def test_device_list_skips_duplicate_host_apis_and_finds_by_name():
    b = FakeBackend()
    ins = list_devices(b, "input")
    outs = list_devices(b, "output")
    assert [d.name for d in ins] == ["Test Mikrofonu"]       # WASAPI kopyası gösterilmez
    assert [d.name for d in outs] == ["Test Hoparlörü"]
    assert ins[0].is_default_input and outs[0].is_default_output
    assert find_device(ins, "Test Mikrofonu") == 0
    assert find_device(ins, "Test Mikrofonu (yeni sürücü)") == 0   # ad hafif değişmiş
    assert find_device(ins, "Yok") is None and find_device(ins, None) is None


# ---------- mikrofon ----------
def test_microphone_frames_and_real_close():
    b = FakeBackend()
    mic = MicrophoneStream(b)
    mic.start()
    assert len(b.active_inputs) == 1
    b.feed([np.full(640, 100, np.int16)] * 5)   # 3200 örnek -> 2 tam kare
    f1, f2 = mic.read(1.0), mic.read(1.0)
    assert f1.samples.shape == (FRAME_SAMPLES,) and f1.samples.dtype == np.int16
    assert f2 is not None and mic.read(0.05) is None
    assert mic.level > 0
    mic.stop()
    assert len(b.active_inputs) == 0 and not mic.active   # akış gerçekten kapandı
    mic.stop()  # ikinci kez sorun değil


def test_microphone_falls_back_to_native_rate_and_resamples():
    b = FakeBackend(input_rates=(48000,))
    mic = MicrophoneStream(b)
    mic.start()
    assert mic.samplerate == 48000
    tone = (sine(500, 1.0, sr=48000) * 32767 * 0.5).astype(np.int16)
    b.feed([tone[i:i + 3840] for i in range(0, len(tone), 3840)])
    frames = [mic.read(1.0) for _ in range(11)]
    assert all(f is not None for f in frames)
    joined = np.concatenate([f.samples for f in frames]).astype(np.float32)
    assert dominant_freq(joined[2000:], 16000) == pytest.approx(500, abs=5)
    mic.stop()


def test_microphone_open_failure_is_friendly():
    b = FakeBackend(input_rates=())
    with pytest.raises(AudioUnavailable) as ei:
        MicrophoneStream(b).start()
    assert "Mikrofon açılamadı" in ei.value.user_message


def test_microphone_queue_drops_oldest_when_consumer_is_slow():
    b = FakeBackend()
    mic = MicrophoneStream(b, max_queue=3)
    mic.start()
    frames = [np.full(FRAME_SAMPLES, i, np.int16) for i in range(6)]
    b.feed(frames)
    assert wait_until(lambda: mic.dropped == 3)
    got = [int(mic.read(0.5).samples[0]) for _ in range(3)]
    assert got == [3, 4, 5]   # en yeniler kaldı: gecikme birikmez
    mic.stop()


# ---------- hoparlör ----------
def test_player_plays_in_order_with_gain_and_finishes():
    b = FakeBackend(speed=20)
    player = AudioPlayer(b, gain=lambda: 0.5)
    a, c = np.full(4000, 0.2, np.float32), np.full(4000, -0.4, np.float32)
    player.play(a, 22050)
    player.play(c, 22050)
    player.finish()
    assert player.wait(3.0)
    out = b.played_audio()
    nz = out[np.abs(out) > 1e-6]
    assert len(nz) == 8000
    assert np.allclose(nz[:4000], 0.1) and np.allclose(nz[4000:], -0.2)   # sıra ve kazanç
    assert player.history.last_time() is not None
    player.close()


def test_player_stop_is_instant_and_drops_queue():
    b = FakeBackend(speed=1)
    player = AudioPlayer(b)
    player.play(sine(300, 5.0, sr=22050), 22050)   # 5 saniyelik ses
    player.finish()
    assert wait_until(lambda: b.played_nonzero_samples() > 0)
    t0 = time.perf_counter()
    player.stop()
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.15
    assert not player.playing and b.aborts == 1 and not b.active_outputs
    played = b.played_nonzero_samples()
    time.sleep(0.2)
    assert b.played_nonzero_samples() == played      # durduktan sonra tek örnek bile çalınmadı
    assert played < 22050                              # 5 saniyenin çok azı çalındı


def test_player_resamples_when_rate_unsupported():
    b = FakeBackend(output_rates=(48000,), speed=20)
    player = AudioPlayer(b)
    player.play(sine(440, 0.5, sr=22050), 22050)
    player.finish()
    assert player.wait(3.0)
    assert b.played_rate == 48000
    out = b.played_audio()
    assert dominant_freq(out[np.abs(out) > 0][:20000], 48000) == pytest.approx(440, abs=5)
    player.close()


# ---------- çıkış geçmişi ve yankı ----------
def test_output_history_segment_alignment():
    h = OutputHistory()
    block = np.ones(1600, np.float32)                  # 0,1 sn @16k
    h.push(block, 16000, t_end=10.0)                   # 9,9 - 10,0 arası çalındı
    seg = h.segment(10.05, 0.2)                        # 9,85 - 10,05
    assert seg.shape == (3200,)
    assert np.all(seg[:800] == 0) and np.all(seg[800:2400] == 1) and np.all(seg[2400:] == 0)


def test_xcorr_finds_delayed_copy():
    rng = np.random.default_rng(0)
    ref = rng.normal(0, 1, 8000).astype(np.float32)
    mic = 0.3 * ref[3000:4280] + 0.01 * rng.normal(0, 1, 1280).astype(np.float32)
    assert max_normalized_xcorr(mic, ref) > 0.9
    assert max_normalized_xcorr(rng.normal(0, 1, 1280).astype(np.float32), ref) < 0.2


def _play_into_history(h: OutputHistory, audio: np.ndarray, t0: float, block: int = 1024) -> None:
    for i in range(0, len(audio), block):
        b = audio[i:i + block]
        h.push(b, 16000, t0 + (i + len(b)) / 16000)


def test_echo_guard_ignores_own_voice_but_hears_user():
    rng = np.random.default_rng(5)
    out = (0.3 * rng.normal(0, 1, 16000 * 3)).astype(np.float32)       # 3 sn "konuşma"
    h = OutputHistory()
    _play_into_history(h, out, t0=100.0)
    guard = EchoGuard(h)
    delay = int(0.12 * 16000)                                           # 120 ms akustik gecikme
    echo_verdicts, user_verdicts = [], []
    for k in range(10, 30):
        end = k * FRAME_SAMPLES
        t = 100.0 + end / 16000
        echo = 0.25 * out[end - FRAME_SAMPLES - delay:end - delay] + 0.002 * rng.normal(0, 1, FRAME_SAMPLES)
        echo_verdicts.append(guard.assess(to_int16(echo), t).user_likely)
    assert not any(echo_verdicts)                                       # kendi sesi: kesme yok
    assert guard.coupling_db < -8                                       # bağlaşım öğrenildi (~-12 dB)
    for k in range(30, 36):
        end = k * FRAME_SAMPLES
        t = 100.0 + end / 16000
        user = 0.25 * out[end - FRAME_SAMPLES - delay:end - delay] + 0.3 * rng.normal(0, 1, FRAME_SAMPLES)
        user_verdicts.append(guard.assess(to_int16(user), t).user_likely)
    assert all(user_verdicts)                                            # üstüne konuşan kullanıcı duyuldu


def test_echo_guard_without_playback_allows_everything():
    guard = EchoGuard(OutputHistory())
    a = guard.assess(silence(1)[0], time.monotonic())
    assert a.user_likely and a.correlation == 0.0
    assert guard.assess(speech(1)[0], time.monotonic()).user_likely


# ---------- yalnızca Windows ----------
@pytest.mark.windows
def test_windows_wheel_bundles_portaudio_with_mme():
    sd = load_backend()   # Windows tekerleği PortAudio DLL'ini içerir; ayrı kurulum gerekmez
    names = [h["name"] for h in sd.query_hostapis()]
    assert "MME" in names, names


@pytest.mark.windows
def test_windows_real_microphone_opens_and_closes():
    from jarvis.voice.audio_io import has_input_device

    sd = load_backend()
    if not has_input_device(sd):
        pytest.skip("bu bilgisayarda mikrofon yok (CI sunucusu olabilir)")
    mic = MicrophoneStream(sd)
    mic.start()
    try:
        frame = mic.read(2.0)
        assert frame is not None and frame.samples.shape == (FRAME_SAMPLES,)
    finally:
        mic.stop()
    assert not mic.active
