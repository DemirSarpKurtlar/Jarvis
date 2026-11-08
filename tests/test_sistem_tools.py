"""SISTEM özellikleri: yer açma, geçici dosya temizliği, İndirilenler rutini, radyolar, hız testi,
winget, Windows Update, uyku/hazırda bekletme, GPU sıcaklık uyarısı ve yerel Türkçe komutlar.

Gerçek Windows'a dokunulmaz: her arka uç sahtedir."""

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from jarvis.config.settings import SistemSettings
from jarvis.core.events import EventBus, Topics
from jarvis.system.gpu_watch import GpuTempWatcher
from jarvis.system.monitor import GpuInfo, SystemSnapshot
from jarvis.tools import registry as regmod
from jarvis.tools.cancel import OPERATIONS, CancelToken, OperationCancelled
from jarvis.tools.defaults import build_registry
from jarvis.tools.intents import parse
from jarvis.tools.maintenance import ScanLimits, SpaceScanner, TempCleaner, find_duplicates, _File
from jarvis.tools.network_tools import SpeedTester, httpx
from jarvis.tools.packages import PackageTools, parse_show, parse_table, validate_id, validate_query
from jarvis.tools.powershell import encode, render
from jarvis.tools.registry import ToolError
from jarvis.tools.sistem_intents import parse_delay
from jarvis.tools.windows_actions import human_delay
from tests.tool_fakes import ConfirmRecorder, make_ctx

YES, NO = ConfirmRecorder(True), ConfirmRecorder(False)
OLD = time.time() - 3 * 86400


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def win(monkeypatch):
    monkeypatch.setattr(regmod, "CURRENT_PLATFORM", "win32")


def make(tmp_path, **fakes):
    ctx = make_ctx(tmp_path)
    ctx.extra["sistem"] = {"state_dir": tmp_path / "state", **fakes}
    return ctx, build_registry(ctx)


def write(p: Path, size: int = 10, age_s: float | None = None, data: bytes | None = None) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data if data is not None else os.urandom(size))
    if age_s is not None:
        t = time.time() - age_s
        os.utime(p, (t, t))
        if sys.platform == "win32":
            _set_creation_time(p, t)   # Windows'ta yaş = max(değişiklik, oluşturulma): ikisi de eski olmalı
    return p


def _set_creation_time(p: Path, t: float) -> None:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    h = k32.CreateFileW(str(p), 0x100, 0x7, None, 3, 0x80, None)   # FILE_WRITE_ATTRIBUTES, OPEN_EXISTING
    assert h not in (None, wintypes.HANDLE(-1).value), ctypes.get_last_error()
    try:
        ft = int(t * 10_000_000) + 116444736000000000
        when = wintypes.FILETIME(ft & 0xFFFFFFFF, ft >> 32)
        assert k32.SetFileTime(h, ctypes.byref(when), None, None), ctypes.get_last_error()
    finally:
        k32.CloseHandle(h)


# ====================================================================== yer açma
def test_space_hogs_finds_large_installers_and_duplicates(tmp_path):
    ctx, reg = make(tmp_path)
    dl, docs = tmp_path / "Downloads", tmp_path / "Documents"
    write(dl / "setup_eski.exe", 3 * 1024 * 1024, age_s=40 * 86400)
    write(dl / "setup_yeni.msi", 2 * 1024 * 1024, age_s=2 * 86400)        # 30 günden yeni: önerilmez
    blob = os.urandom(2 * 1024 * 1024)
    write(docs / "film.mkv", data=blob)
    write(docs / "yedek" / "film kopya.mkv", data=blob)
    write(docs / "ayni boyut farkli.mkv", 2 * 1024 * 1024)                   # boyut aynı, içerik farklı
    write(docs / "notlar.txt", 100)
    r = run(reg.execute("find_space_hogs", {"kind": "hepsi", "scope": "izinli_klasorler", "folder": ""}))
    assert r.ok and r.untrusted, r.error
    d = r.data
    assert Path(d["largest_files"][0]["path"]).name == "setup_eski.exe"
    assert [Path(x["path"]).name for x in d["old_installers"]] == ["setup_eski.exe"]
    assert len(d["duplicates"]) == 1
    assert {Path(p).name for p in d["duplicates"][0]["files"]} == {"film.mkv", "film kopya.mkv"}
    assert d["duplicates"][0]["wasted"] == len(blob)
    assert any(Path(x["path"]).name == "yedek" for x in d["largest_folders"])
    assert "Geri Dönüşüm" in r.summary and not d["partial"]
    # Hiçbir dosya silinmedi
    assert (dl / "setup_eski.exe").exists() and (docs / "film.mkv").exists()


def test_space_hogs_single_folder_and_bad_folder(tmp_path):
    ctx, reg = make(tmp_path)
    write(tmp_path / "Desktop" / "buyuk.bin", 5000)
    r = run(reg.execute("find_space_hogs", {"kind": "buyuk_dosyalar", "scope": "izinli_klasorler",
                                            "folder": "Masaüstü"}))
    assert r.ok and Path(r.data["largest_files"][0]["path"]).name == "buyuk.bin"
    assert "old_installers" not in r.data
    bad = run(reg.execute("find_space_hogs", {"kind": "hepsi", "scope": "izinli_klasorler", "folder": "/etc"}))
    assert not bad.ok


