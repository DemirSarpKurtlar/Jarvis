"""Araç girdilerinin güvenlik denetimi: dosya yolu korumalı alanı, URL ve ana makine adı.

Dosya araçları yalnızca izin verilen kök klasörlerin (varsayılan: Masaüstü,
Belgeler, İndirilenler, Resimler, Müzik, Videolar ve kullanıcının eklediği
klasörler) içinde çalışır. Şunlar her zaman reddedilir:

- `..` içeren yollar (klasör dışına tırmanma),
- UNC ve aygıt yolları (`\\\\sunucu\\paylasim`, `\\\\?\\`, `//sunucu`),
- sembolik bağ/junction ile kök dışına çıkan yollar (yol çözümlendikten sonra denetlenir),
- Windows, Program Files, ProgramData ve AppData gibi sistem klasörleri
  (AppData yalnızca kullanıcı açıkça kök olarak eklerse),
- NTFS alternatif veri akışları (`dosya.txt:gizli`) ve ayrılmış aygıt adları (CON, NUL...).
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from jarvis.core.errors import JarvisError
from jarvis.tools.text import fold

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
MAX_PATH_CHARS = 1000

_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}

# Bilinen klasör anahtarı -> (Windows KNOWNFOLDERID, varsayılan ad)
KNOWN_FOLDERS: dict[str, tuple[str, str]] = {
    "desktop": ("{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}", "Desktop"),
    "documents": ("{FDD39AD0-238F-46AF-ADB4-6C85480369C7}", "Documents"),
    "downloads": ("{374DE290-123F-4565-9164-39C4925E467B}", "Downloads"),
    "pictures": ("{33E28130-4E1E-4676-835A-98395C3BC3BB}", "Pictures"),
    "music": ("{4BD8D571-6D19-48D3-BE97-422220080E43}", "Music"),
    "videos": ("{18989B1D-99B5-455B-841C-AB7C74E4DDFC}", "Videos"),
}

FOLDER_LABELS = {"desktop": "Masaüstü", "documents": "Belgeler", "downloads": "İndirilenler",
                 "pictures": "Resimler", "music": "Müzik", "videos": "Videolar"}

# Konuşmada geçen klasör adları (fold edilmiş) -> bilinen klasör anahtarı
FOLDER_ALIASES: dict[str, str] = {}
for _key, _names in {
    "desktop": ["masaustu", "masa ustu", "desktop", "masaustum"],
    "documents": ["belgeler", "belgelerim", "dokumanlar", "dokumanlarim", "documents", "evraklar"],
    "downloads": ["indirilenler", "indirmeler", "indirdiklerim", "downloads", "download", "yuklemeler"],
    "pictures": ["resimler", "resimlerim", "fotograflar", "fotograflarim", "pictures", "gorseller"],
    "music": ["muzik", "muzikler", "muziklerim", "music"],
    "videos": ["videolar", "videolarim", "videos", "video"],
}.items():
    for _n in _names:
        FOLDER_ALIASES[_n] = _key


class ValidationError(JarvisError):
    """Güvenlik denetimini geçemeyen girdi. `user_message` Türkçe açıklamadır."""


def _known_folder_windows(guid: str) -> Path | None:  # pragma: no cover - yalnızca Windows
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    import uuid
    u = uuid.UUID(guid)
    g = GUID(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8)(*u.bytes[8:]))
    out = ctypes.c_wchar_p()
    shell32 = ctypes.windll.shell32
    ole32 = ctypes.windll.ole32
    hr = shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(out))
    try:
        if hr != 0 or not out.value:
            return None
        return Path(out.value)
    finally:
        ole32.CoTaskMemFree(out)


def known_folder(key: str) -> Path | None:
    """Kullanıcının gerçek Masaüstü/Belgeler... klasörü (OneDrive'a taşınmışsa oradaki)."""
    guid, default = KNOWN_FOLDERS[key]
    if IS_WINDOWS:
        try:
            p = _known_folder_windows(guid)
            if p is not None:
                return p
        except Exception:  # noqa: BLE001
            log.debug("Bilinen klasör okunamadı: %s", key, exc_info=True)
    return Path.home() / default


def default_roots() -> dict[str, Path]:
    roots = {}
    for key in KNOWN_FOLDERS:
        p = known_folder(key)
        if p is not None and p.is_dir():
            roots[key] = p
    return roots


def system_denied_dirs() -> list[Path]:
    """Kök eklense bile içine girilmeyen sistem klasörleri."""
    if not IS_WINDOWS:
        return [Path(p) for p in ("/bin", "/boot", "/dev", "/etc", "/lib", "/proc", "/sbin", "/sys", "/usr",
                                  "/var/lib", "/root")]
    env = os.environ
    names = [env.get("SystemRoot", r"C:\Windows"), env.get("ProgramFiles", r"C:\Program Files"),
             env.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), env.get("ProgramW6432", ""),
             env.get("ProgramData", r"C:\ProgramData")]
    drive = env.get("SystemDrive", "C:")
    names += [drive + "\\$Recycle.Bin", drive + "\\System Volume Information", drive + "\\Recovery",
              drive + "\\$WinREAgent"]
    return [Path(n) for n in names if n]


