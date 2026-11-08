"""Dosya araçları. Hepsi `PathSandbox` içinde çalışır (bkz. security/action_validator.py).

- Arama: ad deseni/içeren, uzantı; derinlik, sonuç, taranan öğe ve süre sınırlı.
- Klasör ve belge oluşturma (txt, md; python-docx varsa docx). Üzerine yazma onay ister.
- Taşıma/yeniden adlandırma: hedef varsa üzerine yazmak onay ister.
- Klasör düzenleme: önce önizleme (değişiklik yok), uygulama onay ister; yalnızca üst düzey dosyalar.
- Silme: varsayılan Geri Dönüşüm Kutusu (send2trash), kalıcı silme güçlü uyarıyla onay ister.
- Okuma: txt/md/csv/json/log..., pdf (pypdf), docx (python-docx); boyut ve karakter sınırlı.
  Okunan içerik güvenilmeyen veri olarak işaretlenir.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import shutil
import stat
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from jarvis.security.action_validator import PathSandbox, ValidationError
from jarvis.security.permission_manager import ConfirmPrompt, Permission
from jarvis.tools import schema as js
from jarvis.tools.registry import NotSupportedError, ToolContext, ToolError, ToolOutput, ToolRegistry, ToolSpec
from jarvis.tools.text import fold, fold_chars

log = logging.getLogger(__name__)

MAX_READ_BYTES = 20 * 1024 * 1024
MAX_TEXT_FILE_BYTES = 5 * 1024 * 1024
MAX_PDF_PAGES = 60
MAX_DOC_CHARS = 200_000
SEARCH_MAX_SCANNED = 60_000
SEARCH_MAX_SECONDS = 8.0

TEXT_EXTS = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log", ".ini", ".cfg", ".toml", ".yaml", ".yml",
             ".xml", ".html", ".htm", ".py", ".js", ".ts", ".css", ".srt", ".bat", ".ps1", ".sh", ".sql", ".rtf"}

CATEGORIES: dict[str, set[str]] = {
    "Belgeler": {".pdf", ".doc", ".docx", ".odt", ".rtf", ".txt", ".md", ".xls", ".xlsx", ".ods", ".csv", ".ppt",
                 ".pptx", ".odp", ".epub"},
    "Resimler": {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".tif", ".tiff", ".svg", ".raw", ".psd"},
    "Videolar": {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".flv", ".m4v"},
    "Müzik": {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma", ".opus"},
    "Arşivler": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso"},
    "Kurulum dosyaları": {".exe", ".msi", ".msix", ".appx", ".appinstaller"},
    "Kod": {".py", ".js", ".ts", ".java", ".c", ".cpp", ".h", ".cs", ".go", ".rs", ".html", ".css", ".json", ".xml",
            ".yaml", ".yml", ".sql", ".ipynb"},
}
OTHER_CATEGORY = "Diğer"

# Sözlü uzantı adları
EXTENSION_WORDS = {"pdf": ".pdf", "word": ".docx", "docx": ".docx", "doc": ".doc", "excel": ".xlsx", "xlsx": ".xlsx",
                   "powerpoint": ".pptx", "pptx": ".pptx", "metin": ".txt", "txt": ".txt", "markdown": ".md",
                   "md": ".md", "mp3": ".mp3", "mp4": ".mp4", "zip": ".zip", "rar": ".rar", "png": ".png",
                   "jpg": ".jpg", "jpeg": ".jpg", "csv": ".csv", "json": ".json", "exe": ".exe"}


def category_of(path: Path) -> str:
    ext = path.suffix.lower()
    for cat, exts in CATEGORIES.items():
        if ext in exts:
            return cat
    return OTHER_CATEGORY


def _hidden(entry: os.DirEntry) -> bool:
    if entry.name.startswith((".", "$", "~$")):
        return True
    try:
        attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
        return bool(attrs & (getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 2) | getattr(stat, "FILE_ATTRIBUTE_SYSTEM", 4)))
    except OSError:
        return True


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    for i in range(2, 1000):
        cand = p.with_name(f"{p.stem} ({i}){p.suffix}")
        if not cand.exists():
            return cand
    raise ToolError("ad yok", f"{p.name} için boş bir ad bulunamadı.")


def search(roots: list[Path], pattern: str, extension: str, max_results: int, max_depth: int,
           max_scanned: int = SEARCH_MAX_SCANNED, max_seconds: float = SEARCH_MAX_SECONDS) -> tuple[list[dict], bool]:
    """BFS arama; sembolik bağlar izlenmez, gizli/sistem öğeleri atlanır. (sonuçlar, sınıra takıldı mı)"""
    glob = any(c in pattern for c in "*?[")
    key = fold_chars if glob else fold
    pat = key(pattern.strip())
    ext = extension.lower().strip()
    if ext and not ext.startswith("."):
        ext = "." + ext
    results: list[dict] = []
    scanned = 0
    t0 = time.monotonic()
    queue: deque[tuple[Path, int]] = deque((r, 0) for r in roots)
    limited = False
    while queue:
        folder, depth = queue.popleft()
        try:
            it = os.scandir(folder)
        except OSError:
            continue
        with it:
            for entry in it:
                scanned += 1
                if scanned > max_scanned or time.monotonic() - t0 > max_seconds:
                    return results, True
                if _hidden(entry):
                    continue
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                    if entry.is_symlink():
                        continue
                except OSError:
                    continue
                if is_dir and depth < max_depth:
                    queue.append((Path(entry.path), depth + 1))
                name = entry.name
                if ext and (is_dir or not name.lower().endswith(ext)):
                    continue
                if pat:
                    ok = fnmatch.fnmatchcase(key(name), pat) if glob else pat in key(name)
                    if not ok:
                        continue
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                results.append({"path": entry.path, "name": name, "is_dir": is_dir,
                                "size": None if is_dir else st.st_size,
                                "modified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")})
                if len(results) >= max_results:
                    limited = True
                    return results, limited
    return results, limited


def read_text_file(p: Path, max_chars: int) -> str:
    if p.stat().st_size > MAX_TEXT_FILE_BYTES:
        with open(p, "rb") as f:
            raw = f.read(max_chars * 4)
    else:
        raw = p.read_bytes()
    if b"\x00" in raw[:4096]:
        raise ToolError("ikili", f"{p.name} bir metin dosyası değil.")
    for enc in ("utf-8-sig", "cp1254", "latin-1"):
        try:
            return raw.decode(enc)[:max_chars]
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")[:max_chars]


def read_pdf(p: Path, max_chars: int) -> tuple[str, dict]:
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise ToolError("pypdf yok", "PDF okumak için pypdf kurulu değil.") from e
    try:
        reader = PdfReader(str(p))
        if reader.is_encrypted:
            raise ToolError("şifreli", f"{p.name} parolalı, okuyamıyorum.")
        pages = len(reader.pages)
        parts, total = [], 0
        for i, page in enumerate(reader.pages[:MAX_PDF_PAGES]):
            t = page.extract_text() or ""
            parts.append(t)
            total += len(t)
            if total >= max_chars:
                break
    except ToolError:
        raise
    except Exception as e:  # noqa: BLE001  bozuk PDF
        raise ToolError(f"pdf: {e}", f"{p.name} okunamadı (bozuk ya da desteklenmeyen PDF).") from e
    text = "\n".join(parts)[:max_chars]
    return text, {"pages": pages, "pages_read": min(pages, i + 1 if pages else 0)}


def read_docx(p: Path, max_chars: int) -> str:
    try:
        import docx
    except ImportError as e:
        raise ToolError("python-docx yok", "Word belgesi okumak için python-docx kurulu değil.") from e
    try:
        d = docx.Document(str(p))
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"docx: {e}", f"{p.name} okunamadı.") from e
    out, total = [], 0
    for para in d.paragraphs:
        out.append(para.text)
        total += len(para.text) + 1
        if total >= max_chars:
            break
    return "\n".join(out)[:max_chars]


def write_document(p: Path, content: str, fmt: str) -> None:
    if fmt == "docx":
        try:
            import docx
        except ImportError as e:
            raise ToolError("python-docx yok", "Word belgesi oluşturmak için python-docx kurulu değil.") from e
        d = docx.Document()
        for line in content.split("\n"):
            d.add_paragraph(line)
        tmp = p.with_name(f".{p.name}.jarvis-tmp")
        d.save(str(tmp))
        os.replace(tmp, p)
        return
    tmp = p.with_name(f".{p.name}.jarvis-tmp")
    tmp.write_text(content, encoding="utf-8", newline="\n" if fmt == "md" else None)
    os.replace(tmp, p)


def _default_trash(path: str) -> None:
    try:
        from send2trash import send2trash
    except ImportError as e:
        raise ToolError("send2trash yok", "Geri Dönüşüm Kutusu için send2trash kurulu değil; silmedim.") from e
    send2trash(path)


class FileTools:
    def __init__(self, ctx: ToolContext, trash: Callable[[str], None] = _default_trash) -> None:
        self.ctx = ctx
        self.trash = trash

    @property
    def sb(self) -> PathSandbox:
        return self.ctx.sandbox

    def _rel(self, p: Path) -> str:
        r = self.sb.root_of(p)
        if r is None:
            return str(p)
        rel = p.relative_to(r.path)
        return r.label + ("" if str(rel) == "." else os.sep + str(rel))

    # ----- izinli klasörler -----
    def roots(self, args: dict) -> ToolOutput:
        d = self.sb.describe()
        return ToolOutput("Erişebildiğim klasörler: " + ", ".join(r["ad"] for r in d) + ".", d)

    # ----- arama -----
    def prepare_search(self, args: dict) -> dict:
        if args["folder"].strip():
            root = self.sb.resolve(args["folder"], must_exist=True)
            if not root.is_dir():
                raise ToolError("klasör değil", f"{root.name} bir klasör değil.")
            roots = [root]
        else:
            roots = [r.path for r in self.sb.roots]
        if not roots:
            raise ToolError("kök yok", "Erişebildiğim bir klasör yok.")
        if not args["pattern"].strip() and not args["extension"].strip():
            raise ToolError("boş arama", "Aranacak bir ad ya da dosya türü söylemelisin.")
        return {**args, "_roots": [str(r) for r in roots]}

    def search(self, args: dict) -> ToolOutput:
        res, limited = search([Path(r) for r in args["_roots"]], args["pattern"], args["extension"],
                              args["max_results"], args["max_depth"])
        for r in res:
            r["display"] = self._rel(Path(r["path"]))
        what = args["pattern"] or args["extension"]
        if not res:
            return ToolOutput(f"'{what}' için bir şey bulamadım.", [])
        more = " (sınıra ulaştım, daha fazlası olabilir)" if limited else ""
        names = ", ".join(r["name"] for r in res[:3])
        return ToolOutput(f"{len(res)} sonuç buldum{more}: {names}" + ("…" if len(res) > 3 else "."), res)

    # ----- listeleme -----
    def list_dir(self, args: dict) -> ToolOutput:
        p = self.sb.resolve(args["path"], must_exist=True)
        if not p.is_dir():
            raise ToolError("klasör değil", f"{p.name} bir klasör değil.")
        items = []
        with os.scandir(p) as it:
            for e in it:
                if _hidden(e):
                    continue
                try:
                    st = e.stat(follow_symlinks=False)
                    is_dir = e.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                items.append({"name": e.name, "is_dir": is_dir, "size": None if is_dir else st.st_size,
                              "modified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")})
        items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        n_dir = sum(1 for i in items if i["is_dir"])
        return ToolOutput(f"{self._rel(p)}: {n_dir} klasör, {len(items) - n_dir} dosya.", items[: args["limit"]])

    # ----- oluşturma -----
    def prepare_new(self, args: dict) -> dict:
        p = self.sb.resolve(args["path"], for_write=True)
        if self.sb.is_root(p):
            raise ToolError("kök", "Bu zaten izinli bir ana klasör.")
        parent = p.parent
        if not parent.is_dir():
            raise ToolError("üst klasör yok", f"Üst klasör yok: {self._rel(parent)}")
        return {**args, "_path": str(p)}

    def create_folder(self, args: dict) -> ToolOutput:
        p = Path(args["_path"])
        if p.exists():
            if p.is_dir():
                return ToolOutput(f"{self._rel(p)} klasörü zaten var.", {"path": str(p), "created": False})
            raise ToolError("dosya var", f"{p.name} adında bir dosya var, klasör oluşturamadım.")
        p.mkdir()
        return ToolOutput(f"{self._rel(p)} klasörünü oluşturdum.", {"path": str(p), "created": True})

    def prepare_doc(self, args: dict) -> dict:
        a = self.prepare_new(args)
        p = Path(a["_path"])
        fmt = args["format"]
        if p.suffix.lower() != "." + fmt:
            p = p.with_name(p.name + "." + fmt)
            a["_path"] = str(self.sb.resolve(str(p), for_write=True))
        if len(args["content"]) > MAX_DOC_CHARS:
            raise ToolError("çok uzun", "Belge içeriği çok uzun.")
        a["_exists"] = Path(a["_path"]).exists()
        if a["_exists"] and Path(a["_path"]).is_dir():
            raise ToolError("klasör var", "Bu adda bir klasör var.")
        if a["_exists"] and not args["overwrite"]:
            raise ToolError("var", f"{Path(a['_path']).name} zaten var. Üzerine yazmamı istersen söyle.")
        return a

    def create_document(self, args: dict) -> ToolOutput:
        p = Path(args["_path"])
        write_document(p, args["content"], args["format"])
        verb = "üzerine yazdım" if args["_exists"] else "oluşturdum"
        return ToolOutput(f"{self._rel(p)} belgesini {verb}.", {"path": str(p), "chars": len(args["content"])})

    # ----- taşıma -----
    def prepare_move(self, args: dict) -> dict:
        src = self.sb.resolve(args["source"], must_exist=True)
        if self.sb.is_root(src):
            raise ToolError("kök", "Ana klasörleri taşımam.")
        dst = self.sb.resolve(args["destination"], for_write=True)
        if dst.is_dir():
            dst = dst / src.name
            dst = self.sb.resolve(str(dst), for_write=True)
        if not dst.parent.is_dir():
            raise ToolError("hedef yok", f"Hedef klasör yok: {self._rel(dst.parent)}")
        if src == dst:
            raise ToolError("aynı", "Kaynak ve hedef aynı.")
        if src.is_dir() and str(dst).startswith(str(src) + os.sep):
            raise ToolError("iç içe", "Bir klasör kendi içine taşınamaz.")
        exists = dst.exists()
        if exists and not args["overwrite"]:
            raise ToolError("var", f"Hedefte {dst.name} zaten var. Üzerine yazmamı istersen söyle.")
        if exists and (dst.is_dir() or src.is_dir()):
            raise ToolError("klasör var", "Klasörlerin üzerine yazmıyorum; önce hedefteki öğeyi taşı ya da adlandır.")
        return {**args, "_src": str(src), "_dst": str(dst), "_overwrite": exists}

    def move(self, args: dict) -> ToolOutput:
        src, dst = Path(args["_src"]), Path(args["_dst"])
        if args["_overwrite"]:
            os.replace(src, dst)  # hedef bir dosya (klasörün üzerine yazma hazırlıkta reddedildi)
        else:
            if dst.exists():
                raise ToolError("var", "Hedef bu arada oluştu, üzerine yazmadım.")
            shutil.move(str(src), str(dst))
        same_dir = src.parent == dst.parent
        verb = "yeniden adlandırdım" if same_dir else "taşıdım"
        return ToolOutput(f"{src.name} → {self._rel(dst)} ({verb}).", {"from": str(src), "to": str(dst)})

    # ----- düzenleme -----
    def plan_organize(self, folder: Path) -> list[dict]:
        plan = []
        taken: set[str] = set()
        with os.scandir(folder) as it:
            entries = sorted(it, key=lambda e: e.name.lower())
        for e in entries:
            if _hidden(e) or e.is_symlink():
                continue
            try:
                if not e.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if e.name.endswith((".crdownload", ".part", ".tmp", ".partial")):
                continue  # inen dosyalara dokunma
            cat = category_of(Path(e.name))
            target = folder / cat / e.name
            if target.exists() or str(target).lower() in taken:
                target = unique_path(target)
                n = 2
                while str(target).lower() in taken:
                    target = target.with_name(f"{Path(e.name).stem} ({n}){Path(e.name).suffix}")
                    n += 1
            taken.add(str(target).lower())
            plan.append({"file": e.name, "category": cat, "target": str(target)})
        return plan

    def prepare_organize(self, args: dict) -> dict:
        folder = self.sb.resolve(args["folder"], must_exist=True)
        if not folder.is_dir():
            raise ToolError("klasör değil", f"{folder.name} bir klasör değil.")
        plan = self.plan_organize(folder)
        return {**args, "_folder": str(folder), "_plan": plan}

    def organize(self, args: dict) -> ToolOutput:
        plan, folder = args["_plan"], Path(args["_folder"])
        counts: dict[str, int] = {}
        for item in plan:
            counts[item["category"]] = counts.get(item["category"], 0) + 1
        summary = ", ".join(f"{c}: {n}" for c, n in sorted(counts.items(), key=lambda x: -x[1]))
        if not plan:
            return ToolOutput(f"{self._rel(folder)} içinde düzenlenecek dosya yok.", {"moved": 0})
        if not args["apply"]:
            return ToolOutput(f"Önizleme: {len(plan)} dosya kategorilere ayrılacak ({summary}). Hiçbir şey taşımadım; "
                              "uygulamamı istersen onayınla yaparım.", {"preview": plan})
        moved, failed = 0, []
        for item in plan:
            src, dst = folder / item["file"], Path(item["target"])
            try:
                # Onaydan sonra klasör değişmiş olabilir: yeniden denetle
                self.sb.resolve(str(dst), for_write=True)
                dst.parent.mkdir(exist_ok=True)
                if dst.exists():
                    dst = unique_path(dst)
                shutil.move(str(src), str(dst))
                moved += 1
            except (OSError, ValidationError, ToolError) as e:
                failed.append({"file": item["file"], "error": str(getattr(e, "user_message", e))[:120]})
        msg = f"{moved} dosyayı kategorilere taşıdım ({summary})."
        if failed:
            msg += f" {len(failed)} dosya taşınamadı."
        return ToolOutput(msg, {"moved": moved, "failed": failed})

    def organize_prompt(self, args: dict) -> ConfirmPrompt:
        plan = args["_plan"]
        sample = "\n".join(f"• {i['file']} → {i['category']}" for i in plan[:8])
        more = f"\n… ve {len(plan) - 8} dosya daha" if len(plan) > 8 else ""
        return ConfirmPrompt("organize_folder", "Klasörü düzenle",
                             f"{self._rel(Path(args['_folder']))} içindeki {len(plan)} dosya türlerine göre alt "
                             f"klasörlere taşınacak:\n{sample}{more}\n\nOnaylıyor musun?", confirm_label="Düzenle")

    # ----- silme -----
    def prepare_delete(self, args: dict) -> dict:
        p = self.sb.resolve(args["path"], must_exist=True)
        if self.sb.is_root(p):
            raise ToolError("kök", "Ana klasörleri silmem.")
        info = {"is_dir": p.is_dir(), "count": 1}
        if p.is_dir():
            n = 0
            for _root, dirs, files in os.walk(p):
                n += len(files) + len(dirs)
                if n > 100_000:
                    break
            info["count"] = n
        return {**args, "_path": str(p), "_info": info}

    def delete(self, args: dict) -> ToolOutput:
        p = Path(args["_path"])
        if args["permanent"]:
            if p.is_symlink() or p.is_file():
                p.unlink()
            else:
                shutil.rmtree(p)
            return ToolOutput(f"{p.name} kalıcı olarak silindi.", {"path": str(p), "permanent": True})
        self.trash(str(p))
        return ToolOutput(f"{p.name} Geri Dönüşüm Kutusu'na taşındı.", {"path": str(p), "permanent": False})

    def delete_prompt(self, args: dict) -> ConfirmPrompt:
        p, info = Path(args["_path"]), args["_info"]
        what = f"{p.name} klasörü ({info['count']} öğe)" if info["is_dir"] else f"{p.name} dosyası"
        if args["permanent"]:
            return ConfirmPrompt("delete_path", "KALICI SİLME",
                                 f"{what} KALICI OLARAK silinecek. Geri Dönüşüm Kutusu'na gitmez ve geri "
                                 f"getirilemez.\n\n{p}\n\nEmin misin?", danger=True, confirm_label="Kalıcı olarak sil")
        return ConfirmPrompt("delete_path", "Geri Dönüşüm Kutusu'na taşı",
                             f"{what} Geri Dönüşüm Kutusu'na taşınacak.\n\n{p}", confirm_label="Sil")

    # ----- okuma -----
    def read(self, args: dict) -> ToolOutput:
        p = self.sb.resolve(args["path"], must_exist=True)
        if not p.is_file():
            raise ToolError("dosya değil", f"{p.name} bir dosya değil.")
        size = p.stat().st_size
        if size > MAX_READ_BYTES:
            raise ToolError("büyük", f"{p.name} çok büyük ({_human(size)}); en çok {_human(MAX_READ_BYTES)} okurum.")
        ext, limit = p.suffix.lower(), args["max_chars"]
        meta: dict = {"path": str(p), "size": size}
        if ext == ".pdf":
            text, extra = read_pdf(p, limit)
            meta.update(extra)
        elif ext == ".docx":
            text = read_docx(p, limit)
        elif ext in TEXT_EXTS:
            text = read_text_file(p, limit)
        else:
            raise ToolError("tür", f"{ext or 'uzantısız'} dosyaları okuyamıyorum (txt, md, pdf, docx desteklenir).")
        meta["truncated"] = len(text) >= limit
        meta["text"] = text
        note = " (sınıra kadar okudum)" if meta["truncated"] else ""
        return ToolOutput(f"{p.name} okundu: {len(text)} karakter{note}.", meta, untrusted=True)

    # ----- açma -----
    def open_path(self, args: dict) -> ToolOutput:
        p = self.sb.resolve(args["path"], must_exist=True)
        if p.is_file() and p.suffix.lower() in {".exe", ".msi", ".bat", ".cmd", ".ps1", ".vbs", ".js", ".lnk", ".scr",
                                                ".com", ".msix", ".appx", ".reg", ".hta", ".jar"}:
            raise ToolError("çalıştırılabilir", "Çalıştırılabilir dosyaları buradan açmam; uygulama açmak için adını söyle.")
        if self.ctx.opener is None:
            raise NotSupportedError()
        self.ctx.opener(str(p))
        return ToolOutput(f"{self._rel(p)} açılıyor.", {"path": str(p)})


def register(reg: ToolRegistry, ctx: ToolContext, trash: Callable[[str], None] = _default_trash) -> FileTools:
    t = FileTools(ctx, trash)
    path_field = js.string("Yol: tam yol ya da 'Masaüstü/...', 'Belgeler/...', 'İndirilenler/...' gibi / path", 1000, 1)

    reg.register(ToolSpec(
        name="list_allowed_folders", title="İzinli klasörler", category="dosya",
        description="Dosya araçlarının erişebildiği klasörler. / Folders file tools may access.",
        input_schema=js.obj(), permission=Permission.READ, handler=t.roots))

    reg.register(ToolSpec(
        name="search_files", title="Dosya ara", category="dosya",
        description=("İzinli klasörlerde ada (içeren ya da *.pdf gibi desen) ve uzantıya göre dosya arar. folder boşsa "
                     "hepsinde arar. / Searches files by name (substring or glob) and extension in allowed folders."),
        input_schema=js.obj(folder=js.string("Klasör, boş = tüm izinli klasörler / folder or empty", 1000),
                            pattern=js.string("Ad parçası ya da desen, boş olabilir / name part or glob", 200),
                            extension=js.string("Uzantı, örn. pdf; boş = hepsi / extension or empty", 20),
                            max_results=js.integer("En çok sonuç / max results", 1, 200),
                            max_depth=js.integer("Alt klasör derinliği / max depth", 0, 8)),
        permission=Permission.READ, handler=t.search, prepare=t.prepare_search, timeout_s=20))

    reg.register(ToolSpec(
        name="list_folder", title="Klasör içeriği", category="dosya",
        description="Bir klasördeki dosya ve klasörleri listeler. / Lists a folder's contents.",
        input_schema=js.obj(path=path_field, limit=js.integer("En çok öğe / max items", 1, 500)),
        permission=Permission.READ, handler=t.list_dir))

    reg.register(ToolSpec(
        name="create_folder", title="Klasör oluştur", category="dosya",
        description="İzinli bir klasörün içinde yeni klasör oluşturur. / Creates a folder inside an allowed folder.",
        input_schema=js.obj(path=path_field), permission=Permission.SAFE, handler=t.create_folder,
        prepare=t.prepare_new))

    reg.register(ToolSpec(
        name="create_document", title="Belge oluştur", category="dosya",
        description=("Metin (txt), Markdown (md) ya da Word (docx) belgesi oluşturur. Var olan dosyanın üzerine yazmak "
                     "overwrite=true ve onay ister. / Creates a txt/md/docx document; overwriting needs confirmation."),
        input_schema=js.obj(path=path_field, content=js.string("Belge içeriği / content", MAX_DOC_CHARS),
                            format=js.string("Biçim / format", enum=["txt", "md", "docx"]),
                            overwrite=js.boolean("Varsa üzerine yaz / overwrite if exists")),
        permission=Permission.SAFE, handler=t.create_document, prepare=t.prepare_doc,
        escalate=lambda a: Permission.CONFIRM if a.get("_exists") else None,
        confirm=lambda a: ConfirmPrompt("create_document", "Üzerine yaz",
                                        f"{Path(a['_path']).name} zaten var ve üzerine yazılacak. Eski içerik kaybolur.",
                                        danger=True, confirm_label="Üzerine yaz")))

    reg.register(ToolSpec(
        name="move_path", title="Taşı / yeniden adlandır", category="dosya",
        description=("Dosya ya da klasörü taşır veya yeniden adlandırır (hedef klasörse içine taşır). Üzerine yazma onay "
                     "ister. / Moves or renames a file/folder; overwriting needs confirmation."),
        input_schema=js.obj(source=path_field, destination=path_field,
                            overwrite=js.boolean("Hedef varsa üzerine yaz / overwrite")),
        permission=Permission.SAFE, handler=t.move, prepare=t.prepare_move,
        escalate=lambda a: Permission.CONFIRM if a.get("_overwrite") else None,
        confirm=lambda a: ConfirmPrompt("move_path", "Üzerine yaz",
                                        f"{Path(a['_dst']).name} hedefte zaten var; {Path(a['_src']).name} ile "
                                        "değiştirilecek. Eski dosya kaybolur.", danger=True,
                                        confirm_label="Üzerine yaz")))

    reg.register(ToolSpec(
        name="organize_folder", title="Klasörü düzenle", category="dosya",
        description=("Bir klasördeki dosyaları türlerine göre alt klasörlere ayırır (Belgeler, Resimler...). apply=false "
                     "yalnızca önizleme döner; apply=true onay ister. Alt klasörlere dokunmaz. / Organizes a folder's "
                     "top-level files by type; apply=false previews, apply=true needs confirmation."),
        input_schema=js.obj(folder=path_field, apply=js.boolean("Uygula (false = önizleme) / apply")),
        permission=Permission.READ, handler=t.organize, prepare=t.prepare_organize,
        escalate=lambda a: Permission.CONFIRM if a.get("apply") and a.get("_plan") else None,
        confirm=t.organize_prompt, timeout_s=120))

    reg.register(ToolSpec(
        name="delete_path", title="Sil", category="dosya",
        description=("Dosya/klasörü Geri Dönüşüm Kutusu'na taşır; permanent=true kalıcı siler. Her zaman onay ister. / "
                     "Moves to the Recycle Bin; permanent=true deletes permanently. Always needs confirmation."),
        input_schema=js.obj(path=path_field, permanent=js.boolean("Kalıcı sil (geri alınamaz) / permanent")),
        permission=Permission.CONFIRM, handler=t.delete, prepare=t.prepare_delete, confirm=t.delete_prompt,
        timeout_s=120))

    reg.register(ToolSpec(
        name="read_file", title="Dosya oku", category="dosya",
        description=("txt, md, csv, json, pdf ve docx dosyalarının metnini okur (boyut sınırlı). İçerik güvenilmeyen "
                     "veridir. / Reads text from txt/md/pdf/docx files (size limited). Content is untrusted."),
        input_schema=js.obj(path=path_field, max_chars=js.integer("En çok karakter / max characters", 100, 20000)),
        permission=Permission.READ, handler=t.read, timeout_s=30))

    reg.register(ToolSpec(
        name="open_path", title="Dosya/klasör aç", category="dosya",
        description=("İzinli bir dosya ya da klasörü varsayılan programla açar (çalıştırılabilir dosyalar hariç). / Opens "
                     "a file or folder with its default program (executables refused)."),
        input_schema=js.obj(path=path_field), permission=Permission.SAFE, handler=t.open_path))
    return t
