"""Bakım araçları: yer kaplayan dosyaları bulma ve geçici dosya temizliği.

Yer açma önerileri (`find_space_hogs`) yalnızca OKUR: en büyük dosyalar/klasörler, İndirilenler'deki
30 günden eski kurulum dosyaları, boyut + özetle doğrulanmış kopyalar. Silme önerilmez, yapılmaz;
kullanıcı isterse mevcut `delete_path` (Geri Dönüşüm Kutusu, onaylı) kullanılır.
"Sistem diski" kapsamı izinli klasörlerin dışını da yalnızca listeler (dosya içeriği okunmaz).

Geçici dosya temizliği (`clean_temp_files`): %TEMP% ve (yönetici gerekmeden yazılabiliyorsa)
Windows\\Temp içinde yalnızca 24 saatten eski dosyalar. Önce ne kadar yer açılacağı gösterilir,
onaydan sonra silinir. Sembolik bağ/bağlantı noktası (junction) asla izlenmez, kökün dışına çıkılmaz,
kullanımda olan (kilitli) dosyalar atlanır. Boşaltılan alan yalnızca gerçekten silinenlerden hesaplanır.

Her tarama süre ve öğe sayısıyla sınırlıdır ve "taramayı durdur" ile iptal edilebilir.
"""

from __future__ import annotations

import hashlib
import heapq
import logging
import os
import stat
import sys
import tempfile
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from jarvis.security.permission_manager import ConfirmPrompt, Permission
from jarvis.tools import schema as js
from jarvis.tools.cancel import OPERATIONS, CancelToken, OperationCancelled
from jarvis.tools.registry import ToolContext, ToolError, ToolOutput, ToolRegistry, ToolSpec

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
DAY = 86400.0

INSTALLER_EXTS = {".exe", ".msi", ".msix", ".zip", ".iso"}
OLD_INSTALLER_DAYS = 30
DUP_MIN_BYTES = 1024 * 1024
KINDS = ["hepsi", "buyuk_dosyalar", "buyuk_klasorler", "eski_kurulumlar", "kopyalar"]
# Sistem diski taramasında içine girilmeyen klasörler (yalnızca okuma yapılsa da yavaş ve anlamsız)
DRIVE_SKIP = {"windows", "$recycle.bin", "system volume information", "$windows.~bt", "$windows.~ws",
              "recovery", "config.msi", "$winreagent"}


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def _is_link(entry: os.DirEntry) -> bool:
    """Sembolik bağ, bağlantı noktası (junction) ya da başka bir yeniden ayrıştırma noktası mı."""
    try:
        if entry.is_symlink():
            return True
        if getattr(entry, "is_junction", None) and entry.is_junction():
            return True
        attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
        return bool(attrs & _REPARSE)
    except OSError:
        return True


@dataclass
class ScanLimits:
    max_entries: int = 300_000
    max_seconds: float = 20.0
    hash_bytes: int = 2 * 1024**3      # kopya doğrulamada en çok okunacak bayt
    hash_seconds: float = 12.0
    top_n: int = 15


@dataclass
class _File:
    path: str
    size: int
    mtime: float


def walk(root: Path, token: CancelToken, limits: ScanLimits, counter: list[int], deadline: float,
         skip_names: set[str] = frozenset(), clock: Callable[[], float] = time.monotonic):  # noqa: ANN201
    """(klasör, dosya listesi) üretir; bağları izlemez, sınırlara uyar. counter[0] taranan öğe sayısı."""
    stack = [root]
    while stack:
        token.check()
        if counter[0] >= limits.max_entries or clock() >= deadline:
            counter.append(-1)   # yarıda kaldı işareti
            return
        d = stack.pop()
        files: list[_File] = []
        try:
            with os.scandir(d) as it:
                for e in it:
                    counter[0] += 1
                    if _is_link(e):
                        continue
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if e.name.lower() not in skip_names:
                                stack.append(Path(e.path))
                        elif e.is_file(follow_symlinks=False):
                            st = e.stat(follow_symlinks=False)
                            files.append(_File(e.path, st.st_size, st.st_mtime))
                    except OSError:
                        continue
        except OSError:
            continue
        yield d, files


def _digest(path: str, limit: int | None, token: CancelToken) -> str | None:
    h = hashlib.blake2b(digest_size=20)
    read = 0
    try:
        with open(path, "rb") as f:
            while True:
                token.check()
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
                read += len(chunk)
                if limit is not None and read >= limit:
                    break
    except OSError:
        return None
    return h.hexdigest()


