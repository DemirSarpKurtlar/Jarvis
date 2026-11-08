"""Uygulama araçları: kurulu uygulamaları bulma, açma, kapatma, pencereye geçme,
Steam oyunları ve başlangıç uygulamaları.

Keşif kaynakları (yalnızca Windows):
- Başlat menüsü kısayolları (.lnk/.url): ProgramData ve kullanıcının AppData klasörü
- shell:AppsFolder (Mağaza/UWP uygulamaları dahil Başlat menüsündeki her şey)
- App Paths kayıt defteri anahtarları (HKLM ve HKCU)
- Uninstall anahtarları (yalnızca ad; listeleme için, başlatılmaz)
- Steam: libraryfolders.vdf + appmanifest_*.acf -> steam://rungameid/<id>

Uygulamalar ShellExecute ile açılır (kabuk yok). Komut istemi, PowerShell ve
kayıt defteri düzenleyicisi gibi hassas programlar onay ister.
"""

from __future__ import annotations

import logging
import os
import re
import struct
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

import psutil

from jarvis.security.permission_manager import ConfirmPrompt, Permission
from jarvis.tools import schema as js
from jarvis.tools.processes import protection_reason, terminate
from jarvis.tools.registry import (NotSupportedError, ToolContext, ToolError, ToolOutput, ToolRegistry, ToolSpec,
                                   WINDOWS_ONLY)
from jarvis.tools.text import fold, suffix_variants

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"


@dataclass(frozen=True)
class AppEntry:
    name: str                          # görünen ad
    target: str                        # .lnk yolu, exe yolu, exe adı ya da URI (steam://, ms-settings:)
    source: str                        # builtin / start_menu / apps_folder / app_paths / uninstall / steam
    keywords: tuple[str, ...] = ()     # ek eşleşme anahtarları (Türkçe adlar vb.)
    exe_names: tuple[str, ...] = ()    # kapatma/pencere bulma için işlem adları (küçük harf)
    kind: str = "app"                  # app / game
    launchable: bool = True
    sensitive: bool = False            # açmak onay ister (kabuk, kayıt defteri...)


_SOURCE_RANK = {"builtin": 5, "apps_folder": 4, "start_menu": 3, "steam": 3, "app_paths": 2, "uninstall": 0}

# Windows ile gelen uygulamalar: adı dil fark etmeksizin bulunabilsin
BUILTIN_APPS: tuple[AppEntry, ...] = (
    AppEntry("Not Defteri", "notepad.exe", "builtin", ("notepad", "not defteri", "metin editörü"), ("notepad.exe",)),
    AppEntry("Hesap Makinesi", "calc.exe", "builtin", ("calculator", "hesap makinesi", "calc"),
             ("calculatorapp.exe", "calculator.exe", "calc.exe")),
    AppEntry("Paint", "mspaint.exe", "builtin", ("paint", "boya", "mspaint"), ("mspaint.exe",)),
    AppEntry("Dosya Gezgini", "explorer.exe", "builtin",
             ("file explorer", "dosya gezgini", "gezgin", "explorer", "bilgisayarım", "dosyalar"), ()),
    AppEntry("Görev Yöneticisi", "taskmgr.exe", "builtin", ("task manager", "görev yöneticisi", "taskmgr"),
             ("taskmgr.exe",)),
    AppEntry("Ayarlar", "ms-settings:", "builtin", ("settings", "ayarlar", "windows ayarları"),
             ("systemsettings.exe",)),
    AppEntry("Denetim Masası", "control.exe", "builtin", ("control panel", "denetim masası", "kontrol paneli"), ()),
    AppEntry("Ekran Alıntısı Aracı", "snippingtool.exe", "builtin",
             ("snipping tool", "ekran alıntısı", "ekran alıntısı aracı", "ekran görüntüsü aracı"),
             ("snippingtool.exe",)),
    AppEntry("Kamera", "microsoft.windows.camera:", "builtin", ("camera", "kamera"), ("windowscamera.exe",)),
    AppEntry("Microsoft Store", "ms-windows-store:", "builtin", ("store", "mağaza", "microsoft store"),
             ("winstore.app.exe",)),
    AppEntry("Microsoft Edge", "msedge.exe", "builtin", ("edge", "microsoft edge"), ("msedge.exe",)),
    AppEntry("Komut İstemi", "cmd.exe", "builtin", ("cmd", "command prompt", "komut istemi", "komut satırı"),
             ("cmd.exe",), sensitive=True),
    AppEntry("Windows PowerShell", "powershell.exe", "builtin", ("powershell",), ("powershell.exe",), sensitive=True),
    AppEntry("Terminal", "wt.exe", "builtin", ("windows terminal", "terminal"), ("windowsterminal.exe",),
             sensitive=True),
    AppEntry("Kayıt Defteri Düzenleyicisi", "regedit.exe", "builtin",
             ("regedit", "registry editor", "kayıt defteri"), ("regedit.exe",), sensitive=True),
)

