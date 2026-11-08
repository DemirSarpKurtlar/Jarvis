"""Medya/ses arka uçlarının gerçek Windows testleri (yalnızca okuma).

Varsayılan aygıt değiştirilmez, tuş gönderilmez, uygulama sesi değiştirilmez: bunlar kullanıcının
bilgisayarında elle denenir (bkz. KARARLAR.md, medya bölümü). CI'da ses aygıtı olmayabilir; testler
yalnızca çağrıların hatasız döndüğünü ve tiplerin doğru olduğunu denetler.
"""

import ctypes
import sys

import pytest

pytestmark = pytest.mark.windows

if sys.platform == "win32":  # pragma: no cover - yalnızca Windows
    from jarvis.tools.media_win import (INPUT, WinAppAudio, WinAudioDevices, WinMedia, _policy_config_interface,
                                        spotify_protocol_registered)


def test_input_struct_matches_win32():
    assert ctypes.sizeof(INPUT) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)


def test_list_outputs_reads_default():
    devices = WinAudioDevices().list_outputs()
    assert isinstance(devices, list)
    if devices:
        assert sum(d.is_default for d in devices) == 1
        assert all(d.id and d.name for d in devices)


def test_policy_config_can_be_created():
    import comtypes

    from jarvis.tools.winapi import com_thread

    com_thread()
    comtypes.CoCreateInstance(comtypes.GUID("{870af99c-171d-4f9e-af0d-e63df40c2bc9}"), _policy_config_interface(),
                              comtypes.CLSCTX_ALL)


def test_app_sessions_read():
    for s in WinAppAudio().sessions():
        assert 0 <= s.volume <= 100 and isinstance(s.muted, bool)


def test_gsmtc_reads_without_error():
    m = WinMedia()
    if not m.gsmtc_available():
        pytest.skip("winrt (Windows.Media.Control) kurulu değil")
    info = m.current()
    assert info is None or info.status in ("playing", "paused", "stopped", "changing", "opened", "closed", "unknown")


def test_spotify_detection_returns_bool():
    assert isinstance(spotify_protocol_registered(), bool)
