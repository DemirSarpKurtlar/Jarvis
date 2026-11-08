"""Bilgisayar kullanan ajanın güvenlik korumaları.

Model ne derse desin bu kurallar kodda uygulanır:

1. Hassas pencere: UAC (consent.exe), Windows kimlik bilgisi penceresi, Windows Güvenliği, oturum
   açma ekranı, komut satırı/terminal/kayıt defteri, "Çalıştır" kutusu ve banka/ödeme sayfaları
   öndeyse oturum durur. Ekran görüntülerindeki talimatlarla (istem enjeksiyonu) yönetici işlemi
   ya da komut çalıştırma yapılamasın diye.
2. Gizli bilgi: parola, API anahtarı, kart numarası, IBAN gibi görünen metin yazılmaz; parola/giriş
   penceresine boşluksuz metin yazılmaz. JARVIS saklı kimlik bilgilerini hiçbir zaman ajana vermez.
3. Tehlikeli kısayollar: Win+R (Çalıştır), Win+X, Ctrl+Alt+Del, Ctrl+Shift+Esc yok.
4. Oyun: maç içinde oynamak (nişan, hareket, ateş) hile sayılır ve hesap kapatılır. Hedef böyle
   görünüyorsa oturum hiç başlamaz; oyun penceresi öndeyken basılı tutma, sürükleme ve hareket
   tuşları engellenir. Yalnızca başlatıcı/menü/lobi gezintisi yapılır.
"""

from __future__ import annotations

import re

from jarvis.agent.backends import ForegroundInfo
from jarvis.agent.keys import Combo
from jarvis.tools.text import fold

# ---------- hassas pencereler ----------
SENSITIVE_PROCESSES: dict[str, str] = {
    "consent.exe": "Kullanıcı Hesabı Denetimi (UAC) penceresi",
    "credentialuibroker.exe": "Windows kimlik bilgisi penceresi",
    "logonui.exe": "Windows oturum açma ekranı",
    "lockapp.exe": "kilit ekranı",
    "sechealthui.exe": "Windows Güvenliği",
    "securityhealthhost.exe": "Windows Güvenliği",
    "cmd.exe": "komut istemi",
    "powershell.exe": "PowerShell",
    "pwsh.exe": "PowerShell",
    "windowsterminal.exe": "Windows Terminal",
    "wt.exe": "Windows Terminal",
    "conhost.exe": "konsol penceresi",
    "openconsole.exe": "konsol penceresi",
    "regedit.exe": "Kayıt Defteri Düzenleyicisi",
    "mmc.exe": "yönetim konsolu",
    "taskmgr.exe": "Görev Yöneticisi",
    "keepass.exe": "parola yöneticisi", "keepassxc.exe": "parola yöneticisi",
    "1password.exe": "parola yöneticisi", "bitwarden.exe": "parola yöneticisi",
}
SENSITIVE_CLASSES: dict[str, str] = {
    "credential dialog xaml host": "Windows kimlik bilgisi penceresi",
    "$$$secure uap dummy window class for interim dialog": "Kullanıcı Hesabı Denetimi (UAC) penceresi",
    "consolewindowclass": "konsol penceresi",
    "cascadia_hosting_window_class": "Windows Terminal",
}
# Pencere başlığında (katlanmış) geçerse: banka, ödeme, satın alma, güvenlik ayarları
_SENSITIVE_TITLE = re.compile(
    r"\b(banka\w*|bank|bankasi|internet subesi|mobil sube|ziraat|garanti bbva|isbank|is bankasi|akbank|"
    r"yapi kredi|vakifbank|halkbank|qnb|denizbank|enpara|papara|paypal|iyzico|odeme\w*|payment|checkout|"
    r"kredi karti|kart bilgi\w*|satin al\w*|purchase|e devlet|edevlet|turkiye gov|windows guvenligi|"
    r"windows security|kullanici hesabi denetimi|user account control)\b")