def test_space_hogs_does_not_follow_symlinks(tmp_path):
    ctx, reg = make(tmp_path)
    outside = tmp_path / "disarida"
    write(outside / "gizli_buyuk.bin", 4 * 1024 * 1024)
    try:
        (tmp_path / "Documents" / "bag").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("sembolik bağ oluşturulamıyor")
    r = run(reg.execute("find_space_hogs", {"kind": "buyuk_dosyalar", "scope": "izinli_klasorler", "folder": ""}))
    assert r.ok and all("gizli_buyuk" not in f["path"] for f in r.data["largest_files"])


def test_space_hogs_bounded_and_drive_scope(tmp_path):
    drive = tmp_path / "surucu"
    for i in range(30):
        write(drive / f"k{i}" / "a.bin", 100)
    write(drive / "Windows" / "dev.bin", 9_000_000)   # sistem klasörüne girilmez
    ctx = make_ctx(tmp_path)
    sc = SpaceScanner(ctx, drive_root=lambda: drive, limits=ScanLimits(max_entries=20))
    with OPERATIONS.track("test") as tok:
        out = sc.scan({"kind": "buyuk_dosyalar", "scope": "sistem_diski", "folder": ""}, tok)
    assert out.data["partial"] and "kısmi" in out.summary and "silemem" in out.summary
    sc2 = SpaceScanner(ctx, drive_root=lambda: drive)
    with OPERATIONS.track("test") as tok:
        out = sc2.scan({"kind": "buyuk_dosyalar", "scope": "sistem_diski", "folder": ""}, tok)
    assert all("dev.bin" not in f["path"] for f in out.data["largest_files"])


def test_space_hogs_cancel(tmp_path):
    ctx = make_ctx(tmp_path)
    write(tmp_path / "Documents" / "x.bin", 10)
    tok = CancelToken("t")
    tok.cancel()
    with pytest.raises(OperationCancelled):
        SpaceScanner(ctx).scan({"kind": "hepsi", "scope": "izinli_klasorler", "folder": ""}, tok)


def test_duplicates_budget_is_honest(tmp_path):
    blob = os.urandom(2 * 1024 * 1024)
    a, b = write(tmp_path / "a.bin", data=blob), write(tmp_path / "b.bin", data=blob)
    files = [_File(str(a), len(blob), 0), _File(str(b), len(blob), 0)]
    groups, unverified = find_duplicates(files, CancelToken("t"), ScanLimits(hash_bytes=1024))
    assert groups == [] and unverified == 1          # bütçe yetmedi: kopya diye SÖYLENMEZ
    groups, unverified = find_duplicates(files, CancelToken("t"), ScanLimits())
    assert len(groups) == 1 and unverified == 0


# ====================================================================== geçici dosyalar
def temp_setup(tmp_path):
    temp = tmp_path / "AppData" / "Local" / "Temp"
    old = write(temp / "eski.tmp", 2000, age_s=3 * 86400)
    old2 = write(temp / "alt" / "eski2.log", 3000, age_s=2 * 86400)
    new = write(temp / "yeni.tmp", 500)
    outside = write(tmp_path / "onemli" / "belge.txt", 1000, age_s=5 * 86400)
    return temp, old, old2, new, outside


def test_temp_cleanup_preview_confirm_and_apply(tmp_path):
    temp, old, old2, new, outside = temp_setup(tmp_path)
    try:
        (temp / "bag").symlink_to(outside.parent, target_is_directory=True)
    except OSError:
        pass
    cleaner = TempCleaner(dirs=lambda: [temp], recycle=lambda: {"size": 5 * 1024**3, "items": 12})
    ctx, reg = make(tmp_path, cleaner=cleaner)
    r = run(reg.execute("clean_temp_files", {"apply": False}))
    assert r.ok and r.data["files"] == 2 and r.data["bytes"] == 5000
    assert "Geri Dönüşüm" in r.summary and old.exists()
    no = ConfirmRecorder(False)
    r = run(reg.execute("clean_temp_files", {"apply": True}, no))
    assert not r.ok and old.exists() and "2 geçici dosya" in no.prompts[0].message
    assert "4.9 KB" in no.prompts[0].message or "KB" in no.prompts[0].message
    r = run(reg.execute("clean_temp_files", {"apply": True}, YES))
    assert r.ok, r.error
    assert r.data["deleted"] == 2 and r.data["freed_bytes"] == 5000
    assert not old.exists() and not old2.exists() and new.exists() and outside.exists()
    assert "0.0 MB" in r.summary   # 5000 bayt: dürüstçe küçük
    r = run(reg.execute("clean_temp_files", {"apply": True}, YES))
    assert r.ok and "Silinecek" in r.summary


def test_temp_cleanup_rejects_unsafe_roots_and_counts_locked(tmp_path, monkeypatch):
    temp, old, old2, new, outside = temp_setup(tmp_path)
    weird = tmp_path / "Belgelerim"   # adı Temp/tmp değil: kök sayılmaz
    write(weird / "x.txt", 10, age_s=5 * 86400)
    cleaner = TempCleaner(dirs=lambda: [temp, weird, Path("/")], recycle=lambda: None)
    roots = cleaner.roots()
    assert [p for p, problem in roots if problem is None] == [Path(os.path.realpath(temp))]
    real_unlink = os.unlink

    def locked(p, *a, **k):
        if p.endswith("eski.tmp"):
            raise PermissionError("kullanımda")
        return real_unlink(p, *a, **k)

    monkeypatch.setattr(os, "unlink", locked)
    res = cleaner.clean(CancelToken("t"))
    assert res["deleted"] == 1 and res["skipped_in_use"] == 1 and res["freed_bytes"] == 3000
    assert (weird / "x.txt").exists()


