"""Medya ve ses arka uçları (Windows): medya tuşları, "şu an ne çalıyor", ses çıkış aygıtı,
uygulama başına ses, klavye kısayolu gönderme.

- Medya tuşları ve kısayollar: user32 `SendInput` (saf ctypes).
- "Ne çalıyor" ve hedefli oynat/duraklat: Windows'un Global System Media Transport Controls
  (GSMTC) arayüzü, `winrt-Windows.Media.Control` paketiyle. Paket yoksa medya tuşlarına düşülür
  ve sonuç "doğrulanamadı" diye bildirilir.
- Ses çıkış aygıtları: Core Audio `IMMDeviceEnumerator` (pycaw/comtypes) ve varsayılan aygıtı
  değiştirmek için belgelenmemiş ama yaygın kullanılan `IPolicyConfig` COM arayüzü.
- Uygulama sesi: oturum başına `ISimpleAudioVolume` (pycaw `AudioUtilities.GetAllSessions`).

Windows dışında `Unsupported*` sınıfları `NotSupportedError` fırlatır; hiçbir şey başarılı
gibi gösterilmez. Testler bu arka uçların sahtelerini `ToolContext.extra["media"]` ile verir.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from jarvis.tools.registry import NotSupportedError, ToolError

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"


# ---------- veri tipleri ----------
@dataclass(frozen=True)
class MediaInfo:
    title: str
    artist: str
    album: str
    app: str
    status: str        # playing | paused | stopped | changing | opened | closed | unknown


@dataclass(frozen=True)
class AudioDevice:
    id: str
    name: str
    is_default: bool


@dataclass(frozen=True)
class AppAudioSession:
    pid: int
    process: str       # küçük harf exe adı ("discord.exe"); sistem sesleri için ""
    name: str          # oturumun görünen adı (çoğu zaman boş)
    volume: int        # 0-100
    muted: bool


GSMTC_STATUS = {0: "closed", 1: "opened", 2: "changing", 3: "stopped", 4: "playing", 5: "paused"}


# ---------- arayüzler ----------
class MediaBackend(Protocol):
    def gsmtc_available(self) -> bool: ...
    def current(self) -> MediaInfo | None: ...
    def control(self, action: str) -> bool: ...          # play|pause|toggle|next|previous|stop
    def send_key(self, key: str) -> None: ...             # play_pause|next|previous|stop


class AudioDeviceBackend(Protocol):
    def list_outputs(self) -> list[AudioDevice]: ...
    def set_default(self, device_id: str) -> None: ...


class AppAudioBackend(Protocol):
    def sessions(self) -> list[AppAudioSession]: ...
    def set_volume(self, pid: int, percent: int) -> None: ...
    def set_mute(self, pid: int, muted: bool) -> None: ...


# ---------- SendInput (saf ctypes) ----------
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002

VK = {
    "ctrl": 0x11, "shift": 0x10, "alt": 0x12,
    "play_pause": 0xB3, "stop": 0xB2, "next": 0xB0, "previous": 0xB1,
    **{chr(c).lower(): c for c in range(ord("A"), ord("Z") + 1)},
}
_EXTENDED = {0xB0, 0xB1, 0xB2, 0xB3}

# Win32 tipleri sabit genişlikle (Linux'ta c_ulong 8 bayt; testler yapıyı orada da doğrular)
_WORD, _DWORD, _LONG, _ULONG_PTR = ctypes.c_uint16, ctypes.c_uint32, ctypes.c_int32, ctypes.c_size_t


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", _WORD), ("wScan", _WORD), ("dwFlags", _DWORD), ("time", _DWORD), ("dwExtraInfo", _ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", _LONG), ("dy", _LONG), ("mouseData", _DWORD), ("dwFlags", _DWORD), ("time", _DWORD),
                ("dwExtraInfo", _ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", _DWORD), ("wParamL", _WORD), ("wParamH", _WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", _DWORD), ("u", _INPUTUNION)]


def build_key_inputs(keys: tuple[str, ...], scan: Callable[[int], int] = lambda vk: 0) -> ctypes.Array:
    """Tuş birleşimi için INPUT dizisi: hepsi sırayla basılır, ters sırayla bırakılır."""
    vks = []
    for k in keys:
        if k not in VK:
            raise ValueError(f"bilinmeyen tuş: {k}")
        vks.append(VK[k])
    events = [(vk, 0) for vk in vks] + [(vk, KEYEVENTF_KEYUP) for vk in reversed(vks)]
    arr = (INPUT * len(events))()
    for i, (vk, flags) in enumerate(events):
        arr[i].type = INPUT_KEYBOARD
        arr[i].ki.wVk = vk
        arr[i].ki.wScan = scan(vk) & 0xFFFF
        arr[i].ki.dwFlags = flags | (KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED else 0)
    return arr


def send_keys(keys: tuple[str, ...]) -> None:  # pragma: no cover - yalnızca Windows
    """Tuş birleşimini (örn. ("ctrl", "shift", "m")) ön plandaki oturuma gönderir."""
    if not IS_WINDOWS:
        raise NotSupportedError()
    user32 = ctypes.windll.user32
    user32.SendInput.argtypes = (ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int)
    user32.SendInput.restype = ctypes.c_uint
    arr = build_key_inputs(keys, scan=lambda vk: user32.MapVirtualKeyW(vk, 0))
    sent = user32.SendInput(len(arr), arr, ctypes.sizeof(INPUT))
    if sent != len(arr):
        raise ToolError(f"SendInput {sent}/{len(arr)}",
                        "Tuşlar gönderilemedi. Yönetici olarak çalışan bir pencere öndeyse Windows buna izin vermez.")


# ---------- GSMTC (winrt) ----------
def _run_winrt(coro_factory: Callable[[], Any], timeout: float = 5.0) -> Any:  # pragma: no cover - Windows
    """WinRT çağrısını temiz bir thread'de (MTA) çalıştırır; araç thread'inin COM durumundan bağımsız."""
    box: dict[str, Any] = {}

    def worker() -> None:
        import asyncio

        try:
            from winrt.runtime import ApartmentType, init_apartment

            init_apartment(ApartmentType.MULTI_THREADED)
        except Exception:  # noqa: BLE001  eski sürüm ya da zaten başlatılmış
            pass
        try:
            box["value"] = asyncio.run(coro_factory())
        except BaseException as e:  # noqa: BLE001
            box["error"] = e

    t = threading.Thread(target=worker, name="jarvis-gsmtc", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise ToolError("gsmtc zaman aşımı", "Windows medya denetimi yanıt vermedi.")
    if "error" in box:
        err = box["error"]
        if isinstance(err, ToolError):
            raise err
        raise ToolError(f"winrt: {err!r}", "Windows medya denetimine ulaşılamadı.") from err
    return box.get("value")


def pretty_app(aumid: str) -> str:
    """GSMTC kaynak kimliğinden okunur uygulama adı ("SpotifyAB.SpotifyMusic_...!Spotify" -> "Spotify")."""
    a = (aumid or "").lower()
    for key, name in (("spotify", "Spotify"), ("chrome", "Chrome"), ("msedge", "Edge"), ("firefox", "Firefox"),
                      ("308046b0af4a39cb", "Firefox"), ("opera", "Opera"), ("brave", "Brave"), ("vlc", "VLC"),
                      ("zunemusic", "Medya Oynatıcı"), ("media.player", "Medya Oynatıcı"),
                      ("applemusic", "Apple Music"), ("itunes", "iTunes"), ("deezer", "Deezer"),
                      ("youtube", "YouTube Music"), ("discord", "Discord")):
        if key in a:
            return name
    return (aumid or "bilinmeyen uygulama")[:60]


class WinMedia:  # pragma: no cover - yalnızca Windows
    def __init__(self) -> None:
        self._gsmtc: bool | None = None

    def gsmtc_available(self) -> bool:
        if self._gsmtc is None:
            try:
                import winrt.windows.media.control  # noqa: F401

                self._gsmtc = True
            except Exception:  # noqa: BLE001
                log.info("winrt (Windows.Media.Control) yok: medya tuşlarına düşülecek")
                self._gsmtc = False
        return self._gsmtc

    @staticmethod
    async def _session():  # noqa: ANN205
        from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Manager

        mgr = await Manager.request_async()
        return mgr.get_current_session()

    def current(self) -> MediaInfo | None:
        async def go() -> MediaInfo | None:
            s = await self._session()
            if s is None:
                return None
            props = await s.try_get_media_properties_async()
            status = GSMTC_STATUS.get(int(s.get_playback_info().playback_status), "unknown")
            return MediaInfo(title=(props.title if props else "") or "", artist=(props.artist if props else "") or "",
                             album=(props.album_title if props else "") or "",
                             app=pretty_app(s.source_app_user_model_id), status=status)

        return _run_winrt(go)

    def control(self, action: str) -> bool:
        method = {"play": "try_play_async", "pause": "try_pause_async", "toggle": "try_toggle_play_pause_async",
                  "next": "try_skip_next_async", "previous": "try_skip_previous_async",
                  "stop": "try_stop_async"}[action]

        async def go() -> bool:
            s = await self._session()
            if s is None:
                return False
            return bool(await getattr(s, method)())

        return bool(_run_winrt(go))

    def send_key(self, key: str) -> None:
        send_keys((key,))


class UnsupportedMedia:
    def gsmtc_available(self) -> bool:
        return False

    def current(self) -> MediaInfo | None:
        raise NotSupportedError()

    def control(self, action: str) -> bool:
        raise NotSupportedError()

    def send_key(self, key: str) -> None:
        raise NotSupportedError()


# ---------- ses çıkış aygıtları ----------
E_RENDER = 0
DEVICE_STATE_ACTIVE = 1
ROLES = (0, 1, 2)   # eConsole, eMultimedia, eCommunications
CLSID_POLICY_CONFIG = "{870af99c-171d-4f9e-af0d-e63df40c2bc9}"


def _policy_config_interface():  # noqa: ANN202  # pragma: no cover - Windows
    """IPolicyConfig (Windows 7+): belgelenmemiş ama Windows'un kendi ses panelinin kullandığı arayüz.
    Yalnızca SetDefaultEndpoint çağrılır; önceki 10 yöntem tablo sırası için yer tutucudur."""
    from ctypes import HRESULT
    from ctypes.wintypes import DWORD, LPCWSTR

    from comtypes import COMMETHOD, GUID, IUnknown

    class IPolicyConfig(IUnknown):
        _iid_ = GUID("{f8679f50-850a-41cf-9c72-430f290290c8}")
        _methods_ = tuple(COMMETHOD([], HRESULT, f"Reserved{i}") for i in range(10)) + (
            COMMETHOD([], HRESULT, "SetDefaultEndpoint", (["in"], LPCWSTR, "wszDeviceId"), (["in"], DWORD, "eRole")),
        )

    return IPolicyConfig


class WinAudioDevices:  # pragma: no cover - yalnızca Windows
    def _enumerator(self):  # noqa: ANN202
        from jarvis.tools.winapi import com_thread

        com_thread()
        try:
            from pycaw.pycaw import AudioUtilities
        except ImportError as e:
            raise ToolError("pycaw yok", "Ses aygıtları için pycaw kurulu değil.") from e
        return AudioUtilities

    def list_outputs(self) -> list[AudioDevice]:
        au = self._enumerator()
        enum = au.GetDeviceEnumerator()
        try:
            default_id = enum.GetDefaultAudioEndpoint(E_RENDER, 1).GetId()
        except Exception:  # noqa: BLE001  hiç çıkış aygıtı yok
            default_id = ""
        coll = enum.EnumAudioEndpoints(E_RENDER, DEVICE_STATE_ACTIVE)
        out = []
        for i in range(coll.GetCount()):
            dev = coll.Item(i)
            try:
                d = au.CreateDevice(dev)
                name = str(d.FriendlyName or d.id)
                out.append(AudioDevice(str(d.id), name, str(d.id) == default_id))
            except Exception:  # noqa: BLE001
                log.debug("Ses aygıtı okunamadı", exc_info=True)
        return out

    def set_default(self, device_id: str) -> None:
        self._enumerator()
        import comtypes

        iface = _policy_config_interface()
        try:
            policy = comtypes.CoCreateInstance(comtypes.GUID(CLSID_POLICY_CONFIG), iface, comtypes.CLSCTX_ALL)
            for role in ROLES:
                policy.SetDefaultEndpoint(device_id, role)
        except Exception as e:  # noqa: BLE001  COMError
            raise ToolError(f"IPolicyConfig: {e}", "Windows varsayılan ses aygıtını değiştirmeme izin vermedi.") from e


class WinAppAudio:  # pragma: no cover - yalnızca Windows
    def _sessions(self) -> list:
        from jarvis.tools.winapi import com_thread

        com_thread()
        try:
            from pycaw.pycaw import AudioUtilities
        except ImportError as e:
            raise ToolError("pycaw yok", "Uygulama sesi için pycaw kurulu değil.") from e
        try:
            return AudioUtilities.GetAllSessions()
        except Exception as e:  # noqa: BLE001  COMError "Element not found": varsayılan çıkış aygıtı yok
            raise ToolError(f"ses oturumları okunamadı: {e}",
                            "Varsayılan bir ses çıkış aygıtı bulunamadı. Hoparlör ya da kulaklık bağlı mı?") from e

    def sessions(self) -> list[AppAudioSession]:
        out = []
        for s in self._sessions():
            try:
                proc = s.Process
                vol = s.SimpleAudioVolume
                out.append(AppAudioSession(int(s.ProcessId), (proc.name() if proc else "").lower(),
                                           str(s.DisplayName or ""), round(vol.GetMasterVolume() * 100),
                                           bool(vol.GetMute())))
            except Exception:  # noqa: BLE001  işlem kapanmış olabilir
                log.debug("Ses oturumu okunamadı", exc_info=True)
        return out

    def _volume(self, pid: int):  # noqa: ANN202
        for s in self._sessions():
            if int(s.ProcessId) == pid:
                return s.SimpleAudioVolume
        raise ToolError("oturum yok", "Uygulamanın ses oturumu kapanmış.")

    def set_volume(self, pid: int, percent: int) -> None:
        self._volume(pid).SetMasterVolume(max(0, min(100, percent)) / 100, None)

    def set_mute(self, pid: int, muted: bool) -> None:
        self._volume(pid).SetMute(1 if muted else 0, None)


class UnsupportedAudioDevices:
    def list_outputs(self) -> list[AudioDevice]:
        raise NotSupportedError()

    def set_default(self, device_id: str) -> None:
        raise NotSupportedError()


class UnsupportedAppAudio:
    def sessions(self) -> list[AppAudioSession]:
        raise NotSupportedError()

    def set_volume(self, pid: int, percent: int) -> None:
        raise NotSupportedError()

    def set_mute(self, pid: int, muted: bool) -> None:
        raise NotSupportedError()


# ---------- yardımcılar ----------
DISCORD_EXES = frozenset({"discord.exe", "discordptb.exe", "discordcanary.exe"})


def discord_running() -> bool:
    import psutil

    for p in psutil.process_iter(["name"]):
        if (p.info.get("name") or "").lower() in DISCORD_EXES:
            return True
    return False


def spotify_protocol_registered() -> bool:  # pragma: no cover - Windows
    """Spotify masaüstü uygulaması `spotify:` adres şemasını kaydeder (Mağaza sürümü AppX altında)."""
    if not IS_WINDOWS:
        return False
    import winreg

    from jarvis.tools.winapi import reg_get

    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_CLASSES_ROOT):
        path = r"Software\Classes\spotify" if root == winreg.HKEY_CURRENT_USER else "spotify"
        if reg_get(root, path, "URL Protocol") is not None:
            return True
    return False


YT_MAX_BYTES = 4 * 1024 * 1024
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/130.0 Safari/537.36")


def fetch_text(url: str, timeout: float = 8.0) -> str:
    """Bir sayfayı indirir (en çok 4 MB). Yalnızca youtube.com'a izin verilir."""
    import httpx2

    headers = {"User-Agent": _UA, "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8"}
    # SOCS: Google'ın çerez onay sayfasını atlar (yalnızca "zorunlu çerezler")
    with httpx2.Client(timeout=timeout, follow_redirects=True, headers=headers, cookies={"SOCS": "CAI"}) as c:
        with c.stream("GET", url) as r:
            host = (r.url.host or "").lower()
            if host != "www.youtube.com" and not host.endswith(".youtube.com"):
                raise ToolError(f"yönlendirme: {host}", "YouTube beklenmeyen bir sayfaya yönlendirdi.")
            r.raise_for_status()
            chunks, size = [], 0
            for chunk in r.iter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size >= YT_MAX_BYTES:
                    break
            return b"".join(chunks).decode(r.encoding or "utf-8", errors="replace")
