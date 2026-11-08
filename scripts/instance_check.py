"""Tek örnek davranışını uçtan uca doğrular (CI ve elle kontrol).

    python scripts/instance_check.py --log LOG_DOSYASI -- python -m jarvis
    python scripts/instance_check.py --log "%LOCALAPPDATA%\\JARVIS\\Logs\\jarvis.log" -- "C:\\...\\JARVIS.exe"

Adımlar:
1. JARVIS'i başlatır ve "Arayüz hazır" satırını bekler.
2. İkinci kez başlatır: ikinci süreç hemen 0 koduyla çıkmalı, ilki "activate" mesajını almalı.
3. `--kapat` ile çalışan örneği kapatır (kurulum/kaldırma programının kullandığı yol):
   `--kapat` 0 ile dönmeli, ilk süreç 0 koduyla kapanmalı.
4. Bu çalıştırmada eklenen log kısmında ERROR/CRITICAL/Traceback olmamalı.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_run import check_log  # noqa: E402


def _read_from(path: Path, offset: int) -> str:
    if not path.exists():
        return ""
    with open(path, "rb") as f:
        f.seek(offset if path.stat().st_size >= offset else 0)
        return f.read().decode("utf-8", "replace")


def _wait_for(path: Path, offset: int, needle: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if needle in _read_from(path, offset):
            return True
        time.sleep(0.2)
    return False


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and not stream.isatty():
            stream.reconfigure(encoding="utf-8", errors="replace")
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" not in argv or argv.index("--") == len(argv) - 1:
        print("Kullanım: instance_check.py --log DOSYA [--timeout SN] -- KOMUT...", file=sys.stderr)
        return 2
    split = argv.index("--")
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", required=True, type=Path)
    ap.add_argument("--timeout", type=float, default=60)
    args = ap.parse_args(argv[:split])
    cmd = argv[split + 1:]
    log_path = Path(os.path.expandvars(str(args.log)))
    offset = log_path.stat().st_size if log_path.exists() else 0
    problems: list[str] = []

    print(f"[örnek] 1) ilk örnek başlatılıyor: {' '.join(cmd)}")
    # Emniyet: bir şey ters giderse ilk örnek en geç bu süre sonra kendini kapatır
    primary = subprocess.Popen([*cmd, "--smoke-test", str(args.timeout * 2)])
    try:
        if not _wait_for(log_path, offset, "Arayüz hazır", args.timeout):
            problems.append("ilk örnek 'Arayüz hazır' yazmadı")
        else:
            print("[örnek] 2) ikinci açılış")
            t0 = time.perf_counter()
            second = subprocess.run(cmd, timeout=args.timeout)
            took = time.perf_counter() - t0
            print(f"[örnek]    ikinci süreç {second.returncode} koduyla {took:.1f} sn'de çıktı")
            if second.returncode != 0:
                problems.append(f"ikinci açılış çıkış kodu {second.returncode}")
            if not _wait_for(log_path, offset, "İkinci açılıştan mesaj: activate", 10):
                problems.append("ilk örnek 'activate' mesajını almadı")
            if primary.poll() is not None:
                problems.append("ikinci açılış ilk örneği kapattı")

            print("[örnek] 3) --kapat")
            close = subprocess.run([*cmd, "--kapat"], timeout=args.timeout)
            print(f"[örnek]    --kapat çıkış kodu {close.returncode}")
            if close.returncode != 0:
                problems.append(f"--kapat çıkış kodu {close.returncode}")
        try:
            rc = primary.wait(timeout=args.timeout)
            print(f"[örnek]    ilk örnek {rc} koduyla kapandı")
            if rc != 0:
                problems.append(f"ilk örnek çıkış kodu {rc}")
        except subprocess.TimeoutExpired:
            problems.append("ilk örnek --kapat sonrası kapanmadı")
    finally:
        if primary.poll() is None:
            primary.kill()
            primary.wait()

    text = _read_from(log_path, offset)
    print(f"--- log (bu çalıştırma) ---\n{text}--- log sonu ---")
    problems += check_log(text, ["JARVIS zaten çalışıyor", "--kapat: çalışan JARVIS kapandı"])
    if problems:
        print("[örnek] BAŞARISIZ:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("[örnek] TAMAM")
    return 0


if __name__ == "__main__":
    sys.exit(main())
