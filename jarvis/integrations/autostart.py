"""Windows açılınca JARVIS'i başlatma.

Kayıt defterinde kullanıcıya özel `HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run`
anahtarına `JARVIS` adlı bir değer yazılır. Yönetici izni gerekmez ve
yalnızca bu kullanıcıyı etkiler. Komut satırına `--baslangic` eklenir:
JARVIS bu bayrakla açılınca pencereyi göstermeden bildirim alanında bekler.

Görev Yöneticisi > Başlangıç uygulamaları'nda "Devre dışı bırak" denirse
Windows kaydı silmez, `...\\Explorer\\StartupApproved\\Run` altına bir
"kapalı" işareti koyar. Bu yüzden durum okunurken o işarete de bakılır ve
kullanıcı JARVIS ayarlarından açınca işaret kaldırılır.

Gerçek durumun kaynağı kayıt defteridir; `settings.general.autostart`
açılışta buna göre eşitlenir (kurulum programı da aynı değeri yazabilir).
Windows dışındaki sistemlerde desteklenmez.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from jarvis import APP_NAME
from jarvis.core.errors import JarvisError

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
VALUE_NAME = APP_NAME
STARTUP_FLAG = "--baslangic"
UNSUPPORTED_MESSAGE = "Windows açılınca başlatma bu işletim sisteminde desteklenmiyor."

_ROOT = Path(__file__).resolve().parents[2]


class AutostartError(JarvisError):
    pass


def is_supported() -> bool:
    return sys.platform == "win32"


def launch_command(frozen: bool | None = None, executable: str | None = None) -> str:
    """Kayıt defterine yazılacak komut.

    Kurulu (PyInstaller ile paketlenmiş) sürümde exe'nin kendisi; kaynaktan
    çalışırken konsol penceresi açmayan `pythonw.exe` ile giriş betiği.
    """
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    exe = Path(executable or sys.executable)
    if frozen:
        return f'"{exe}" {STARTUP_FLAG}'
    pythonw = exe.with_name("pythonw.exe")
    if not pythonw.exists():
        pythonw = exe
    entry = _ROOT / "packaging" / "entry.py"
    return f'"{pythonw}" "{entry}" {STARTUP_FLAG}'


def command_target(command: str) -> str:
    """Komut satırındaki program yolunu çıkarır (tırnaklı ya da tırnaksız)."""
    command = command.strip()
    if command.startswith('"'):
        end = command.find('"', 1)
        return command[1:end] if end > 0 else command[1:]
    return command.split(" ", 1)[0]


class Autostart:
    """Run anahtarı üzerinden otomatik başlatma.

    `winreg_module` testlerde sahte bir kayıt defteri vermek içindir.
    """

    def __init__(self, winreg_module: Any | None = None, command: str | None = None,
                 value_name: str = VALUE_NAME) -> None:
        self._reg = winreg_module
        if self._reg is None and is_supported():
            import winreg

            self._reg = winreg
        self.command = command or launch_command()
        self.value_name = value_name

    @property
    def supported(self) -> bool:
        return self._reg is not None

    def _require(self) -> Any:
        if self._reg is None:
            raise AutostartError("autostart desteklenmiyor", UNSUPPORTED_MESSAGE)
        return self._reg

    def _read(self, subkey: str) -> Any | None:
        reg = self._require()
        try:
            with reg.OpenKey(reg.HKEY_CURRENT_USER, subkey, 0, reg.KEY_READ) as key:
                value, _kind = reg.QueryValueEx(key, self.value_name)
                return value
        except FileNotFoundError:
            return None

    def _delete(self, subkey: str) -> None:
        reg = self._require()
        try:
            with reg.OpenKey(reg.HKEY_CURRENT_USER, subkey, 0, reg.KEY_SET_VALUE) as key:
                reg.DeleteValue(key, self.value_name)
        except FileNotFoundError:
            pass

    def registered_command(self) -> str | None:
        value = self._read(RUN_KEY)
        return value if isinstance(value, str) and value.strip() else None

    def disabled_by_user(self) -> bool:
        """Görev Yöneticisi'nden devre dışı bırakılmış mı (ilk baytın son biti 1)?"""
        flag = self._read(APPROVED_KEY)
        return isinstance(flag, (bytes, bytearray)) and len(flag) > 0 and bool(flag[0] & 1)

    def is_enabled(self) -> bool:
        if not self.supported:
            return False
        return self.registered_command() is not None and not self.disabled_by_user()

    def enable(self) -> None:
        reg = self._require()
        try:
            with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_SET_VALUE) as key:
                reg.SetValueEx(key, self.value_name, 0, reg.REG_SZ, self.command)
            # Kullanıcı açıkça istedi: Görev Yöneticisi'ndeki eski "kapalı" işareti kalksın
            self._delete(APPROVED_KEY)
        except OSError as exc:
            raise AutostartError(f"Run anahtarı yazılamadı: {exc}",
                                 "Windows açılınca başlatma açılamadı (kayıt defterine yazılamadı).") from exc
        log.info("Windows açılınca başlatma açıldı: %s", self.command)

    def disable(self) -> None:
        self._require()
        try:
            self._delete(RUN_KEY)
            self._delete(APPROVED_KEY)
        except OSError as exc:
            raise AutostartError(f"Run değeri silinemedi: {exc}",
                                 "Windows açılınca başlatma kapatılamadı (kayıt defterine yazılamadı).") from exc
        log.info("Windows açılınca başlatma kapatıldı")

    def set_enabled(self, enabled: bool) -> None:
        self.enable() if enabled else self.disable()

    def sync(self, store: Any, frozen: bool | None = None) -> bool:
        """Ayardaki değeri kayıt defterindeki gerçek duruma eşitler.

        Kurulu sürüm taşındıysa (kayıttaki exe artık yoksa) yolu günceller.
        Ayar değiştiyse True döner. Hata uygulamanın açılışını engellemez.
        """
        if not self.supported:
            return False
        try:
            enabled = self.is_enabled()
            frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
            current = self.registered_command()
            if enabled and frozen and current and current != self.command \
                    and not Path(command_target(current)).exists():
                log.info("Başlangıç kaydı eski bir konumu gösteriyordu, güncelleniyor")
                self.enable()
        except (OSError, AutostartError):
            log.warning("Windows açılınca başlatma durumu okunamadı", exc_info=True)
            return False
        settings = store.settings
        if settings.general.autostart == enabled:
            return False
        settings.general.autostart = enabled
        store.save()
        log.info("Başlangıç ayarı kayıt defterine eşitlendi: %s", "açık" if enabled else "kapalı")
        return True
