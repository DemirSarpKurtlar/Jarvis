"""Program kurma/güncelleme (winget) ve Windows Update denetimi.

winget: yalnızca argüman listesiyle, `shell=False`. Kullanıcı/Claude'dan gelen metin yalnızca
`--query` değeri olarak gider ve sıkı bir desenle doğrulanır (tire ile başlayamaz); kurulum ve
güncelleme yalnızca `--id <tam kimlik> --exact --source winget` ile yapılır. Kullanıcının söylediği ad
önce aramayla bir winget kimliğine çözülür; onay penceresinde paketin adı, kimliği, yayıncısı ve
sürümü gösterilir. Kurulumdan sonra `winget list` ile gerçekten kurulduğu doğrulanır.

Windows Update: sabit bir PowerShell betiği (Microsoft.Update.Session COM) bekleyen güncellemeleri
listeler. Güncelleme YÜKLENMEZ; yalnızca Ayarlar > Windows Update sayfası açılabilir.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from typing import Protocol

from jarvis.security.permission_manager import ConfirmPrompt, Permission
from jarvis.tools import powershell
from jarvis.tools import schema as js
from jarvis.tools.registry import NotSupportedError, ToolContext, ToolError, ToolOutput, ToolRegistry, ToolSpec, \
    WINDOWS_ONLY

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000

QUERY_RE = re.compile(r"^\w[\w .+#\-]{0,59}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_\-]{1,127}$")
COMMON = ("--accept-source-agreements", "--disable-interactivity")
INSTALL_FLAGS = ("--exact", "--source", "winget", "--silent", "--accept-source-agreements",
                 "--accept-package-agreements", "--disable-interactivity")
ACTIONS = ["ara", "guncellenebilirler", "kur", "guncelle", "tumunu_guncelle"]
NO_WINGET = ("winget bulunamadı. Microsoft Store'dan 'Uygulama Yükleyici' (App Installer) kurulursa program "
             "kurup güncelleyebilirim.")


def validate_query(q: str) -> str:
    q = " ".join(q.split())
    if not QUERY_RE.fullmatch(q):
        raise ToolError("ad", "Program adı geçersiz: harf/rakam ile başlamalı, en çok 60 karakter olmalı.")
    return q


def validate_id(pid: str) -> str:
    if not ID_RE.fullmatch(pid) or ".." in pid:
        raise ToolError("kimlik", "Geçersiz winget paket kimliği.")
    return pid


class WingetRunner(Protocol):
    def __call__(self, args: list[str], timeout_s: float) -> tuple[int, str]: ...


def find_winget() -> str | None:
    return shutil.which("winget") if IS_WINDOWS else None


def run_winget(args: list[str], timeout_s: float) -> tuple[int, str]:  # pragma: no cover - Windows
    if not IS_WINDOWS:
        raise NotSupportedError("Bu özellik yalnızca Windows'ta çalışır.")
    exe = find_winget()
    if exe is None:
        raise ToolError("winget yok", NO_WINGET)
    try:
        cp = subprocess.run([exe, *args], shell=False, capture_output=True, timeout=timeout_s,
                            creationflags=CREATE_NO_WINDOW, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        raise ToolError("zaman aşımı", f"winget {timeout_s:.0f} saniyede bitmedi.") from e
    except OSError as e:
        raise ToolError("winget", NO_WINGET) from e
    return cp.returncode, (cp.stdout or b"").decode("utf-8", errors="replace")


def _clean_lines(text: str) -> list[str]:
    """winget'in ilerleme göstergelerini (\\r ile yazılan dönen çubuk, █▒ çubukları) ve boş satırları ayıklar."""
    out = []
    for line in text.replace("\r\n", "\n").split("\n"):
        line = line.rsplit("\r", 1)[-1].rstrip()
        if not line.strip() or re.fullmatch(r"\s*[\\|/\-]\s*", line) or re.search(r"[█▒]", line):
            continue
        out.append(line)
    return out


def parse_table(text: str, kind: str = "search") -> list[dict]:
    """winget tablo çıktısı (search/list/upgrade). Sütunlar başlık satırındaki konumlardan çıkarılır;
    başlıklar yerelleştirilmiş olabileceği için sıra kullanılır: ad, kimlik, sürüm, [eşleşme|mevcut], kaynak."""
    lines = _clean_lines(text)
    sep = next((i for i, ln in enumerate(lines) if re.fullmatch(r"-{5,}", ln.strip())), None)
    if sep is None or sep == 0:
        return []
    cols = [m.start() for m in re.finditer(r"\S+", lines[sep - 1])]
    if len(cols) < 3:
        return []
    rows: list[dict] = []
    for ln in lines[sep + 1:]:
        cells = [ln[c:(cols[i + 1] if i + 1 < len(cols) else None)].strip() for i, c in enumerate(cols)]
        if not ID_RE.fullmatch(cells[1]) or not cells[0]:
            if rows and "…" not in ln:
                break          # tablo bitti (alt bilgi ya da ikinci bölüm)
            continue
        row = {"name": cells[0], "id": cells[1], "version": cells[2]}
        if len(cells) >= 5:
            row["match" if kind == "search" else "available"] = cells[3]
            row["source"] = cells[-1]
        elif len(cells) == 4:
            row["source"] = cells[3]
        rows.append(row)
    return rows