# ====================================================================== İndirilenler rutini
def test_downloads_routine_enable_run_undo(tmp_path):
    ctx, reg = make(tmp_path)
    dl = tmp_path / "Downloads"
    write(dl / "rapor.pdf", 10, age_s=3600)
    write(dl / "foto.jpg", 10, age_s=3600)
    write(dl / "iniyor.crdownload", 10, age_s=3600)
    write(dl / "taze.zip", 10)                       # son 10 dakikada değişmiş: dokunulmaz
    r = run(reg.execute("downloads_routine", {"action": "onizle"}))
    assert r.ok and r.data["count"] == 2 and (dl / "rapor.pdf").exists()
    no = ConfirmRecorder(False)
    assert not run(reg.execute("downloads_routine", {"action": "ac"}, no)).ok
    assert "rapor.pdf → Belgeler" in no.prompts[0].message and "silinmez" in no.prompts[0].message
    assert not ctx.extra["sistem_routine"].enabled
    r = run(reg.execute("downloads_routine", {"action": "ac"}, YES))
    assert r.ok and r.data["moved"] == 2, r.error
    assert (dl / "Belgeler" / "rapor.pdf").exists() and (dl / "Resimler" / "foto.jpg").exists()
    assert (dl / "iniyor.crdownload").exists() and (dl / "taze.zip").exists()
    routine = ctx.extra["sistem_routine"]
    assert routine.enabled and not routine.due()      # bugün çalıştı
    st = run(reg.execute("downloads_routine", {"action": "durum"}))
    assert st.ok and st.data["undo_available"] and "açık" in st.summary
    # Geri al: onay ister, dosyaları geri taşır, oluşturulan boş klasörleri kaldırır
    write(dl / "foto.jpg", 5, age_s=3600)            # yerinde aynı adla yeni dosya: ona dokunulmaz
    r = run(reg.execute("downloads_routine", {"action": "geri_al"}, YES))
    assert r.ok and r.data["restored"] == 1 and r.data["conflicts"] == 1
    assert (dl / "rapor.pdf").exists() and not (dl / "Belgeler").exists()
    assert (dl / "Resimler" / "foto.jpg").exists()   # çakışan dosya silinmedi, yerinde kaldı
    assert not run(reg.execute("downloads_routine", {"action": "geri_al"}, YES)).ok   # geri alınacak yok
    assert run(reg.execute("downloads_routine", {"action": "kapat"})).ok and not routine.enabled


def test_downloads_routine_due_and_catch_up(tmp_path):
    ctx, reg = make(tmp_path)
    routine = ctx.extra["sistem_routine"]
    assert not routine.due()            # kapalı
    routine.set_enabled(True)
    assert routine.due()                # hiç çalışmadı
    data = json.loads(routine.path.read_text(encoding="utf-8"))
    data["last_run"] = (datetime.now() - timedelta(days=3)).isoformat(timespec="seconds")
    routine.path.write_text(json.dumps(data), encoding="utf-8")
    assert routine.due()                # kaçırılan günler: bir kez telafi
    routine.run()
    assert not routine.due()
    routine.path.write_text("{bozuk", encoding="utf-8")
    assert not routine.due()            # bozuk durum dosyası: güvenli varsayılan (kapalı)


def test_downloads_routine_never_overwrites(tmp_path):
    ctx, reg = make(tmp_path)
    dl = tmp_path / "Downloads"
    write(dl / "a.pdf", 10, age_s=3600, data=b"yeni")
    write(dl / "Belgeler" / "a.pdf", data=b"eski")
    res = ctx.extra["sistem_routine"].run("elle")
    assert res["moved"] == 1
    assert (dl / "Belgeler" / "a.pdf").read_bytes() == b"eski"
    assert (dl / "Belgeler" / "a (2).pdf").read_bytes() == b"yeni"


def test_routine_service_runs_when_due(tmp_path):
    from jarvis.system.routines import SistemServices

    ctx, reg = make(tmp_path)
    write(tmp_path / "Downloads" / "x.pdf", 10, age_s=3600)
    ctx.extra["sistem_routine"].set_enabled(True)
    notes = []

    class A:  # asistanın gereken kadarı
        bus = EventBus()
        settings = type("S", (), {"sistem": SistemSettings()})()

        class tool_service:  # noqa: N801
            executor = None
        tool_service.ctx = ctx

        def spawn(self, coro, name=""):
            coro.close()

    A.bus.subscribe(Topics.NOTICE, notes.append)
    svc = SistemServices(A())
    res = run(svc.run_downloads_if_due())
    assert res["moved"] == 1 and any("geri al" in n for n in notes)
    assert run(svc.run_downloads_if_due()) is None   # bugün zaten çalıştı


# ====================================================================== radyolar
class FakeRadios:
    def __init__(self, wifi="On", bt="Off", stuck=False, access="Allowed", present=True):
        self.state = {"WiFi": wifi, "Bluetooth": bt}
        self.stuck, self.access, self.present = stuck, access, present
        self.calls = []

    def _st(self):
        radios = [{"kind": k, "name": k, "state": v} for k, v in self.state.items()] if self.present else []
        return {"access": self.access, "radios": radios, "results": []}

    def status(self):
        return self._st()

    def set_state(self, kind, state):
        self.calls.append((kind, state))
        if not self.stuck and self.access == "Allowed":
            self.state[kind] = state
        out = self._st()
        out["results"] = [self.access]
        return out


