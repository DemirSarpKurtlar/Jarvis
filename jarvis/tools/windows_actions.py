"""Windows eylemleri: ses, Ayarlar sayfaları, web adresleri, ekran kilidi, kapatma ve
onaylı komutlar.

Serbest kabuk komutu YOKTUR. "Onaylı komutlar" sabit bir listedeki adlandırılmış
işlemlerdir: her biri System32'deki tam yoluyla, sabit argümanlarla ve
shell=False ile çalışır. Kullanıcıdan/Claude'dan gelen tek değişken (ping
hedefi) ana makine adı/IP olarak doğrulanır.
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus

from jarvis.security.action_validator import ValidationError, validate_host, validate_url
from jarvis.security.permission_manager import ConfirmPrompt, Permission
from jarvis.tools import schema as js
from jarvis.tools.registry import (NotSupportedError, ToolContext, ToolError, ToolOutput, ToolRegistry, ToolSpec,
                                   WINDOWS_ONLY)

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000
MAX_COMMAND_OUTPUT = 8000

# Anahtar -> (ms-settings URI, Türkçe ad). Yalnızca bu sayfalar açılabilir.
SETTINGS_PAGES: dict[str, tuple[str, str]] = {
    "ana_sayfa": ("ms-settings:", "Ayarlar"),
    "bluetooth": ("ms-settings:bluetooth", "Bluetooth ve cihazlar"),
    "wifi": ("ms-settings:network-wifi", "Wi-Fi"),
    "ag": ("ms-settings:network", "Ağ ve internet"),
    "vpn": ("ms-settings:network-vpn", "VPN"),
    "ses": ("ms-settings:sound", "Ses"),
    "ekran": ("ms-settings:display", "Ekran"),
    "gece_isigi": ("ms-settings:nightlight", "Gece ışığı"),
    "bildirimler": ("ms-settings:notifications", "Bildirimler"),
    "odak": ("ms-settings:quiethours", "Odak / rahatsız etmeyin"),
    "guc": ("ms-settings:powersleep", "Güç ve uyku"),
    "pil": ("ms-settings:batterysaver", "Pil"),
    "depolama": ("ms-settings:storagesense", "Depolama"),
    "uygulamalar": ("ms-settings:appsfeatures", "Yüklü uygulamalar"),
    "varsayilan_uygulamalar": ("ms-settings:defaultapps", "Varsayılan uygulamalar"),
    "baslangic": ("ms-settings:startupapps", "Başlangıç uygulamaları"),
    "kisisellestirme": ("ms-settings:personalization", "Kişiselleştirme"),
    "arka_plan": ("ms-settings:personalization-background", "Arka plan"),
    "renkler": ("ms-settings:personalization-colors", "Renkler"),
    "temalar": ("ms-settings:themes", "Temalar"),
    "kilit_ekrani": ("ms-settings:lockscreen", "Kilit ekranı"),
    "gorev_cubugu": ("ms-settings:taskbar", "Görev çubuğu"),
    "fare": ("ms-settings:mousetouchpad", "Fare"),
    "klavye": ("ms-settings:typing", "Yazma ve klavye"),
    "yazicilar": ("ms-settings:printers", "Yazıcılar ve tarayıcılar"),
    "tarih_saat": ("ms-settings:dateandtime", "Tarih ve saat"),
    "dil": ("ms-settings:regionlanguage", "Dil ve bölge"),
    "hesaplar": ("ms-settings:yourinfo", "Hesap bilgileri"),
    "oturum_acma": ("ms-settings:signinoptions", "Oturum açma seçenekleri"),
    "gizlilik": ("ms-settings:privacy", "Gizlilik ve güvenlik"),
    "mikrofon_izni": ("ms-settings:privacy-microphone", "Mikrofon izinleri"),
    "kamera_izni": ("ms-settings:privacy-webcam", "Kamera izinleri"),
    "konum": ("ms-settings:privacy-location", "Konum"),
    "guncelleme": ("ms-settings:windowsupdate", "Windows Update"),
    "windows_guvenligi": ("windowsdefender:", "Windows Güvenliği"),
    "oyun": ("ms-settings:gaming-gamebar", "Oyun"),
    "erisilebilirlik": ("ms-settings:easeofaccess", "Erişilebilirlik"),
    "hakkinda": ("ms-settings:about", "Sistem hakkında"),
    "projeksiyon": ("ms-settings:project", "Bu bilgisayara yansıtma"),
    "mobil_erisim_noktasi": ("ms-settings:network-mobilehotspot", "Mobil etkin nokta"),
    "ucak_modu": ("ms-settings:network-airplanemode", "Uçak modu"),
}

WEB_SEARCH = {
    "google": "https://www.google.com/search?q={}",
    "youtube": "https://www.youtube.com/results?search_query={}",
    "bing": "https://www.bing.com/search?q={}",
    "vikipedi": "https://tr.wikipedia.org/w/index.php?search={}",
}


def system32(exe: str) -> str:
    root = os.environ.get("SystemRoot", r"C:\Windows")
    return str(Path(root) / "System32" / exe)


@dataclass(frozen=True)
class ApprovedCommand:
    key: str
    title: str
    exe: str
    args: tuple[str, ...]
    permission: Permission
    needs_target: bool = False
    timeout_s: float = 30.0
    wait: bool = True       # False: penceresi olan bir aracı başlatır, çıktı beklemez


APPROVED_COMMANDS: dict[str, ApprovedCommand] = {c.key: c for c in (
    ApprovedCommand("ip_yapilandirmasi", "IP yapılandırması (ipconfig /all)", "ipconfig.exe", ("/all",),
                    Permission.READ),
    ApprovedCommand("dns_onbellegini_temizle", "DNS önbelleğini temizle (ipconfig /flushdns)", "ipconfig.exe",
                    ("/flushdns",), Permission.SAFE),
    ApprovedCommand("ping", "Bağlantı testi (ping)", "ping.exe", ("-n", "4"), Permission.READ, needs_target=True),
    ApprovedCommand("rota_izle", "Rota izleme (tracert)", "tracert.exe", ("-d", "-h", "15", "-w", "1000"),
                    Permission.READ, needs_target=True, timeout_s=90),
    ApprovedCommand("sistem_bilgisi", "Sistem bilgisi (systeminfo)", "systeminfo.exe", (), Permission.READ,
                    timeout_s=60),
    ApprovedCommand("surucu_listesi", "Sürücü listesi (driverquery)", "driverquery.exe", ("/fo", "list"),
                    Permission.READ, timeout_s=60),
    ApprovedCommand("wifi_durumu", "Wi-Fi durumu (netsh wlan show interfaces)", "netsh.exe",
                    ("wlan", "show", "interfaces"), Permission.READ),
    ApprovedCommand("disk_temizleme", "Disk Temizleme aracını aç (cleanmgr)", "cleanmgr.exe", (), Permission.SAFE,
                    wait=False),
)}


def _oem_encoding() -> str:
    if IS_WINDOWS:  # pragma: no cover
        try:
            return f"cp{ctypes.windll.kernel32.GetOEMCP()}"
        except Exception:  # noqa: BLE001
            pass
    return "utf-8"


def run_approved(cmd: ApprovedCommand, target: str) -> tuple[int, str]:  # pragma: no cover - Windows
    if not IS_WINDOWS:
        raise NotSupportedError()
    argv = [system32(cmd.exe), *cmd.args] + ([target] if cmd.needs_target else [])
    if not Path(argv[0]).is_file():
        raise ToolError("yok", f"{cmd.exe} bu sistemde bulunamadı.")
    log.info("Onaylı komut: %s", cmd.key)
    if not cmd.wait:
        subprocess.Popen(argv, shell=False)
        return 0, ""
    try:
        cp = subprocess.run(argv, shell=False, capture_output=True, timeout=cmd.timeout_s,
                            creationflags=CREATE_NO_WINDOW, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        raise ToolError("zaman aşımı", f"{cmd.title} {cmd.timeout_s:.0f} saniyede bitmedi.") from e
    enc = _oem_encoding()
    out = (cp.stdout or b"").decode(enc, errors="replace") + (cp.stderr or b"").decode(enc, errors="replace")
    return cp.returncode, out.strip()


def lock_workstation() -> None:  # pragma: no cover - Windows
    if not IS_WINDOWS:
        raise NotSupportedError()
    if not ctypes.windll.user32.LockWorkStation():
        raise ToolError("kilit", "Ekran kilitlenemedi.")


def power(action: str, delay_s: int) -> None:  # pragma: no cover - Windows
    if not IS_WINDOWS:
        raise NotSupportedError()
    flag = {"shutdown": "/s", "restart": "/r"}[action]
    argv = [system32("shutdown.exe"), flag, "/t", str(delay_s), "/c",
            "JARVIS: kullanıcının isteğiyle. İptal için 'kapatmayı iptal et' de."]
    cp = subprocess.run(argv, shell=False, capture_output=True, timeout=15, creationflags=CREATE_NO_WINDOW)
    if cp.returncode != 0:
        raise ToolError("shutdown", "Windows kapatma isteğini kabul etmedi.")


def cancel_power() -> bool:  # pragma: no cover - Windows
    if not IS_WINDOWS:
        raise NotSupportedError()
    cp = subprocess.run([system32("shutdown.exe"), "/a"], shell=False, capture_output=True, timeout=15,
                        creationflags=CREATE_NO_WINDOW)
    return cp.returncode == 0


class ActionTools:
    def __init__(self, ctx: ToolContext, runner=run_approved, locker=lock_workstation,  # noqa: ANN001
                 power_fn=power, cancel_fn=cancel_power) -> None:
        self.ctx, self.runner, self.locker, self.power_fn, self.cancel_fn = ctx, runner, locker, power_fn, cancel_fn

    def _open(self, target: str) -> None:
        if self.ctx.opener is None:
            raise NotSupportedError()
        self.ctx.opener(target)

    @property
    def audio(self):  # noqa: ANN201
        if self.ctx.audio is None:
            raise NotSupportedError()
        return self.ctx.audio

    # ses
    def get_volume(self, args: dict) -> ToolOutput:
        v, m = self.audio.get_volume(), self.audio.is_muted()
        return ToolOutput(f"Ses seviyesi %{v}" + (", ses kapalı." if m else "."), {"volume": v, "muted": m})

    def set_volume(self, args: dict) -> ToolOutput:
        self.audio.set_volume(args["level"])
        if args["level"] > 0 and self.audio.is_muted():
            self.audio.set_mute(False)
        v = self.audio.get_volume()
        return ToolOutput(f"Sesi %{v} yaptım.", {"volume": v})

    def change_volume(self, args: dict) -> ToolOutput:
        cur = self.audio.get_volume()
        new = max(0, min(100, cur + args["delta"]))
        self.audio.set_volume(new)
        if args["delta"] > 0 and self.audio.is_muted():
            self.audio.set_mute(False)
        v = self.audio.get_volume()
        return ToolOutput(f"Sesi %{cur}'den %{v}'e {'açtım' if v >= cur else 'kıstım'}.", {"from": cur, "volume": v})

    def set_mute(self, args: dict) -> ToolOutput:
        self.audio.set_mute(args["muted"])
        return ToolOutput("Sesi kapattım." if args["muted"] else "Sesi açtım.", {"muted": args["muted"]})

    # ayarlar / web
    def open_settings(self, args: dict) -> ToolOutput:
        uri, label = SETTINGS_PAGES[args["page"]]
        self._open(uri)
        extra = (" Açma/kapama düğmesine senin basman gerekiyor; ayarları kendim değiştirmiyorum."
                 if args["page"] in ("bluetooth", "wifi", "ucak_modu", "gece_isigi", "mobil_erisim_noktasi") else "")
        return ToolOutput(f"{label} ayarlarını açtım.{extra}", {"page": args["page"], "uri": uri})

    def prepare_url(self, args: dict) -> dict:
        return {**args, "url": validate_url(args["url"])}

    def open_url(self, args: dict) -> ToolOutput:
        self._open(args["url"])
        return ToolOutput("Adresi tarayıcıda açtım.", {"url": args["url"]})

    def web_search(self, args: dict) -> ToolOutput:
        url = WEB_SEARCH[args["engine"]].format(quote_plus(args["query"]))
        self._open(url)
        return ToolOutput(f"'{args['query']}' için {args['engine'].capitalize()} aramasını açtım.", {"url": url})

    # kilit / güç
    def lock(self, args: dict) -> ToolOutput:
        self.locker()
        return ToolOutput("Ekranı kilitledim.", {"locked": True})

    def power(self, args: dict) -> ToolOutput:
        self.power_fn(args["action"], args["delay_seconds"])
        what = "kapanacak" if args["action"] == "shutdown" else "yeniden başlayacak"
        return ToolOutput(f"Bilgisayar {args['delay_seconds']} saniye içinde {what}. İptal etmek için "
                          "'kapatmayı iptal et' diyebilirsin.", {"action": args["action"]})

    def power_prompt(self, args: dict) -> ConfirmPrompt:
        what = "kapatılacak" if args["action"] == "shutdown" else "yeniden başlatılacak"
        return ConfirmPrompt("power_action", "Bilgisayarı " + ("kapat" if args["action"] == "shutdown" else
                                                                "yeniden başlat"),
                             f"Bilgisayar {args['delay_seconds']} saniye sonra {what}. Kaydedilmemiş işler kaybolabilir.",
                             danger=True, confirm_label="Onayla")

    def cancel(self, args: dict) -> ToolOutput:
        if self.cancel_fn():
            return ToolOutput("Planlanan kapatma iptal edildi.", {"cancelled": True})
        return ToolOutput("İptal edilecek planlı bir kapatma yoktu.", {"cancelled": False})

    # onaylı komutlar
    def prepare_command(self, args: dict) -> dict:
        cmd = APPROVED_COMMANDS[args["command"]]
        target = args["target"].strip()
        if cmd.needs_target:
            if not target:
                raise ToolError("hedef", f"{cmd.title} için bir ana makine adı ya da IP gerekli.")
            try:
                target = validate_host(target)
            except ValidationError as e:
                raise ToolError("hedef", e.user_message) from e
        elif target:
            raise ToolError("hedef", f"{cmd.title} hedef almaz.")
        return {**args, "target": target}

    def run_command(self, args: dict) -> ToolOutput:
        cmd = APPROVED_COMMANDS[args["command"]]
        code, out = self.runner(cmd, args["target"])
        if not cmd.wait:
            return ToolOutput(f"{cmd.title} başlatıldı.", {"started": True})
        out = out[:MAX_COMMAND_OUTPUT]
        status = "tamamlandı" if code == 0 else f"hata koduyla bitti ({code})"
        return ToolOutput(f"{cmd.title} {status}.", {"exit_code": code, "output": out}, untrusted=True)


def register(reg: ToolRegistry, ctx: ToolContext, **backends) -> ActionTools:  # noqa: ANN003
    t = ActionTools(ctx, **backends)

    reg.register(ToolSpec(
        name="get_volume", title="Ses seviyesi", category="eylem", platforms=WINDOWS_ONLY,
        description="Sistem ses seviyesini ve sessiz durumunu okur. / Reads system volume and mute state.",
        input_schema=js.obj(), permission=Permission.READ, handler=t.get_volume))
    reg.register(ToolSpec(
        name="set_volume", title="Sesi ayarla", category="eylem", platforms=WINDOWS_ONLY,
        description="Sistem sesini yüzde olarak ayarlar. / Sets system volume percent.",
        input_schema=js.obj(level=js.integer("Ses yüzdesi / volume percent", 0, 100)),
        permission=Permission.SAFE, handler=t.set_volume))
    reg.register(ToolSpec(
        name="change_volume", title="Sesi artır/azalt", category="eylem", platforms=WINDOWS_ONLY,
        description="Sistem sesini yüzde puan olarak artırır ya da azaltır. / Changes volume by a delta.",
        input_schema=js.obj(delta=js.integer("Değişim (negatif = kıs) / delta", -100, 100)),
        permission=Permission.SAFE, handler=t.change_volume))
    reg.register(ToolSpec(
        name="set_mute", title="Sesi kapat/aç", category="eylem", platforms=WINDOWS_ONLY,
        description="Sistem sesini kapatır ya da açar. / Mutes or unmutes system audio.",
        input_schema=js.obj(muted=js.boolean("Sessiz / muted")), permission=Permission.SAFE, handler=t.set_mute))
    reg.register(ToolSpec(
        name="open_settings", title="Ayarları aç", category="eylem", platforms=WINDOWS_ONLY,
        description=("Windows Ayarlar'ın izinli bir sayfasını açar (yalnızca açar, ayarı değiştirmez). / Opens an "
                     "allowlisted Windows Settings page; does not change settings."),
        input_schema=js.obj(page=js.string("Sayfa / page", enum=list(SETTINGS_PAGES))),
        permission=Permission.SAFE, handler=t.open_settings))
    reg.register(ToolSpec(
        name="open_url", title="Web adresi aç", category="eylem",
        description="Bir http/https adresini varsayılan tarayıcıda açar. / Opens an http(s) URL in the default browser.",
        input_schema=js.obj(url=js.string("Adres / URL", 2048, 8)), permission=Permission.SAFE, handler=t.open_url,
        prepare=t.prepare_url))
    reg.register(ToolSpec(
        name="web_search_in_browser", title="Tarayıcıda ara", category="eylem",
        description="Tarayıcıda bir arama sayfası açar. / Opens a search results page in the browser.",
        input_schema=js.obj(query=js.string("Aranacak metin / query", 300, 1),
                            engine=js.string("Arama motoru / engine", enum=list(WEB_SEARCH))),
        permission=Permission.SAFE, handler=t.web_search))
    reg.register(ToolSpec(
        name="lock_screen", title="Ekranı kilitle", category="eylem", platforms=WINDOWS_ONLY,
        description="Windows oturumunu kilitler. / Locks the Windows session.",
        input_schema=js.obj(), permission=Permission.SAFE, handler=t.lock))
    reg.register(ToolSpec(
        name="power_action", title="Kapat / yeniden başlat", category="eylem", platforms=WINDOWS_ONLY,
        description=("Bilgisayarı gecikmeyle kapatır ya da yeniden başlatır (iptal edilebilir). Onay ister. / Shuts down "
                     "or restarts after a delay (cancellable). Needs confirmation."),
        input_schema=js.obj(action=js.string("İşlem / action", enum=["shutdown", "restart"]),
                            delay_seconds=js.integer("Gecikme saniye / delay seconds", 30, 3600)),
        permission=Permission.CONFIRM, handler=t.power, confirm=t.power_prompt))
    reg.register(ToolSpec(
        name="cancel_power_action", title="Kapatmayı iptal et", category="eylem", platforms=WINDOWS_ONLY,
        description="Planlanmış kapatma/yeniden başlatmayı iptal eder. / Cancels a pending shutdown/restart.",
        input_schema=js.obj(), permission=Permission.SAFE, handler=t.cancel))
    reg.register(ToolSpec(
        name="run_approved_command", title="Onaylı komut", category="eylem", platforms=WINDOWS_ONLY,
        description=("Sabit listeden adlandırılmış bir sistem komutu çalıştırır; serbest komut çalıştırılamaz. "
                     "Komutlar: " + "; ".join(f"{k} = {c.title}" for k, c in APPROVED_COMMANDS.items()) +
                     ". target yalnızca ping/rota_izle için. / Runs one of a fixed list of named commands; free-form "
                     "shell is not available."),
        input_schema=js.obj(command=js.string("Komut / command", enum=list(APPROVED_COMMANDS)),
                            target=js.string("Ana makine ya da IP (gerekmiyorsa boş) / host or empty", 253)),
        permission=Permission.READ, handler=t.run_command, prepare=t.prepare_command,
        escalate=lambda a: APPROVED_COMMANDS[a["command"]].permission, timeout_s=100))
    return t