def parse_show(text: str) -> dict:
    """`winget show` çıktısından yayıncı ve sürüm (İngilizce ya da Türkçe etiketler)."""
    info: dict = {}
    for ln in _clean_lines(text):
        m = re.match(r"^\s*(Publisher|Yayımcı|Yayıncı)\s*:\s*(.+)$", ln, re.I)
        if m:
            info.setdefault("publisher", m.group(2).strip())
        m = re.match(r"^\s*(Version|Sürüm)\s*:\s*(.+)$", ln, re.I)
        if m:
            info.setdefault("version", m.group(2).strip())
        m = re.match(r"^\s*(?:Found|Bulundu|Bulunan)\s+(.+?)\s+\[([^\]]+)\]\s*$", ln, re.I)
        if m:
            info.setdefault("name", m.group(1).strip())
            info.setdefault("id", m.group(2).strip())
    return info


class PackageTools:
    def __init__(self, runner: WingetRunner = run_winget, available: Callable[[], bool] = lambda: bool(find_winget()),
                 ps_runner: Callable[[str, float], tuple[int, str, str]] = powershell.run_script) -> None:
        self.runner, self.available, self.ps_runner = runner, available, ps_runner

    def _run(self, args: list[str], timeout_s: float = 60.0) -> tuple[int, str]:
        if not self.available():
            raise ToolError("winget yok", NO_WINGET)
        return self.runner(args, timeout_s)

    def search(self, query: str) -> list[dict]:
        _code, out = self._run(["search", "--query", query, "--source", "winget", *COMMON])
        return parse_table(out)

    def installed(self, pid: str) -> dict | None:
        _code, out = self._run(["list", "--id", pid, "--exact", *COMMON])
        return next((r for r in parse_table(out, "list") if r["id"].lower() == pid.lower()), None)

    def upgradable(self) -> list[dict]:
        _code, out = self._run(["upgrade", "--source", "winget", *COMMON], 120)
        return [r for r in parse_table(out, "upgrade") if r.get("available")]

    def resolve(self, query: str) -> dict:
        """Kullanıcının söylediği adı tek bir winget paketine çözer; emin değilse adayları söyler."""
        q = validate_query(query)
        rows = self.search(q)
        if ID_RE.fullmatch(q) and "." in q:
            rows = [r for r in rows if r["id"].lower() == q.lower()] or rows
        if not rows:
            raise ToolError("bulunamadı", f"winget'te '{q}' için paket bulamadım.")
        exact = [r for r in rows if r["name"].lower() == q.lower() or r["id"].lower() == q.lower()]
        if len(exact) == 1:
            pick = exact[0]
        elif len(rows) == 1:
            pick = rows[0]
        else:
            cands = ", ".join(f"{r['name']} ({r['id']})" for r in rows[:6])
            raise ToolError("belirsiz", f"Birden çok paket buldum: {cands}. Hangisini istediğini (kimliğiyle) söyler "
                                        "misin?")
        pkg = {"name": pick["name"], "id": validate_id(pick["id"]), "version": pick.get("version", ""),
               "publisher": "bilinmiyor", "source": "winget"}
        _code, out = self._run(["show", "--id", pkg["id"], "--exact", "--source", "winget", *COMMON])
        info = parse_show(out)
        if info.get("id") and info["id"].lower() != pkg["id"].lower():
            raise ToolError("uyuşmazlık", "winget farklı bir paket gösterdi; güvenlik için kurmadım.")
        pkg["publisher"] = info.get("publisher", pkg["publisher"])
        pkg["version"] = info.get("version", pkg["version"])
        return pkg

    # ---------- araç ----------
    def prepare(self, args: dict) -> dict:
        a, q = args["action"], args["query"].strip()
        if a in ("ara", "kur", "guncelle") and not q:
            raise ToolError("ad", "Bir program adı gerekli.")
        if a == "ara":
            return {**args, "query": validate_query(q)}
        if a == "kur":
            pkg = self.resolve(q)
            have = self.installed(pkg["id"])
            if have is not None:
                raise ToolError("kurulu", f"{pkg['name']} zaten kurulu (sürüm {have.get('version', '?')}). "
                                          "Güncellemek istersen 'güncelle' diyebilirsin.")
            return {**args, "_package": pkg}
        if a == "guncelle":
            ups = self.upgradable()
            ql = validate_query(q).lower()
            match = [r for r in ups if r["id"].lower() == ql or r["name"].lower() == ql] or \
                [r for r in ups if ql in r["name"].lower() or ql in r["id"].lower()]
            if not match:
                raise ToolError("güncel", f"'{q}' için bekleyen bir güncelleme bulamadım (ya kurulu değil ya güncel).")
            if len(match) > 1:
                raise ToolError("belirsiz", "Birden çok eşleşme var: " + ", ".join(
                    f"{r['name']} ({r['id']})" for r in match[:6]) + ". Hangisi?")
            return {**args, "_package": {**match[0], "id": validate_id(match[0]["id"])}}
        if a == "tumunu_guncelle":
            ups = self.upgradable()
            if not ups:
                raise ToolError("güncel", "Güncellenecek program yok; hepsi güncel görünüyor.")
            return {**args, "_packages": [{**r, "id": validate_id(r["id"])} for r in ups]}
        return dict(args)

    @staticmethod
    def escalate(args: dict) -> Permission | None:
        return Permission.CONFIRM if args["action"] in ("kur", "guncelle", "tumunu_guncelle") else None

    @staticmethod
    def prompt(args: dict) -> ConfirmPrompt:
        a = args["action"]
        if a == "tumunu_guncelle":
            pk = args["_packages"]
            lines = "\n".join(f"• {r['name']} ({r['id']}): {r['version']} → {r.get('available', '?')}" for r in pk[:12])
            more = f"\n… ve {len(pk) - 12} paket daha" if len(pk) > 12 else ""
            return ConfirmPrompt("winget_packages", "Programları güncelle",
                                 f"{len(pk)} program winget ile sessizce güncellenecek:\n{lines}{more}\n\nBazıları "
                                 "yönetici izni (UAC) isteyebilir; açık programlar kapanabilir.",
                                 confirm_label="Güncelle")
        p = args["_package"]
        if a == "guncelle":
            return ConfirmPrompt("winget_packages", "Programı güncelle",
                                 f"Ad: {p['name']}\nKimlik: {p['id']}\nSürüm: {p['version']} → {p.get('available', '?')}"
                                 "\nKaynak: winget\n\nSessizce güncellenecek; yönetici izni (UAC) istenebilir.",
                                 confirm_label="Güncelle")
        return ConfirmPrompt("winget_packages", "Program kur",
                             f"Şu paket kurulacak:\nAd: {p['name']}\nKimlik: {p['id']}\nYayıncı: {p['publisher']}\n"
                             f"Sürüm: {p['version']}\nKaynak: winget\n\nKurulum sessiz yapılır; bazı programlar "
                             "yönetici izni (UAC) isteyebilir.", confirm_label="Kur")

    def handle(self, args: dict) -> ToolOutput:
        a = args["action"]
        if a == "ara":
            rows = self.search(args["query"])[:10]
            if not rows:
                return ToolOutput(f"winget'te '{args['query']}' için paket bulamadım.", [])
            return ToolOutput("Bulunan paketler: " + ", ".join(f"{r['name']} ({r['id']})" for r in rows[:5]) + ".",
                              rows, untrusted=True)
        if a == "guncellenebilirler":
            ups = self.upgradable()
            if not ups:
                return ToolOutput("Güncellenebilir program yok; winget'e göre hepsi güncel.", [])
            names = ", ".join(f"{r['name']} ({r['version']} → {r['available']})" for r in ups[:8])
            return ToolOutput(f"{len(ups)} program güncellenebilir: {names}.", ups, untrusted=True)
        if a == "kur":
            p = args["_package"]
            code, out = self._run(["install", "--id", p["id"], *INSTALL_FLAGS], 900)
            have = self.installed(p["id"])
            if have is None:
                raise ToolError("kurulamadı", f"{p['name']} kurulamadı (winget çıkış kodu {code}). "
                                              "Yönetici onayı reddedilmiş ya da kurulum başarısız olmuş olabilir.")
            return ToolOutput(f"{p['name']} kuruldu (sürüm {have.get('version', p['version'])}); winget listesinde "
                              "doğruladım.", {"id": p["id"], "installed": have, "exit_code": code})
        if a == "guncelle":
            p = args["_package"]
            code, _out = self._run(["upgrade", "--id", p["id"], *INSTALL_FLAGS], 900)
            still = [r for r in self.upgradable() if r["id"].lower() == p["id"].lower()]
            if still:
                raise ToolError("güncellenemedi", f"{p['name']} güncellenemedi (winget çıkış kodu {code}).")
            return ToolOutput(f"{p['name']} güncellendi ({p['version']} → {p.get('available', '?')}).",
                              {"id": p["id"], "exit_code": code})
        if a == "tumunu_guncelle":
            pk = args["_packages"]
            code, _out = self._run(["upgrade", "--all", *INSTALL_FLAGS[1:]], 3600)
            still = {r["id"].lower() for r in self.upgradable()}
            done = [r for r in pk if r["id"].lower() not in still]
            failed = [r for r in pk if r["id"].lower() in still]
            msg = f"{len(done)} program güncellendi."
            if failed:
                msg += " Güncellenemeyenler: " + ", ".join(r["name"] for r in failed[:8]) + "."
            return ToolOutput(msg, {"updated": [r["id"] for r in done], "failed": [r["id"] for r in failed],
                                    "exit_code": code})
        raise ToolError("işlem", "Bilinmeyen işlem.")

    # ---------- Windows Update ----------
    def windows_updates(self, args: dict) -> ToolOutput:
        code, out, err = self.ps_runner(WU_SCRIPT, 240.0)
        try:
            data = next(json.loads(ln) for ln in reversed(out.splitlines()) if ln.strip().startswith("{"))
        except (StopIteration, ValueError):
            log.warning("Windows Update betiği beklenmeyen çıktı verdi (kod %s): %s", code, err[:300])
            raise ToolError("windows update", "Windows Update'e ulaşamadım; bekleyen güncellemeleri okuyamadım.") \
                from None
        if not data.get("ok"):
            raise ToolError("windows update", "Windows Update denetlenemedi: " + str(data.get("error", ""))[:200])
        ups = data.get("updates") or []
        if isinstance(ups, dict):
            ups = [ups]
        reboot = bool(data.get("reboot"))
        tail = (" Yüklemek için 'güncelleme ayarlarını aç' diyebilirsin; güncellemeleri kendim sessizce "
                "yüklemiyorum.")
        rb = " Bilgisayarın yeniden başlatılmayı bekliyor." if reboot else ""
        if not ups:
            return ToolOutput("Bekleyen Windows güncellemesi yok." + rb, {"count": 0, "reboot_required": reboot})
        names = "; ".join(str(u.get("title", "?"))[:90] for u in ups[:5])
        return ToolOutput(f"{len(ups)} bekleyen Windows güncellemesi var: {names}.{rb}{tail}",
                          {"count": len(ups), "updates": ups[:30], "reboot_required": reboot}, untrusted=True)


