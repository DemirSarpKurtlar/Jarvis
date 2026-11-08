"""Sabit PowerShell betikleri (yalnızca Windows).

Serbest PowerShell YOKTUR. Betik metinleri bu kod tabanında sabittir; modelden ya da
kullanıcıdan gelen hiçbir metin betiğe eklenmez. Tek istisna `render()`: yer tutuculara
yalnızca önceden tanımlı bir izin listesindeki (enum) değerler konabilir.

Betik `-EncodedCommand` (UTF-16LE base64) ile verilir: tırnak/kaçış sorunu ve komut satırı
yorumlaması olmaz. `shell=False`, profil yüklenmez, etkileşim kapalıdır.
"""

from __future__ import annotations

import base64
import logging
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from jarvis.tools.registry import NotSupportedError, ToolError

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000
MAX_OUTPUT_CHARS = 200_000

# Betiklerin çıktısı UTF-8 JSON olsun
PREAMBLE = ("$ProgressPreference='SilentlyContinue'; $ErrorActionPreference='Stop'; "
            "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;\n")


def powershell_exe() -> str:
    root = os.environ.get("SystemRoot", r"C:\Windows")
    return str(Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")


def render(template: str, allowed: Mapping[str, frozenset[str] | set[str]], **values: str) -> str:
    """Şablondaki `{ad}` yer tutucularını yalnızca izinli değerlerle doldurur."""
    out = template
    for key, val in values.items():
        if key not in allowed or val not in allowed[key]:
            raise ToolError("betik değeri", "Geçersiz bir değer istendi; işlem yapılmadı.")
        out = out.replace("{" + key + "}", val)
    return out


def encode(script: str) -> str:
    return base64.b64encode((PREAMBLE + script).encode("utf-16-le")).decode("ascii")


def run_script(script: str, timeout_s: float = 60.0) -> tuple[int, str, str]:  # pragma: no cover - Windows
    """Sabit betiği çalıştırır: (çıkış kodu, stdout, stderr)."""
    if not IS_WINDOWS:
        raise NotSupportedError("Bu özellik yalnızca Windows'ta çalışır.")
    exe = powershell_exe()
    if not Path(exe).is_file():
        raise ToolError("powershell yok", "Windows PowerShell bu sistemde bulunamadı.")
    argv = [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encode(script)]
    try:
        cp = subprocess.run(argv, shell=False, capture_output=True, timeout=timeout_s,
                            creationflags=CREATE_NO_WINDOW, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        raise ToolError("zaman aşımı", f"İşlem {timeout_s:.0f} saniyede bitmedi.") from e
    out = (cp.stdout or b"").decode("utf-8", errors="replace")[:MAX_OUTPUT_CHARS]
    err = (cp.stderr or b"").decode("utf-8", errors="replace")[:4000]
    return cp.returncode, out.strip(), err.strip()
