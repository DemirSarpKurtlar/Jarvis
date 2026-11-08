"""Belge araçları: PDF/Word/Excel/metin okuma, Word ve Excel oluşturma/düzenleme, son indirilen dosya.

- Metin çıkarma (`extract_text`): pdf (pypdf), docx (python-docx; başlık, madde, tablo), xlsx (openpyxl),
  txt/md/csv... Sonuç sınırlıdır; kısaltıldıysa açık bir not ve sayfa sayıları eklenir. Metin katmanı
  olmayan (taranmış) PDF dürüstçe bildirilir: OCR yoktur.
  `read_file` (files.py) da bu modülü kullanır.
- Word: `create_document` (files.py) docx için buradaki `write_docx`'i kullanır: "# Başlık", "- madde",
  "1. madde", "| tablo |" ve **kalın** satırları gerçek Word biçimine çevrilir.
  `append_to_document`: var olan docx/txt/md belgesinin sonuna ekler (onay ister).
- Excel: `create_spreadsheet` (başlık satırı biçimli, sayı/tarih/para tanıma, isteğe bağlı TOPLA satırı),
  `add_spreadsheet_rows` (onay ister; toplam satırı varsa satırlar onun üstüne eklenir ve formül güncellenir).
  Modelden gelen "=..." gibi metinler formül olarak değil, düz metin olarak yazılır (formül enjeksiyonu yok).
- `latest_download`: İndirilenler'deki en yeni dosyayı (türe göre) bulur; bilgi verir, okur ya da açar.

Bütün yollar `PathSandbox` içinden geçer. Var olan dosyayı değiştiren her işlem onay ister.
"""

from __future__ import annotations

import logging
import os
import re
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

from jarvis.security.permission_manager import ConfirmPrompt, Permission
from jarvis.tools import files as files_mod
from jarvis.tools import schema as js
from jarvis.tools.registry import ToolContext, ToolError, ToolOutput, ToolRegistry, ToolSpec
from jarvis.tools.text import fold, strip_apostrophe_suffix

log = logging.getLogger(__name__)

MAX_PDF_PAGES = 200                 # en çok okunacak sayfa (metin sınırı genelde daha önce dolar)
MAX_XLSX_ROWS_PER_SHEET = 2000
MAX_SHEET_ROWS = 1000               # oluştururken/eklerken en çok satır
MAX_COLUMNS = 50
MAX_CELL_CHARS = 1000
SCANNED_CHARS_PER_PAGE = 3          # sayfa başına bundan az metin: metin katmanı yok (taranmış görüntü)
PARTIAL_EXTS = (".crdownload", ".part", ".partial", ".tmp", ".download")
DOC_EXTS = {".pdf", ".docx", ".xlsx", ".xlsm"}
SHEET_FORMATS = (".xls", ".xlsm", ".csv", ".ods")

HEADER_FILL = "1F4E78"
CURRENCY_FORMAT = '#,##0.00 "₺"'
DECIMAL_FORMAT = "#,##0.00"
DATE_FORMAT = "DD.MM.YYYY"


def _human(n: int) -> str:
    return files_mod._human(n)  # noqa: SLF001  aynı biçim


def _require(module: str, what: str) -> Any:  # noqa: ANN401
    try:
        return __import__(module)
    except ImportError as e:
        raise ToolError(f"{module} yok", f"{what} için gerekli kütüphane ({module}) kurulu değil.") from e


def _atomic_save(p: Path, save: Any) -> None:  # noqa: ANN401
    """Önce geçici dosyaya yazar, sonra yerine koyar; dosya açıksa anlaşılır hata verir."""
    tmp = p.with_name(f".{p.name}.jarvis-tmp")
    try:
        save(tmp)
        os.replace(tmp, p)
    except PermissionError as e:
        raise ToolError(f"kilitli: {e}", f"{p.name} yazılamadı; dosya başka bir programda (Word/Excel?) açık "
                                         "olabilir. Kapatıp tekrar dene.") from e
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