SENSITIVE_EXES = frozenset({"cmd.exe", "powershell.exe", "pwsh.exe", "wt.exe", "regedit.exe", "mmc.exe",
                            "gpedit.msc", "services.msc", "secpol.msc", "wscript.exe", "cscript.exe", "mshta.exe"})

# Konuşma dilindeki adlar -> aranacak uygulama adları (sıra tercihtir)
ALIASES: dict[str, tuple[str, ...]] = {
    "tarayici": ("google chrome", "microsoft edge", "mozilla firefox", "brave", "opera"),
    "internet tarayicisi": ("google chrome", "microsoft edge", "mozilla firefox"),
    "web tarayicisi": ("google chrome", "microsoft edge", "mozilla firefox"),
    "internet": ("google chrome", "microsoft edge", "mozilla firefox"),
    "chrome": ("google chrome",),
    "krom": ("google chrome",),
    "firefox": ("mozilla firefox",),
    "vs code": ("visual studio code",),
    "vscode": ("visual studio code",),
    "kod": ("visual studio code",),
    "word": ("word", "microsoft word"),
    "excel": ("excel", "microsoft excel"),
    "powerpoint": ("powerpoint", "microsoft powerpoint"),
    "outlook": ("outlook", "microsoft outlook"),
    "teams": ("microsoft teams", "teams"),
    "muzik": ("spotify",),
}

# Kısayol/uygulama adında bunlar varsa uygulama sayılmaz
_JUNK = re.compile(r"(?i)\b(uninstall|kaldır|kaldir|readme|benioku|help|yardım|license|lisans|website|web site|"
                   r"release notes|documentation|changelog|manual|kılavuz|setup|install)\b")


# ---------- eşleştirme ----------
def _key_score(q: str, k: str) -> float:
    if not q or not k:
        return 0.0
    if q == k:
        return 1.0
    qs, ks = q.replace(" ", ""), k.replace(" ", "")
    if qs == ks:
        return 0.98
    k_tokens, q_tokens = k.split(), q.split()
    if len(q) >= 3 and (k.startswith(q + " ") or q in k_tokens):
        return 0.9
    if q_tokens and all(t in k_tokens for t in q_tokens):
        return 0.88
    if len(q) >= 4 and q in k:
        return 0.8
    return SequenceMatcher(None, q, k).ratio() * 0.9


def match_score(query: str, entry: AppEntry) -> float:
    best = 0.0
    keys = [fold(entry.name), *(fold(k) for k in entry.keywords)]
    for cand in suffix_variants(query):
        q = fold(cand)
        for k in keys:
            s = _key_score(q, k)
            if s > best:
                best = s
    return best


class AppIndex:
    """Kurulu uygulamaların önbellekli dizini. `discover` engelleyicidir: arka plan thread'inde çağrılır."""

    def __init__(self, entries: list[AppEntry] | None = None,
                 discover: Callable[[], list[AppEntry]] | None = None, ttl_s: float = 600.0) -> None:
        self._static = entries
        self._discover = discover or discover_apps
        self._ttl = ttl_s
        self._cache: list[AppEntry] | None = None
        self._at = 0.0
        self._lock = threading.Lock()

    def entries(self) -> list[AppEntry]:
        if self._static is not None:
            return list(self._static)
        with self._lock:
            if self._cache is None or time.monotonic() - self._at > self._ttl:
                self._cache = self._discover()
                self._at = time.monotonic()
            return list(self._cache)

    def refresh(self) -> None:
        with self._lock:
            self._cache = None

    def find(self, query: str, limit: int = 5, kind: str | None = None, min_score: float = 0.55,
             launchable_only: bool = False) -> list[tuple[AppEntry, float]]:
        queries = [query]
        f = fold(query)
        for cand in suffix_variants(query):
            queries += list(ALIASES.get(fold(cand), ()))
        if f in ALIASES:
            queries += list(ALIASES[f])
        scored: dict[tuple[str, str], tuple[AppEntry, float]] = {}
        for e in self.entries():
            if kind and e.kind != kind:
                continue
            if launchable_only and not e.launchable:
                continue
            s = 0.0
            for i, q in enumerate(queries):
                # Takma adın sonraki tercihleri biraz daha düşük puan alır
                s = max(s, match_score(q, e) - (0.01 * i if i else 0))
            if s >= min_score:
                key = (fold(e.name), e.kind)
                prev = scored.get(key)
                if prev is None or (s, _SOURCE_RANK.get(e.source, 0)) > (prev[1], _SOURCE_RANK.get(prev[0].source, 0)):
                    scored[key] = (e, s)
        out = sorted(scored.values(), key=lambda x: (x[1], _SOURCE_RANK.get(x[0].source, 0), x[0].launchable),
                     reverse=True)
        return out[:limit]

    def best(self, query: str, kind: str | None = None, threshold: float = 0.78) -> AppEntry | None:
        found = self.find(query, limit=2, kind=kind, launchable_only=True)
        if not found or found[0][1] < threshold:
            return None
        return found[0][0]


