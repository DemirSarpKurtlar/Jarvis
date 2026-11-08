"""Sabah brifingi: "Günaydın Jarvis", "sabah brifingi".

İçerik (her bölüm bağımsız; biri alınamazsa nedeni söylenir, uydurulmaz):
- Türkçe tarih ve gün
- Hava durumu: open-meteo (anahtarsız, ücretsiz): önce şehir adından konum (geocoding), sonra tahmin.
  Şehir ayarlardan gelir (`settings.briefing.city`); boşsa kullanıcıya nereye yazacağı söylenir.
- Bilgisayar durumu: sistem diski boş alanı, ekran kartı sıcaklığı (izleyicinin son ölçümü), işlemci
  sıcaklığı (yalnızca güvenilir sensör varsa), Windows'ta bekleyen yeniden başlatma (kayıt defteri).
- Bugünün zamanlanmış JARVIS görevleri (zamanlayıcının herkese açık `list()` arayüzü)
- Kısa haber başlıkları: ücretsiz RSS (Anadolu Ajansı / BBC Türkçe / TRT Haber), yalnızca başlıklar.
  Başlıklar güvenilmeyen içeriktir: sonuç `untrusted=True` işaretlenir.

Cevap yerelde şablonla kurulur (ücretsiz). Araç Claude üzerinden çağrılırsa Claude aynı veriyi kendi
cümleleriyle anlatabilir; yerel komut ("günaydın") hiç API çağrısı yapmaz.

Ağ: yalnızca izin listesindeki HTTPS adresleri, yönlendirmeler de aynı listeye tabi; yanıt boyutu sınırlı;
DOCTYPE/ENTITY içeren XML reddedilir (XML bombası).
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

import psutil

from jarvis import __version__
from jarvis.security.permission_manager import Permission
from jarvis.tools import schema as js
from jarvis.tools.registry import ToolContext, ToolError, ToolOutput, ToolRegistry, ToolSpec

log = logging.getLogger(__name__)

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
NEWS_SOURCES: dict[str, tuple[str, str]] = {
    "aa": ("Anadolu Ajansı", "https://www.aa.com.tr/tr/rss/default?cat=guncel"),
    "bbc": ("BBC Türkçe", "https://feeds.bbci.co.uk/turkce/rss.xml"),
    "trt": ("TRT Haber", "https://www.trthaber.com/manset_articles.rss"),
}
ALLOWED_HOSTS = frozenset({"geocoding-api.open-meteo.com", "api.open-meteo.com", "www.aa.com.tr",
                           "feeds.bbci.co.uk", "www.trthaber.com"})
MAX_BYTES = 1_500_000
HTTP_TIMEOUT_S = 6.0
CACHE_S = 15 * 60
SPOKEN_HEADLINES = 3
MAX_HEADLINES = 5
CITY_RE = re.compile(r"[^\W\d_][\w\s'’\-\.]{1,79}")

MONTHS = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım",
          "Aralık"]
DAYS = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]

# WMO hava durumu kodları (open-meteo belgesi)
WMO_TR: dict[int, str] = {
    0: "açık", 1: "çoğunlukla açık", 2: "parçalı bulutlu", 3: "kapalı", 45: "sisli", 48: "kırağılı sisli",
    51: "hafif çiseleyen", 53: "çiseleyen", 55: "yoğun çiseleyen", 56: "dondurucu çiseleyen",
    57: "yoğun dondurucu çiseleyen", 61: "hafif yağmurlu", 63: "yağmurlu", 65: "şiddetli yağmurlu",
    66: "dondurucu yağmurlu", 67: "şiddetli dondurucu yağmurlu", 71: "hafif kar yağışlı", 73: "kar yağışlı",
    75: "yoğun kar yağışlı", 77: "kar taneli", 80: "hafif sağanak yağışlı", 81: "sağanak yağışlı",
    82: "şiddetli sağanak yağışlı", 85: "hafif kar sağanaklı", 86: "yoğun kar sağanaklı",
    95: "gök gürültülü fırtınalı", 96: "dolulu fırtınalı", 99: "şiddetli dolulu fırtınalı",
}

Fetcher = Callable[[str], bytes]


class FetchError(Exception):
    """Ağ isteği başarısız; `str(e)` Türkçe kısa neden."""


# ---------- ağ ----------
def _check_url(url: str) -> None:
    p = urllib.parse.urlparse(url)
    if p.scheme != "https" or (p.hostname or "") not in ALLOWED_HOSTS:
        raise FetchError("izin verilmeyen adres")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201, PLR0913
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def http_get(url: str, timeout: float = HTTP_TIMEOUT_S) -> bytes:
    """İzin listesindeki bir HTTPS adresinden en çok MAX_BYTES okur."""
    _check_url(url)
    opener = urllib.request.build_opener(_SafeRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": f"JARVIS/{__version__} (masaustu asistani)",
                                               "Accept": "application/json, application/rss+xml, text/xml, */*"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            data = resp.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        raise FetchError(f"sunucu {e.code} hatası verdi") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason):
            raise FetchError("zaman aşımı") from None
        raise FetchError("internete ulaşılamadı") from None
    if len(data) > MAX_BYTES:
        raise FetchError("yanıt çok büyük")
    return data


def _json(fetch: Fetcher, url: str) -> dict:
    try:
        data = json.loads(fetch(url).decode("utf-8", "replace"))
    except ValueError:
        raise FetchError("yanıt okunamadı") from None
    if not isinstance(data, dict):
        raise FetchError("yanıt okunamadı")
    if data.get("error"):
        raise FetchError("servis isteği reddetti")
    return data


# ---------- bölümler ----------
def turkish_date(now: datetime) -> str:
    return f"{now.day} {MONTHS[now.month - 1]} {now.year}, {DAYS[now.weekday()]}"


def greeting(now: datetime) -> str:
    h = now.hour
    if 5 <= h < 12:
        return "Günaydın"
    if 12 <= h < 18:
        return "İyi günler"
    if 18 <= h < 23:
        return "İyi akşamlar"
    return "İyi geceler"


def parse_headlines(data: bytes, limit: int = MAX_HEADLINES) -> list[str]:
    """RSS/Atom başlıkları (yalnızca metin). DOCTYPE/ENTITY içeren belge reddedilir."""
    head = data[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in data.lower():
        raise FetchError("haber kaynağı beklenmeyen biçimde")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise FetchError("haber kaynağı okunamadı") from None
    titles: list[str] = []
    items = [el for el in root.iter() if el.tag.rsplit("}", 1)[-1] in ("item", "entry")]
    for it in items:
        t = next((c for c in it if c.tag.rsplit("}", 1)[-1] == "title"), None)
        text = "".join(t.itertext()) if t is not None else ""
        text = re.sub(r"<[^>]{0,200}>", " ", html.unescape(text))
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        if len(text) > 160:
            text = text[:157].rstrip() + "…"
        if text not in titles:
            titles.append(text)
        if len(titles) >= limit:
            break
    return titles


def pending_reboot() -> bool | None:
    """Windows'ta bekleyen yeniden başlatma (güncelleme/bileşen bakımı). Bilinmiyorsa None."""
    if sys.platform != "win32":
        return None
    try:  # pragma: no cover - Windows
        import winreg

        keys = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending",
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired")
        for k in keys:
            try:
                winreg.CloseKey(winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, k))
                return True
            except FileNotFoundError:
                continue
        return False
    except OSError:  # pragma: no cover
        return None