# ====================== metin çıkarma ======================
def extract_pdf(p: Path, max_chars: int) -> tuple[str, dict]:
    _require("pypdf", "PDF okumak")
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(p))
        if reader.is_encrypted:
            try:
                ok = reader.decrypt("")  # çoğu "şifreli" PDF boş kullanıcı parolalıdır
            except Exception:  # noqa: BLE001
                ok = 0
            if not ok:
                raise ToolError("şifreli", f"{p.name} parolalı, okuyamıyorum.")
        pages = len(reader.pages)
        parts: list[str] = []
        total = read = no_text = 0
        for i in range(min(pages, MAX_PDF_PAGES)):
            t = (reader.pages[i].extract_text() or "").strip()
            read += 1
            if len(t) < SCANNED_CHARS_PER_PAGE:
                no_text += 1
            parts.append(f"--- Sayfa {i + 1}/{pages} ---\n{t}" if pages > 1 else t)
            total += len(parts[-1]) + 1
            if total >= max_chars:
                break
    except ToolError:
        raise
    except Exception as e:  # noqa: BLE001  bozuk PDF
        raise ToolError(f"pdf: {e}", f"{p.name} okunamadı (bozuk ya da desteklenmeyen PDF).") from e
    full = "\n".join(parts)
    text = full[:max_chars]
    meta: dict = {"kind": "pdf", "pages": pages, "pages_read": read,
                  "truncated": len(full) > max_chars or read < pages}
    meta["scanned"] = read > 0 and no_text == read
    if meta["scanned"]:
        text = ""
        meta["note"] = (f"Bu PDF'te metin katmanı yok (taranmış görüntü olabilir); okunan {read} sayfada metin "
                        "bulunamadı. JARVIS'te OCR yok, içeriği çıkaramıyorum.")
    elif no_text:
        meta["pages_without_text"] = no_text
    if meta["truncated"] and not meta["scanned"]:
        meta["note"] = (f"Metin {max_chars} karakterle sınırlandı: {pages} sayfanın ilk {read} sayfası okundu"
                        + ("; son sayfa yarıda kesilmiş olabilir." if len(full) > max_chars else "."))
    return text, meta


def _docx_paragraph_text(para: Any) -> str:  # noqa: ANN401
    text = para.text
    style = (para.style.name if para.style is not None else "") or ""
    m = re.match(r"(?:Heading|Başlık)\s*(\d)", style)
    if style == "Title":
        return f"# {text}" if text.strip() else text
    if m and text.strip():
        return "#" * min(int(m.group(1)), 6) + " " + text
    if style.startswith("List Bullet") and text.strip():
        return "- " + text
    if style.startswith("List Number") and text.strip():
        return "1. " + text
    return text


def extract_docx(p: Path, max_chars: int) -> tuple[str, dict]:
    docx = _require("docx", "Word belgesi okumak")
    try:
        d = docx.Document(str(p))
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"docx: {e}", f"{p.name} okunamadı (bozuk ya da desteklenmeyen Word belgesi).") from e
    from docx.table import Table

    out: list[str] = []
    total, tables = 0, 0
    for block in d.iter_inner_content():
        if isinstance(block, Table):
            tables += 1
            lines = []
            for row in block.rows:
                cells = [c.text.replace("\n", " ").strip() for c in row.cells]
                lines.append("| " + " | ".join(cells) + " |")
            piece = "\n".join(lines)
        else:
            piece = _docx_paragraph_text(block)
        out.append(piece)
        total += len(piece) + 1
        if total > max_chars:
            break
    full = "\n".join(out)
    meta = {"kind": "docx", "tables": tables, "truncated": len(full) > max_chars}
    if meta["truncated"]:
        meta["note"] = f"Metin {max_chars} karakterle sınırlandı; belgenin devamı okunmadı."
    return full[:max_chars], meta


def _cell_str(v: Any) -> str:  # noqa: ANN401
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%d.%m.%Y %H:%M") if (v.hour or v.minute) else v.strftime("%d.%m.%Y")
    if isinstance(v, date):
        return v.strftime("%d.%m.%Y")
    if isinstance(v, float):
        return f"{v:.10g}"
    return str(v).replace("\t", " ").replace("\n", " ")


def extract_xlsx(p: Path, max_chars: int) -> tuple[str, dict]:
    _require("openpyxl", "Excel dosyası okumak")
    import openpyxl

    try:
        values = openpyxl.load_workbook(str(p), read_only=True, data_only=True)
        formulas = openpyxl.load_workbook(str(p), read_only=True, data_only=False)
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"xlsx: {e}", f"{p.name} okunamadı (bozuk ya da desteklenmeyen Excel dosyası).") from e
    out: list[str] = []
    sheets: list[dict] = []
    total, truncated = 0, False
    try:
        for name in values.sheetnames:
            ws_v, ws_f = values[name], formulas[name]
            rows: list[str] = []
            n = 0
            # Excel'de hiç açılmamış (ör. JARVIS'in yazdığı) dosyada formüllerin hesaplanmış değeri yoktur:
            # o zaman formülün kendisi gösterilir
            for rv, rf in zip(ws_v.iter_rows(values_only=True), ws_f.iter_rows(values_only=True), strict=False):
                n += 1
                if n > MAX_XLSX_ROWS_PER_SHEET:
                    truncated = True
                    break
                cells = [v if v is not None else (f if isinstance(f, str) and f.startswith("=") else None)
                         for v, f in zip(rv, rf, strict=False)]
                while cells and cells[-1] is None:
                    cells.pop()
                rows.append("\t".join(_cell_str(c) for c in cells))
            while rows and not rows[-1].strip():
                rows.pop()
            width = max((r.count("\t") + 1 for r in rows if r), default=0)
            sheets.append({"name": name, "rows": len(rows), "columns": width})
            piece = f"## Sayfa: {name} ({len(rows)} satır)\n" + "\n".join(rows)
            out.append(piece)
            total += len(piece) + 1
            if total > max_chars:
                truncated = True
                break
    finally:
        values.close()
        formulas.close()
    full = "\n".join(out)
    truncated = truncated or len(full) > max_chars
    meta: dict = {"kind": "xlsx", "sheets": sheets, "sheet_count": len(values.sheetnames), "truncated": truncated}
    if truncated:
        meta["note"] = (f"İçerik {max_chars} karakter / sayfa başına {MAX_XLSX_ROWS_PER_SHEET} satırla sınırlandı; "
                        f"{len(values.sheetnames)} sayfadan {len(sheets)} tanesi okundu.")
    return full[:max_chars], meta