# ---------- Steam ----------
def parse_vdf(text: str) -> dict:
    """Valve KeyValues (VDF/ACF) metnini sözlüğe çevirir. Anahtarlar küçük harfe çevrilmez."""
    stack: list[dict] = [{}]
    pending: str | None = None
    for m in re.finditer(r'"(?P<q>(?:[^"\\]|\\.)*)"|(?P<b>[{}])|//[^\n]*', text):
        quoted, brace = m.group("q"), m.group("b")
        if quoted is None and brace is None:
            continue  # yorum
        if brace == "{":
            new: dict = {}
            if pending is None:
                raise ValueError("VDF: anahtarsız blok")
            stack[-1][pending] = new
            stack.append(new)
            pending = None
        elif brace == "}":
            if len(stack) == 1:
                raise ValueError("VDF: fazladan kapanış")
            stack.pop()
            pending = None
        else:
            value = quoted.replace('\\"', '"').replace("\\\\", "\\")
            if pending is None:
                pending = value
            else:
                stack[-1][pending] = value
                pending = None
    return stack[0]


# Oyun olmayan Steam bileşenleri
_STEAM_TOOLS = re.compile(r"(?i)^(steamworks|steam linux runtime|proton|steamvr|steam controller|"
                          r"spacewar$|source sdk|dedicated server)")
_STEAM_TOOL_IDS = {"228980", "1070560", "1391110", "1628350", "1493710", "250820", "1826330", "961940"}


@dataclass(frozen=True)
class SteamGame:
    appid: str
    name: str
    library: str
    installdir: str


