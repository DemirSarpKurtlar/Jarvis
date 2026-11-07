"""Windows'a özgü arka uçlar: pencere yönetimi, ses, dosya/URI açma.

pywin32, pycaw ve comtypes yalnızca Windows'ta ve ilk kullanımda yüklenir.
Diğer işletim sistemlerinde bu sınıflar `NotSupportedError` fırlatır; hiçbir
işlem başarılıymış gibi gösterilmez.

COM, onu kullanan her thread'de ayrı başlatılmalıdır; araçlar arka plan
thread'lerinde çalıştığı için `com_thread()` her çağrıda bunu güvenceye alır.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Protocol

from jarvis.tools.registry import NotSupportedError, ToolError

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000
_com_local = threading.local()


def require_windows() -> None:
    if not IS_WINDOWS:
        raise NotSupportedError()


def com_thread() -> None:  # pragma: no cover - yalnızca Windows
    """Bu thread'de COM'u (bir kez) başlatır."""
    if getattr(_com_local, "ready", False):
        return
    try:
        import pythoncom

        pythoncom.CoInitialize()
    except ImportError:
        import comtypes

        comtypes.CoInitialize()
    _com_local.ready = True


# ---------- açıcı ----------
def default_opener(target: str) -> None:
    """Dosya, klasör, http(s) adresi ya da ms-settings:/steam:// gibi bir URI'yi varsayılan programla açar.

    Kabuk (shell=True) kullanılmaz; ShellExecute doğrudan çağrılır.
    """
    if IS_WINDOWS:  # pragma: no cover
        if target.lower().startswith("shell:appsfolder\\"):
            # Mağaza (UWP) uygulamaları: explorer'a tek argüman olarak verilir
            subprocess.Popen(["explorer.exe", target], creationflags=CREATE_NO_WINDOW)
            return
        try:
            os.startfile(target)  # type: ignore[attr-defined]
        except OSError as e:
            raise ToolError(f"startfile: {e}", f"Açılamadı: {e.strerror or e}") from e
        return
    if target.lower().startswith(("http://", "https://")):
        import webbrowser

        if not webbrowser.open(target):
            raise ToolError("tarayıcı yok", "Varsayılan tarayıcı açılamadı.")
        return
    raise NotSupportedError()


# ---------- pencereler ----------
@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    pid: int


class WindowBackend(Protocol):
    def list_windows(self) -> list[WindowInfo]: ...
    def close(self, hwnd: int) -> None: ...
    def focus(self, hwnd: int) -> bool: ...


class Win32Windows:  # pragma: no cover - yalnızca Windows
    """pywin32 ile görünür, başlıklı üst düzey pencereler."""

    def __init__(self) -> None:
        require_windows()

    def list_windows(self) -> list[WindowInfo]:
        import win32gui
        import win32process

        out: list[WindowInfo] = []

        def cb(hwnd: int, _: object) -> bool:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = win32gui.GetWindowText(hwnd)
            if not title or win32gui.GetWindow(hwnd, 4):  # GW_OWNER: sahipli (iletişim kutusu vb.) değil
                return True
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            out.append(WindowInfo(int(hwnd), title, int(pid)))
            return True

        win32gui.EnumWindows(cb, None)
        return out

    def close(self, hwnd: int) -> None:
        import win32con
        import win32gui

        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)

    def focus(self, hwnd: int) -> bool:
        import win32api
        import win32con
        import win32gui

        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:  # noqa: BLE001
            # Windows ön plan kilidi: kısa bir Alt basımı odak değiştirme iznini açar
            win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
            win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:  # noqa: BLE001
                return False
        time.sleep(0.15)
        return win32gui.GetForegroundWindow() == hwnd


class UnsupportedWindows:
    def list_windows(self) -> list[WindowInfo]:
        raise NotSupportedError()

    def close(self, hwnd: int) -> None:
        raise NotSupportedError()

    def focus(self, hwnd: int) -> bool:
        raise NotSupportedError()


def default_windows() -> WindowBackend:
    return Win32Windows() if IS_WINDOWS else UnsupportedWindows()


# ---------- ses ----------
class AudioBackend(Protocol):
    def get_volume(self) -> int: ...
    def set_volume(self, percent: int) -> None: ...
    def is_muted(self) -> bool: ...
    def set_mute(self, muted: bool) -> None: ...


class CoreAudio:  # pragma: no cover - yalnızca Windows
    """Windows Core Audio (pycaw): varsayılan çıkış aygıtının ana ses düzeyi."""

    def __init__(self) -> None:
        require_windows()

    def _endpoint(self):  # noqa: ANN202
        com_thread()
        try:
            from pycaw.pycaw import AudioUtilities
        except ImportError as e:
            raise ToolError("pycaw yok", "Ses denetimi için pycaw kurulu değil.") from e
        try:
            dev = AudioUtilities.GetSpeakers()
        except Exception as e:  # noqa: BLE001  COMError: aygıt yok
            raise ToolError(f"ses aygıtı: {e}", "Ses çıkış aygıtı bulunamadı.") from e
        if dev is None:
            raise ToolError("ses aygıtı yok", "Ses çıkış aygıtı bulunamadı.")
        if hasattr(dev, "EndpointVolume"):
            return dev.EndpointVolume
        # Eski pycaw sürümleri IMMDevice döndürür
        from ctypes import POINTER, cast

        import comtypes
        from pycaw.pycaw import IAudioEndpointVolume

        iface = dev.Activate(IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None)
        return cast(iface, POINTER(IAudioEndpointVolume))

    def get_volume(self) -> int:
        return round(self._endpoint().GetMasterVolumeLevelScalar() * 100)

    def set_volume(self, percent: int) -> None:
        self._endpoint().SetMasterVolumeLevelScalar(max(0, min(100, percent)) / 100, None)

    def is_muted(self) -> bool:
        return bool(self._endpoint().GetMute())

    def set_mute(self, muted: bool) -> None:
        self._endpoint().SetMute(1 if muted else 0, None)


class UnsupportedAudio:
    def get_volume(self) -> int:
        raise NotSupportedError()

    def set_volume(self, percent: int) -> None:
        raise NotSupportedError()

    def is_muted(self) -> bool:
        raise NotSupportedError()

    def set_mute(self, muted: bool) -> None:
        raise NotSupportedError()


def default_audio() -> AudioBackend:
    return CoreAudio() if IS_WINDOWS else UnsupportedAudio()


# ---------- kayıt defteri (yalnızca okuma yardımcıları) ----------
def reg_values(root: int, path: str) -> list[tuple[str, object, int]]:  # pragma: no cover - Windows
    import winreg

    out = []
    try:
        with winreg.OpenKey(root, path) as k:
            i = 0
            while True:
                try:
                    out.append(winreg.EnumValue(k, i))
                except OSError:
                    break
                i += 1
    except OSError:
        pass
    return out


def reg_subkeys(root: int, path: str) -> list[str]:  # pragma: no cover - Windows
    import winreg

    out = []
    try:
        with winreg.OpenKey(root, path) as k:
            i = 0
            while True:
                try:
                    out.append(winreg.EnumKey(k, i))
                except OSError:
                    break
                i += 1
    except OSError:
        pass
    return out


def reg_get(root: int, path: str, name: str = "") -> object | None:  # pragma: no cover - Windows
    import winreg

    try:
        with winreg.OpenKey(root, path) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None
