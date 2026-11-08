"""Sürüm bilgisini `jarvis/__init__.py`'den okur (paketleme ve kurulum için).

    python scripts/version_info.py            -> 1.0.0.dev1   (Inno AppVersion)
    python scripts/version_info.py --numeric  -> 1.0.0.0      (exe/kurulum dosya sürümü, 4 sayı)

`jarvis` paketini içe aktarmaz; düz metin olarak okur, böylece bağımlılıklar
kurulu olmadan da çalışır.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

INIT = Path(__file__).resolve().parents[1] / "jarvis" / "__init__.py"


def read_version(path: Path = INIT) -> str:
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', path.read_text(encoding="utf-8"), re.MULTILINE)
    if not m:
        raise ValueError(f"__version__ bulunamadı: {path}")
    return m.group(1)


def numeric_version(version: str) -> tuple[int, int, int, int]:
    """PEP 440 sürümünün yayın kısmından Windows'un istediği 4 sayılı sürüm (1.0.0.dev1 -> 1.0.0.0)."""
    m = re.match(r"^\s*v?(\d+(?:\.\d+)*)", version)
    if not m:
        raise ValueError(f"sürüm sayıyla başlamıyor: {version}")
    nums = [int(x) for x in m.group(1).split(".")][:4]
    nums += [0] * (4 - len(nums))
    if any(n > 65535 for n in nums):
        raise ValueError(f"Windows sürüm parçaları 65535'i aşamaz: {version}")
    return tuple(nums)  # type: ignore[return-value]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="JARVIS sürümünü yazdırır")
    ap.add_argument("--numeric", action="store_true", help="4 sayılı Windows sürümü")
    args = ap.parse_args(argv)
    v = read_version()
    print(".".join(map(str, numeric_version(v))) if args.numeric else v)
    return 0


if __name__ == "__main__":
    sys.exit(main())