WU_SCRIPT = r"""
try {
  $s = New-Object -ComObject Microsoft.Update.Session
  $r = $s.CreateUpdateSearcher().Search("IsInstalled=0 and IsHidden=0")
  $list = @()
  foreach ($u in $r.Updates) {
    $list += [pscustomobject]@{ title = $u.Title; kb = (@($u.KBArticleIDs) -join ',');
      size_mb = [math]::Round($u.MaxDownloadSize / 1MB, 1); reboot = [bool]$u.RebootRequired;
      severity = [string]$u.MsrcSeverity; categories = (@($u.Categories | ForEach-Object { $_.Name }) -join ', ') }
  }
  $reboot = $false
  try { $reboot = (New-Object -ComObject Microsoft.Update.SystemInfo).RebootRequired } catch { }
  [pscustomobject]@{ ok = $true; count = $r.Updates.Count; updates = @($list); reboot = [bool]$reboot } |
    ConvertTo-Json -Depth 4 -Compress
} catch {
  [pscustomobject]@{ ok = $false; error = $_.Exception.Message } | ConvertTo-Json -Compress
}
"""


def register(reg: ToolRegistry, ctx: ToolContext, tools: PackageTools | None = None) -> PackageTools:
    t = tools or PackageTools()
    reg.register(ToolSpec(
        name="winget_packages", title="Program kur/güncelle", category="sistem", platforms=WINDOWS_ONLY,
        description=("winget ile program arar (ara), güncellenebilirleri listeler, kurar (kur), günceller (guncelle, "
                     "tumunu_guncelle). query = program adı ya da winget kimliği; gerekmiyorsa ''. Kurma/güncelleme "
                     "onay ister. / Search, install or upgrade programs via winget (install/upgrade need confirmation)."),
        input_schema=js.obj(action=js.string("İşlem / action", enum=ACTIONS),
                            query=js.string("Program adı ya da kimliği / name or id", 60)),
        permission=Permission.READ, handler=t.handle, prepare=t.prepare, escalate=t.escalate, confirm=t.prompt,
        timeout_s=3700))
    reg.register(ToolSpec(
        name="windows_update_check", title="Windows Update denetimi", category="sistem", platforms=WINDOWS_ONLY,
        description=("Bekleyen Windows güncellemelerini listeler (yüklemez). / Lists pending Windows updates "
                     "(does not install)."),
        input_schema=js.obj(), permission=Permission.READ, handler=t.windows_updates, timeout_s=250))
    return t