def find_duplicates(files: list[_File], token: CancelToken, limits: ScanLimits,
                    clock: Callable[[], float] = time.monotonic) -> tuple[list[dict], int]:
    """Boyutu aynı olanlar önce ilk 64 KB'ın, sonra tamamının özetiyle karşılaştırılır.

    Okuma bütçesi ya da süre biterse doğrulanamayan gruplar KOPYA SAYILMAZ; sayıları döner."""
    by_size: dict[int, list[_File]] = defaultdict(list)
    for f in files:
        if f.size >= DUP_MIN_BYTES:
            by_size[f.size].append(f)
    groups, unverified, budget = [], 0, limits.hash_bytes
    deadline = clock() + limits.hash_seconds
    for size, same in sorted(by_size.items(), key=lambda kv: -kv[0]):
        if len(same) < 2:
            continue
        if clock() >= deadline or budget < size * len(same):
            unverified += 1
            continue
        partial: dict[str, list[_File]] = defaultdict(list)
        for f in same:
            d = _digest(f.path, 64 * 1024, token)
            if d:
                partial[d].append(f)
        for cands in partial.values():
            if len(cands) < 2:
                continue
            full: dict[str, list[_File]] = defaultdict(list)
            for f in cands:
                budget -= f.size
                d = _digest(f.path, None, token)
                if d:
                    full[d].append(f)
            for dup in full.values():
                if len(dup) >= 2:
                    groups.append({"size": size, "size_text": human(size), "wasted": size * (len(dup) - 1),
                                   "files": sorted(x.path for x in dup)})
    groups.sort(key=lambda g: -g["wasted"])
    return groups, unverified


def system_drive() -> Path:
    if IS_WINDOWS:
        return Path(os.environ.get("SystemDrive", "C:") + "\\")
    return Path("/")