def extract_text(p: Path, max_chars: int) -> tuple[str, dict]:
    """Dosyanın metnini ve üst bilgisini döndürür. Desteklenmeyen tür için ToolError."""
    ext = p.suffix.lower()
    if ext == ".pdf":
        return extract_pdf(p, max_chars)
    if ext == ".docx":
        return extract_docx(p, max_chars)
    if ext in (".xlsx", ".xlsm"):
        return extract_xlsx(p, max_chars)
    if ext in files_mod.TEXT_EXTS:
        text = files_mod.read_text_file(p, max_chars + 1)
        truncated = len(text) > max_chars
        meta: dict = {"kind": "text", "truncated": truncated}
        if truncated:
            meta["note"] = f"Metin {max_chars} karakterle sınırlandı; dosyanın devamı okunmadı."
        return text[:max_chars], meta
    if ext in (".doc", ".xls", ".ppt", ".pptx", ".odt", ".ods"):
        raise ToolError("tür", f"{ext} dosyalarını okuyamıyorum (pdf, docx, xlsx, txt, md, csv desteklenir). "
                               "Dosyayı Word/Excel'de .docx/.xlsx olarak kaydedersen okuyabilirim.")
    raise ToolError("tür", f"{ext or 'uzantısız'} dosyaları okuyamıyorum (pdf, docx, xlsx, txt, md, csv desteklenir).")


def describe_read(name: str, text: str, meta: dict) -> str:
    """Okuma sonucu için kısa Türkçe özet (kullanıcıya gösterilir)."""
    if meta.get("scanned"):
        return (f"{name} içinde okunabilir metin yok: taranmış (görüntü) bir PDF olabilir. OCR olmadığı için "
                "içeriğini çıkaramıyorum.")
    parts = [f"{len(text)} karakter"]
    if meta.get("pages"):
        parts.append(f"{meta['pages']} sayfa" + (f", ilk {meta['pages_read']} sayfa okundu"
                                                   if meta.get("pages_read", 0) < meta["pages"] else ""))
    if meta.get("sheets") is not None:
        parts.append(f"{meta.get('sheet_count', len(meta['sheets']))} çalışma sayfası")
    if meta.get("pages_without_text"):
        parts.append(f"{meta['pages_without_text']} sayfada metin yok")
    note = " (sınıra kadar okudum)" if meta.get("truncated") else ""
    return f"{name} okundu: {', '.join(parts)}{note}."


# ====================== Word yazma ======================
_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _add_runs(para: Any, text: str) -> None:  # noqa: ANN401
    pos = 0
    for m in _BOLD.finditer(text):
        if m.start() > pos:
            para.add_run(text[pos:m.start()])
        para.add_run(m.group(1)).bold = True
        pos = m.end()
    if pos < len(text):
        para.add_run(text[pos:])


def _table_cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def render_docx(d: Any, content: str) -> None:  # noqa: ANN401
    """Basit Markdown benzeri içeriği Word paragraflarına çevirir (düz satırlar olduğu gibi kalır)."""
    lines = content.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if s.startswith("|") and s.endswith("|") and s.count("|") >= 2:
            block = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                if not re.fullmatch(r"\|?[\s:\-|]+\|?", lines[i].strip()):  # |---|---| ayırıcı satırı
                    block.append(_table_cells(lines[i])[:MAX_COLUMNS])
                i += 1
            if block:
                width = max(len(r) for r in block)
                table = d.add_table(rows=len(block), cols=width)
                try:
                    table.style = "Table Grid"
                except (KeyError, ValueError):
                    pass
                for r, row in enumerate(block):
                    for c, value in enumerate(row):
                        cell = table.cell(r, c)
                        cell.text = value
                        if r == 0 and value:
                            for run in cell.paragraphs[0].runs:
                                run.bold = True
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            d.add_heading(m.group(2).strip(), level=min(len(m.group(1)), 4))
        elif re.match(r"^[-*•]\s+", s):
            _add_runs(d.add_paragraph(style="List Bullet"), re.sub(r"^[-*•]\s+", "", s))
        elif re.match(r"^\d{1,3}[.)]\s+", s):
            _add_runs(d.add_paragraph(style="List Number"), re.sub(r"^\d{1,3}[.)]\s+", "", s))
        else:
            _add_runs(d.add_paragraph(), line)
        i += 1