def test_radio_on_off_verified(tmp_path, win):
    radios = FakeRadios()
    ctx, reg = make(tmp_path, radios=radios, sleep=lambda s: None)
    r = run(reg.execute("radio_control", {"radio": "bluetooth", "action": "durum"}))
    assert r.ok and r.data["state"] == "Off" and "kapalı" in r.summary
    r = run(reg.execute("radio_control", {"radio": "bluetooth", "action": "ac"}))
    assert r.ok and r.data["verified"] and "Bluetooth'u açtım" in r.summary
    # Wi-Fi'yi kapatmak onay ister (internet gidebilir)
    no = ConfirmRecorder(False)
    assert not run(reg.execute("radio_control", {"radio": "wifi", "action": "kapat"}, no)).ok
    assert radios.state["WiFi"] == "On" and "internet" in no.prompts[0].message.lower()
    r = run(reg.execute("radio_control", {"radio": "wifi", "action": "kapat"}, YES))
    assert r.ok and radios.state["WiFi"] == "Off" and "Wi-Fi'yi kapattım" in r.summary
    # Wi-Fi'yi açmak onay istemez
    assert run(reg.execute("radio_control", {"radio": "wifi", "action": "ac"})).ok


def test_radio_failures_are_honest(tmp_path, win):
    ctx, reg = make(tmp_path, radios=FakeRadios(stuck=True), sleep=lambda s: None)
    r = run(reg.execute("radio_control", {"radio": "bluetooth", "action": "ac"}))
    assert not r.ok and "doğrulayamadım" in r.error
    ctx, reg = make(tmp_path, radios=FakeRadios(access="DeniedByUser"), sleep=lambda s: None)
    r = run(reg.execute("radio_control", {"radio": "bluetooth", "action": "ac"}))
    assert not r.ok and "izin vermedi" in r.error
    ctx, reg = make(tmp_path, radios=FakeRadios(present=False), sleep=lambda s: None)
    r = run(reg.execute("radio_control", {"radio": "bluetooth", "action": "durum"}))
    assert not r.ok and "bulunamadı" in r.error


def test_radio_unsupported_off_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(regmod, "CURRENT_PLATFORM", "linux")
    ctx, reg = make(tmp_path, radios=FakeRadios())
    r = run(reg.execute("radio_control", {"radio": "wifi", "action": "durum"}))
    assert not r.ok and "desteklenmiyor" in r.error


def test_powershell_render_only_allowlisted_values():
    allowed = {"kind": frozenset({"WiFi", "Bluetooth"}), "state": frozenset({"On", "Off"})}
    assert render("x {kind} {state}", allowed, kind="WiFi", state="Off") == "x WiFi Off"
    for bad in ("WiFi'; Remove-Item C:\\ -Recurse; '", "Cellular", "$(calc)"):
        with pytest.raises(ToolError):
            render("{kind}", allowed, kind=bad, state="On")
    assert encode("Write-Output 1").isascii()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows dışı davranış")
def test_real_backends_honest_off_windows(tmp_path, win):
    ctx = make_ctx(tmp_path)
    reg = build_registry(ctx)   # gerçek arka uçlar
    r = run(reg.execute("radio_control", {"radio": "wifi", "action": "durum"}))
    assert not r.ok and "Windows" in r.error
    r = run(reg.execute("windows_update_check", {}))
    assert not r.ok and "Windows" in r.error
    r = run(reg.execute("winget_packages", {"action": "guncellenebilirler", "query": ""}))
    assert not r.ok and "winget" in r.error


# ====================================================================== hız testi
def speed_client(fail=False, slow_bytes=None):
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path, dict(req.url.params)))
        if fail:
            raise httpx.ConnectError("yok", request=req)
        if req.url.path == "/__down":
            n = int(req.url.params["bytes"])
            return httpx.Response(200, content=b"\0" * n, headers={"cf-ray": "abc-IST",
                                                                    "server-timing": "cfRequestDuration;dur=1.5"})
        if req.url.path == "/__up":
            body = req.read()
            seen.append(("body", len(body)))
            return httpx.Response(200)
        return httpx.Response(404)

    return (lambda: httpx.Client(transport=httpx.MockTransport(handler))), seen


def test_speed_test_reports_mbps(tmp_path):
    factory, seen = speed_client()
    tester = SpeedTester(factory, down_bytes=2_000_000, up_bytes=500_000, pings=3)
    ctx, reg = make(tmp_path, speed=tester)
    r = run(reg.execute("internet_speed_test", {}))
    assert r.ok, r.error
    assert r.data["download_mbps"] > 0 and r.data["upload_mbps"] > 0 and r.data["ping_ms"] is not None
    assert r.data["server"] == "speed.cloudflare.com (IST)"
    assert ("body", 500_000) in seen   # yükleme sınırlı
    assert "Mbps" in r.summary
    assert all(p[1] in ("/__down", "/__up") for p in seen if p[0] != "body")


def test_speed_test_network_error_and_cancel(tmp_path):
    factory, _ = speed_client(fail=True)
    ctx, reg = make(tmp_path, speed=SpeedTester(factory))
    r = run(reg.execute("internet_speed_test", {}))
    assert not r.ok and "bağlanamadım" in r.error
    factory, _ = speed_client()
    tok = CancelToken("t")
    tok.cancel()
    with pytest.raises(OperationCancelled):
        SpeedTester(factory, down_bytes=1000, up_bytes=1000).run(tok)