_RUN_DIALOG = re.compile(r"^(calistir|run)$")


def sensitive_reason(fg: ForegroundInfo) -> str | None:
    """Ön plandaki pencere hassassa Türkçe neden, değilse None."""
    proc = (fg.process or "").lower()
    if proc in SENSITIVE_PROCESSES:
        return SENSITIVE_PROCESSES[proc]
    cls = (fg.class_name or "").lower()
    if cls in SENSITIVE_CLASSES:
        return SENSITIVE_CLASSES[cls]
    title = fold(fg.title or "")
    if cls == "#32770" and _RUN_DIALOG.match(title):
        return "Çalıştır kutusu"
    if title and _SENSITIVE_TITLE.search(title):
        return f"hassas bir pencere ('{(fg.title or '')[:60]}')"
    return None


# ---------- gizli bilgi ----------
_KEY_PATTERNS = re.compile(
    r"(sk-ant-[A-Za-z0-9_\-]{8,}|sk-[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{30,}|xox[abpr]-[A-Za-z0-9\-]{10,}|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}\s?[A-Z0-9]{1,4}\b")
_LOGIN_TITLE = re.compile(r"\b(parola|sifre|password|passcode|pin|giris yap|oturum ac|sign in|log in|login|"
                          r"kimlik dogrula\w*|2fa|dogrulama kodu|verification code)\b")


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def _looks_random_token(word: str) -> bool:
    """Uzun, boşluksuz, harf + rakam (+ simge) karışık: büyük olasılıkla parola ya da anahtar."""
    if len(word) < 16 or " " in word:
        return False
    classes = sum(bool(re.search(p, word)) for p in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]"))
    return classes >= 3 and not re.fullmatch(r"https?://\S+", word)


def secret_reason(text: str, fg: ForegroundInfo | None = None) -> str | None:
    """Yazılacak metin gizli bilgiye benziyorsa Türkçe neden."""
    if _KEY_PATTERNS.search(text):
        return "metin bir API anahtarı ya da gizli anahtar gibi görünüyor"
    for m in re.finditer(r"(?:\d[ \-]?){13,19}", text):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return "metin bir kart numarası gibi görünüyor"
    if _IBAN.search(text.upper().replace("-", " ")):
        return "metin bir IBAN gibi görünüyor"
    if any(_looks_random_token(w) for w in text.split()):
        return "metin bir parola ya da erişim anahtarı gibi görünüyor"
    if fg is not None and _LOGIN_TITLE.search(fold(fg.title or "")) and text.strip() and " " not in text.strip():
        return "parola ya da giriş penceresine kimlik bilgisi yazmam"
    return None


# ---------- kısayollar ----------
VK_CTRL, VK_SHIFT, VK_ALT, VK_WIN = (0xA2, 0xA3), (0xA0, 0xA1), (0xA4, 0xA5), (0x5B, 0x5C)


def key_reason(combo: Combo) -> str | None:
    mods = set(combo.modifiers)
    has = lambda group: bool(mods & set(group))  # noqa: E731
    vk = combo.key.vk if combo.key else 0
    ch = (combo.key.char.lower() if combo.key and combo.key.char else "")
    if has(VK_WIN) and (vk in (0x52, 0x58) or ch in ("r", "x")):
        return "Win+R / Win+X (komut çalıştırma ve yönetici menüsü) kullanmam"
    if has(VK_CTRL) and has(VK_ALT) and vk == 0x2E:
        return "Ctrl+Alt+Del kullanmam"
    if has(VK_CTRL) and has(VK_SHIFT) and vk == 0x1B:
        return "Görev Yöneticisi kısayolunu kullanmam"
    return None