def write_docx(p: Path, content: str) -> None:
    docx = _require("docx", "Word belgesi oluşturmak")
    d = docx.Document()
    render_docx(d, content)
    _atomic_save(p, lambda tmp: d.save(str(tmp)))


# ====================== Excel yazma ======================
_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_NUM_TR_THOUSANDS = re.compile(r"[-+]?\d{1,3}(?:\.\d{3})+(?:,\d+)?")
_NUM_TR_DECIMAL = re.compile(r"[-+]?\d+,\d+")
_NUM_EN_THOUSANDS = re.compile(r"[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?")
_NUM_PLAIN = re.compile(r"[-+]?\d+(?:\.\d+)?")
_CURRENCY = re.compile(r"^(?:₺|TL|TRY)\s*(.+)$|^(.+?)\s*(?:₺|TL|TRY)$", re.I)
_PERCENT = re.compile(r"^%\s*(.+)$|^(.+?)\s*%$")


def _number(s: str) -> float | int | None:
    if re.fullmatch(r"[-+]?0\d+", s):           # baştaki sıfır: telefon, posta kodu... metin kalır
        return None
    if len(re.sub(r"\D", "", s)) > 15:            # Excel 15 haneden sonrasını yuvarlar (kimlik/kart no)
        return None
    if _NUM_TR_THOUSANDS.fullmatch(s):
        v = float(s.replace(".", "").replace(",", "."))
    elif _NUM_TR_DECIMAL.fullmatch(s):
        v = float(s.replace(",", "."))
    elif _NUM_EN_THOUSANDS.fullmatch(s):
        v = float(s.replace(",", ""))
    elif _NUM_PLAIN.fullmatch(s):
        v = float(s)
    else:
        return None
    return int(v) if v.is_integer() and not re.search(r"[.,]\d*[1-9]", s) and abs(v) < 1e15 else v


def parse_cell(raw: str) -> tuple[Any, str | None]:
    """Metni Excel değerine çevirir: (değer, sayı biçimi). Tanınmayan her şey düz metin kalır."""
    s = _ILLEGAL.sub("", raw).strip()[:MAX_CELL_CHARS]
    if not s:
        return None, None
    m = _CURRENCY.match(s)
    if m:
        n = _number((m.group(1) or m.group(2)).strip())
        if n is not None:
            return n, CURRENCY_FORMAT
    m = _PERCENT.match(s)
    if m:
        n = _number((m.group(1) or m.group(2)).strip())
        if n is not None:
            return n / 100, "0%" if float(n).is_integer() else "0.0%"
    n = _number(s)
    if n is not None:
        return n, (DECIMAL_FORMAT if isinstance(n, float) else None)
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s) or re.fullmatch(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", s)
    if m:
        a, b, c = (int(x) for x in m.groups())
        y, mo, d = (a, b, c) if a > 31 else (c, b, a)
        try:
            return date(y, mo, d), DATE_FORMAT
        except ValueError:
            pass
    return s, None


def _set(cell: Any, value: Any, fmt: str | None) -> None:  # noqa: ANN401
    cell.value = value
    if isinstance(value, str):
        cell.data_type = "s"   # "=...", "+...", "@..." formül olarak yorumlanmaz
    if fmt:
        cell.number_format = fmt


def sheet_title(name: str) -> str:
    t = re.sub(r"[\[\]:*?/\\]", " ", name).strip().strip("'")[:31].strip()
    return t or "Sayfa1"


def _is_number(v: Any) -> bool:  # noqa: ANN401
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def build_workbook(sheet_name: str, headers: list[str], rows: list[list[str]], totals: bool) -> tuple[Any, dict]:
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_title(sheet_name)
    width = max([len(headers)] + [len(r) for r in rows] + [0])
    widths = [8] * width
    r0 = 1
    if headers:
        for c, h in enumerate(headers, start=1):
            cell = ws.cell(row=1, column=c)
            _set(cell, _ILLEGAL.sub("", h).strip()[:MAX_CELL_CHARS] or None, None)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor=HEADER_FILL)
            widths[c - 1] = max(widths[c - 1], len(h) + 2)
        ws.freeze_panes = "A2"
        r0 = 2
    parsed: list[list[tuple[Any, str | None]]] = [[parse_cell(v) for v in row] for row in rows]
    for i, row in enumerate(parsed):
        for c, (value, fmt) in enumerate(row, start=1):
            if value is None:
                continue
            _set(ws.cell(row=r0 + i, column=c), value, fmt)
            widths[c - 1] = max(widths[c - 1], len(_cell_str(value)) + 2)
    last = r0 + len(rows) - 1
    summed: list[int] = []
    if totals and rows:
        for c in range(width):
            col = [row[c][0] for row in parsed if c < len(row) and row[c][0] is not None]
            if col and all(_is_number(v) for v in col):
                summed.append(c)
        if summed:
            tr = last + 1
            if 0 not in summed:
                ws.cell(row=tr, column=1, value="Toplam").font = Font(bold=True)
            for c in summed:
                letter = get_column_letter(c + 1)
                cell = ws.cell(row=tr, column=c + 1, value=f"=SUM({letter}{r0}:{letter}{last})")
                cell.font = Font(bold=True)
                fmts = [row[c][1] for row in parsed if c < len(row) and row[c][1]]
                if fmts:
                    cell.number_format = fmts[0]
    if headers and rows:
        ws.auto_filter.ref = f"A1:{get_column_letter(max(width, 1))}{last}"
    for c, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(c)].width = min(max(w, 8), 60)
    return wb, {"sheet": ws.title, "rows": len(rows), "columns": width,
                "total_columns": [get_column_letter(c + 1) for c in summed]}