def test_cancel_long_operation_tool(tmp_path):
    ctx, reg = make(tmp_path)
    r = run(reg.execute("cancel_long_operation", {}))
    assert r.ok and r.data["cancelled"] == []
    with OPERATIONS.track("disk taraması") as tok:
        r = run(reg.execute("cancel_long_operation", {}))
        assert tok.cancelled and r.data["cancelled"] == ["disk taraması"]


# ====================================================================== winget
SEARCH_VLC = """Name               Id                  Version  Match        Source
---------------------------------------------------------------------
VLC media player   VideoLAN.VLC        3.0.21   Moniker: vlc winget
"""
SEARCH_MANY = """Name                Id                      Version  Source
--------------------------------------------------------------
Mozilla Firefox     Mozilla.Firefox         126.0    winget
Firefox Nightly     Mozilla.Firefox.Nightly 128.0a1  winget
"""
SHOW_VLC = "Bulundu VLC media player [VideoLAN.VLC]\nSürüm: 3.0.21\nYayımcı: VideoLAN\n"
UPGRADES = """Name                Id                 Version  Available Source
-----------------------------------------------------------------
Git                 Git.Git            2.44.0   2.45.1    winget
Mozilla Firefox     Mozilla.Firefox    125.0    126.0     winget
2 upgrades available.
"""
def list_table(pid, version):
    w = [20, 24, 10, 11]
    head = "".join(h.ljust(n) for h, n in zip(("Name", "Id", "Version", "Available"), w)) + "Source"
    row = "".join(c.ljust(n) for c, n in zip(("Paket", pid, version, ""), w)) + "winget"
    return f"{head}\n{'-' * len(head)}\n{row}\n"


class FakeWinget:
    def __init__(self, search=SEARCH_VLC, installed=None, upgrades=UPGRADES, install_works=True):
        self.calls = []
        self.search, self.upgrades, self.install_works = search, upgrades, install_works
        self.installed = dict(installed or {})

    def __call__(self, args, timeout_s):
        self.calls.append(list(args))
        cmd = args[0]
        if cmd == "search":
            return 0, self.search
        if cmd == "show":
            return 0, SHOW_VLC
        if cmd == "list":
            pid = args[args.index("--id") + 1]
            if pid in self.installed:
                return 0, list_table(pid, self.installed[pid])
            return 1, "No installed package found matching input criteria."
        if cmd == "install":
            if self.install_works:
                self.installed[args[args.index("--id") + 1]] = "3.0.21"
            return 0, "Successfully installed"
        if cmd == "upgrade":
            if "--all" in args or "--id" in args:
                self.upgrades = ""
                return 0, "ok"
            return 0, self.upgrades
        return 1, ""


def test_winget_install_resolves_and_confirms_exact_package(tmp_path, win):
    wg = FakeWinget()
    ctx, reg = make(tmp_path, packages=PackageTools(runner=wg, available=lambda: True))
    no = ConfirmRecorder(False)
    r = run(reg.execute("winget_packages", {"action": "kur", "query": "VLC"}, no))
    assert not r.ok and not any(c[0] == "install" for c in wg.calls)
    msg = no.prompts[0].message
    assert "VideoLAN.VLC" in msg and "VideoLAN" in msg and "3.0.21" in msg and "VLC media player" in msg
    r = run(reg.execute("winget_packages", {"action": "kur", "query": "VLC"}, YES))
    assert r.ok and "doğruladım" in r.summary, r.error
    install = next(c for c in wg.calls if c[0] == "install")
    assert install[:4] == ["install", "--id", "VideoLAN.VLC", "--exact"]
    for flag in ("--silent", "--accept-source-agreements", "--accept-package-agreements"):
        assert flag in install
    # Zaten kurulu: kurmaya çalışmaz
    r = run(reg.execute("winget_packages", {"action": "kur", "query": "VLC"}, YES))
    assert not r.ok and "zaten kurulu" in r.error


def test_winget_install_failure_and_ambiguous(tmp_path, win):
    wg = FakeWinget(install_works=False)
    ctx, reg = make(tmp_path, packages=PackageTools(runner=wg, available=lambda: True))
    r = run(reg.execute("winget_packages", {"action": "kur", "query": "VLC"}, YES))
    assert not r.ok and "kurulamadı" in r.error
    wg = FakeWinget(search=SEARCH_MANY)
    ctx, reg = make(tmp_path, packages=PackageTools(runner=wg, available=lambda: True))
    r = run(reg.execute("winget_packages", {"action": "kur", "query": "firefox"}, YES))
    assert not r.ok and "Mozilla.Firefox.Nightly" in r.error and "Hangisini" in r.error


def test_winget_rejects_injection_and_handles_missing(tmp_path, win):
    wg = FakeWinget()
    ctx, reg = make(tmp_path, packages=PackageTools(runner=wg, available=lambda: True))
    for bad in ("--override evil", "vlc & calc", "-h", "x|y", "$(calc)", "a;b"):
        r = run(reg.execute("winget_packages", {"action": "kur", "query": bad}, YES))
        assert not r.ok, bad
    assert wg.calls == []
    for bad in ("Video..LAN", "-x", "a b"):
        with pytest.raises(ToolError):
            validate_id(bad)
    assert validate_query("  VLC   media ") == "VLC media"
    ctx, reg = make(tmp_path, packages=PackageTools(runner=wg, available=lambda: False))
    r = run(reg.execute("winget_packages", {"action": "ara", "query": "vlc"}))
    assert not r.ok and "App Installer" in r.error


