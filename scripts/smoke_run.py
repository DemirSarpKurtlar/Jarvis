"""JARVIS'i kısa süre çalıştırıp temiz açılıp kapandığını doğrular (CI ve elle kontrol).

Kullanım:
    python scripts/smoke_run.py --log LOG_DOSYASI -- python -m jarvis --smoke-test 5
    python scripts/smoke_run.py --log "%LOCALAPPDATA%\\JARVIS\\Logs\\jarvis.log" -- "C:\\...\\JARVIS.exe" --smoke-test 5

Başarı koşulları:
* süreç zaman aşımına uğramadan 0 koduyla biter,
* log dosyasının BU çalıştırmada eklenen kısmında "Arayüz hazır" ve "JARVIS kapatıldı" geçer,
* aynı kısımda ERROR/CRITICAL seviyesinde satır ya da "Traceback" yoktur,
* `--expect` ile verilen her metin geçer.

Pencereli (konsolsuz) exe'ler için de çalışır: subprocess süreç bitene kadar bekler.
Log dosyası önceki çalıştırmalardan dolu olabilir; yalnızca yeni eklenen kısım incelenir.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REQUIRED = ("Arayüz hazır", "JARVIS kapatıldı")
BAD_LINE = re.compile(r"^\S+ \S+ (ERROR|CRITICAL)\s")


def check_log(text: str, expect: list[str]) -> list[str]:
    problems = []
    for needle in (*REQUIRED, *expect):
        if needle not in text:
            problems.append(f"logda bulunamadı: {needle!r}")
    for line in text.splitlines():
        if BAD_LINE.match(line):
            problems.append(f"hata satırı: {line.strip()}")
    if "Traceback" in text:
        problems.append("logda Traceback var")
    return problems


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        # CI günlüğü UTF-8; Windows'ta varsayılan cp1252 Türkçe karakterde çökmesin
        if hasattr(stream, "reconfigure") and not stream.isatty():
            stream.reconfigure(encoding="utf-8", errors="replace")
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" not in argv:
        print("Kullanım: smoke_run.py --log DOSYA [--timeout SN] [--expect METİN]... -- KOMUT...", file=sys.stderr)
        return 2
    split = argv.index("--")
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", required=True, type=Path)
    ap.add_argument("--timeout", type=float, default=120)
    ap.add_argument("--expect", action="append", default=[])
    args = ap.parse_args(argv[:split])
    cmd = argv[split + 1:]
    if not cmd:
        print("Çalıştırılacak komut verilmedi", file=sys.stderr)
        return 2

    log_path = Path(os.path.expandvars(str(args.log)))
    offset = log_path.stat().st_size if log_path.exists() else 0
    print(f"[smoke] komut: {' '.join(cmd)}")
    print(f"[smoke] log:   {log_path} (önceki boyut {offset} bayt)")
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=args.timeout)
    except subprocess.TimeoutExpired as exc:
        print(f"[smoke] HATA: {args.timeout:.0f} sn içinde kapanmadı")
        for name, data in (("stdout", exc.stdout), ("stderr", exc.stderr)):
            if data:
                print(f"--- {name} ---\n{data if isinstance(data, str) else data.decode('utf-8', 'replace')}")
        return 1
    elapsed = time.perf_counter() - t0
    print(f"[smoke] çıkış kodu {proc.returncode}, süre {elapsed:.1f} sn")
    if proc.stdout:
        print(f"--- stdout ---\n{proc.stdout}")
    if proc.stderr:
        print(f"--- stderr ---\n{proc.stderr}")

    problems = []
    if proc.returncode != 0:
        problems.append(f"çıkış kodu {proc.returncode}")
    text = ""
    if log_path.exists():
        with open(log_path, "rb") as f:
            # Log döndürüldüyse (boyut küçüldüyse) baştan oku
            f.seek(offset if log_path.stat().st_size >= offset else 0)
            text = f.read().decode("utf-8", "replace")
    else:
        problems.append("log dosyası oluşmadı")
    print(f"--- log (bu çalıştırma) ---\n{text}--- log sonu ---")
    problems += check_log(text, args.expect)
    if problems:
        print("[smoke] BAŞARISIZ:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("[smoke] TAMAM")
    return 0


if __name__ == "__main__":
    sys.exit(main())
