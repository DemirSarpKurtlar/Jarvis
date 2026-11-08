"""İndirilenler klasörünü her gün otomatik düzenleme rutini.

- Düzenleme `organize_folder` ile aynı planı kullanır (`FileTools.plan_organize`): yalnızca üst
  düzeydeki dosyalar türlerine göre alt klasörlere (Belgeler, Resimler...) TAŞINIR. Hiçbir şey
  silinmez; aynı ad varsa "ad (2).uzantı" olur. İnmekte olan (.crdownload/.part) ve son 10 dakikada
  değişmiş dosyalara dokunulmaz.
- Rutin açılırken onay penceresinde şu an neyin taşınacağı (kuru çalıştırma önizlemesi) gösterilir.
- Her taşıma bir geri alma kaydına yazılır; "düzenlemeyi geri al" son çalıştırmayı geri taşır
  (yerinde aynı adla yeni bir dosya varsa o dosyaya dokunmaz, atlar).
- Yalnızca JARVIS açıkken çalışır; kaçırılan gün bir sonraki açılışta telafi edilir
  (zamanlama `jarvis/system/routines.py`).
Durum ve geri alma kaydı veri klasöründe `indirilenler_rutini.json` dosyasında tutulur.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from jarvis.security.action_validator import ValidationError
from jarvis.security.permission_manager import ConfirmPrompt, Permission
from jarvis.tools import files as files_mod
from jarvis.tools import schema as js
from jarvis.tools.registry import ToolContext, ToolError, ToolOutput, ToolRegistry, ToolSpec

log = logging.getLogger(__name__)

STATE_FILE = "indirilenler_rutini.json"
MAX_RUNS_KEPT = 10
MIN_FILE_AGE_S = 600
ACTIONS = ["durum", "onizle", "ac", "kapat", "simdi_calistir", "geri_al"]


class DownloadsRoutine:
    def __init__(self, ctx: ToolContext, state_dir: Path, now: Callable[[], datetime] = datetime.now,
                 clock: Callable[[], float] = time.time) -> None:
        self.ctx = ctx
        self.path = Path(state_dir) / STATE_FILE
        self.now, self.clock = now, clock
        self._lock = threading.RLock()
        self.notify: Callable[[str], None] = lambda msg: None

    # ---------- durum dosyası ----------
    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("enabled", False)
                data.setdefault("runs", [])
                return data
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            log.warning("İndirilenler rutini durum dosyası okunamadı; sıfırdan başlıyor", exc_info=True)
        return {"enabled": False, "last_run": None, "runs": []}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    # ---------- sorgular ----------
    def folder(self) -> Path:
        root = self.ctx.sandbox.root_for_alias("downloads") if self.ctx.sandbox else None
        if root is None:
            raise ToolError("indirilenler yok", "İndirilenler klasörüne erişimim yok.")
        return root.path

    @property
    def enabled(self) -> bool:
        with self._lock:
            return bool(self._load().get("enabled"))

    def due(self) -> bool:
        """Etkin ve bugün henüz çalışmadıysa (kaçırılan günler bir kez telafi edilir)."""
        with self._lock:
            data = self._load()
            if not data.get("enabled"):
                return False
            last = data.get("last_run")
            return not last or datetime.fromisoformat(last).date() < self.now().date()

    def plan(self, skip_recent: bool = True) -> list[dict]:
        folder = self.folder()
        plan = files_mod.FileTools(self.ctx).plan_organize(folder)
        if not skip_recent:
            return plan
        cutoff = self.clock() - MIN_FILE_AGE_S
        out = []
        for item in plan:
            try:
                if (folder / item["file"]).stat().st_mtime <= cutoff:
                    out.append(item)
            except OSError:
                continue
        return out

    def preview(self) -> dict:
        plan = self.plan()
        counts: dict[str, int] = {}
        for item in plan:
            counts[item["category"]] = counts.get(item["category"], 0) + 1
        return {"count": len(plan), "categories": counts, "sample": [f"{i['file']} → {i['category']}"
                                                                      for i in plan[:8]]}

    def status(self) -> dict:
        with self._lock:
            data = self._load()
        last = next((r for r in reversed(data["runs"]) if not r.get("undone")), None)
        return {"enabled": bool(data.get("enabled")), "last_run": data.get("last_run"),
                "last_moved": len(last["moves"]) if last else 0,
                "undo_available": bool(last and last["moves"])}

    # ---------- değişiklikler ----------
    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            data = self._load()
            data["enabled"] = enabled
            self._save(data)

    def run(self, source: str = "otomatik") -> dict:
        """Planı uygular, her taşımayı kaydeder. Asla silmez, üzerine yazmaz."""
        with self._lock:
            folder = self.folder()
            moves, failed, created = [], [], []
            for item in self.plan():
                src, dst = folder / item["file"], Path(item["target"])
                try:
                    self.ctx.sandbox.resolve(str(dst), for_write=True)
                    if not src.is_file() or src.is_symlink():
                        continue
                    if not dst.parent.exists():
                        dst.parent.mkdir()
                        created.append(str(dst.parent))
                    if dst.exists():
                        dst = files_mod.unique_path(dst)
                    os.rename(src, dst)   # aynı birim: yeniden adlandırma (kopyala-sil yok)
                    moves.append({"from": str(src), "to": str(dst)})
                except (OSError, ValidationError, ToolError) as e:
                    failed.append({"file": item["file"], "error": str(getattr(e, "user_message", e))[:120]})
            data = self._load()
            stamp = self.now().isoformat(timespec="seconds")
            data["last_run"] = stamp
            if moves:
                data["runs"].append({"time": stamp, "source": source, "folder": str(folder), "moves": moves,
                                     "created_dirs": created, "undone": False})
                data["runs"] = data["runs"][-MAX_RUNS_KEPT:]
            self._save(data)
        return {"moved": len(moves), "failed": failed, "time": stamp}

    def undo_info(self) -> dict | None:
        with self._lock:
            data = self._load()
        for r in reversed(data["runs"]):
            if not r.get("undone") and r["moves"]:
                return {"time": r["time"], "count": len(r["moves"])}
        return None

    def undo(self) -> dict:
        with self._lock:
            data = self._load()
            run = next((r for r in reversed(data["runs"]) if not r.get("undone") and r["moves"]), None)
            if run is None:
                raise ToolError("geri alınacak yok", "Geri alınacak bir düzenleme yok.")
            back, conflicts, missing = 0, 0, 0
            for mv in reversed(run["moves"]):
                src, dst = Path(mv["to"]), Path(mv["from"])
                try:
                    if not src.exists():
                        missing += 1
                        continue
                    if dst.exists():
                        conflicts += 1   # yerinde yeni bir dosya var: ona dokunma
                        continue
                    self.ctx.sandbox.resolve(str(dst), for_write=True)
                    os.rename(src, dst)
                    back += 1
                except (OSError, ValidationError) as e:
                    log.warning("Geri alınamadı: %s (%s)", src, e)
                    conflicts += 1
            for d in run.get("created_dirs", []):
                try:
                    os.rmdir(d)   # yalnızca boşsa kaldırılır
                except OSError:
                    pass
            run["undone"] = True
            self._save(data)
        return {"restored": back, "conflicts": conflicts, "missing": missing, "run_time": run["time"]}


def _fmt_counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{c}: {n}" for c, n in sorted(counts.items(), key=lambda x: -x[1]))


class RoutineTools:
    def __init__(self, routine: DownloadsRoutine) -> None:
        self.r = routine

    def prepare(self, args: dict) -> dict:
        a = args["action"]
        if a in ("onizle", "ac", "simdi_calistir"):
            return {**args, "_preview": self.r.preview()}
        if a == "geri_al":
            info = self.r.undo_info()
            if info is None:
                raise ToolError("geri alınacak yok", "Geri alınacak bir düzenleme yok.")
            return {**args, "_undo": info}
        return dict(args)

    @staticmethod
    def escalate(args: dict) -> Permission | None:
        return {"kapat": Permission.SAFE, "ac": Permission.CONFIRM, "simdi_calistir": Permission.CONFIRM,
                "geri_al": Permission.CONFIRM}.get(args["action"])

    def prompt(self, args: dict) -> ConfirmPrompt:
        a = args["action"]
        if a == "geri_al":
            u = args["_undo"]
            return ConfirmPrompt("downloads_routine", "Düzenlemeyi geri al",
                                 f"{u['time'].replace('T', ' ')} düzenlemesinde taşınan {u['count']} dosya İndirilenler'e "
                                 "geri taşınacak. Hiçbir dosya silinmez.", confirm_label="Geri al")
        p = args["_preview"]
        now = (f"Şu an taşınacak {p['count']} dosya ({_fmt_counts(p['categories'])}):\n" + "\n".join(
            f"• {s}" for s in p["sample"]) + (f"\n… ve {p['count'] - 8} dosya daha" if p["count"] > 8 else "")) \
            if p["count"] else "Şu an taşınacak dosya yok."
        head = ("Her gün (JARVIS açıkken) İndirilenler'deki dosyalar türlerine göre alt klasörlere taşınacak. "
                "Hiçbir dosya silinmez; her düzenleme 'düzenlemeyi geri al' ile geri alınabilir.\n\n"
                if a == "ac" else "İndirilenler şimdi düzenlenecek (silme yok, geri alınabilir).\n\n")
        return ConfirmPrompt("downloads_routine", "İndirilenler'i düzenle", head + now,
                             confirm_label="Aç ve düzenle" if a == "ac" else "Düzenle")

    def handle(self, args: dict) -> ToolOutput:
        a = args["action"]
        if a == "durum":
            st = self.r.status()
            if not st["enabled"]:
                msg = "İndirilenler'i her gün düzenleme rutini kapalı."
            else:
                last = st["last_run"].replace("T", " ")[:16] if st["last_run"] else "henüz çalışmadı"
                msg = f"İndirilenler rutini açık; son çalışma: {last}."
            if st["undo_available"]:
                msg += f" Son düzenlemede {st['last_moved']} dosya taşındı; geri alınabilir."
            return ToolOutput(msg, st)
        if a == "onizle":
            p = args["_preview"]
            if not p["count"]:
                return ToolOutput("İndirilenler'de şu an taşınacak dosya yok.", p)
            return ToolOutput(f"Önizleme: {p['count']} dosya taşınacak ({_fmt_counts(p['categories'])}). "
                              "Hiçbir şey yapmadım.", p, untrusted=True)
        if a == "kapat":
            self.r.set_enabled(False)
            return ToolOutput("İndirilenler'i her gün düzenleme rutinini kapattım.", {"enabled": False})
        if a == "geri_al":
            res = self.r.undo()
            msg = f"{res['restored']} dosyayı İndirilenler'e geri taşıdım."
            if res["conflicts"]:
                msg += f" {res['conflicts']} dosyanın yerinde aynı adla yeni bir dosya olduğu için dokunmadım."
            if res["missing"]:
                msg += f" {res['missing']} dosya artık yerinde değildi."
            return ToolOutput(msg, res)
        if a == "ac":
            self.r.set_enabled(True)
        res = self.r.run("ilk" if a == "ac" else "elle")
        msg = f"{res['moved']} dosyayı türlerine göre alt klasörlere taşıdım." if res["moved"] else \
            "Taşınacak dosya yoktu."
        if res["failed"]:
            msg += f" {len(res['failed'])} dosya taşınamadı."
        if a == "ac":
            msg = "Rutini açtım: İndirilenler her gün düzenlenecek. " + msg
        if res["moved"]:
            msg += " Geri almak için 'düzenlemeyi geri al' diyebilirsin."
        return ToolOutput(msg, res)


def register(reg: ToolRegistry, ctx: ToolContext, state_dir: Path | None = None) -> DownloadsRoutine:
    if state_dir is None:
        from jarvis.paths import AppPaths
        state_dir = AppPaths.resolve().data_dir
    routine = DownloadsRoutine(ctx, state_dir)
    t = RoutineTools(routine)
    reg.register(ToolSpec(
        name="downloads_routine", title="İndirilenler rutini", category="dosya",
        description=("İndirilenler'i her gün türlerine göre alt klasörlere düzenleme rutini (silmez): durum, onizle, "
                     "ac, kapat, simdi_calistir, geri_al (son düzenlemeyi geri taşır). / Daily Downloads organizer "
                     "(moves only, undoable)."),
        input_schema=js.obj(action=js.string("İşlem / action", enum=ACTIONS)),
        permission=Permission.READ, handler=t.handle, prepare=t.prepare, escalate=t.escalate, confirm=t.prompt,
        timeout_s=120))
    return routine