def _default_cpu_temp() -> tuple[float | None, str]:
    from jarvis.tools.hardware import cpu_temperature

    return cpu_temperature()


def _default_disk() -> tuple[str, float] | None:
    from jarvis.system.monitor import system_disk_path

    path = system_disk_path()
    try:
        return path, psutil.disk_usage(path).free / 1024 ** 3
    except OSError:
        return None


class BriefingService:
    def __init__(self, settings: Callable[[], Any] = lambda: None, fetch: Fetcher = http_get,
                 clock: Callable[[], datetime] = datetime.now, snapshot: Callable[[], Any] = lambda: None,
                 scheduler: Callable[[], Any] = lambda: None,
                 cpu_temp: Callable[[], tuple[float | None, str]] = _default_cpu_temp,
                 disk: Callable[[], tuple[str, float] | None] = _default_disk,
                 reboot: Callable[[], bool | None] = pending_reboot, parallel: bool = True) -> None:
        self.settings = settings
        self.fetch = fetch
        self.clock = clock
        self.snapshot = snapshot
        self.scheduler = scheduler
        self.cpu_temp = cpu_temp
        self.disk = disk
        self.reboot = reboot
        self.parallel = parallel
        self._geo: dict[str, dict | None] = {}
        self._cache: dict[str, tuple[float, Any]] = {}

    # ---------- önbellek ----------
    def _cached(self, key: str, make: Callable[[], Any]) -> Any:  # noqa: ANN401
        hit = self._cache.get(key)
        if hit is not None and time.monotonic() - hit[0] < CACHE_S:
            return hit[1]
        value = make()
        self._cache[key] = (time.monotonic(), value)
        return value

    # ---------- hava ----------
    def geocode(self, city: str) -> dict | None:
        """{"name", "latitude", "longitude", "country"} ya da bulunamazsa None. Ağ hatasında FetchError."""
        key = city.strip().casefold()
        if key in self._geo:
            return self._geo[key]
        q = urllib.parse.urlencode({"name": city.strip(), "count": 1, "language": "tr", "format": "json"})
        data = _json(self.fetch, f"{GEOCODE_URL}?{q}")
        results = data.get("results") or []
        loc = None
        if results and isinstance(results[0], dict):
            r = results[0]
            try:
                loc = {"name": str(r.get("name") or city)[:80], "latitude": float(r["latitude"]),
                       "longitude": float(r["longitude"]), "country": str(r.get("country") or "")[:80],
                       "admin1": str(r.get("admin1") or "")[:80]}
            except (KeyError, TypeError, ValueError):
                loc = None
        self._geo[key] = loc
        return loc

    def weather(self) -> dict:
        s = self.settings()
        city = (getattr(s, "city", "") or "").strip()
        if not city:
            return {"ok": False, "reason": "şehir ayarlanmamış"}
        try:
            loc = self.geocode(city)
            if loc is None:
                return {"ok": False, "reason": f"'{city}' diye bir yer bulamadım"}
            q = urllib.parse.urlencode({
                "latitude": f"{loc['latitude']:.4f}", "longitude": f"{loc['longitude']:.4f}",
                "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "timezone": "auto", "forecast_days": 1})
            data = self._cached(f"w:{loc['latitude']:.3f},{loc['longitude']:.3f}",
                                lambda: _json(self.fetch, f"{FORECAST_URL}?{q}"))
        except FetchError as e:
            return {"ok": False, "reason": f"hava durumu alınamadı ({e})"}
        cur, daily = data.get("current") or {}, data.get("daily") or {}

        def first(key: str) -> Any:  # noqa: ANN401
            v = daily.get(key)
            return v[0] if isinstance(v, list) and v else None

        def num(v: Any) -> float | None:  # noqa: ANN401
            return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

        now_c = num(cur.get("temperature_2m"))
        code = cur.get("weather_code", first("weather_code"))
        if now_c is None and num(first("temperature_2m_max")) is None:
            return {"ok": False, "reason": "hava durumu yanıtı eksik"}
        return {"ok": True, "city": loc["name"], "country": loc["country"], "now_c": now_c,
                "feels_c": num(cur.get("apparent_temperature")), "wind_kmh": num(cur.get("wind_speed_10m")),
                "desc": WMO_TR.get(int(code), "") if isinstance(code, (int, float)) else "",
                "min_c": num(first("temperature_2m_min")), "max_c": num(first("temperature_2m_max")),
                "rain_pct": num(first("precipitation_probability_max")), "source": "open-meteo.com"}

    # ---------- haber ----------
    def news(self) -> dict:
        s = self.settings()
        if s is not None and not getattr(s, "news_enabled", True):
            return {"ok": False, "reason": "kapalı", "disabled": True}
        key = getattr(s, "news_source", "aa") if s is not None else "aa"
        name, url = NEWS_SOURCES.get(key, NEWS_SOURCES["aa"])
        try:
            titles = self._cached(f"n:{key}", lambda: parse_headlines(self.fetch(url)))
        except FetchError as e:
            return {"ok": False, "source": name, "reason": str(e)}
        if not titles:
            return {"ok": False, "source": name, "reason": "başlık bulunamadı"}
        return {"ok": True, "source": name, "titles": titles}

    # ---------- bilgisayar ----------
    def computer(self) -> dict:
        out: dict[str, Any] = {}
        snap = self.snapshot()
        if snap is not None and getattr(snap, "disk_path", None):
            out["disk"] = {"path": snap.disk_path, "free_gb": round(float(snap.disk_free_gb), 1)}
        else:
            d = self.disk()
            if d is not None:
                out["disk"] = {"path": d[0], "free_gb": round(d[1], 1)}
        gpu = getattr(snap, "gpu", None) if snap is not None else None
        if gpu is not None and getattr(gpu, "temperature_c", None) is not None:
            out["gpu"] = {"name": gpu.name, "temperature_c": gpu.temperature_c}
        try:
            t, src = self.cpu_temp()
        except Exception:  # noqa: BLE001
            t, src = None, "okunamadı"
        out["cpu_temperature_c"] = t
        if t is None:
            out["cpu_temperature_unavailable"] = src
        try:
            out["reboot_pending"] = self.reboot()
        except Exception:  # noqa: BLE001
            out["reboot_pending"] = None
        return out

    # ---------- görevler ----------
    def tasks(self) -> list[dict]:
        sched = self.scheduler()
        if sched is None:
            return []
        from jarvis.tools.scheduler import ACTIONS

        today = self.clock().date()
        out = []
        for t in sched.list():
            if t.due.date() != today:
                continue
            label = ACTIONS.get(t.action, (None, None, t.action))[2]
            out.append({"time": f"{t.due:%H:%M}", "action": label, "text": t.text,
                        "value": t.value if t.action == "ses_ayarla" else None})
        return out

    # ---------- birleştirme ----------
    def collect(self) -> dict:
        now = self.clock()
        if self.parallel:
            with ThreadPoolExecutor(max_workers=3, thread_name_prefix="jarvis-brifing") as ex:
                fw, fn, fc = ex.submit(self.weather), ex.submit(self.news), ex.submit(self.computer)
                weather, news, comp = fw.result(), fn.result(), fc.result()
        else:
            weather, news, comp = self.weather(), self.news(), self.computer()
        return {"date": now.strftime("%Y-%m-%d"), "date_tr": turkish_date(now), "time": now.strftime("%H:%M"),
                "greeting": greeting(now), "weather": weather, "computer": comp, "tasks": self.tasks(),
                "news": news}


def _deg(v: float | None) -> str:
    """Seslendirme için tam sayı; eksi değerler "eksi 3" diye yazılır ("-3" yanlış okunabiliyor)."""
    if v is None:
        return "?"
    n = round(v)
    return f"eksi {abs(n)}" if n < 0 else str(n)


def compose(d: dict) -> str:
    """Seslendirmeye uygun kısa Türkçe brifing (şablon, API'siz)."""
    parts = [f"{d['greeting']}! Bugün {d['date_tr']}."]
    w = d.get("weather") or {}
    if w.get("ok"):
        s = f"{w['city']} için hava"
        s += f" {w['desc']}" if w.get("desc") else ""
        if w.get("now_c") is not None:
            s += f", şu an {_deg(w['now_c'])} derece"
        if w.get("min_c") is not None and w.get("max_c") is not None:
            s += f"; gün içinde en düşük {_deg(w['min_c'])}, en yüksek {_deg(w['max_c'])} derece"
        if w.get("rain_pct") is not None:
            s += f", yağış ihtimali yüzde {_deg(w['rain_pct'])}"
        parts.append(s + ".")
    elif w.get("reason") == "şehir ayarlanmamış":
        parts.append("Hava durumu için şehrini Otomasyon ekranındaki Sabah brifingi bölümüne yaz ya da "
                     "\"şehrim Ankara\" de.")
    else:
        parts.append(f"Hava durumunu şu an veremiyorum: {w.get('reason', 'bilinmeyen hata')}.")

    c = d.get("computer") or {}
    bits = []
    if c.get("disk"):
        drive = str(c["disk"]["path"]).rstrip(":\\/") or "sistem"
        bits.append(f"{drive} diskinde {round(c['disk']['free_gb'])} GB boş alan var")
    if c.get("gpu"):
        bits.append(f"ekran kartı {c['gpu']['temperature_c']} derece")
    if c.get("cpu_temperature_c") is not None:
        bits.append(f"işlemci {_deg(c['cpu_temperature_c'])} derece")
    if bits:
        parts.append("Bilgisayar: " + ", ".join(bits) + ".")
    if c.get("disk") and c["disk"]["free_gb"] < 10:
        parts.append("Dikkat: sistem diskinde yer azalmış.")
    if c.get("reboot_pending"):
        parts.append("Bekleyen bir Windows güncellemesi yeniden başlatma istiyor.")

    tasks = d.get("tasks") or []
    if tasks:
        items = []
        for t in tasks[:5]:
            item = f"saat {t['time']} {t['action']}"
            if t.get("text"):
                item += f": {t['text']}"
            elif t.get("value") is not None:
                item += f" yüzde {t['value']}"
            items.append(item)
        parts.append(f"Bugün {len(tasks)} zamanlanmış görev var: " + "; ".join(items) + ".")
    else:
        parts.append("Bugün için zamanlanmış görev yok.")

    n = d.get("news") or {}
    if n.get("ok"):
        heads = n["titles"][:SPOKEN_HEADLINES]
        parts.append(f"{n['source']} gündeminden başlıklar: " + " ".join(f"{i}) {h}." if not h.endswith(
            (".", "!", "?", "…")) else f"{i}) {h}" for i, h in enumerate(heads, 1)))
    elif not n.get("disabled"):
        parts.append(f"Haber başlıklarını alamadım ({n.get('reason', 'bilinmeyen hata')}).")
    return " ".join(parts)


# ---------- otomatik brifing ----------
class AutoBriefing:
    """Ayar açıksa günde bir kez, günün başında brifingi kendiliğinden verir.

    Ne zaman: 05:00–12:00 arasında; JARVIS bugün açıldıysa ilk sistem ölçümünde, gece boyunca açık
    kaldıysa kullanıcının bugünkü ilk etkinliğinden (uyandırma ya da yazılı istek) sonra, asistan
    boştayken. Brifing yerel komut yolundan geçer (sohbet ekranına yazılır, API çağrısı yok).
    """

    def __init__(self, store: Any, bus: Any, tool_service: Any,  # noqa: ANN401
                 clock: Callable[[], datetime] = datetime.now) -> None:
        from jarvis.core.events import Topics

        self.store = store
        self.tool_service = tool_service
        self.clock = clock
        self.started_day = clock().strftime("%Y-%m-%d")
        self.activity_day = ""
        self.running = False
        self.task: asyncio.Future | None = None
        bus.subscribe(Topics.SYSTEM_SNAPSHOT, self.tick)
        bus.subscribe(Topics.STATE_CHANGED, self._on_state)

    def _on_state(self, change: Any) -> None:  # noqa: ANN401
        from jarvis.core.state import AssistantState

        if getattr(change, "new", None) not in (None, AssistantState.STANDBY, AssistantState.ERROR):
            self.activity_day = self.clock().strftime("%Y-%m-%d")

    def due(self) -> bool:
        from jarvis.core.state import AssistantState

        s = self.store.settings.briefing
        now = self.clock()
        today = now.strftime("%Y-%m-%d")
        if not s.auto_enabled or s.last_auto_day == today or not 5 <= now.hour < 12 or self.running:
            return False
        if getattr(self.tool_service, "busy", False):
            return False
        if self.tool_service.state.state is not AssistantState.STANDBY:
            return False
        return today in (self.started_day, self.activity_day)

    def tick(self, _payload: Any = None) -> None:  # noqa: ANN401
        if not self.due():
            return
        self.store.settings.briefing.last_auto_day = self.clock().strftime("%Y-%m-%d")
        try:
            self.store.save()
        except OSError:
            log.warning("Otomatik brifing günü kaydedilemedi", exc_info=True)
        self.running = True
        log.info("Otomatik sabah brifingi veriliyor")

        async def go() -> None:
            try:
                await self.tool_service.handle_local("sabah brifingi")
            finally:
                self.running = False

        self.task = asyncio.ensure_future(go())


# ---------- araçlar ----------
def register(reg: ToolRegistry, ctx: ToolContext) -> BriefingService:
    store = ctx.extra.get("settings_store")

    def settings() -> Any:  # noqa: ANN401
        return store.settings.briefing if store is not None else None

    svc = ctx.extra.get("briefing_service") or BriefingService(
        settings=settings, snapshot=ctx.snapshot, scheduler=lambda: ctx.scheduler)
    ctx.extra["briefing_service"] = svc

    def brief(args: dict) -> ToolOutput:
        data = svc.collect()
        return ToolOutput(compose(data), data, untrusted=True)

    reg.register(ToolSpec(
        name="morning_briefing", title="Sabah brifingi", category="rutin",
        description=("Günün brifingi: tarih, kayıtlı şehrin hava durumu, bilgisayar durumu, bugünkü JARVIS görevleri, "
                     "haber başlıkları. / Daily briefing: date, weather, PC status, today's tasks, headlines."),
        input_schema=js.obj(), permission=Permission.READ, handler=brief, timeout_s=40))

    def set_city(args: dict) -> ToolOutput:
        if store is None:
            raise ToolError("ayar yok", "Ayarlar hazır değil.")
        city = re.sub(r"\s+", " ", args["city"]).strip(" .,!?'\"")
        if not city:
            store.settings.briefing.city = ""
            store.save()
            return ToolOutput("Brifing şehrini sildim; hava durumu artık söylenmeyecek.", {"city": ""})
        if not CITY_RE.fullmatch(city):
            raise ToolError("şehir", "Şehir adı yalnızca harf, boşluk ve - ' . içerebilir.")
        try:
            loc = svc.geocode(city)
        except FetchError as e:
            store.settings.briefing.city = city
            store.save()
            return ToolOutput(f"Şehri '{city}' olarak kaydettim ama şu an doğrulayamadım ({e}).",
                              {"city": city, "verified": False})
        if loc is None:
            raise ToolError("yok", f"'{city}' diye bir yer bulamadım; şehir adını kontrol eder misin?")
        store.settings.briefing.city = loc["name"]
        store.save()
        where = ", ".join(x for x in (loc["name"], loc.get("admin1") if loc.get("admin1") != loc["name"] else "",
                                      loc.get("country")) if x)
        return ToolOutput(f"Brifing şehri {where} olarak kaydedildi.", {"city": loc["name"], "verified": True,
                                                                         "location": where})

    reg.register(ToolSpec(
        name="set_briefing_city", title="Brifing şehri", category="rutin",
        description="Brifingdeki hava durumu için şehri kaydeder. / Saves the user's city for the briefing weather.",
        input_schema=js.obj(city=js.string("Şehir / city", 80)), permission=Permission.SAFE, handler=set_city,
        timeout_s=20))

    bus, tool_service = ctx.extra.get("bus"), ctx.extra.get("tool_service")
    if store is not None and bus is not None and tool_service is not None:
        ctx.extra["auto_briefing"] = AutoBriefing(store, bus, tool_service)
    return svc
