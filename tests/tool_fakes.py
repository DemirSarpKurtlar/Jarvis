"""Aşama 5 testleri için sahte arka uçlar (gerçek Windows'a dokunmaz)."""

from __future__ import annotations

from pathlib import Path

from jarvis.security.action_validator import PathSandbox
from jarvis.tools.applications import BUILTIN_APPS, AppEntry, AppIndex
from jarvis.tools.registry import ToolContext
from jarvis.tools.winapi import WindowInfo


class FakeOpener:
    def __init__(self) -> None:
        self.opened: list[str] = []

    def __call__(self, target: str) -> None:
        self.opened.append(target)


class FakeWindows:
    def __init__(self, windows: list[WindowInfo] | None = None, focus_ok: bool = True) -> None:
        self.windows = windows or []
        self.closed: list[int] = []
        self.focused: list[int] = []
        self.focus_ok = focus_ok
        self.on_close = None

    def list_windows(self) -> list[WindowInfo]:
        return list(self.windows)

    def close(self, hwnd: int) -> None:
        self.closed.append(hwnd)
        if self.on_close:
            self.on_close(hwnd)

    def focus(self, hwnd: int) -> bool:
        self.focused.append(hwnd)
        return self.focus_ok


class FakeAudio:
    def __init__(self, volume: int = 50, muted: bool = False) -> None:
        self.volume, self.muted = volume, muted

    def get_volume(self) -> int:
        return self.volume

    def set_volume(self, percent: int) -> None:
        self.volume = max(0, min(100, percent))

    def is_muted(self) -> bool:
        return self.muted

    def set_mute(self, muted: bool) -> None:
        self.muted = muted


FAKE_APPS = list(BUILTIN_APPS) + [
    AppEntry("Google Chrome", r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Google Chrome.lnk", "start_menu",
             exe_names=("chrome.exe",)),
    AppEntry("Spotify", "shell:AppsFolder\\SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify", "apps_folder",
             exe_names=("spotify.exe",)),
    AppEntry("Discord", r"C:\Users\u\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Discord Inc\Discord.lnk",
             "start_menu", exe_names=("discord.exe",)),
    AppEntry("Steam", r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Steam\Steam.lnk", "start_menu",
             exe_names=("steam.exe",)),
    AppEntry("Visual Studio Code", r"C:\Users\u\AppData\Local\Programs\Microsoft VS Code\Code.exe", "app_paths",
             exe_names=("code.exe",)),
    AppEntry("Mozilla Firefox", "firefox.exe", "app_paths", exe_names=("firefox.exe",)),
    AppEntry("Word", "shell:AppsFolder\\Microsoft.Office.WINWORD.EXE.15", "apps_folder", exe_names=("winword.exe",)),
    AppEntry("Excel", "shell:AppsFolder\\Microsoft.Office.EXCEL.EXE.15", "apps_folder", exe_names=("excel.exe",)),
    AppEntry("WhatsApp", "shell:AppsFolder\\5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App", "apps_folder"),
    AppEntry("7-Zip", "", "uninstall", launchable=False),
    AppEntry("Counter-Strike 2", "steam://rungameid/730", "steam", kind="game"),
    AppEntry("Hades", "steam://rungameid/1145360", "steam", kind="game"),
    AppEntry("Euro Truck Simulator 2", "steam://rungameid/227300", "steam", kind="game"),
]


def make_ctx(tmp_path: Path, apps: list[AppEntry] | None = None) -> ToolContext:
    roots = {}
    for key, name in (("desktop", "Desktop"), ("documents", "Documents"), ("downloads", "Downloads")):
        p = tmp_path / name
        p.mkdir(exist_ok=True)
        roots[key] = p
    # Windows'ta pytest'in geçici klasörü AppData altında: test kökleri için açıkça izin verilir
    return ToolContext(sandbox=PathSandbox(roots, allow_appdata_roots=True), apps=AppIndex(apps if apps is not None else FAKE_APPS),
                       windows=FakeWindows(), audio=FakeAudio(), opener=FakeOpener())


class ConfirmRecorder:
    """Onay geri çağrısı: verilen yanıtı döndürür, gelen istemleri saklar."""

    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.prompts = []

    async def __call__(self, prompt) -> bool:  # noqa: ANN001
        self.prompts.append(prompt)
        return self.answer