def appdata_dirs() -> list[Path]:
    if IS_WINDOWS:
        return [Path(p) for p in (os.environ.get("APPDATA", ""), os.environ.get("LOCALAPPDATA", ""),
                                  str(Path.home() / "AppData")) if p]
    return [Path.home() / ".config", Path.home() / ".local", Path.home() / ".ssh", Path.home() / ".gnupg"]


def _norm(p: Path) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


def is_within(child: Path, parent: Path) -> bool:
    c, p = _norm(child), _norm(parent)
    return c == p or c.startswith(p.rstrip("\\/") + os.sep)


def _real(p: Path) -> Path:
    return Path(os.path.realpath(p))


@dataclass(frozen=True)
class Root:
    key: str       # "desktop", "documents"... ya da "ozel1"
    label: str     # kullanıcıya gösterilen ad
    path: Path     # çözümlenmiş gerçek yol


class PathSandbox:
    def __init__(self, roots: dict[str, Path] | None = None, extra_roots: Iterable[str | Path] = (),
                 allow_appdata_roots: bool = False) -> None:
        self._denied = [_real(d) for d in system_denied_dirs()]
        self._appdata = [_real(d) for d in appdata_dirs()]
        self.roots: list[Root] = []
        for key, path in (roots if roots is not None else default_roots()).items():
            self._add(key, FOLDER_LABELS.get(key, key), Path(path), allow_appdata_roots)
        for i, extra in enumerate(extra_roots, start=1):
            try:
                self._add(f"ozel{i}", Path(extra).name or str(extra), Path(extra), True)
            except ValidationError as e:
                log.warning("Ek klasör kök olarak eklenmedi (%s): %s", extra, e.user_message)

    def _add(self, key: str, label: str, path: Path, allow_appdata: bool) -> None:
        raw = str(path)
        if raw.startswith(("\\\\", "//")):
            raise ValidationError("UNC kök", "Ağ (UNC) yolları kök klasör olarak eklenemez.")
        real = _real(path.expanduser())
        if not real.is_dir():
            raise ValidationError("kök yok", f"Klasör bulunamadı: {raw}")
        if any(is_within(real, d) or is_within(d, real) for d in self._denied):
            raise ValidationError("sistem kökü", "Sistem klasörleri ya da onları içeren klasörler kök olamaz.")
        if not allow_appdata and any(is_within(real, d) for d in self._appdata):
            raise ValidationError("appdata", "AppData klasörü yalnızca açıkça eklenirse kullanılabilir.")
        if real == Path(real.anchor):
            raise ValidationError("sürücü kökü", "Bütün bir sürücü kök klasör olarak eklenemez.")
        self.roots.append(Root(key, label, real))

    # ---------- sorgular ----------
    def describe(self) -> list[dict]:
        return [{"anahtar": r.key, "ad": r.label, "yol": str(r.path)} for r in self.roots]

    def root_for_alias(self, name: str) -> Root | None:
        f = fold(name)
        f = re.sub(r"\s*(klasoru|klasorum|klasorunde|klasorune|klasorumde|dizini)$", "", f).strip()
        key = FOLDER_ALIASES.get(f, f)
        for r in self.roots:
            if r.key == key or fold(r.label) == f or fold(r.path.name) == f:
                return r
        return None

    def root_of(self, path: Path) -> Root | None:
        for r in self.roots:
            if is_within(path, r.path):
                return r
        return None

    def is_root(self, path: Path) -> bool:
        return any(_norm(path) == _norm(r.path) for r in self.roots)

    # ---------- doğrulama ----------
    def resolve(self, raw: str, *, must_exist: bool = False, for_write: bool = False) -> Path:
        """Kullanıcı/Claude'dan gelen yolu denetler ve gerçek (çözümlenmiş) yolu döndürür.

        Göreli yol bir kök adıyla başlamalı: "Masaüstü/notlar.txt", "Downloads\\rapor.pdf".
        """
        if not isinstance(raw, str) or not raw.strip():
            raise ValidationError("boş yol", "Bir dosya ya da klasör yolu gerekli.")
        raw = raw.strip().strip('"')
        if len(raw) > MAX_PATH_CHARS:
            raise ValidationError("uzun yol", "Yol çok uzun.")
        if any(ord(c) < 32 for c in raw):
            raise ValidationError("kontrol karakteri", "Yol geçersiz karakter içeriyor.")
        if raw.startswith(("\\\\", "//", "\\??\\")):
            raise ValidationError("UNC", "Ağ (UNC) ve aygıt yollarına erişmiyorum.")
        parts = re.split(r"[\\/]+", raw)
        if any(p == ".." for p in parts):
            raise ValidationError("traversal", "Yolda '..' kullanılamaz.")
        # Sürücü harfinden sonraki ':' NTFS alternatif veri akışı demektir
        rest_of_path = raw[2:] if re.match(r"^[A-Za-z]:", raw) else raw
        if ":" in rest_of_path:
            raise ValidationError("ADS", "Yolda ':' kullanılamaz.")
        if raw.startswith("~"):
            raw = os.path.expanduser(raw)

        p = Path(raw)
        if not p.is_absolute():
            first, *rest = [x for x in re.split(r"[\\/]+", raw) if x and x != "."]
            root = self.root_for_alias(first)
            if root is None:
                names = ", ".join(r.label for r in self.roots) or "yok"
                raise ValidationError("göreli yol", f"Yol tam olmalı ya da şu klasörlerden biriyle başlamalı: {names}.")
            p = root.path.joinpath(*rest)

        for part in p.parts[1:]:
            stem = part.split(".")[0].strip().lower()
            if stem in _RESERVED:
                raise ValidationError("ayrılmış ad", f"'{part}' Windows'ta ayrılmış bir ad, kullanılamaz.")
            if for_write and (part.endswith((" ", ".")) or re.search(r'[<>"|?*]', part)):
                raise ValidationError("geçersiz ad", f"'{part}' geçerli bir dosya adı değil.")

        real = _real(p)
        root = self.root_of(real)
        if root is None:
            names = ", ".join(r.label for r in self.roots) or "yok"
            raise ValidationError("kök dışı", f"Bu konuma erişim iznim yok. İzinli klasörler: {names}.")
        if any(is_within(real, d) for d in self._denied):
            raise ValidationError("sistem", "Sistem klasörlerine dokunmuyorum.")
        # Kök AppData'nın üstündeyse (örn. kullanıcı klasörü) AppData yine kapalı kalır
        if any(is_within(real, d) and not is_within(root.path, d) for d in self._appdata):
            raise ValidationError("appdata", "AppData klasörüne yalnızca açıkça izin verilirse erişirim.")
        if must_exist and not real.exists():
            raise ValidationError("yok", f"Bulunamadı: {real}")
        return real


# ---------- URL ve ana makine ----------
_HOST_RE = re.compile(r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*$")


def validate_url(url: str) -> str:
    """Yalnızca http/https, kullanıcı adı/parola içermeyen, makul uzunlukta adresler."""
    if not isinstance(url, str):
        raise ValidationError("url", "Adres metin olmalı.")
    url = url.strip()
    if len(url) > 2048 or any(ord(c) < 33 for c in url):
        raise ValidationError("url", "Adres geçersiz.")
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        raise ValidationError("url şeması", "Yalnızca http ve https adreslerini açarım.")
    if not parts.hostname or parts.username or parts.password:
        raise ValidationError("url", "Adres geçersiz ya da içinde kullanıcı bilgisi var.")
    validate_host(parts.hostname)
    return url


def validate_host(host: str) -> str:
    host = host.strip()
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    if not _HOST_RE.match(host):
        raise ValidationError("host", "Geçersiz ana makine adı.")
    return host