class SpaceScanner:
    def __init__(self, ctx: ToolContext, drive_root: Callable[[], Path] = system_drive,
                 limits: ScanLimits | None = None, now: Callable[[], float] = time.time) -> None:
        self.ctx, self.drive_root, self.limits, self.now = ctx, drive_root, limits or ScanLimits(), now

    def _roots(self, scope: str, folder: str) -> list[tuple[str, Path]]:
        sb = self.ctx.sandbox
        if scope == "sistem_diski":
            return [("Sistem diski", self.drive_root())]
        if folder.strip():
            p = sb.resolve(folder, must_exist=True)
            if not p.is_dir():
                raise ToolError("klasör değil", f"{p.name} bir klasör değil.")
            return [(p.name, p)]
        return [(r.label, r.path) for r in sb.roots]

    def scan(self, args: dict, token: CancelToken) -> ToolOutput:
        kind, scope = args["kind"], args["scope"]
        lim = self.limits
        roots = self._roots(scope, args["folder"])
        counter = [0]
        deadline = time.monotonic() + lim.max_seconds
        top: list[tuple[int, str]] = []
        dir_sizes: dict[str, int] = defaultdict(int)
        big_files: list[_File] = []
        installers: list[dict] = []
        downloads = self.ctx.sandbox.root_for_alias("downloads") if self.ctx.sandbox else None
        cutoff = self.now() - OLD_INSTALLER_DAYS * DAY
        skip = DRIVE_SKIP if scope == "sistem_diski" else set()
        for label, root in roots:
            for d, files in walk(root, token, lim, counter, deadline, skip):
                total = sum(f.size for f in files)
                # Klasör boyutu: kökün 1. ve 2. düzey alt klasörlerine eklenir
                try:
                    rel = Path(d).relative_to(root).parts
                except ValueError:
                    rel = ()
                for depth in (1, 2):
                    if len(rel) >= depth:
                        dir_sizes[str(root.joinpath(*rel[:depth]))] += total
                for f in files:
                    if len(top) < lim.top_n:
                        heapq.heappush(top, (f.size, f.path))
                    elif f.size > top[0][0]:
                        heapq.heapreplace(top, (f.size, f.path))
                    if f.size >= DUP_MIN_BYTES:
                        big_files.append(f)
                    if downloads is not None and Path(f.path).suffix.lower() in INSTALLER_EXTS \
                            and f.mtime < cutoff and _within(Path(f.path), downloads.path):
                        installers.append({"path": f.path, "size": f.size, "size_text": human(f.size),
                                           "age_days": int((self.now() - f.mtime) // DAY)})
            if len(counter) > 1:
                break
        partial = len(counter) > 1
        data: dict = {"scope": scope, "scanned_entries": counter[0], "partial": partial}
        parts: list[str] = []
        if kind in ("hepsi", "buyuk_dosyalar"):
            files = [{"path": p, "size": s, "size_text": human(s)} for s, p in sorted(top, reverse=True)]
            data["largest_files"] = files
            if files:
                parts.append("En büyük dosyalar: " + ", ".join(f"{Path(f['path']).name} ({f['size_text']})"
                                                                for f in files[:3]))
        if kind in ("hepsi", "buyuk_klasorler"):
            dirs = sorted(dir_sizes.items(), key=lambda kv: -kv[1])[:lim.top_n]
            data["largest_folders"] = [{"path": p, "size": s, "size_text": human(s)} for p, s in dirs if s > 0]
            if data["largest_folders"]:
                parts.append("En büyük klasörler: " + ", ".join(
                    f"{Path(x['path']).name} ({x['size_text']})" for x in data["largest_folders"][:3]))
        if kind in ("hepsi", "eski_kurulumlar"):
            installers.sort(key=lambda x: -x["size"])
            data["old_installers"] = installers[:50]
            if downloads is None:
                parts.append("İndirilenler klasörüne erişimim yok; eski kurulum dosyalarına bakamadım")
            elif installers:
                parts.append(f"İndirilenler'de {OLD_INSTALLER_DAYS} günden eski {len(installers)} kurulum/arşiv "
                             f"dosyası ({human(sum(x['size'] for x in installers))})")
        if kind in ("hepsi", "kopyalar"):
            groups, unverified = find_duplicates(big_files, token, lim)
            data["duplicates"] = groups[:30]
            data["duplicate_groups_unverified"] = unverified
            if groups:
                parts.append(f"{len(groups)} kopya dosya grubu ({human(sum(g['wasted'] for g in groups))} "
                             "gereksiz yer)")
            if unverified:
                parts.append(f"{unverified} olası kopya grubu süre/okuma sınırı yüzünden doğrulanamadı")
        msg = ("; ".join(parts) + ".") if parts else "Dikkat çekici büyük ya da gereksiz dosya bulamadım."
        if partial:
            msg += " Tarama süre/öğe sınırına ulaştı, sonuçlar kısmi."
        if scope == "sistem_diski":
            msg += " Sistem diskini yalnızca listeledim; izinli klasörler dışındakileri silemem."
        else:
            msg += " Silmek istediklerini söylersen onayınla Geri Dönüşüm Kutusu'na taşırım."
        return ToolOutput(msg, data, untrusted=True)


def _within(child: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(str(child)), os.path.normcase(str(parent))]) == \
            os.path.normcase(str(parent))
    except ValueError:
        return False


# ---------------------------------------------------------------- geçici dosyalar
def default_temp_dirs() -> list[Path]:
    """Kullanıcının %TEMP% klasörü ve yönetici gerekmeden yazılabiliyorsa Windows\\Temp."""
    dirs = [Path(tempfile.gettempdir())]
    if IS_WINDOWS:
        dirs.append(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Temp")
    return dirs


def recycle_bin_info() -> dict | None:  # pragma: no cover - Windows
    """Geri Dönüşüm Kutusu toplam boyutu ve öğe sayısı (yalnızca bilgi; boşaltılmaz)."""
    if not IS_WINDOWS:
        return None
    import ctypes
    from ctypes import wintypes

    class SHQUERYRBINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("i64Size", ctypes.c_longlong), ("i64NumItems", ctypes.c_longlong)]

    info = SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(SHQUERYRBINFO)
    if ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info)) != 0:
        return None
    return {"size": int(info.i64Size), "items": int(info.i64NumItems)}


def _age_stamp(st: os.stat_result) -> float:
    """Dosyanın "en son dokunulma" zamanı: değişiklik ve (Windows'ta) oluşturulma zamanının büyüğü.
    POSIX'te st_ctime oluşturma değil, üst veri değişikliğidir; orada yalnızca değişiklik zamanı."""
    if IS_WINDOWS:
        return max(st.st_mtime, getattr(st, "st_birthtime", st.st_ctime))
    return st.st_mtime


def _safe_temp_root(p: Path) -> Path | None:
    """Kök gerçekten bir geçici klasör mü: var, bağ değil, sürücü kökü değil, adı Temp/tmp."""
    try:
        if p.is_symlink() or not p.is_dir():
            return None
        if IS_WINDOWS and getattr(os.lstat(p), "st_file_attributes", 0) & _REPARSE:
            return None
        real = Path(os.path.realpath(p))
    except OSError:
        return None
    if real == Path(real.anchor) or len(real.parts) < 3 or real.name.lower() not in ("temp", "tmp"):
        return None
    return real