_SUM_RANGE = re.compile(r"(\$?[A-Z]{1,3}\$?)(\d+)(:\$?[A-Z]{1,3}\$?)(\d+)")
_TOTAL_WORDS = {"toplam", "genel toplam", "total", "grand total", "ara toplam"}


def _last_used_row(ws: Any) -> int:  # noqa: ANN401
    last = 0
    for idx, row in enumerate(ws.iter_rows(values_only=True), start=1):
        if any(v is not None and str(v).strip() for v in row):
            last = idx
    return last


def _is_total_row(ws: Any, r: int) -> bool:  # noqa: ANN401
    values = [c.value for c in ws[r]]
    label = next((v for v in values if v is not None and str(v).strip()), None)
    has_sum = any(isinstance(v, str) and v.upper().startswith("=SUM(") for v in values)
    return has_sum and isinstance(label, str) and fold(label) in _TOTAL_WORDS


def append_rows(p: Path, sheet: str, rows: list[list[str]]) -> dict:
    import openpyxl

    try:
        wb = openpyxl.load_workbook(str(p))
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"xlsx: {e}", f"{p.name} açılamadı (bozuk ya da desteklenmeyen Excel dosyası).") from e
    ws = wb[sheet]
    last = _last_used_row(ws)
    n = len(rows)
    total_row = last if last > 1 and _is_total_row(ws, last) else 0
    if total_row:
        ws.insert_rows(total_row, n)
        start = total_row
        new_total = total_row + n
        for cell in ws[new_total]:
            v = cell.value
            if isinstance(v, str) and v.startswith("="):
                # TOPLA aralığının sonu eski son veri satırıysa yeni satırları da kapsasın
                cell.value = _SUM_RANGE.sub(
                    lambda m: m.group(0) if int(m.group(4)) != total_row - 1
                    else f"{m.group(1)}{m.group(2)}{m.group(3)}{int(m.group(4)) + n}", v)
    else:
        start = last + 1
    template = start - 1 if start - 1 > 1 else 0   # biçimi (sayı/para/tarih) bir üstteki veri satırından al
    for i, row in enumerate(rows):
        for c, raw in enumerate(row, start=1):
            value, fmt = parse_cell(raw)
            if value is None:
                continue
            cell = ws.cell(row=start + i, column=c)
            if template and fmt is None and _is_number(value):
                fmt = ws.cell(row=template, column=c).number_format
            _set(cell, value, fmt)
    _atomic_save(p, lambda tmp: wb.save(str(tmp)))
    return {"sheet": ws.title, "first_row": start, "last_row": start + n - 1, "added": n,
            "total_row_updated": bool(total_row)}


def _unsafe_parts(p: Path) -> list[str]:
    """openpyxl'in kaydederken koruyamayacağı öğeler (grafik, resim, pivot, makro)."""
    found = []
    try:
        with zipfile.ZipFile(p) as z:
            names = z.namelist()
    except (OSError, zipfile.BadZipFile):
        return found
    checks = (("xl/charts/", "grafik"), ("xl/media/", "resim"), ("xl/drawings/", "çizim/şekil"),
              ("xl/pivotTables/", "pivot tablo"), ("xl/vbaProject", "makro"), ("xl/slicers/", "dilimleyici"))
    for prefix, label in checks:
        if any(n.startswith(prefix) for n in names) and label not in found:
            found.append(label)
    return found


