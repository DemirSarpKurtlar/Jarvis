"""Paketleme dosyalarının tutarlılığı: simge, sürüm, duman testi betiği, spec, Inno Setup, CI."""

import re
import sys
from pathlib import Path

import pytest

from jarvis import __version__
from jarvis.app import WINDOWS_APP_ID
from jarvis.integrations.autostart import STARTUP_FLAG, VALUE_NAME

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import make_icon  # noqa: E402
import smoke_run  # noqa: E402
import version_info  # noqa: E402

sys.path.pop(0)

ICON_DIR = ROOT / "jarvis" / "assets" / "icon"
ISS = ROOT / "packaging" / "jarvis.iss"


def test_committed_icon_has_all_sizes(qapp):
    from PySide6.QtGui import QImage

    sizes = make_icon.parse_ico_sizes((ICON_DIR / "jarvis.ico").read_bytes())
    assert {16, 24, 32, 48, 64, 128, 256} <= set(sizes)
    png = QImage(str(ICON_DIR / "jarvis.png"))
    assert (png.width(), png.height()) == (256, 256)
    assert png.pixelColor(0, 0).alpha() == 0                      # köşeler saydam (yuvarlak kare)
    center = png.pixelColor(128, 128)
    assert center.green() > 200 and center.blue() > 200 and center.red() < 120  # camgöbeği nokta
    assert png.pixelColor(128, 60).red() < 60                     # halka ile nokta arası lacivert


def test_make_icon_script_output_is_readable_by_qt(qapp, tmp_path):
    from PySide6.QtGui import QImageReader

    ico, png = make_icon.make(tmp_path)
    assert make_icon.parse_ico_sizes(ico.read_bytes()) == list(make_icon.ICO_SIZES)
    reader = QImageReader(str(ico))
    assert reader.imageCount() == len(make_icon.ICO_SIZES)
    widths = []
    for i in range(reader.imageCount()):
        reader.jumpToImage(i)
        img = reader.read()
        assert not img.isNull()
        widths.append(img.width())
    assert sorted(widths) == sorted(make_icon.ICO_SIZES)
    # BMP girdilerinde satır sırası doğru mu: 16 px'te merkez camgöbeği, köşe saydam
    reader = QImageReader(str(ico))
    reader.jumpToImage(0)
    small = reader.read()
    assert small.pixelColor(0, 0).alpha() == 0
    assert small.pixelColor(8, 8).green() > 150
    assert png.exists()


def test_version_info():
    assert version_info.read_version() == __version__
    assert version_info.numeric_version("1.0.0.dev1") == (1, 0, 0, 0)
    assert version_info.numeric_version("2.3") == (2, 3, 0, 0)
    assert version_info.numeric_version("1.2.3.4.5rc1") == (1, 2, 3, 4)
    with pytest.raises(ValueError):
        version_info.numeric_version("dev")


def test_smoke_log_checker():
    ok = ("2026-11-07 10:00:00,000 INFO     [MainThread] jarvis: Arayüz hazır (300 ms)\n"
          "2026-11-07 10:00:01,000 WARNING  [MainThread] qt: uyarı olabilir\n"
          "2026-11-07 10:00:05,000 INFO     [MainThread] jarvis: JARVIS kapatıldı\n")
    assert smoke_run.check_log(ok, []) == []
    assert smoke_run.check_log(ok, ["yok"]) == ["logda bulunamadı: 'yok'"]
    bad = ok + "2026-11-07 10:00:06,000 ERROR    [MainThread] x: patladı\nTraceback (most recent call last):\n"
    problems = smoke_run.check_log(bad, [])
    assert any("hata satırı" in p for p in problems) and "logda Traceback var" in problems
    # Mesaj içinde geçen "ERROR" kelimesi seviye değildir
    assert smoke_run.check_log(ok + "2026-11-07 10:00:07,000 INFO     [M] x: ERROR kodu yok\n", []) == []


def test_smoke_run_end_to_end(jarvis_home, monkeypatch):
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    log = jarvis_home / "logs" / "jarvis.log"
    assert smoke_run.main(["--log", str(log), "--", sys.executable, "-m", "jarvis", "--smoke-test", "1"]) == 0
    assert smoke_run.main(["--log", str(log), "--", sys.executable, "-c", "raise SystemExit(3)"]) == 1


def test_spec_is_valid_python_and_includes_assets():
    src = (ROOT / "packaging" / "jarvis.spec").read_text(encoding="utf-8")
    compile(src, "jarvis.spec", "exec")
    assert '"jarvis/assets"' in src and "jarvis.ico" in src
    assert "console=False" in src and "keyring.backends.Windows" in src
    assert (ROOT / "packaging" / "entry.py").exists()


def test_inno_script_matches_app_constants():
    raw = ISS.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "jarvis.iss UTF-8 BOM ile kaydedilmeli"
    iss = raw.decode("utf-8-sig")
    assert re.search(r"^PrivilegesRequired=lowest", iss, re.MULTILINE)
    assert re.search(r"^AppId=\{\{[0-9A-F-]{36}\}", iss, re.MULTILINE)
    assert "compiler:Languages\\Turkish.isl" in iss
    assert f'#define AppUserModelID "{WINDOWS_APP_ID}"' in iss
    assert f'#define AppName "{VALUE_NAME}"' in iss
    assert STARTUP_FLAG in iss and "--kapat" in iss and "--anahtari-sil" in iss
    assert "DELETEUSERDATA" in iss and "uninsdeletevalue" in iss
    assert (ROOT / "packaging" / "build.ps1").read_bytes().startswith(b"\xef\xbb\xbf")


def test_ci_workflow_contract():
    wf = (ROOT / ".github" / "workflows" / "windows.yml").read_text(encoding="utf-8")
    for needle in ("windows-latest", "actions/checkout@v4", "actions/setup-python@v5", "actions/cache@v4",
                   "actions/upload-artifact@v4", "-m models", "LASTEXITCODE -eq 5", "--smoke-test",
                   "choco install innosetup", "/VERYSILENT", "unins000.exe"):
        assert needle in wf, needle
    try:
        import yaml
    except ImportError:
        return
    data = yaml.safe_load(wf)
    assert set(data["jobs"]) == {"linux", "windows"}