class TempCleaner:
    def __init__(self, dirs: Callable[[], list[Path]] = default_temp_dirs,
                 recycle: Callable[[], dict | None] = recycle_bin_info, now: Callable[[], float] = time.time,
                 min_age_s: float = DAY, max_entries: int = 200_000, max_seconds: float = 40.0) -> None:
        self.dirs, self.recycle, self.now = dirs, recycle, now
        self.min_age_s, self.max_entries, self.max_seconds = min_age_s, max_entries, max_seconds

    def roots(self) -> list[tuple[Path, str | None]]:
        out, seen = [], set()
        for d in self.dirs():
            real = _safe_temp_root(d)
            key = os.path.normcase(str(real)) if real else None
            if key in seen:
                continue
            seen.add(key)
            if real is None:
                out.append((d, "geçerli bir geçici klasör değil"))
            elif not os.access(real, os.R_OK | os.W_OK | os.X_OK):
                out.append((real, "yönetici izni olmadan erişilemiyor"))
            else:
                out.append((real, None))
        return out

    def _eligible(self, root: Path, token: CancelToken, counter: list[int], deadline: float):  # noqa: ANN202
        """Silinebilecek (24 saatten eski, bağ olmayan, kökün içindeki) dosyalar: (yol, boyut, üst klasör)."""
        cutoff = self.now() - self.min_age_s
        lim = ScanLimits(max_entries=self.max_entries, max_seconds=self.max_seconds)
        for d, files in walk(root, token, lim, counter, deadline):
            for f in files:
                try:
                    st = os.lstat(f.path)
                except OSError:
                    continue
                if _age_stamp(st) < cutoff:
                    yield f.path, st.st_size, str(d)

    def plan(self, token: CancelToken) -> dict:
        dirs, total_n, total_b = [], 0, 0
        counter, deadline = [0], time.monotonic() + self.max_seconds
        for root, problem in self.roots():
            if problem:
                dirs.append({"path": str(root), "skipped": problem})
                continue
            n = b = 0
            for _p, size, _d in self._eligible(root, token, counter, deadline):
                n += 1
                b += size
            dirs.append({"path": str(root), "files": n, "bytes": b, "size_text": human(b)})
            total_n, total_b = total_n + n, total_b + b
        rb = None
        try:
            rb = self.recycle()
        except Exception:  # noqa: BLE001
            log.debug("Geri Dönüşüm Kutusu bilgisi okunamadı", exc_info=True)
        return {"dirs": dirs, "files": total_n, "bytes": total_b, "size_text": human(total_b),
                "partial": len(counter) > 1, "recycle_bin": rb}

    def clean(self, token: CancelToken) -> dict:
        deleted = freed = locked = 0
        counter, deadline = [0], time.monotonic() + self.max_seconds
        cancelled = False
        try:
            for root, problem in self.roots():
                if problem:
                    continue
                root_s = str(root)
                for path, size, parent in self._eligible(root, token, counter, deadline):
                    # Silmeden hemen önce: üst klasör hâlâ kökün içinde ve bağa dönüşmemiş mi
                    try:
                        if os.path.realpath(parent) != os.path.normpath(parent) or not _within(Path(parent), root):
                            continue
                        if Path(path).is_symlink() or not _within(Path(os.path.realpath(path)), Path(root_s)):
                            continue
                        os.unlink(path)
                    except FileNotFoundError:
                        continue
                    except OSError:
                        locked += 1   # kullanımda ya da izin yok: atla
                        continue
                    deleted += 1
                    freed += size
        except OperationCancelled:
            cancelled = True
        return {"deleted": deleted, "freed_bytes": freed, "freed_mb": round(freed / 1024**2, 1),
                "skipped_in_use": locked, "cancelled": cancelled, "partial": len(counter) > 1}


def _plan_text(plan: dict) -> str:
    lines = []
    for d in plan["dirs"]:
        if "skipped" in d:
            lines.append(f"• {d['path']}: atlandı ({d['skipped']})")
        else:
            lines.append(f"• {d['path']}: {d['files']} dosya, {d['size_text']}")
    rb = plan.get("recycle_bin")
    if rb:
        lines.append(f"• Geri Dönüşüm Kutusu: {human(rb['size'])} ({rb['items']} öğe), boşaltılmayacak")
    return "\n".join(lines)


