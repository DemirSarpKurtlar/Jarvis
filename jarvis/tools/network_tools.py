"""Ağ araçları: Wi-Fi/Bluetooth radyosunu gerçekten açıp kapatma ve internet hız testi.

Radyolar: Windows.Devices.Radios (WinRT) sabit bir PowerShell betiğiyle kullanılır. Betiğe
yalnızca izin listesindeki değerler girer (radyo türü: WiFi/Bluetooth, durum: On/Off).
İşlemden sonra durum yeniden okunur; değişmediyse başarı söylenmez.
Wi-Fi'yi kapatmak onay ister: JARVIS'in internet bağlantısı (Claude) kesilebilir.

Hız testi: speed.cloudflare.com (__down / __up), ücretsiz ve anahtarsız. Veri sınırlı
(~25 MB indirme, ~5 MB yükleme), her aşamanın süre sınırı var, "hız testini iptal et" durdurur.
"""

from __future__ import annotations

import json
import logging
import statistics
import time
from collections.abc import Callable, Iterator
from typing import Any, Protocol

from jarvis.security.permission_manager import ConfirmPrompt, Permission
from jarvis.tools import powershell
from jarvis.tools import schema as js
from jarvis.tools.cancel import OPERATIONS, CancelToken, OperationCancelled
from jarvis.tools.registry import ToolContext, ToolError, ToolOutput, ToolRegistry, ToolSpec, WINDOWS_ONLY

try:  # anthropic SDK'sı httpx2 ile gelir
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    import httpx  # type: ignore[no-redef]

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- radyolar
RADIOS: dict[str, tuple[str, str, str]] = {"wifi": ("WiFi", "Wi-Fi", "Wi-Fi'yi"),
                                           "bluetooth": ("Bluetooth", "Bluetooth", "Bluetooth'u")}
RADIO_ALLOWED = {"kind": frozenset({"WiFi", "Bluetooth"}), "state": frozenset({"On", "Off"})}
STATE_TR = {"On": "açık", "Off": "kapalı", "Disabled": "devre dışı (donanım anahtarı ya da uçak modu)",
            "Unknown": "bilinmiyor"}