# ---------- oyunlar ----------
# Bilinen oyun süreçleri (küçük harf). Unreal oyunlarının çoğu "-win64-shipping.exe" ile biter.
GAME_PROCESSES = frozenset({
    "valorant-win64-shipping.exe", "valorant.exe", "league of legends.exe", "cs2.exe", "csgo.exe",
    "fortniteclient-win64-shipping.exe", "r5apex.exe", "r5apex_dx12.exe", "overwatch.exe",
    "rocketleague.exe", "dota2.exe", "gta5.exe", "gta5_enhanced.exe", "eldenring.exe", "cod.exe",
    "minecraft.exe", "rainbowsix.exe", "pubg.exe", "tslgame.exe", "destiny2.exe",
    "marvel-win64-shipping.exe", "fc25.exe", "fc26.exe",
})
# Vanguard çekirdek hile önleyicisi: yazılımsal girdiyi yok sayabilir
VANGUARD_PROCESSES = frozenset({"valorant-win64-shipping.exe", "valorant.exe", "league of legends.exe"})


def is_game(fg: ForegroundInfo) -> bool:
    p = (fg.process or "").lower()
    return p in GAME_PROCESSES or p.endswith("-win64-shipping.exe")


def is_vanguard_game(fg: ForegroundInfo) -> bool:
    return (fg.process or "").lower() in VANGUARD_PROCESSES


# Maç içinde karakteri oynatan tuşlar: hareket, zıplama, eğilme, yetenek, silah, etkileşim
_GAME_KEYS = frozenset({ord(c) for c in "WASDQECXZFGRV12345"} | {0x20, 0xA0, 0xA1, 0xA2, 0xA3, 0x10, 0x11})
GAME_BLOCKED_MEMBERS = frozenset({"hold_key", "left_mouse_down", "left_mouse_up", "left_click_drag"})
IN_GAME_REFUSAL = ("Oyun penceresinde yalnızca menü ve lobi gezintisi yapıyorum. Maç içinde nişan alma, hareket "
                   "etme ya da ateş etme hile sayılır ve hesabının kapatılmasına yol açar; bunu yapmam.")


def game_action_reason(member: str, fg: ForegroundInfo, combo: Combo | None = None) -> str | None:
    if not is_game(fg):
        return None
    if member in GAME_BLOCKED_MEMBERS:
        return IN_GAME_REFUSAL
    if member == "key" and combo is not None and combo.key is not None and not combo.modifiers:
        vk = combo.key.vk or (ord(combo.key.char.upper()) if combo.key.char else 0)
        if vk in _GAME_KEYS:
            return IN_GAME_REFUSAL
    if member == "key" and combo is not None and combo.key is None:
        return IN_GAME_REFUSAL   # yalnızca shift/ctrl: eğilme/koşma
    return None


_GAMEPLAY_GOAL = re.compile(
    r"\b(nisan al\w*|aim|ates et\w*|vur|vurarak|vurmak|vursun|oldur\w*|kill\w*|headshot|hareket et\w*|yuru|"
    r"yuruyerek|yurumek|kos|kosarak|kosmak|zipla\w*|"
    r"benim yerime oyna\w*|yerime oyna\w*|maci oyna\w*|mac oyna\w*|oyunu oyna\w*|turu kazan\w*|maci kazan\w*|"
    r"oyunu kazan\w*|rank kas\w*|elo kas\w*|kasar misin|farm\w*|bot gibi|otomatik oyna\w*|"
    r"spike kur\w*|spike coz\w*|bomba kur\w*|strafe|wasd|trigger ?bot|aimbot|hile)\b")


def gameplay_goal_reason(goal: str) -> str | None:
    """Hedef maç içinde oynamayı istiyorsa Türkçe ret açıklaması."""
    if _GAMEPLAY_GOAL.search(fold(goal)):
        return ("Bu isteği yapamam: oyunun içinde senin yerine oynamak (nişan almak, hareket etmek, ateş etmek, "
                "maç kazanmak) hile sayılır ve hesabının kalıcı olarak kapatılmasına yol açabilir. Oyunu açıp "
                "menüden bir mod seçip sıraya girmene yardım edebilirim; maçı sen oynarsın.")
    return None