def steam_libraries(steam_root: Path) -> list[Path]:
    libs = [steam_root]
    vdf = steam_root / "steamapps" / "libraryfolders.vdf"
    try:
        data = parse_vdf(vdf.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return libs
    folders = data.get("libraryfolders") or data.get("LibraryFolders") or {}
    for k, v in folders.items():
        if not k.isdigit():
            continue
        path = v.get("path") if isinstance(v, dict) else v  # yeni ve eski biçim
        if path:
            p = Path(path)
            if p not in libs:
                libs.append(p)
    return libs


def steam_games(steam_root: Path) -> list[SteamGame]:
    games: dict[str, SteamGame] = {}
    for lib in steam_libraries(steam_root):
        apps = lib / "steamapps"
        try:
            manifests = sorted(apps.glob("appmanifest_*.acf"))
        except OSError:
            continue
        for m in manifests:
            try:
                st = parse_vdf(m.read_text(encoding="utf-8", errors="replace")).get("AppState", {})
            except (OSError, ValueError):
                log.debug("Steam bildirimi okunamadı: %s", m)
                continue
            appid, name = str(st.get("appid", "")), str(st.get("name", "")).strip()
            if not appid.isdigit() or not name or appid in _STEAM_TOOL_IDS or _STEAM_TOOLS.match(name):
                continue
            games[appid] = SteamGame(appid, name, str(lib), str(st.get("installdir", "")))
    return sorted(games.values(), key=lambda g: g.name.lower())


def steam_root() -> Path | None:  # pragma: no cover - Windows
    if not IS_WINDOWS:
        return None
    import winreg

    from jarvis.tools.winapi import reg_get

    for root, path, name in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                             (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
                             (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath")):
        v = reg_get(root, path, name)
        if isinstance(v, str) and Path(v).is_dir():
            return Path(v)
    default = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam"
    return default if default.is_dir() else None


def game_entries(root: Path | None) -> list[AppEntry]:
    if root is None:
        return []
    return [AppEntry(g.name, f"steam://rungameid/{g.appid}", "steam", (), (), kind="game")
            for g in steam_games(root)]


# ---------- Windows keşfi ----------
def _start_menu_dirs() -> list[Path]:
    dirs = []
    for env in ("ProgramData", "APPDATA"):
        base = os.environ.get(env)
        if base:
            dirs.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return dirs


def _read_url_file(p: Path) -> str | None:
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.lower().startswith("url="):
                return line[4:].strip()
    except OSError:
        pass
    return None


def scan_start_menu(dirs: list[Path] | None = None) -> list[AppEntry]:
    out = []
    for d in dirs if dirs is not None else _start_menu_dirs():
        if not d.is_dir():
            continue
        for p in d.rglob("*"):
            if p.suffix.lower() not in (".lnk", ".url") or _JUNK.search(p.stem):
                continue
            if p.suffix.lower() == ".url":
                url = _read_url_file(p)
                if not url:
                    continue
                kind = "game" if url.lower().startswith(("steam://rungameid/", "com.epicgames.launcher://")) else "app"
                if not url.lower().startswith(("steam://", "com.epicgames.launcher://")):
                    continue  # web sitesi kısayolları uygulama değil
                out.append(AppEntry(p.stem, url, "start_menu", kind=kind))
            else:
                out.append(AppEntry(p.stem, str(p), "start_menu"))
    return out


def scan_apps_folder() -> list[AppEntry]:  # pragma: no cover - Windows
    """shell:AppsFolder: Başlat menüsündeki tüm uygulamalar, Mağaza uygulamaları dahil."""
    from jarvis.tools.winapi import com_thread

    com_thread()
    import win32com.client

    shell = win32com.client.Dispatch("Shell.Application")
    folder = shell.NameSpace("shell:AppsFolder")
    out = []
    if folder is None:
        return out
    for item in folder.Items():
        name, path = str(item.Name or "").strip(), str(item.Path or "").strip()
        if not name or not path or _JUNK.search(name) or path.lower().startswith(("http://", "https://")):
            continue
        out.append(AppEntry(name, "shell:AppsFolder\\" + path, "apps_folder"))
    return out


def scan_registry() -> list[AppEntry]:  # pragma: no cover - Windows
    import winreg

    from jarvis.tools.winapi import reg_get, reg_subkeys

    out = []
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
        for exe in reg_subkeys(root, base):
            target = reg_get(root, base + "\\" + exe)
            if isinstance(target, str) and target.strip():
                out.append(AppEntry(Path(exe).stem, exe, "app_paths", (), (exe.lower(),)))
        for base in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                     r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
            for sub in reg_subkeys(root, base):
                name = reg_get(root, base + "\\" + sub, "DisplayName")
                if isinstance(name, str) and name.strip() and not reg_get(root, base + "\\" + sub, "SystemComponent"):
                    out.append(AppEntry(name.strip(), "", "uninstall", launchable=False))
    return out


def discover_apps() -> list[AppEntry]:
    """Bu bilgisayardaki uygulamalar. Windows dışında boş liste (uydurma veri yok)."""
    if not IS_WINDOWS:
        return []
    entries: list[AppEntry] = list(BUILTIN_APPS)  # pragma: no cover - Windows
    for name, fn in (("Başlat menüsü", scan_start_menu), ("AppsFolder", scan_apps_folder),
                     ("kayıt defteri", scan_registry), ("Steam", lambda: game_entries(steam_root()))):
        try:
            found = fn()
            entries += found
            log.info("Uygulama keşfi: %s -> %d kayıt", name, len(found))
        except Exception:  # noqa: BLE001
            log.warning("Uygulama keşfi başarısız: %s", name, exc_info=True)
    return entries


def lnk_target(path: str) -> str | None:  # pragma: no cover - Windows
    if not IS_WINDOWS or not path.lower().endswith(".lnk"):
        return None
    try:
        from jarvis.tools.winapi import com_thread

        com_thread()
        import win32com.client

        return str(win32com.client.Dispatch("WScript.Shell").CreateShortcut(path).TargetPath or "") or None
    except Exception:  # noqa: BLE001
        return None


def exe_names_for(entry: AppEntry) -> set[str]:
    names = {n.lower() for n in entry.exe_names}
    t = entry.target
    if t.lower().endswith(".exe"):
        names.add(Path(t).name.lower())
    elif t.lower().endswith(".lnk"):
        real = lnk_target(t)
        if real and real.lower().endswith(".exe"):
            names.add(Path(real).name.lower())
    return names


_SENSITIVE_NAMES = re.compile(r"(powershell|command prompt|komut istemi|terminal|regedit|registry editor|"
                              r"kayit defteri|group policy|grup ilkesi|services|hizmetler|bilgisayar yonetimi|"
                              r"computer management|local security policy|yerel guvenlik)")


def is_sensitive(entry: AppEntry) -> bool:
    """Sistem ayarlarını değiştirebilen araçlar (kabuk, kayıt defteri, yönetim konsolu): açmak onay ister."""
    if entry.sensitive or _SENSITIVE_NAMES.search(fold(entry.name)):
        return True
    return bool(exe_names_for(entry) & SENSITIVE_EXES)


def running_matches(entry: AppEntry | None, query: str) -> list[psutil.Process]:
    """Uygulamaya ait çalışan işlemler (exe adı ya da işlem adının sorguya benzerliğiyle)."""
    names = exe_names_for(entry) if entry else set()
    qs = [fold(c) for c in suffix_variants(query)] + ([fold(entry.name)] if entry else [])
    out = []
    for p in psutil.process_iter(["pid", "name"]):
        n = (p.info.get("name") or "").lower()
        if not n:
            continue
        stem = fold(n).removesuffix(" exe")
        if n in names or any(q and (stem == q or stem.replace(" ", "") == q.replace(" ", "")) for q in qs):
            out.append(p)
    return out


# ---------- başlangıç uygulamaları ----------
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"


def startup_enabled(data: bytes | None) -> bool:
    """StartupApproved değeri: yoksa etkin; ilk baytın en düşük biti 1 ise devre dışı (0x03, 0x07)."""
    if not data:
        return True
    return (data[0] & 1) == 0


def startup_value(enabled: bool, now: datetime | None = None) -> bytes:
    """Görev Yöneticisi'nin yazdığı 12 baytlık değer: durum (4 bayt) + devre dışı bırakılma zamanı (FILETIME)."""
    if enabled:
        return bytes([2]) + bytes(11)
    now = now or datetime.now(timezone.utc)
    filetime = int((now - datetime(1601, 1, 1, tzinfo=timezone.utc)).total_seconds() * 10_000_000)
    return bytes([3, 0, 0, 0]) + struct.pack("<Q", filetime)


@dataclass(frozen=True)
class StartupItem:
    name: str
    command: str
    scope: str          # "kullanici" (HKCU, değiştirilebilir) / "sistem" (HKLM, yalnızca okunur)
    enabled: bool


def list_startup() -> list[StartupItem]:  # pragma: no cover - Windows
    import winreg

    from jarvis.tools.winapi import reg_values

    items = []
    for root, scope in ((winreg.HKEY_CURRENT_USER, "kullanici"), (winreg.HKEY_LOCAL_MACHINE, "sistem")):
        approved = {n: d for n, d, _t in reg_values(root, APPROVED_KEY)}
        for name, cmd, _t in reg_values(root, RUN_KEY):
            data = approved.get(name)
            items.append(StartupItem(name, str(cmd), scope,
                                     startup_enabled(data if isinstance(data, bytes) else None)))
    return items


def set_startup(name: str, enabled: bool) -> None:  # pragma: no cover - Windows
    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, APPROVED_KEY, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, name, 0, winreg.REG_BINARY, startup_value(enabled))


# ---------- araçlar ----------
def _entry_dict(e: AppEntry, score: float | None = None) -> dict:
    d = {"name": e.name, "source": e.source, "kind": e.kind, "launchable": e.launchable}
    if score is not None:
        d["score"] = round(score, 2)
    return d


@dataclass
class AppTools:
    ctx: ToolContext
    close_wait_s: float = 5.0
    startup_reader: Callable[[], list[StartupItem]] = list_startup
    startup_writer: Callable[[str, bool], None] = set_startup

    @property
    def index(self) -> AppIndex:
        return self.ctx.apps

    def _open(self, target: str) -> None:
        if self.ctx.opener is None:
            raise NotSupportedError()
        self.ctx.opener(target)

    def _resolve(self, name: str, kind: str | None = None) -> AppEntry:
        entry = self.index.best(name, kind=kind)
        if entry is None:
            near = self.index.find(name, limit=3, kind=kind, launchable_only=True)
            hint = (" Şunlardan birini mi demek istedin: " + ", ".join(e.name for e, _ in near) + "?") if near else ""
            what = "oyun" if kind == "game" else "uygulama"
            raise ToolError("bulunamadı", f"'{name}' adında bir {what} bulamadım.{hint}")
        return entry

    # open_app
    def prepare_open(self, args: dict) -> dict:
        e = self._resolve(args["name"])
        return {**args, "_entry": e, "_sensitive": is_sensitive(e)}

    def open_app(self, args: dict) -> ToolOutput:
        e: AppEntry = args["_entry"]
        self._open(e.target)
        return ToolOutput(f"{e.name} açılıyor.", {"opened": e.name, "source": e.source})

    # find_apps
    def find_apps(self, args: dict) -> ToolOutput:
        if not args["query"].strip():
            entries = [e for e in self.index.entries() if e.launchable]
            items = sorted({e.name for e in entries}, key=str.lower)
            return ToolOutput(f"{len(items)} uygulama kayıtlı.", items[: args["limit"]])
        found = self.index.find(args["query"], limit=args["limit"])
        if not found:
            return ToolOutput(f"'{args['query']}' ile eşleşen uygulama yok.", [])
        return ToolOutput("Bulunanlar: " + ", ".join(e.name for e, _ in found[:5]) + ".",
                          [_entry_dict(e, s) for e, s in found])

    # close_app
    def prepare_close(self, args: dict) -> dict:
        entry = self.index.best(args["name"])
        procs = running_matches(entry, args["name"])
        label = entry.name if entry else args["name"]
        if not procs:
            raise ToolError("çalışmıyor", f"Çalışan bir {label} bulamadım.")
        for p in procs:
            reason = protection_reason(p)
            if reason:
                raise ToolError("korumalı", reason)
        return {**args, "_label": label, "_pids": [p.pid for p in procs]}

    def close_app(self, args: dict) -> ToolOutput:
        label, pids = args["_label"], args["_pids"]
        procs = []
        for pid in pids:
            try:
                procs.append(psutil.Process(pid))
            except psutil.NoSuchProcess:
                pass
        if not procs:
            return ToolOutput(f"{label} zaten kapalı.", {"closed": True})
        if args["force"]:
            _, alive = terminate(procs, force=True)
            if alive:
                return ToolOutput(f"{label} zorla da kapatılamadı.", {"closed": False, "alive": alive})
            return ToolOutput(f"{label} zorla kapatıldı.", {"closed": True})
        if self.ctx.windows is None:
            raise NotSupportedError()
        wins = [w for w in self.ctx.windows.list_windows() if w.pid in set(pids)]
        if not wins:
            if not IS_WINDOWS:
                _, alive = terminate(procs, force=False)  # POSIX'te SIGTERM nazik kapatmadır
                if not alive:
                    return ToolOutput(f"{label} kapatıldı.", {"closed": True})
            raise ToolError("pencere yok", f"{label} için kapatılabilecek bir pencere bulamadım. "
                                           "Zorla kapatmamı istersen onayınla yapabilirim.")
        for w in wins:
            self.ctx.windows.close(w.hwnd)
        deadline = time.monotonic() + self.close_wait_s
        while time.monotonic() < deadline:
            if not any(_alive(p) for p in procs):
                return ToolOutput(f"{label} kapatıldı.", {"closed": True})
            time.sleep(0.2)
        return ToolOutput(f"{label} kapanma isteği gönderildi ama hâlâ açık; kaydedilmemiş bir belge soruyor olabilir.",
                          {"closed": False, "windows_signalled": len(wins)})

    def close_prompt(self, args: dict) -> ConfirmPrompt:
        return ConfirmPrompt("close_app", "Uygulamayı zorla kapat",
                             f"{args['_label']} zorla kapatılacak ({len(args['_pids'])} işlem). "
                             "Kaydedilmemiş değişiklikler kaybolur. Onaylıyor musun?",
                             danger=True, confirm_label="Zorla kapat")

    # switch_to_app
    def switch_to(self, args: dict) -> ToolOutput:
        if self.ctx.windows is None:
            raise NotSupportedError()
        wins = self.ctx.windows.list_windows()
        entry = self.index.best(args["name"])
        pids = {p.pid for p in running_matches(entry, args["name"])}
        cands = [w for w in wins if w.pid in pids]
        if not cands:
            # Pencere başlığına göre (örn. "rapor.docx - Word")
            q = fold(args["name"])
            scored = sorted(((SequenceMatcher(None, q, fold(w.title)).ratio() + (0.5 if q in fold(w.title) else 0), w)
                             for w in wins), key=lambda x: x[0], reverse=True)
            cands = [w for s, w in scored if s >= 0.8][:1]
        if not cands:
            raise ToolError("pencere yok", f"Açık bir {entry.name if entry else args['name']} penceresi bulamadım.")
        w = cands[0]
        if not self.ctx.windows.focus(w.hwnd):
            raise ToolError("odak", f"'{w.title}' penceresini öne getiremedim; Windows izin vermedi.")
        return ToolOutput(f"{w.title} penceresine geçtim.", {"title": w.title, "pid": w.pid})

    # oyunlar
    def list_games(self, args: dict) -> ToolOutput:
        games = [e for e in self.index.entries() if e.kind == "game"]
        names = sorted({g.name for g in games}, key=str.lower)
        if not names:
            return ToolOutput("Kurulu oyun bulamadım (Steam kütüphanesi ve Başlat menüsü tarandı).", [])
        return ToolOutput(f"{len(names)} oyun buldum: " + ", ".join(names[:8]) + ("…" if len(names) > 8 else "."),
                          names[: args["limit"]])

    def prepare_game(self, args: dict) -> dict:
        return {**args, "_entry": self._resolve(args["name"], kind="game")}

    def launch_game(self, args: dict) -> ToolOutput:
        e: AppEntry = args["_entry"]
        self._open(e.target)
        return ToolOutput(f"{e.name} başlatılıyor.", {"game": e.name, "source": e.source})

    # başlangıç uygulamaları
    def list_startup_apps(self, args: dict) -> ToolOutput:
        items = self.startup_reader()
        on = [i.name for i in items if i.enabled]
        data = [{"name": i.name, "scope": i.scope, "enabled": i.enabled, "command": i.command[:200]} for i in items]
        return ToolOutput(f"{len(items)} başlangıç uygulaması var, {len(on)} tanesi etkin.", data, untrusted=True)

    def prepare_startup(self, args: dict) -> dict:
        items = self.startup_reader()
        q = fold(args["name"])
        match = [i for i in items if fold(i.name) == q] or \
                [i for i in items if q and q in fold(i.name)]
        if not match:
            raise ToolError("yok", f"'{args['name']}' adında bir başlangıç uygulaması yok.")
        item = match[0]
        if item.scope != "kullanici":
            raise ToolError("sistem", f"{item.name} tüm kullanıcılar için kayıtlı; değiştirmek yönetici yetkisi "
                                      "ister, yapmıyorum. Görev Yöneticisi'nden değiştirebilirsin.")
        return {**args, "_item": item}

    def set_startup_app(self, args: dict) -> ToolOutput:
        item: StartupItem = args["_item"]
        if item.enabled == args["enabled"]:
            return ToolOutput(f"{item.name} zaten {'etkin' if item.enabled else 'devre dışı'}.", {"changed": False})
        self.startup_writer(item.name, args["enabled"])
        state = "etkinleştirildi" if args["enabled"] else "devre dışı bırakıldı"
        return ToolOutput(f"{item.name} Windows açılışında {state}. Bir sonraki oturum açışta geçerli olur.",
                          {"changed": True})

    def startup_prompt(self, args: dict) -> ConfirmPrompt:
        item: StartupItem = args["_item"]
        verb = "Windows açılışında çalışacak" if args["enabled"] else "artık Windows açılışında çalışmayacak"
        return ConfirmPrompt("set_startup_app", "Başlangıç uygulaması",
                             f"{item.name} {verb} (kayıt defteri değişikliği, HKCU). Onaylıyor musun?")


def _alive(p: psutil.Process) -> bool:
    try:
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def register(reg: ToolRegistry, ctx: ToolContext) -> AppTools:
    t = AppTools(ctx)
    name_field = js.string("Uygulama adı, Türkçe ya da İngilizce (örn. 'not defteri', 'chrome') / app name", 120, 1)

    reg.register(ToolSpec(
        name="open_app", title="Uygulama aç", category="uygulama", platforms=WINDOWS_ONLY,
        description=("Kurulu bir uygulamayı adıyla açar (bulanık eşleşme, Türkçe adlar desteklenir). Komut istemi, "
                     "PowerShell, kayıt defteri gibi hassas araçlar onay ister. / Opens an installed application by "
                     "name (fuzzy match, Turkish names supported)."),
        input_schema=js.obj(name=name_field), permission=Permission.SAFE, handler=t.open_app, prepare=t.prepare_open,
        escalate=lambda a: Permission.CONFIRM if a.get("_sensitive") else None,
        confirm=lambda a: ConfirmPrompt("open_app", "Hassas uygulama",
                                        f"{a['_entry'].name} sistem ayarlarını değiştirebilen bir araç. Açayım mı?",
                                        confirm_label="Aç")))

    reg.register(ToolSpec(
        name="find_apps", title="Uygulama ara", category="uygulama", platforms=WINDOWS_ONLY,
        description=("Kurulu uygulamalarda arar; boş sorgu tümünü listeler. / Searches installed applications; an empty "
                     "query lists them."),
        input_schema=js.obj(query=js.string("Aranan ad, boş olabilir / search text, may be empty", 120),
                            limit=js.integer("En çok sonuç / max results", 1, 200)),
        permission=Permission.READ, handler=t.find_apps))

    reg.register(ToolSpec(
        name="close_app", title="Uygulama kapat", category="uygulama", platforms=WINDOWS_ONLY,
        description=("Bir uygulamayı nazikçe kapatır (pencereye kapatma isteği). force=true zorla sonlandırır ve onay "
                     "ister. / Gracefully closes an app (WM_CLOSE); force=true kills it and needs confirmation."),
        input_schema=js.obj(name=name_field, force=js.boolean("Zorla kapat / force kill")),
        permission=Permission.SAFE, handler=t.close_app, prepare=t.prepare_close,
        escalate=lambda a: Permission.CONFIRM if a.get("force") else None, confirm=t.close_prompt, timeout_s=20))

    reg.register(ToolSpec(
        name="switch_to_app", title="Uygulamaya geç", category="uygulama", platforms=WINDOWS_ONLY,
        description=("Açık bir uygulamanın penceresini öne getirir. / Brings an open application's window to the "
                     "foreground."),
        input_schema=js.obj(name=js.string("Uygulama adı ya da pencere başlığı / app name or window title", 200, 1)),
        permission=Permission.SAFE, handler=t.switch_to))

    reg.register(ToolSpec(
        name="list_games", title="Oyunları listele", category="uygulama", platforms=WINDOWS_ONLY,
        description="Kurulu oyunları listeler (Steam ve Başlat menüsü). / Lists installed games (Steam, Start menu).",
        input_schema=js.obj(limit=js.integer("En çok sonuç / max results", 1, 300)),
        permission=Permission.READ, handler=t.list_games))

    reg.register(ToolSpec(
        name="launch_game", title="Oyun başlat", category="uygulama", platforms=WINDOWS_ONLY,
        description="Kurulu bir oyunu adıyla başlatır (Steam: steam://rungameid). / Launches an installed game by name.",
        input_schema=js.obj(name=js.string("Oyun adı / game name", 200, 1)),
        permission=Permission.SAFE, handler=t.launch_game, prepare=t.prepare_game))

    reg.register(ToolSpec(
        name="list_startup_apps", title="Başlangıç uygulamaları", category="uygulama", platforms=WINDOWS_ONLY,
        description=("Windows açılışında çalışan uygulamaları ve durumlarını listeler. / Lists startup apps (Run keys) "
                     "and whether they are enabled."),
        input_schema=js.obj(), permission=Permission.READ, handler=t.list_startup_apps))

    reg.register(ToolSpec(
        name="set_startup_app", title="Başlangıç uygulamasını değiştir", category="uygulama", platforms=WINDOWS_ONLY,
        description=("Kullanıcıya ait bir başlangıç uygulamasını etkinleştirir/devre dışı bırakır (Görev Yöneticisi ile "
                     "aynı yöntem; tüm kullanıcılar için olanlar değiştirilmez). / Enables or disables a per-user "
                     "startup app; machine-wide entries are refused."),
        input_schema=js.obj(name=js.string("Başlangıç kaydının adı / startup entry name", 200, 1),
                            enabled=js.boolean("Açılışta çalışsın mı / run at sign-in")),
        permission=Permission.CONFIRM, handler=t.set_startup_app, prepare=t.prepare_startup, confirm=t.startup_prompt))
    return t