class TempTools:
    def __init__(self, cleaner: TempCleaner) -> None:
        self.cleaner = cleaner

    def prepare(self, args: dict) -> dict:
        with OPERATIONS.track("geçici dosya taraması", 60) as token:
            try:
                plan = self.cleaner.plan(token)
            except OperationCancelled as e:
                raise ToolError("iptal", "Taramayı durdurdum.") from e
        return {**args, "_plan": plan}

    def handle(self, args: dict) -> ToolOutput:
        plan = args["_plan"]
        rb = plan.get("recycle_bin")
        rb_text = f" Geri Dönüşüm Kutusu'nda {human(rb['size'])} var (boşaltmadım)." if rb else ""
        if not args["apply"]:
            return ToolOutput(f"Geçici klasörlerde 24 saatten eski {plan['files']} dosya var; silinirse yaklaşık "
                              f"{plan['size_text']} yer açılır. Kullanımdaki dosyalar atlanır.{rb_text} Temizlememi "
                              "istersen onayınla yaparım.", plan)
        if plan["files"] == 0:
            return ToolOutput("Silinecek 24 saatten eski geçici dosya yok." + rb_text, plan)
        with OPERATIONS.track("geçici dosya temizliği", 90) as token:
            res = self.cleaner.clean(token)
        msg = f"{res['deleted']} geçici dosya silindi, {res['freed_mb']:.1f} MB boşaldı."
        if res["skipped_in_use"]:
            msg += f" {res['skipped_in_use']} dosya kullanımda olduğu için atlandı."
        if res["cancelled"]:
            msg += " Temizlik yarıda durduruldu."
        return ToolOutput(msg + rb_text, res)

    @staticmethod
    def prompt(args: dict) -> ConfirmPrompt:
        plan = args["_plan"]
        return ConfirmPrompt("clean_temp_files", "Geçici dosyaları temizle",
                             f"24 saatten eski {plan['files']} geçici dosya KALICI olarak silinecek; yaklaşık "
                             f"{plan['size_text']} yer açılır. Kullanımdaki dosyalar atlanır.\n\n{_plan_text(plan)}",
                             confirm_label="Temizle")


def register(reg: ToolRegistry, ctx: ToolContext, scanner: SpaceScanner | None = None,
             cleaner: TempCleaner | None = None) -> None:
    sc = scanner or SpaceScanner(ctx)

    def scan(args: dict) -> ToolOutput:
        with OPERATIONS.track("disk taraması") as token:
            try:
                return sc.scan(args, token)
            except OperationCancelled:
                return ToolOutput("Taramayı durdurdum.", {"cancelled": True})

    reg.register(ToolSpec(
        name="find_space_hogs", title="Yer kaplayanları bul", category="sistem",
        description=("Yer açmak için öneri: en büyük dosya/klasörler, İndirilenler'de 30 günden eski kurulum "
                     "dosyaları, kopya dosyalar. Yalnızca okur; silme delete_path ile. folder='' tüm izinli "
                     "klasörler. / Finds largest files/folders, old installers, duplicates (read-only)."),
        input_schema=js.obj(kind=js.string("Ne aranacak / kind", enum=KINDS),
                            scope=js.string("Kapsam / scope", enum=["izinli_klasorler", "sistem_diski"]),
                            folder=js.string("Klasör (boş = hepsi) / folder or empty", 1000)),
        permission=Permission.READ, handler=scan, timeout_s=60))

    tt = TempTools(cleaner or TempCleaner())
    reg.register(ToolSpec(
        name="clean_temp_files", title="Geçici dosyaları temizle", category="sistem",
        description=("%TEMP% ve Windows\\Temp içindeki 24 saatten eski dosyalar: apply=false ne kadar yer açılacağını "
                     "söyler, apply=true onayla siler. / Temp cleanup: preview or delete files older than 24h."),
        input_schema=js.obj(apply=js.boolean("Sil (false = yalnızca hesapla) / apply")),
        permission=Permission.READ, handler=tt.handle, prepare=tt.prepare,
        escalate=lambda a: Permission.CONFIRM if a.get("apply") else None, confirm=tt.prompt, timeout_s=120))

    def cancel(args: dict) -> ToolOutput:
        labels = OPERATIONS.cancel_all()
        if not labels:
            return ToolOutput("Şu an süren uzun bir işlem yok.", {"cancelled": []})
        return ToolOutput("Durdurdum: " + ", ".join(labels) + ".", {"cancelled": labels})

    reg.register(ToolSpec(
        name="cancel_long_operation", title="Uzun işlemi durdur", category="sistem",
        description="Süren disk taramasını, hız testini ya da temizliği durdurur. / Cancels a running long operation.",
        input_schema=js.obj(), permission=Permission.SAFE, handler=cancel))
