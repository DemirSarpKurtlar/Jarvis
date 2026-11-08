"""Windows açılınca başlatma: sahte kayıt defteriyle (her sistemde) ve gerçek kayıt defteriyle (Windows)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from jarvis.config.settings import SettingsStore
from jarvis.integrations import autostart as au
from jarvis.integrations.autostart import (APPROVED_KEY, RUN_KEY, UNSUPPORTED_MESSAGE, Autostart,
                                           AutostartError, command_target, launch_command)

ROOT = Path(__file__).resolve().parents[1]
CMD = '"C:\\Users\\demir\\AppData\\Local\\Programs\\JARVIS\\JARVIS.exe" --baslangic'


class FakeKey:
    def __init__(self, path: str) -> None:
        self.path = path

    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, *exc):  # noqa: ANN002, ANN204
        return False


class FakeWinreg:
    """winreg modülünün Autostart'ın kullandığı kısmı (HKCU, bellekte)."""

    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 0x20019
    KEY_SET_VALUE = 0x0002
    REG_SZ = 1
    REG_BINARY = 3

    def __init__(self) -> None:
        self.keys: dict[str, dict[str, tuple[object, int]]] = {}
        self.fail_writes = False

    def OpenKey(self, root, sub, reserved=0, access=KEY_READ):  # noqa: N802, ANN001, ANN201
        assert root == self.HKEY_CURRENT_USER
        if sub not in self.keys:
            raise FileNotFoundError(sub)
        return FakeKey(sub)

    def CreateKeyEx(self, root, sub, reserved=0, access=KEY_SET_VALUE):  # noqa: N802, ANN001, ANN201
        assert root == self.HKEY_CURRENT_USER
        self.keys.setdefault(sub, {})
        return FakeKey(sub)

    def QueryValueEx(self, key, name):  # noqa: N802, ANN001, ANN201
        try:
            return self.keys[key.path][name]
        except KeyError:
            raise FileNotFoundError(name) from None

    def SetValueEx(self, key, name, reserved, kind, value):  # noqa: N802, ANN001, ANN201
        if self.fail_writes:
            raise PermissionError("erişim engellendi")
        self.keys[key.path][name] = (value, kind)

    def DeleteValue(self, key, name):  # noqa: N802, ANN001, ANN201
        if name not in self.keys.get(key.path, {}):
            raise FileNotFoundError(name)
        del self.keys[key.path][name]


@pytest.fixture
def reg():
    return FakeWinreg()


@pytest.fixture
def store():
    from jarvis.paths import AppPaths

    s = SettingsStore(AppPaths.resolve().settings_file)
    s.load()
    return s


def test_enable_writes_run_value_and_disable_removes_it(reg):
    a = Autostart(reg, command=CMD)
    assert not a.is_enabled()
    a.enable()
    assert reg.keys[RUN_KEY]["JARVIS"] == (CMD, reg.REG_SZ)
    assert a.is_enabled() and a.registered_command() == CMD
    a.disable()
    assert "JARVIS" not in reg.keys[RUN_KEY]
    assert not a.is_enabled()
    a.disable()  # zaten kapalıyken hata vermez


def test_task_manager_disable_flag_is_respected_and_cleared_on_enable(reg):
    a = Autostart(reg, command=CMD)
    a.enable()
    reg.keys.setdefault(APPROVED_KEY, {})["JARVIS"] = (b"\x03" + b"\x00" * 11, reg.REG_BINARY)
    assert a.registered_command() == CMD
    assert a.disabled_by_user() and not a.is_enabled()
    a.enable()  # kullanıcı JARVIS ayarlarından açıkça açtı
    assert "JARVIS" not in reg.keys[APPROVED_KEY]
    assert a.is_enabled()
    reg.keys[APPROVED_KEY]["JARVIS"] = (b"\x02" + b"\x00" * 11, reg.REG_BINARY)  # "etkin" işareti
    assert a.is_enabled()


def test_write_failure_raises_user_friendly_error(reg):
    reg.fail_writes = True
    with pytest.raises(AutostartError) as exc:
        Autostart(reg, command=CMD).enable()
    assert "kayıt defterine yazılamadı" in exc.value.user_message


