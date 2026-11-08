"""Testleri çalıştırır: Qt kullanan her test dosyası kendi sürecinde, geri kalanlar tek süreçte.

Neden: Tek süreçte yüzlerce pencere açılıp kapanınca Windows'ta Qt nadiren önceki testten kalmış
silinmiş bir nesneye olay iletip çöküyor (access violation). Uygulama tek pencereyle çalıştığından
bu durum gerçek kullanımda oluşmaz; testlerin birbirini etkilememesi için süreçler ayrılır.

Kullanım: python scripts/run_tests_isolated.py [--junit-dir KLASÖR] [-- ek pytest argümanları]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QT_PATTERN = re.compile(r"\b(qapp|qtbot|MainWindow|QApplication|QWidget)\b")


def qt_test_files() -> list[Path]:
    return sorted(p for p in (ROOT / "tests").glob("test_*.py")
                  if QT_PATTERN.search(p.read_text(encoding="utf-8")))


def run(args: list[str]) -> int:
    print("\n$ python -m pytest " + " ".join(args), flush=True)
    return subprocess.call([sys.executable, "-m", "pytest", *args], cwd=ROOT)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--junit-dir")
    ap.add_argument("extra", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    extra = [x for x in a.extra if x != "--"]
    junit = Path(a.junit_dir) if a.junit_dir else None
    if junit:
        junit.mkdir(parents=True, exist_ok=True)

    def jx(name: str) -> list[str]:
        return [f"--junitxml={junit / (name + '.xml')}"] if junit else []

    qt_files = qt_test_files()
    results: dict[str, int] = {}
    ignore = [f"--ignore={p.relative_to(ROOT)}" for p in qt_files]
    results["qt-dışı testler"] = run(["-q", "-ra", *ignore, *jx("pytest-genel"), *extra])
    for p in qt_files:
        results[p.name] = run(["-v", "-ra", str(p.relative_to(ROOT)), *jx("pytest-" + p.stem), *extra])

    print("\n===== Özet =====")
    failed = False
    for name, code in results.items():
        # 5 = bu seçimle test yok (ör. -m models) -> başarısızlık değil
        ok = code in (0, 5)
        failed |= not ok
        print(f"{'TAMAM ' if ok else 'HATA  '} {name} (çıkış {code})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