# ====================== araçlar ======================
class DocumentTools:
    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx
        self.files = files_mod.FileTools(ctx)

    @property
    def sb(self) -> Any:  # noqa: ANN401
        return self.ctx.sandbox

    def _rel(self, p: Path) -> str:
        return self.files._rel(p)  # noqa: SLF001

    @staticmethod
    def _check_rows(rows: list[list[str]], headers: list[str] | None = None) -> None:
        if len(rows) > MAX_SHEET_ROWS:
            raise ToolError("çok satır", f"Bir seferde en çok {MAX_SHEET_ROWS} satır yazabilirim.")
        if (headers is not None and len(headers) > MAX_COLUMNS) or any(len(r) > MAX_COLUMNS for r in rows):
            raise ToolError("çok sütun", f"En çok {MAX_COLUMNS} sütun destekleniyor.")

    # ----- Excel oluşturma -----
    def prepare_create_sheet(self, args: dict) -> dict:
        a = self.files.prepare_new(args)
        p = Path(a["_path"])
        if p.suffix.lower() in SHEET_FORMATS:
            p = p.with_suffix(".xlsx")
        elif p.suffix.lower() != ".xlsx":
            p = p.with_name(p.name + ".xlsx")
        p = self.sb.resolve(str(p), for_write=True)
        self._check_rows(args["rows"], args["headers"])
        if not args["headers"] and not args["rows"]:
            raise ToolError("boş", "Tabloya yazılacak başlık ya da satır yok.")
        exists = p.exists()
        if exists and p.is_dir():
            raise ToolError("klasör var", "Bu adda bir klasör var.")
        if exists and not args["overwrite"]:
            raise ToolError("var", f"{p.name} zaten var. Üzerine yazmamı istersen söyle; satır eklemek için "
                                   "add_spreadsheet_rows kullanılabilir.")
        return {**a, "_path": str(p), "_exists": exists}

    def create_sheet(self, args: dict) -> ToolOutput:
        _require("openpyxl", "Excel dosyası oluşturmak")
        p = Path(args["_path"])
        wb, info = build_workbook(args["sheet_name"], args["headers"], args["rows"], args["totals"])
        _atomic_save(p, lambda tmp: wb.save(str(tmp)))
        # Doğrulama: dosya gerçekten açılıyor ve satırlar yerinde mi?
        import openpyxl

        check = openpyxl.load_workbook(str(p), read_only=True)
        try:
            got = check[info["sheet"]].max_row or 0
        finally:
            check.close()
        expected = (1 if args["headers"] else 0) + len(args["rows"]) + (1 if info["total_columns"] else 0)
        if got < expected:
            raise ToolError("doğrulanamadı", f"{p.name} yazıldı ama içeriği doğrulanamadı.")
        verb = "üzerine yazdım" if args["_exists"] else "oluşturdum"
        extra = f", {'/'.join(info['total_columns'])} sütunlarına toplam satırı ile" if info["total_columns"] else ""
        return ToolOutput(f"{self._rel(p)} tablosunu {verb} ({info['rows']} satır, {info['columns']} sütun{extra}). "
                          "Açmamı ister misin?", {"path": str(p), **info, "can_open_with": "open_path"})

    # ----- Excel'e satır ekleme -----
    def prepare_add_rows(self, args: dict) -> dict:
        _require("openpyxl", "Excel dosyasını düzenlemek")
        import openpyxl

        p = self.sb.resolve(args["path"], must_exist=True)
        ext = p.suffix.lower()
        if ext == ".xlsm":
            raise ToolError("makrolu", "Makrolu Excel dosyalarını (.xlsm) değiştirmiyorum.")
        if ext != ".xlsx" or not p.is_file():
            raise ToolError("tür", "Yalnızca .xlsx Excel dosyalarına satır ekleyebilirim.")
        if not args["rows"]:
            raise ToolError("boş", "Eklenecek satır yok.")
        self._check_rows(args["rows"])
        try:
            wb = openpyxl.load_workbook(str(p), read_only=True)
            names = list(wb.sheetnames)
            wb.close()
        except Exception as e:  # noqa: BLE001
            raise ToolError(f"xlsx: {e}", f"{p.name} açılamadı (bozuk ya da desteklenmeyen Excel dosyası).") from e
        want = args["sheet_name"].strip()
        if want:
            sheet = next((n for n in names if fold(n) == fold(want)), None)
            if sheet is None:
                raise ToolError("sayfa yok", f"'{want}' adlı sayfa yok. Sayfalar: {', '.join(names)}.")
        else:
            sheet = names[0]
        return {**args, "_path": str(p), "_sheet": sheet, "_warn": _unsafe_parts(p)}

    def add_rows(self, args: dict) -> ToolOutput:
        p = Path(args["_path"])
        info = append_rows(p, args["_sheet"], args["rows"])
        import openpyxl

        check = openpyxl.load_workbook(str(p), read_only=True)
        try:
            got = check[info["sheet"]].max_row or 0
        finally:
            check.close()
        if got < info["last_row"]:
            raise ToolError("doğrulanamadı", f"{p.name} kaydedildi ama eklenen satırlar doğrulanamadı.")
        total = " Toplam satırını da güncelledim." if info["total_row_updated"] else ""
        return ToolOutput(f"{self._rel(p)} dosyasının '{info['sheet']}' sayfasına {info['added']} satır ekledim "
                          f"(satır {info['first_row']}-{info['last_row']}).{total} Açmamı ister misin?",
                          {"path": str(p), **info, "can_open_with": "open_path"})

    def add_rows_prompt(self, args: dict) -> ConfirmPrompt:
        rows = args["rows"]
        first = " | ".join(v for v in rows[0] if v)[:200]
        warn = ""
        if args["_warn"]:
            warn = (f"\n\nUyarı: dosyada {', '.join(args['_warn'])} var; dosya yeniden kaydedilirken bunlar "
                    "kaybolabilir.")
        return ConfirmPrompt("add_spreadsheet_rows", "Excel'e satır ekle",
                             f"{Path(args['_path']).name} dosyasının '{args['_sheet']}' sayfasına {len(rows)} satır "
                             f"eklenecek.\n\nİlk satır: {first}{warn}", danger=bool(args["_warn"]),
                             confirm_label="Ekle")

    # ----- belgeye ekleme -----
    def prepare_append(self, args: dict) -> dict:
        p = self.sb.resolve(args["path"], must_exist=True)
        ext = p.suffix.lower()
        if not p.is_file() or ext not in (".docx", ".txt", ".md"):
            raise ToolError("tür", "Yalnızca var olan .docx, .txt ve .md belgelerine ekleme yapabilirim.")
        if not args["content"].strip():
            raise ToolError("boş", "Eklenecek metin yok.")
        if len(args["content"]) > files_mod.MAX_DOC_CHARS:
            raise ToolError("çok uzun", "Eklenecek metin çok uzun.")
        if ext != ".docx" and p.stat().st_size > files_mod.MAX_TEXT_FILE_BYTES:
            raise ToolError("büyük", f"{p.name} çok büyük, ekleme yapmıyorum.")
        return {**args, "_path": str(p)}

    def append(self, args: dict) -> ToolOutput:
        p = Path(args["_path"])
        content = args["content"]
        if p.suffix.lower() == ".docx":
            docx = _require("docx", "Word belgesini düzenlemek")
            try:
                d = docx.Document(str(p))
            except Exception as e:  # noqa: BLE001
                raise ToolError(f"docx: {e}", f"{p.name} açılamadı (bozuk ya da desteklenmeyen Word belgesi).") from e
            before = len(d.element.body)
            render_docx(d, content)
            _atomic_save(p, lambda tmp: d.save(str(tmp)))
            if len(docx.Document(str(p)).element.body) <= before:
                raise ToolError("doğrulanamadı", f"{p.name} kaydedildi ama eklenen metin doğrulanamadı.")
        else:
            raw = p.read_bytes()
            bom = raw.startswith(b"\xef\xbb\xbf")
            try:
                old = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                old = raw.decode("cp1254", errors="replace")  # eski Türkçe Windows metni: UTF-8'e çevrilir
            sep = "" if not old or old.endswith("\n") else "\n"
            new = old + sep + content
            _atomic_save(p, lambda tmp: tmp.write_bytes((b"\xef\xbb\xbf" if bom else b"") + new.encode("utf-8")))
            if not p.read_bytes().decode("utf-8-sig").endswith(content):
                raise ToolError("doğrulanamadı", f"{p.name} kaydedildi ama eklenen metin doğrulanamadı.")
        return ToolOutput(f"{self._rel(p)} belgesinin sonuna ekledim ({len(content)} karakter). Açmamı ister misin?",
                          {"path": str(p), "chars": len(content), "can_open_with": "open_path"})

    def append_prompt(self, args: dict) -> ConfirmPrompt:
        preview = re.sub(r"\s+", " ", args["content"]).strip()
        preview = preview if len(preview) <= 200 else preview[:200] + "…"
        return ConfirmPrompt("append_to_document", "Belgeye ekle",
                             f"{Path(args['_path']).name} belgesinin sonuna {len(args['content'])} karakter "
                             f"eklenecek:\n\n{preview}", confirm_label="Ekle")

    # ----- son indirilen dosya -----
    def prepare_latest(self, args: dict) -> dict:
        root = next((r for r in self.sb.roots if r.key == "downloads"), None)
        if root is None:
            raise ToolError("indirilenler yok", "İndirilenler klasörüne erişim iznim yok.")
        word = fold(strip_apostrophe_suffix(args["extension"])).strip(" .*")
        ext = files_mod.EXTENSION_WORDS.get(word, "." + word if word else "")
        if ext and not re.fullmatch(r"\.[a-z0-9]{1,10}", ext):
            raise ToolError("uzantı", "Geçersiz dosya türü.")
        best: tuple[float, str] | None = None
        with os.scandir(root.path) as it:
            for e in it:
                name = e.name.lower()
                if files_mod._hidden(e) or name.endswith(PARTIAL_EXTS):  # noqa: SLF001
                    continue
                try:
                    if not e.is_file(follow_symlinks=False):
                        continue
                    mtime = e.stat(follow_symlinks=False).st_mtime
                except OSError:
                    continue
                if ext and not name.endswith(ext):
                    continue
                if best is None or mtime > best[0]:
                    best = (mtime, e.path)
        what = f"{ext.lstrip('.').upper()} dosyası" if ext else "dosya"
        if best is None:
            raise ToolError("yok", f"İndirilenler klasöründe {what} bulamadım.")
        p = self.sb.resolve(best[1], must_exist=True)
        return {**args, "_path": str(p), "_what": what}

    def latest(self, args: dict) -> ToolOutput:
        p = Path(args["_path"])
        st = p.stat()
        info = {"path": str(p), "name": p.name, "size": st.st_size,
                "modified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")}
        if args["action"] == "open":
            self.files.open_path({"path": str(p)})
            return ToolOutput(f"Son indirilen {args['_what']}: {p.name} açılıyor.", info)
        if args["action"] == "read":
            if st.st_size > files_mod.MAX_READ_BYTES:
                raise ToolError("büyük", f"{p.name} çok büyük ({_human(st.st_size)}); en çok "
                                         f"{_human(files_mod.MAX_READ_BYTES)} okurum.")
            text, meta = extract_text(p, args["max_chars"])
            return ToolOutput(describe_read(p.name, text, meta), {**info, **meta, "text": text}, untrusted=True)
        return ToolOutput(f"Son indirilen {args['_what']}: {p.name} ({info['modified']}, {_human(st.st_size)}).", info)


