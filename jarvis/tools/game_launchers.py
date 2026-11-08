"""Oyun başlatıcıları: Riot (VALORANT, League of Legends...), Epic Games ve Xbox / Microsoft Store oyunları.

Steam `applications.py`'de. Buradaki kaynaklar:

- Riot: `C:\\ProgramData\\Riot Games\\RiotClientInstalls.json` ("rc_default"/"rc_live") Riot Client yolunu,
  `Metadata\\<ürün>.<yama hattı>\\` klasörleri kurulu ürünleri verir. Başlatma:
  `RiotClientServices.exe --launch-product=valorant --launch-patchline=live` (kabuk yok, argüman listesi,
  ürün ve yama hattı adları doğrulanır). Riot'un belgelenmemiş yerel HTTP API'leri kullanılmaz.
- Epic: `C:\\ProgramData\\Epic\\EpicGamesLauncher\\Data\\Manifests\\*.item` (JSON) ->
  `com.epicgames.launcher://apps/<ns>%3A<id>%3A<AppName>?action=launch&silent=true`.
- Xbox / Microsoft Store: oyunlar zaten shell:AppsFolder'dan uygulama olarak gelir; paket kök klasöründe
  `MicrosoftGame.config` olanlar (GDK oyunları, XboxGames klasörü) oyun olarak işaretlenir.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import replace
from pathlib import Path, PureWindowsPath
from urllib.parse import quote

from jarvis.tools.registry import ToolError

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
RIOT_SCHEME = "riotclient://"
_RIOT_TARGET = re.compile(r"^riotclient://([a-z0-9_]{1,40})/([a-z0-9_]{1,20})$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9._\-]{1,128}$")

# ürün kimliği -> (görünen ad, ek anahtar kelimeler, işlem adları)
RIOT_PRODUCTS: dict[str, tuple[str, tuple[str, ...], tuple[str, ...]]] = {
    "valorant": ("VALORANT", ("valorant", "valo"), ("valorant-win64-shipping.exe", "valorant.exe")),
    "league_of_legends": ("League of Legends", ("lol", "league", "lig of legends"),
                          ("leagueclient.exe", "league of legends.exe")),
    "bacon": ("Legends of Runeterra", ("runeterra", "lor"), ("lor.exe",)),
    "lion": ("2XKO", ("2xko",), ()),
}


def _program_data() -> Path:
    return Path(os.environ.get("ProgramData", r"C:\ProgramData"))


def riot_root() -> Path:
    return _program_data() / "Riot Games"


def epic_manifests_dir() -> Path:
    return _program_data() / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"


# ---------- Riot ----------
def riot_client_path(root: Path | None = None) -> Path | None:
    """RiotClientServices.exe yolu (kurulum dosyasından, yoksa bilinen klasörlerden)."""
    root = root or riot_root()
    candidates: list[str] = []
    try:
        data = json.loads((root / "RiotClientInstalls.json").read_text(encoding="utf-8", errors="replace"))
        if isinstance(data, dict):
            candidates += [str(data[k]) for k in ("rc_default", "rc_live", "rc_beta") if isinstance(data.get(k), str)]
    except (OSError, ValueError):
        log.debug("RiotClientInstalls.json okunamadı", exc_info=True)
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    candidates += [r"C:\Riot Games\Riot Client\RiotClientServices.exe",
                   str(Path(pf) / "Riot Games" / "Riot Client" / "RiotClientServices.exe")]
    for c in candidates:
        p = Path(c)
        if p.name.lower() == "riotclientservices.exe" and p.is_file():
            return p
    return None


def _install_path(settings_file: Path) -> str | None:
    """product_settings.yaml içindeki kurulum klasörü (YAML kütüphanesi gerekmez: tek satır)."""
    try:
        for line in settings_file.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r'\s*product_install_full_path:\s*"?([^"\n]+)"?\s*$', line)
            if m:
                return m.group(1).strip()
    except OSError:
        pass
    return None


def riot_products(root: Path | None = None) -> list[tuple[str, str]]:
    """Kurulu (ürün, yama hattı) çiftleri: Metadata\\valorant.live -> ("valorant", "live")."""
    meta = (root or riot_root()) / "Metadata"
    out: list[tuple[str, str]] = []
    try:
        dirs = sorted(p for p in meta.iterdir() if p.is_dir())
    except OSError:
        return out
    for d in dirs:
        m = re.fullmatch(r"([a-z0-9_]{1,40})\.([a-z0-9_]{1,20})", d.name.lower())
        if not m or m.group(1) == "riot_client":
            continue
        settings = d / f"{d.name}.product_settings.yaml"
        if settings.is_file():
            path = _install_path(settings)
            if path and not Path(path).exists():
                continue   # kaldırılmış ürünün kalıntısı
        out.append((m.group(1), m.group(2)))
    return out


def riot_entries(root: Path | None = None):  # noqa: ANN201
    from jarvis.tools.applications import AppEntry

    entries = []
    for product, patchline in riot_products(root):
        name, keywords, exes = RIOT_PRODUCTS.get(product, (product.replace("_", " ").title(), (), ()))
        if patchline != "live":
            name = f"{name} ({patchline})"
        entries.append(AppEntry(name, f"{RIOT_SCHEME}{product}/{patchline}", "riot", keywords, exes, kind="game"))
    return entries


def launch_riot(target: str, root: Path | None = None, popen=subprocess.Popen) -> None:  # noqa: ANN001
    m = _RIOT_TARGET.match(target)
    if not m:
        raise ToolError("riot hedefi", "Geçersiz Riot oyunu.")
    exe = riot_client_path(root)
    if exe is None:
        raise ToolError("riot yok", "Riot Client bulunamadı; oyunu Riot Client'ı kurup giriş yaptıktan sonra açabilirim.")
    product, patchline = m.groups()
    try:
        popen([str(exe), f"--launch-product={product}", f"--launch-patchline={patchline}"], shell=False,
              close_fds=True)
    except OSError as e:
        raise ToolError(f"riot: {e}", f"Riot Client başlatılamadı: {e.strerror or e}") from e


# ---------- Epic ----------
def epic_entries(manifests: Path | None = None):  # noqa: ANN201
    from jarvis.tools.applications import AppEntry

    folder = manifests or epic_manifests_dir()
    out = []
    try:
        files = sorted(folder.glob("*.item"))
    except OSError:
        return out
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            log.debug("Epic bildirimi okunamadı: %s", f)
            continue
        if not isinstance(d, dict):
            continue
        name = str(d.get("DisplayName") or "").strip()
        app, ns, item = (str(d.get(k) or "") for k in ("AppName", "CatalogNamespace", "CatalogItemId"))
        if not name or not all(_SAFE_ID.match(x) for x in (app, ns, item)):
            continue
        if d.get("bIsIncompleteInstall"):
            continue
        main = d.get("MainGameAppName")
        if main and main != app:
            continue   # DLC
        cats = d.get("AppCategories")
        if isinstance(cats, list) and cats and "games" not in cats:
            continue   # Unreal Engine gibi araçlar
        target = (f"com.epicgames.launcher://apps/{quote(ns, safe='')}%3A{quote(item, safe='')}%3A"
                  f"{quote(app, safe='')}?action=launch&silent=true")
        exe = PureWindowsPath(str(d.get("LaunchExecutable") or "")).name.lower()
        out.append(AppEntry(name, target, "epic", (), (exe,) if exe.endswith(".exe") else (), kind="game"))
    return out


# ---------- Xbox / Microsoft Store ----------
APPX_PACKAGES_KEY = (r"Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppModel"
                     r"\Repository\Packages")


def package_family(full_name: str) -> str | None:
    """"Name_1.2.3.0_x64__8wekyb3d8bbwe" -> "Name_8wekyb3d8bbwe"."""
    parts = full_name.split("_")
    if len(parts) < 5 or not parts[0] or not parts[-1]:
        return None
    return f"{parts[0]}_{parts[-1]}"


def xbox_game_families(packages: dict[str, str]) -> set[str]:
    """{paket tam adı: kök klasör} -> oyun olan paket aileleri."""
    out: set[str] = set()
    for full, root in packages.items():
        fam = package_family(full)
        if not fam or not root:
            continue
        parts = [p.lower() for p in PureWindowsPath(root).parts]
        if "xboxgames" in parts or "modifiablewindowsapps" in parts or (Path(root) / "MicrosoftGame.config").is_file():
            out.add(fam)
    return out


def read_appx_packages() -> dict[str, str]:  # pragma: no cover - Windows
    if not IS_WINDOWS:
        return {}
    import winreg

    from jarvis.tools.winapi import reg_get, reg_subkeys

    out = {}
    for sub in reg_subkeys(winreg.HKEY_CURRENT_USER, APPX_PACKAGES_KEY):
        root = reg_get(winreg.HKEY_CURRENT_USER, APPX_PACKAGES_KEY + "\\" + sub, "PackageRootFolder")
        if isinstance(root, str):
            out[sub] = root
    return out


def mark_xbox_games(entries: list, families: set[str]) -> list:
    """shell:AppsFolder kayıtlarından oyun paketlerine ait olanları `kind="game"` yapar."""
    if not families:
        return entries
    out = []
    for e in entries:
        if e.source == "apps_folder" and e.kind != "game" and e.target.lower().startswith("shell:appsfolder\\"):
            fam = e.target.split("\\", 1)[1].split("!", 1)[0]
            if fam in families:
                e = replace(e, kind="game")
        out.append(e)
    return out


def discover_launcher_games() -> list:  # pragma: no cover - Windows
    """Riot ve Epic oyunları (Windows). Her kaynak ayrı denenir; biri bozuksa diğerleri gelir."""
    out = []
    for label, fn in (("Riot", riot_entries), ("Epic", epic_entries)):
        try:
            found = fn()
            out += found
            log.info("Uygulama keşfi: %s -> %d oyun", label, len(found))
        except Exception:  # noqa: BLE001
            log.warning("Oyun keşfi başarısız: %s", label, exc_info=True)
    return out