def test_unsupported_platform(monkeypatch):
    monkeypatch.setattr(au, "is_supported", lambda: False)
    a = Autostart()
    assert not a.supported and not a.is_enabled()
    with pytest.raises(AutostartError) as exc:
        a.enable()
    assert exc.value.user_message == UNSUPPORTED_MESSAGE
    assert "desteklenmiyor" in UNSUPPORTED_MESSAGE


def test_launch_command_frozen_and_source():
    exe = "C:\\Program Files\\JARVIS\\JARVIS.exe"
    assert launch_command(frozen=True, executable=exe) == f'"{exe}" --baslangic'
    src = launch_command(frozen=False)
    assert src.endswith(" --baslangic") and "entry.py" in src
    assert command_target(src) in (sys.executable, str(Path(sys.executable).with_name("pythonw.exe")))
    assert command_target(CMD).endswith("JARVIS.exe")
    assert command_target("C:\\x\\a.exe --baslangic") == "C:\\x\\a.exe"


def test_sync_mirrors_registry_into_settings(reg, store):
    a = Autostart(reg, command=CMD)
    assert store.settings.general.autostart is False
    a.enable()  # ör. kurulum programındaki "Windows açılınca başlat" görevi
    assert a.sync(store, frozen=True) is True
    assert SettingsStore(store.path).load().general.autostart is True
    assert a.sync(store, frozen=True) is False  # değişiklik yok
    a.disable()  # ör. kullanıcı Run değerini elle sildi
    assert a.sync(store, frozen=True) is True
    assert store.settings.general.autostart is False


def test_sync_repairs_entry_pointing_to_missing_exe(reg, store, tmp_path):
    reg.keys[RUN_KEY] = {"JARVIS": (f'"{tmp_path / "eski" / "JARVIS.exe"}" --baslangic', reg.REG_SZ)}
    a = Autostart(reg, command=CMD)
    a.sync(store, frozen=True)
    assert a.registered_command() == CMD
    # Kaynaktan çalışırken (frozen değil) kurulu sürümün kaydına dokunulmaz
    reg.keys[RUN_KEY]["JARVIS"] = ('"C:\\yok\\JARVIS.exe" --baslangic', reg.REG_SZ)
    Autostart(reg, command='"python.exe" entry.py --baslangic').sync(store, frozen=False)
    assert reg.keys[RUN_KEY]["JARVIS"][0] == '"C:\\yok\\JARVIS.exe" --baslangic'


def test_entry_script_runs_from_any_directory(tmp_path):
    """Kaynaktan otomatik başlatma: giriş betiği çalışma klasöründen bağımsız jarvis'i bulur."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONIOENCODING"] = "utf-8"  # Türkçe yardım metni Windows konsol kodlamasında çökmesin
    proc = subprocess.run([sys.executable, str(ROOT / "packaging" / "entry.py"), "--help"],
                          cwd=tmp_path, env=env, capture_output=True, text=True, encoding="utf-8",
                          timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "--baslangic" in proc.stdout and "--kapat" in proc.stdout


@pytest.mark.windows
@pytest.mark.skipif(sys.platform != "win32", reason="gerçek kayıt defteri yalnızca Windows'ta")
def test_real_registry_roundtrip():
    import winreg

    name = f"JARVIS-pytest-{os.getpid()}"  # kullanıcının gerçek JARVIS kaydına dokunma
    cmd = '"C:\\JARVIS-pytest\\JARVIS.exe" --baslangic'
    a = Autostart(command=cmd, value_name=name)
    assert a.supported
    try:
        a.enable()
        assert a.is_enabled()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            assert winreg.QueryValueEx(key, name) == (cmd, winreg.REG_SZ)
        # Görev Yöneticisi'nin "devre dışı" işareti
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, APPROVED_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_BINARY, b"\x03" + b"\x00" * 11)
        assert not a.is_enabled()
        a.enable()
        assert a.is_enabled() and not a.disabled_by_user()
    finally:
        a.disable()
    assert not a.is_enabled() and a.registered_command() is None