_RADIO_PRELUDE = r"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
  $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, [Type]$t) {
  $task = $asTaskGeneric.MakeGenericMethod($t).Invoke($null, @($op))
  if (-not $task.Wait(10000)) { throw 'zaman asimi' }
  $task.Result
}
[Windows.Devices.Radios.Radio,Windows.System.Devices,ContentType=WindowsRuntime] | Out-Null
$access = Await ([Windows.Devices.Radios.Radio]::RequestAccessAsync()) ([Windows.Devices.Radios.RadioAccessStatus])
$radios = Await ([Windows.Devices.Radios.Radio]::GetRadiosAsync()) `
  ([System.Collections.Generic.IReadOnlyList[Windows.Devices.Radios.Radio]])
function Describe { @($radios | ForEach-Object { [pscustomobject]@{
  kind = $_.Kind.ToString(); name = $_.Name; state = $_.State.ToString() } }) }
"""

RADIO_STATUS_SCRIPT = _RADIO_PRELUDE + r"""
[pscustomobject]@{ access = $access.ToString(); radios = @(Describe); results = @() } | ConvertTo-Json -Depth 4 -Compress
"""

RADIO_SET_TEMPLATE = _RADIO_PRELUDE + r"""
$results = @()
foreach ($r in @($radios | Where-Object { $_.Kind.ToString() -eq '{kind}' })) {
  $results += (Await ($r.SetStateAsync([Windows.Devices.Radios.RadioState]::{state})) `
    ([Windows.Devices.Radios.RadioAccessStatus])).ToString()
}
Start-Sleep -Milliseconds 700
[pscustomobject]@{ access = $access.ToString(); radios = @(Describe); results = @($results) } | ConvertTo-Json -Depth 4 -Compress
"""


class RadioBackend(Protocol):
    def status(self) -> dict: ...
    def set_state(self, kind: str, state: str) -> dict: ...


def _parse_json_tail(out: str) -> Any:  # noqa: ANN401
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith(("{", "[")):
            return json.loads(line)
    raise ValueError("JSON yok")


class PowerShellRadios:
    """Gerçek arka uç (Windows). Betik metni sabittir."""

    def __init__(self, runner: Callable[[str, float], tuple[int, str, str]] = powershell.run_script) -> None:
        self.runner = runner

    def _run(self, script: str) -> dict:
        code, out, err = self.runner(script, 40.0)
        try:
            data = _parse_json_tail(out)
        except (ValueError, json.JSONDecodeError):
            log.warning("Radyo betiği beklenmeyen çıktı verdi (kod %s): %s", code, err[:300])
            raise ToolError("radyo", "Windows radyo arayüzüne ulaşamadım (Windows.Devices.Radios).") from None
        radios = data.get("radios") or []
        if isinstance(radios, dict):
            radios = [radios]
        results = data.get("results") or []
        if isinstance(results, str):
            results = [results]
        return {"access": str(data.get("access", "")), "radios": radios, "results": results}

    def status(self) -> dict:
        return self._run(RADIO_STATUS_SCRIPT)

    def set_state(self, kind: str, state: str) -> dict:
        return self._run(powershell.render(RADIO_SET_TEMPLATE, RADIO_ALLOWED, kind=kind, state=state))


class RadioTools:
    def __init__(self, backend: RadioBackend, sleep: Callable[[float], None] = time.sleep) -> None:
        self.backend, self.sleep = backend, sleep

    @staticmethod
    def _find(st: dict, kind: str) -> list[dict]:
        return [r for r in st.get("radios", []) if str(r.get("kind")) == kind]

    def handle(self, args: dict) -> ToolOutput:
        kind, label, accusative = RADIOS[args["radio"]]
        if args["action"] == "durum":
            radios = self._find(self.backend.status(), kind)
            if not radios:
                raise ToolError("radyo yok", f"Bu bilgisayarda {label} radyosu bulunamadı.")
            state = str(radios[0].get("state"))
            return ToolOutput(f"{label} {STATE_TR.get(state, state)}.", {"radio": args["radio"], "state": state})
        want = "On" if args["action"] == "ac" else "Off"
        res = self.backend.set_state(kind, want)
        radios = self._find(res, kind)
        if not radios:
            raise ToolError("radyo yok", f"Bu bilgisayarda {label} radyosu bulunamadı.")
        denied = [r for r in res.get("results", []) if r != "Allowed"] or \
            ([res["access"]] if res.get("access") not in ("", "Allowed") else [])
        # Doğrula: durum gerçekten değişti mi (Windows birkaç yüz ms gecikebilir)
        state = str(radios[0].get("state"))
        for _ in range(3):
            if state == want:
                break
            self.sleep(1.0)
            radios = self._find(self.backend.status(), kind) or radios
            state = str(radios[0].get("state"))
        verb = "açtım" if want == "On" else "kapattım"
        if state == want:
            extra = " İnternet bağlantın kesilmiş olabilir; açmak için 'Wi-Fi'yi aç' diyebilirsin." \
                if kind == "WiFi" and want == "Off" else ""
            return ToolOutput(f"{accusative} {verb}; durumu doğruladım: {STATE_TR[want]}.{extra}",
                              {"radio": args["radio"], "state": state, "verified": True})
        if denied:
            raise ToolError("izin", f"Windows {label} radyosunu değiştirmeme izin vermedi ({', '.join(denied)}). "
                                    "Ayarlar > Gizlilik ve güvenlik > Radyolar bölümünden izin verebilir ya da "
                                    "'bluetooth ayarlarını aç' diyerek elle değiştirebilirsin.")
        if state == "Disabled":
            raise ToolError("devre dışı", f"{label} radyosu devre dışı (uçak modu ya da donanım anahtarı); "
                                          "yazılımla açılamıyor.")
        raise ToolError("doğrulanamadı", f"{label} için istek gönderdim ama durumu hâlâ "
                                         f"{STATE_TR.get(state, state)} görünüyor; değişikliği doğrulayamadım.")

    @staticmethod
    def escalate(args: dict) -> Permission | None:
        if args["action"] == "durum":
            return None
        if args["radio"] == "wifi" and args["action"] == "kapat":
            return Permission.CONFIRM
        return Permission.SAFE

    @staticmethod
    def prompt(args: dict) -> ConfirmPrompt:
        return ConfirmPrompt("radio_control", "Wi-Fi'yi kapat",
                             "Wi-Fi kapatılacak. İnternet bağlantın ve JARVIS'in Claude bağlantısı kesilebilir. "
                             "Tekrar açmak için 'Wi-Fi'yi aç' diyebilirsin (bu komut internetsiz de çalışır).",
                             confirm_label="Wi-Fi'yi kapat")


# ---------------------------------------------------------------- hız testi
SPEED_HOST = "https://speed.cloudflare.com"
DOWN_BYTES = 25_000_000
UP_BYTES = 5_000_000
CHUNK = 64 * 1024


class SpeedTester:
    def __init__(self, client_factory: Callable[[], Any] | None = None, down_bytes: int = DOWN_BYTES,
                 up_bytes: int = UP_BYTES, pings: int = 5, ping_s: float = 6.0, down_s: float = 20.0,
                 up_s: float = 15.0, clock: Callable[[], float] = time.perf_counter) -> None:
        self.client_factory = client_factory or (lambda: httpx.Client(
            timeout=httpx.Timeout(10.0), headers={"User-Agent": "JARVIS-asistan"}, follow_redirects=False))
        self.down_bytes, self.up_bytes, self.pings = down_bytes, up_bytes, pings
        self.ping_s, self.down_s, self.up_s, self.clock = ping_s, down_s, up_s, clock

    @staticmethod
    def _server_ms(resp: Any) -> float:  # noqa: ANN401
        st = resp.headers.get("server-timing", "")
        for part in st.split(","):
            if "dur=" in part:
                try:
                    return float(part.split("dur=", 1)[1].split(";")[0])
                except ValueError:
                    return 0.0
        return 0.0

    def _latency(self, client: Any, token: CancelToken) -> tuple[float | None, float | None, str]:  # noqa: ANN401
        samples, colo, start = [], "", self.clock()
        for i in range(self.pings + 1):   # ilki bağlantı kurulumu: sayılmaz
            token.check()
            if self.clock() - start > self.ping_s:
                break
            t0 = self.clock()
            r = client.get(f"{SPEED_HOST}/__down", params={"bytes": "0"})
            dt = (self.clock() - t0) * 1000
            r.raise_for_status()
            colo = colo or r.headers.get("cf-ray", "").rpartition("-")[2]
            if i > 0:
                samples.append(max(0.1, dt - self._server_ms(r)))
        if not samples:
            return None, None, colo
        jitter = statistics.mean(abs(a - b) for a, b in zip(samples, samples[1:])) if len(samples) > 1 else 0.0
        return statistics.median(samples), jitter, colo

    def _download(self, client: Any, token: CancelToken) -> tuple[float, int]:  # noqa: ANN401
        got, t0 = 0, None
        with client.stream("GET", f"{SPEED_HOST}/__down", params={"bytes": str(self.down_bytes)}) as r:
            r.raise_for_status()
            start = self.clock()
            for chunk in r.iter_bytes():
                if t0 is None:
                    t0 = self.clock()
                got += len(chunk)
                token.check()
                if self.clock() - start > self.down_s or got >= self.down_bytes:
                    break
            end = self.clock()
        dt = max(1e-6, end - (t0 if t0 is not None else start))
        return got * 8 / dt / 1e6, got

    def _upload(self, client: Any, token: CancelToken) -> tuple[float, int]:  # noqa: ANN401
        sent = [0]
        block = bytes(CHUNK)
        start = self.clock()

        def body() -> Iterator[bytes]:
            while sent[0] < self.up_bytes:
                token.check()
                if self.clock() - start > self.up_s:
                    return
                n = min(CHUNK, self.up_bytes - sent[0])
                sent[0] += n
                yield block[:n]

        r = client.post(f"{SPEED_HOST}/__up", content=body(),
                        headers={"Content-Type": "application/octet-stream"})
        r.raise_for_status()
        dt = max(1e-6, self.clock() - start)
        return sent[0] * 8 / dt / 1e6, sent[0]

    def run(self, token: CancelToken) -> dict:
        try:
            with self.client_factory() as client:
                ping, jitter, colo = self._latency(client, token)
                down, got = self._download(client, token)
                up, sent = self._upload(client, token)
        except OperationCancelled:
            raise
        except httpx.HTTPStatusError as e:
            raise ToolError("hız testi", f"Hız testi sunucusu hata döndürdü ({e.response.status_code}).") from e
        except httpx.HTTPError as e:
            raise ToolError("hız testi", "İnternete bağlanamadım; hız testi yapılamadı.") from e
        return {"ping_ms": round(ping, 1) if ping is not None else None,
                "jitter_ms": round(jitter, 1) if jitter is not None else None,
                "download_mbps": round(down, 1), "upload_mbps": round(up, 1),
                "downloaded_mb": round(got / 1e6, 1), "uploaded_mb": round(sent / 1e6, 1),
                "server": "speed.cloudflare.com" + (f" ({colo})" if colo else "")}


def speed_handler(tester: SpeedTester) -> Callable[[dict], ToolOutput]:
    def handle(args: dict) -> ToolOutput:
        with OPERATIONS.track("hız testi") as token:
            try:
                d = tester.run(token)
            except OperationCancelled:
                return ToolOutput("Hız testini durdurdum.", {"cancelled": True})
        ping = f", gecikme {d['ping_ms']:.0f} ms" if d["ping_ms"] is not None else ""
        return ToolOutput(f"İndirme {d['download_mbps']:.1f} Mbps, yükleme {d['upload_mbps']:.1f} Mbps{ping} "
                          f"(sunucu: {d['server']}; {d['downloaded_mb']:.0f} MB indirildi, "
                          f"{d['uploaded_mb']:.0f} MB gönderildi).", d)
    return handle


def register(reg: ToolRegistry, ctx: ToolContext, radios: RadioBackend | None = None,
             speed: SpeedTester | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
    rt = RadioTools(radios or PowerShellRadios(), sleep=sleep)
    reg.register(ToolSpec(
        name="radio_control", title="Wi-Fi / Bluetooth", category="sistem", platforms=WINDOWS_ONLY,
        description=("Wi-Fi ya da Bluetooth radyosunu açar, kapatır veya durumunu söyler; sonra durumu doğrular. "
                     "Wi-Fi'yi kapatmak onay ister. / Turns Wi-Fi or Bluetooth on/off (verified) or reads state."),
        input_schema=js.obj(radio=js.string("Radyo / radio", enum=list(RADIOS)),
                            action=js.string("İşlem / action", enum=["durum", "ac", "kapat"])),
        permission=Permission.READ, handler=rt.handle, escalate=rt.escalate, confirm=rt.prompt, timeout_s=60))
    reg.register(ToolSpec(
        name="internet_speed_test", title="İnternet hız testi", category="sistem",
        description=("speed.cloudflare.com ile indirme/yükleme hızı ve gecikmeyi ölçer (~30 MB veri, ~45 sn). / "
                     "Measures download/upload Mbps and latency (bounded data)."),
        input_schema=js.obj(), permission=Permission.SAFE, handler=speed_handler(speed or SpeedTester()),
        timeout_s=70))