def register(reg: ToolRegistry, ctx: ToolContext) -> DocumentTools:
    t = DocumentTools(ctx)
    path_field = js.string("Yol: tam yol ya da 'Belgeler/...', 'Masaüstü/...' / path", 1000, 1)
    cell = js.string("Hücre / cell", MAX_CELL_CHARS)
    rows_field = {"type": "array", "description": "Satırlar; her satır hücre metinleri listesi / rows of cell strings",
                  "maxItems": MAX_SHEET_ROWS, "items": {"type": "array", "items": cell, "maxItems": MAX_COLUMNS}}

    reg.register(ToolSpec(
        name="create_spreadsheet", title="Excel tablosu oluştur", category="belge",
        description=("Excel (.xlsx) tablosu oluşturur: biçimli başlık satırı; sayı, para (₺), yüzde ve tarih tanınır; "
                     "totals=true sayısal sütunlara toplam satırı ekler. Üzerine yazma onay ister. / Creates an .xlsx "
                     "table; totals adds SUM row; overwrite needs confirmation."),
        input_schema=js.obj(path=path_field, sheet_name=js.string("Sayfa adı / sheet name", 31),
                            headers={"type": "array", "items": js.string("Başlık / header", 200),
                                     "maxItems": MAX_COLUMNS, "description": "Sütun başlıkları / column headers"},
                            rows=rows_field, totals=js.boolean("Toplam satırı ekle / add totals row"),
                            overwrite=js.boolean("Varsa üzerine yaz / overwrite if exists")),
        permission=Permission.SAFE, handler=t.create_sheet, prepare=t.prepare_create_sheet, timeout_s=60,
        escalate=lambda a: Permission.CONFIRM if a.get("_exists") else None,
        confirm=lambda a: ConfirmPrompt("create_spreadsheet", "Üzerine yaz",
                                        f"{Path(a['_path']).name} zaten var ve üzerine yazılacak. Eski içerik "
                                        "kaybolur.",
                                        danger=True, confirm_label="Üzerine yaz")))

    reg.register(ToolSpec(
        name="add_spreadsheet_rows", title="Excel'e satır ekle", category="belge",
        description=("Var olan .xlsx dosyasına satır ekler (toplam satırı varsa üstüne ekler, formülü günceller). "
                     "sheet_name boşsa ilk sayfa. / Appends rows to an existing .xlsx (keeps totals row last)."),
        input_schema=js.obj(path=path_field, sheet_name=js.string("Sayfa adı, boş = ilk sayfa / sheet or empty", 31),
                            rows=rows_field),
        permission=Permission.CONFIRM, handler=t.add_rows, prepare=t.prepare_add_rows, confirm=t.add_rows_prompt,
        timeout_s=60))

    reg.register(ToolSpec(
        name="append_to_document", title="Belgeye ekle", category="belge",
        description=("Var olan docx/txt/md belgesinin sonuna metin ekler (docx'te '# başlık', '- madde', '| tablo |' "
                     "biçimlenir). / Appends text to an existing docx/txt/md document."),
        input_schema=js.obj(path=path_field, content=js.string("Eklenecek metin / content", files_mod.MAX_DOC_CHARS)),
        permission=Permission.CONFIRM, handler=t.append, prepare=t.prepare_append, confirm=t.append_prompt,
        timeout_s=60))

    reg.register(ToolSpec(
        name="latest_download", title="Son indirilen dosya", category="belge",
        description=("İndirilenler'deki en yeni dosyayı (extension ile türe göre, örn. pdf) bulur; action: info, read "
                     "(metni okur: pdf/docx/xlsx/txt; güvenilmeyen içerik) ya da open. / Newest file in Downloads: "
                     "info, read text (untrusted) or open."),
        input_schema=js.obj(extension=js.string("Uzantı, örn. pdf; boş = hepsi / extension or empty", 20),
                            action=js.string("Ne yapılsın / action", enum=["info", "read", "open"]),
                            max_chars=js.integer("Okumada en çok karakter / max characters", 100, 10000)),
        permission=Permission.READ, handler=t.latest, prepare=t.prepare_latest, timeout_s=30,
        escalate=lambda a: Permission.SAFE if a.get("action") == "open" else None))
    return t
