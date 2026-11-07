"""Türkçe sayı, para ve süre biçimleri."""

from __future__ import annotations


def tr_num(x: float, digits: int = 1) -> str:
    """9.8 -> '9,8'; binlik ayırıcı nokta: 41200 -> '41.200'."""
    s = f"{x:,.{digits}f}"
    return s.replace(",", "_").replace(".", ",").replace("_", ".")


def usd(x: float) -> str:
    """Küçük tutarlarda daha fazla basamak: $0,0003 / $0,04 / $1,12."""
    if x == 0:
        return "$0"
    if abs(x) < 0.01:
        return "$" + tr_num(x, 4)
    return "$" + tr_num(x, 2)


def tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{tr_num(n / 1_000_000, 1)} milyon"
    if n >= 1_000:
        return f"{tr_num(n / 1_000, 1)} bin"
    return str(n)