def test_winget_upgrades(tmp_path, win):
    wg = FakeWinget()
    ctx, reg = make(tmp_path, packages=PackageTools(runner=wg, available=lambda: True))
    r = run(reg.execute("winget_packages", {"action": "guncellenebilirler", "query": ""}))
    assert r.ok and "2 program" in r.summary and "Git" in r.summary
    no = ConfirmRecorder(False)
    assert not run(reg.execute("winget_packages", {"action": "tumunu_guncelle", "query": ""}, no)).ok
    assert "Git.Git" in no.prompts[0].message and "Mozilla.Firefox" in no.prompts[0].message
    r = run(reg.execute("winget_packages", {"action": "tumunu_guncelle", "query": ""}, YES))
    assert r.ok and "2 program güncellendi" in r.summary
    assert any(c[:2] == ["upgrade", "--all"] for c in wg.calls)
    wg2 = FakeWinget()
    ctx, reg = make(tmp_path, packages=PackageTools(runner=wg2, available=lambda: True))
    r = run(reg.execute("winget_packages", {"action": "guncelle", "query": "git"}, YES))
    assert r.ok and any(c[:3] == ["upgrade", "--id", "Git.Git"] for c in wg2.calls)
    r = run(reg.execute("winget_packages", {"action": "guncelle", "query": "vlc"}, YES))
    assert not r.ok


def test_winget_parsers():
    rows = parse_table(SEARCH_VLC)
    assert rows == [{"name": "VLC media player", "id": "VideoLAN.VLC", "version": "3.0.21", "match": "Moniker: vlc",
                     "source": "winget"}]
    ups = parse_table(UPGRADES, "upgrade")
    assert [u["available"] for u in ups] == ["2.45.1", "126.0"]
    assert parse_table("hata") == []
    assert parse_show(SHOW_VLC)["publisher"] == "VideoLAN"
    assert parse_show("Found Git [Git.Git]\nVersion: 2.45.1\nPublisher: The Git Development Community\n")["id"] == \
        "Git.Git"


# ====================================================================== Windows Update
def test_windows_update_check(tmp_path, win):
    ok = {"ok": True, "count": 2, "reboot": True,
          "updates": [{"title": "2026-11 Toplu Güncelleştirme (KB5099999)", "kb": "5099999"},
                      {"title": "Microsoft Defender güncelleştirmesi", "kb": "2267602"}]}
    tools = PackageTools(runner=FakeWinget(), available=lambda: True,
                         ps_runner=lambda script, t: (0, "uyarı\n" + json.dumps(ok), ""))
    ctx, reg = make(tmp_path, packages=tools)
    r = run(reg.execute("windows_update_check", {}))
    assert r.ok and r.untrusted and r.data["count"] == 2 and "yeniden başlatılmayı" in r.summary
    assert "sessizce yüklemiyorum" in r.summary
    tools.ps_runner = lambda s, t: (0, json.dumps({"ok": True, "count": 0, "updates": [], "reboot": False}), "")
    assert "yok" in run(reg.execute("windows_update_check", {})).summary
    tools.ps_runner = lambda s, t: (0, json.dumps({"ok": False, "error": "0x8024402c"}), "")
    r = run(reg.execute("windows_update_check", {}))
    assert not r.ok and "0x8024402c" in r.error
    tools.ps_runner = lambda s, t: (1, "", "çöktü")
    assert not run(reg.execute("windows_update_check", {})).ok


# ====================================================================== güç: uyku / hazırda bekletme
def test_sleep_and_hibernate_via_scheduler(tmp_path, win):
    async def main():
        calls = []
        backends = {"power_fn": lambda a, d: calls.append(("power", a, d)), "cancel_fn": lambda: calls.append("iptal")
                    or True, "suspend_fn": lambda hib: calls.append(("suspend", hib)), "hibernate_fn": lambda: True}
        ctx = make_ctx(tmp_path)
        reg = build_registry(ctx, action_backends=backends)
        notes = []
        ctx.scheduler.notify = notes.append
        ctx.scheduler.bind(asyncio.get_running_loop())
        no = ConfirmRecorder(False)
        r = await reg.execute("power_action", {"action": "sleep", "delay_seconds": 3600}, no)
        assert not r.ok and not no.prompts[0].danger and "1 saat sonra" in no.prompts[0].message
        assert ctx.scheduler.list() == []
        r = await reg.execute("power_action", {"action": "sleep", "delay_seconds": 3600}, YES)
        assert r.ok and "1 saat sonra uyku moduna geçecek" in r.summary
        tasks = ctx.scheduler.list()
        assert [t.action for t in tasks] == ["guc_uyku"]
        listed = await reg.execute("list_scheduled_tasks", {})
        assert listed.data[0]["action"] == "guc_uyku"
        # "kapatmayı iptal et" uyku işini de iptal eder
        r = await reg.execute("cancel_power_action", {})
        assert r.ok and ctx.scheduler.list() == []
        # Kısa gecikmeli gerçek çalıştırma
        r = await reg.execute("power_action", {"action": "hibernate", "delay_seconds": 0}, YES)
        assert r.ok and "birkaç saniye" in r.summary
        task = ctx.scheduler.list()[0]
        task.handle.cancel()
        await ctx.scheduler._fire(task.id)
        assert ("suspend", True) in calls
        # Kapatma: Windows'un geri sayımı + görev listesinde görünür; listeden iptal shutdown /a çağırır
        r = await reg.execute("power_action", {"action": "shutdown", "delay_seconds": 1800}, YES)
        assert r.ok and ("power", "shutdown", 1800) in calls and "30 dakika" in r.summary
        await asyncio.sleep(0)
        t = ctx.scheduler.list()[0]
        assert t.action == "guc_kapat"
        assert (await reg.execute("cancel_scheduled_task", {"task_id": t.id})).ok
        assert calls[-1] == "iptal"
        ctx.scheduler.cancel_all()

    run(main())


