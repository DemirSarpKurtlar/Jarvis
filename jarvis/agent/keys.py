"""Tuş adları -> Windows sanal tuş kodları (VK).

Claude'un bilgisayar araç seti xdotool tarzı adlar kullanır: "Return", "ctrl+s", "alt+Tab",
"ctrl+shift+Escape", "Page_Down", "F5". Adlar büyük/küçük harf duyarsızdır. Tek karakterler
(harf, rakam, noktalama) için VK kodu klavye düzeninden bulunur (Türkçe Q klavyede de doğru tuş);
bulunamazsa karakter Unicode olarak gönderilir.
"""

from __future__ import annotations

from dataclasses import dataclass

MODIFIERS: dict[str, int] = {
    "ctrl": 0xA2, "control": 0xA2, "lctrl": 0xA2, "rctrl": 0xA3, "ctrl_l": 0xA2, "ctrl_r": 0xA3,
    "shift": 0xA0, "lshift": 0xA0, "rshift": 0xA1, "shift_l": 0xA0, "shift_r": 0xA1,
    "alt": 0xA4, "lalt": 0xA4, "ralt": 0xA5, "alt_l": 0xA4, "alt_r": 0xA5, "altgr": 0xA5,
    "super": 0x5B, "win": 0x5B, "windows": 0x5B, "cmd": 0x5B, "meta": 0x5B, "super_l": 0x5B, "super_r": 0x5C,
}

NAMED: dict[str, int] = {
    "return": 0x0D, "enter": 0x0D, "kp_enter": 0x0D, "tab": 0x09, "iso_left_tab": 0x09,
    "escape": 0x1B, "esc": 0x1B, "backspace": 0x08, "back_space": 0x08,
    "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "ins": 0x2D, "home": 0x24, "end": 0x23,
    "page_up": 0x21, "pageup": 0x21, "prior": 0x21, "pgup": 0x21,
    "page_down": 0x22, "pagedown": 0x22, "next": 0x22, "pgdn": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "space": 0x20, "spacebar": 0x20, "print": 0x2C, "printscreen": 0x2C, "print_screen": 0x2C,
    "caps_lock": 0x14, "capslock": 0x14, "num_lock": 0x90, "scroll_lock": 0x91, "pause": 0x13,
    "menu": 0x5D, "apps": 0x5D,
    "minus": 0xBD, "plus": 0xBB, "equal": 0xBB, "comma": 0xBC, "period": 0xBE, "slash": 0xBF,
    "semicolon": 0xBA, "apostrophe": 0xDE, "grave": 0xC0, "bracketleft": 0xDB, "bracketright": 0xDD,
    "backslash": 0xDC,
    "audiovolumeup": 0xAF, "audiovolumedown": 0xAE, "audiomute": 0xAD, "audioplay": 0xB3,
    "audionext": 0xB0, "audioprev": 0xB1,
}
for _i in range(1, 25):
    NAMED[f"f{_i}"] = 0x6F + _i
for _i in range(10):
    NAMED[f"kp_{_i}"] = 0x60 + _i

# Genişletilmiş tuşlar (KEYEVENTF_EXTENDEDKEY ile gönderilir)
EXTENDED_VK = frozenset({0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x2C, 0xA3, 0xA5, 0x5B, 0x5C,
                         0x5D, 0x90, 0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB3})


class KeyError_(ValueError):
    """Bilinmeyen tuş adı."""


@dataclass(frozen=True)
class Key:
    vk: int = 0            # 0 ise `char` Unicode olarak gönderilir
    char: str = ""

    @property
    def extended(self) -> bool:
        return self.vk in EXTENDED_VK


@dataclass(frozen=True)
class Combo:
    modifiers: tuple[int, ...]
    key: Key | None          # yalnızca değiştirici tuşlar basılabilir ("shift")


def parse_modifiers(text: str | None) -> tuple[int, ...]:
    """Tıklama/kaydırmada basılı tutulacak değiştiriciler: "ctrl", "ctrl+shift"."""
    if not text:
        return ()
    out: list[int] = []
    for part in text.replace(" ", "").split("+"):
        if not part:
            continue
        vk = MODIFIERS.get(part.lower())
        if vk is None:
            raise KeyError_(f"'{part}' bir değiştirici tuş değil (shift, ctrl, alt, super).")
        if vk not in out:
            out.append(vk)
    return tuple(out)


def parse_combo(text: str, char_vk=None) -> Combo:  # noqa: ANN001
    """"ctrl+shift+Escape" -> Combo. `char_vk(ch) -> int | None` tek karakterin VK kodunu verir."""
    if not isinstance(text, str) or not text.strip():
        raise KeyError_("Tuş adı boş olamaz.")
    raw = text.strip()
    # "ctrl++" gibi: artı tuşunun kendisi
    parts = raw.split("+")
    if raw.endswith("++"):
        parts = raw[:-2].split("+") + ["+"]
    parts = [p.strip() for p in parts if p.strip() != ""]
    if not parts:
        raise KeyError_("Tuş adı boş olamaz.")
    mods: list[int] = []
    for p in parts[:-1]:
        vk = MODIFIERS.get(p.lower())
        if vk is None:
            raise KeyError_(f"'{p}' bir değiştirici tuş değil.")
        mods.append(vk)
    last = parts[-1]
    low = last.lower()
    if low in MODIFIERS:
        mods.append(MODIFIERS[low])
        return Combo(tuple(mods), None)
    if low in NAMED:
        return Combo(tuple(mods), Key(NAMED[low]))
    if len(last) == 1:
        vk = char_vk(last) if char_vk else None
        if vk is None and ("a" <= low <= "z" or "0" <= low <= "9"):
            vk = ord(low.upper())
        return Combo(tuple(mods), Key(vk or 0, last))
    raise KeyError_(f"Bilinmeyen tuş: '{last}'.")