def test_power_validation(tmp_path, win):
    backends = {"power_fn": lambda a, d: None, "cancel_fn": lambda: False, "suspend_fn": lambda h: None,
                "hibernate_fn": lambda: False}
    ctx = make_ctx(tmp_path)
    reg = build_registry(ctx, action_backends=backends)
    r = run(reg.execute("power_action", {"action": "shutdown", "delay_seconds": 10}, YES))
    assert not r.ok and "30 saniye" in r.error
    r = run(reg.execute("power_action", {"action": "hibernate", "delay_seconds": 60}, YES))
    assert not r.ok and "Hazırda bekletme bu bilgisayarda kapalı" in r.error
    r = run(reg.execute("power_action", {"action": "sleep", "delay_seconds": 60}, YES))
    assert not r.ok and "Zamanlayıcı" in r.error      # döngü yok: uyku zamanlanamaz, söylenir
    assert not run(reg.execute("power_action", {"action": "sleep", "delay_seconds": 90000}, YES)).ok
    r = run(reg.execute("cancel_power_action", {}))
    assert r.ok and not r.data["cancelled"]
    assert human_delay(3600) == "1 saat" and human_delay(5400) == "1 saat 30 dakika"
    assert human_delay(45) == "45 saniye" and human_delay(90) == "1 dakika 30 saniye"


def test_scheduler_internal_warning_and_shutdown_cancel_all(tmp_path):
    async def main():
        ctx = make_ctx(tmp_path)
        build_registry(ctx)
        sched, notes, cancelled = ctx.scheduler, [], []
        sched.notify = notes.append
        sched.bind(asyncio.get_running_loop())
        ran = []
        sched.schedule_internal("guc_uyku", "Uyku modu", 0.3, lambda: ran.append(1) or "bitti",
                                warn_before_s=0.0)
        sched.schedule_internal("guc_kapat", "Bilgisayar kapanacak", 60, None, on_cancel=lambda: cancelled.append(1))
        await asyncio.sleep(0.6)
        assert ran == [1] and "bitti" in notes
        sched.cancel_all()          # kapanış: Windows'un kapatma geri sayımına dokunulmaz
        assert cancelled == [] and sched.list() == []

    run(main())


# ====================================================================== GPU sıcaklık uyarısı
def snap(temp):
    g = GpuInfo("RTX 5070 Ti", temp, 99, 1000, 16000, 60) if temp is not None else None
    return SystemSnapshot(time.time(), 10, 8, 32, 25, "C:\\", 100, 1000, 10, None, None, 100, g, None)


def test_gpu_watcher_threshold_duration_cooldown():
    bus, now, notes, spoken = EventBus(), [0.0], [], []
    settings = SistemSettings(gpu_alert_threshold_c=85, gpu_alert_duration_s=30, gpu_alert_cooldown_min=10)
    bus.subscribe(Topics.NOTICE, notes.append)

    async def speak(text):
        spoken.append(text)

    async def main():
        w = GpuTempWatcher(bus, lambda: settings, speak=speak, clock=lambda: now[0])

        def at(t, temp):
            now[0] = t
            bus.publish(Topics.SYSTEM_SNAPSHOT, snap(temp))

        at(0, 90)
        at(20, 91)
        assert notes == []                 # henüz 30 sn olmadı
        at(31, 92)
        assert len(notes) == 1 and "92 °C" in notes[0] and "30 saniyedir" in notes[0]
        await asyncio.sleep(0)
        assert spoken and "92 derece" in spoken[0]
        at(60, 93)
        at(300, 95)
        assert len(notes) == 1             # bekleme süresi (10 dk) dolmadı
        at(400, 80)                        # soğudu
        at(700, 90)
        at(735, 90)
        assert len(notes) == 2
        at(740, None)                      # sensör okunamıyor: uydurma yok
        settings.gpu_alert_enabled = False
        at(2000, 99)
        at(2100, 99)
        assert len(notes) == 2
        w.close()
        assert not bus.has_subscribers(Topics.SYSTEM_SNAPSHOT)

    run(main())


def test_gpu_watcher_hysteresis_and_no_speak():
    bus, now, notes = EventBus(), [0.0], []
    settings = SistemSettings(gpu_alert_threshold_c=85, gpu_alert_duration_s=10, gpu_alert_speak=False)
    bus.subscribe(Topics.NOTICE, notes.append)
    w = GpuTempWatcher(bus, lambda: settings, speak=None, clock=lambda: now[0])
    for t, temp in ((0, 86), (5, 84), (11, 86)):    # 84 eşiğin hemen altı: sayaç sıfırlanmaz
        now[0] = t
        w.on_snapshot(snap(temp))
    assert len(notes) == 1


# ====================================================================== yerel komutlar
INTENTS = [
    ("1 saat sonra bilgisayarı kapat", "power_action", {"action": "shutdown", "delay_seconds": 3600}),
    ("Jarvis, yarım saat sonra bilgisayarı kapat", "power_action", {"action": "shutdown", "delay_seconds": 1800}),
    ("30 dakika sonra bilgisayarı uykuya al", "power_action", {"action": "sleep", "delay_seconds": 1800}),
    ("otuz dakika sonra uykuya al", "power_action", {"action": "sleep", "delay_seconds": 1800}),
    ("bir buçuk saat sonra bilgisayarı kapat", "power_action", {"action": "shutdown", "delay_seconds": 5400}),
    ("2 saat sonra yeniden başlat", "power_action", {"action": "restart", "delay_seconds": 7200}),
    ("10 saniye sonra bilgisayarı kapat", "power_action", {"action": "shutdown", "delay_seconds": 30}),
    ("bilgisayarı uykuya al", "power_action", {"action": "sleep", "delay_seconds": 0}),
    ("uyku moduna al", "power_action", {"action": "sleep", "delay_seconds": 0}),
    ("bilgisayarı hazırda beklet", "power_action", {"action": "hibernate", "delay_seconds": 0}),
    ("kapatmayı iptal et", "cancel_power_action", {}),
    ("uykuya almayı iptal et", "cancel_power_action", {}),
    ("bluetooth'u kapat", "radio_control", {"radio": "bluetooth", "action": "kapat"}),
    ("Wi-Fi'yi kapat", "radio_control", {"radio": "wifi", "action": "kapat"}),
    ("wifi aç", "radio_control", {"radio": "wifi", "action": "ac"}),
    ("bluetooth açık mı", "radio_control", {"radio": "bluetooth", "action": "durum"}),
    ("internet hızımı test et", "internet_speed_test", {}),
    ("hız testi yap", "internet_speed_test", {}),
    ("hız testini iptal et", "cancel_long_operation", {}),
    ("büyük dosyaları bul", "find_space_hogs", {"kind": "buyuk_dosyalar"}),
    ("diskimde yer aç", "find_space_hogs", {"kind": "hepsi", "scope": "izinli_klasorler"}),
    ("kopya dosyaları bul", "find_space_hogs", {"kind": "kopyalar"}),
    ("geçici dosyaları temizle", "clean_temp_files", {"apply": True}),
    ("indirilenleri her gün düzenle", "downloads_routine", {"action": "ac"}),
    ("indirilenleri düzenle", "downloads_routine", {"action": "simdi_calistir"}),
    ("düzenlemeyi geri al", "downloads_routine", {"action": "geri_al"}),
    ("otomatik düzenlemeyi kapat", "downloads_routine", {"action": "kapat"}),
    ("VLC kur", "winget_packages", {"action": "kur", "query": "vlc"}),
    ("Steam'i kur", "winget_packages", {"action": "kur", "query": "steam"}),
    ("7-Zip programını yükle", "winget_packages", {"action": "kur", "query": "7 zip"}),
    ("programları güncelle", "winget_packages", {"action": "tumunu_guncelle"}),
    ("neler güncellenebilir", "winget_packages", {"action": "guncellenebilirler"}),
    ("windows güncellemelerini kontrol et", "windows_update_check", {}),
    ("güncelleme var mı", "windows_update_check", {}),
    ("güncellemeleri yükle", "open_settings", {"page": "guncelleme"}),
]


@pytest.mark.parametrize("text,tool,args", INTENTS, ids=[c[0] for c in INTENTS])
def test_sistem_intents(text, tool, args):
    intent = parse(text)
    assert intent is not None, text
    assert intent.tool == tool, text
    for k, v in args.items():
        assert intent.args[k] == v, (text, k, intent.args)


@pytest.mark.parametrize("text", ["saat 7'ye alarm kur", "1 saat sonra kapat", "sonra bilgisayarı kapat",
                                  "1 saat sonra bilgisayarı kapat ve müziği aç", "bağlantı kur",
                                  "bluetooth ayarlarını aç", "bir saat sonra bana su içmeyi hatırlat"])
def test_sistem_intents_conservative(text):
    intent = parse(text)
    assert intent is None or intent.tool not in ("power_action", "winget_packages", "radio_control"), text


def test_intents_match_schemas(tmp_path, win):
    reg = build_registry(make_ctx(tmp_path))
    from jarvis.tools import schema as js
    for text, tool, _args in INTENTS:
        intent = parse(text)
        js.validate(reg.get(intent.tool).input_schema, intent.args)


def test_parse_delay():
    assert parse_delay("1 saat") == 3600 and parse_delay("yarim saat") == 1800
    assert parse_delay("ceyrek saat") == 900 and parse_delay("2 bucuk saat") == 9000
    assert parse_delay("1 saat 15 dakika") == 4500 and parse_delay("45 sn") == 45
    assert parse_delay("biraz") is None and parse_delay("0 dakika") is None


def test_new_tools_registered_and_strict(tmp_path, win):
    reg = build_registry(make_ctx(tmp_path))
    for name in ("find_space_hogs", "clean_temp_files", "cancel_long_operation", "downloads_routine",
                 "radio_control", "internet_speed_test", "winget_packages", "windows_update_check"):
        spec = reg.get(name)
        assert spec is not None and " / " in spec.description
        props = spec.input_schema["properties"]
        assert not {"command_line", "shell", "script", "cmd"} & set(props)
