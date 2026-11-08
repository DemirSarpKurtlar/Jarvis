"""Medya ve ses araçları: oynat/duraklat/sonraki, "ne çalıyor", Spotify'da arama, YouTube'da ilk
videoyu açma, ses çıkış aygıtını değiştirme, uygulama başına ses ve Discord mikrofon/sağırlaştırma.

Dürüstlük kuralları:
- Medya komutundan sonra durum yeniden okunur; değişiklik görülemezse "doğrulayamadım" denir.
- Spotify'da çalmayı kendimiz başlatamıyoruz (ücretli Web API gerekmeden): arama açılır ve
  kullanıcıya oynatması gerektiği söylenir.
- Discord kısayolu bir aç/kapa tuşudur ve Discord'un durumu okunamaz: sonuç doğrulanamaz.
- Aygıt/uygulama sesi değiştirildikten sonra yeniden okunur.

Arka uçlar `ToolContext.extra["media"]` (`MediaBackends`) üzerinden verilir; testler sahtelerini koyar.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import quote, quote_plus

from jarvis.security.permission_manager import Permission
from jarvis.tools import schema as js
from jarvis.tools.media_win import (AppAudioBackend, AppAudioSession, AudioDevice, AudioDeviceBackend, MediaBackend,
                                    MediaInfo)
from jarvis.tools.registry import (ALL_PLATFORMS, NotSupportedError, ToolContext, ToolError, ToolOutput, ToolRegistry,
                                   ToolSpec, WINDOWS_ONLY)
from jarvis.tools.text import fold, phonetic_key, suffix_variants

log = logging.getLogger(__name__)

MEDIA_ACTIONS = ["play", "pause", "toggle", "next", "previous", "stop"]
_KEY_FOR = {"play": "play_pause", "pause": "play_pause", "toggle": "play_pause", "next": "next",
            "previous": "previous", "stop": "stop"}
_STATUS_TR = {"playing": "çalıyor", "paused": "duraklatıldı", "stopped": "durduruldu", "changing": "değişiyor",
              "opened": "açık", "closed": "kapalı", "unknown": "bilinmiyor"}
DISCORD_KEYS = {"mute": ("ctrl", "shift", "m"), "deafen": ("ctrl", "shift", "d")}
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


@dataclass
class MediaBackends:
    media: MediaBackend
    devices: AudioDeviceBackend
    apps: AppAudioBackend
    send_keys: Callable[[tuple[str, ...]], None]
    discord_running: Callable[[], bool]
    spotify_installed: Callable[[], bool]
    fetch: Callable[[str], str]
    sleep: Callable[[float], None] = time.sleep
    wait_s: float = 1.5           # komuttan sonra durum değişikliği için en çok bekleme
    extra: dict = field(default_factory=dict)


def default_backends() -> MediaBackends:
    from jarvis.tools import media_win as mw

    if mw.IS_WINDOWS:  # pragma: no cover - Windows
        return MediaBackends(mw.WinMedia(), mw.WinAudioDevices(), mw.WinAppAudio(), mw.send_keys,
                             mw.discord_running, mw.spotify_protocol_registered, mw.fetch_text)
    return MediaBackends(mw.UnsupportedMedia(), mw.UnsupportedAudioDevices(), mw.UnsupportedAppAudio(),
                         _unsupported_keys, mw.discord_running, lambda: False, mw.fetch_text)


def _unsupported_keys(keys: tuple[str, ...]) -> None:
    raise NotSupportedError()


def clean_text(text: str, limit: int = 120) -> str:
    """Dış kaynaktan gelen kısa metin (şarkı/video adı): kontrol karakterleri atılır, kısaltılır."""
    t = _CONTROL_CHARS.sub(" ", str(text or "")).strip()
    return t if len(t) <= limit else t[: limit - 1] + "…"


def describe(info: MediaInfo) -> str:
    what = " - ".join(x for x in (clean_text(info.artist, 60), clean_text(info.title, 80)) if x)
    return what or "adı bilinmeyen bir parça"


# ---------- YouTube ----------
def parse_first_video(html: str) -> dict | None:
    """YouTube arama sayfasındaki ytInitialData içinden ilk normal videoyu (reklam/kısa değil) bulur."""
    if not html:
        return None
    m = re.search(r"(?:var\s+ytInitialData|window\[\"ytInitialData\"\])\s*=\s*", html)
    if m:
        try:
            data, _ = json.JSONDecoder().raw_decode(html, m.end())
        except ValueError:
            data = None
        if data is not None:
            for vr in _video_renderers(data):
                vid = vr.get("videoId")
                if isinstance(vid, str) and VIDEO_ID.fullmatch(vid):
                    return {"video_id": vid, "title": _runs_text(vr.get("title")),
                            "channel": _runs_text(vr.get("ownerText") or vr.get("longBylineText")),
                            "length": _runs_text(vr.get("lengthText"))}
    # Yedek: yapı değişmişse ilk videoRenderer kimliği
    m = re.search(r"\"videoRenderer\"\s*:\s*\{\s*\"videoId\"\s*:\s*\"([A-Za-z0-9_-]{11})\"", html)
    return {"video_id": m.group(1), "title": "", "channel": "", "length": ""} if m else None


def _video_renderers(node, depth: int = 0):  # noqa: ANN001, ANN202
    if depth > 40:
        return
    if isinstance(node, dict):
        vr = node.get("videoRenderer")
        if isinstance(vr, dict):
            yield vr
        for k, v in node.items():
            if k in ("videoRenderer", "adSlotRenderer", "promotedVideoRenderer", "reelShelfRenderer"):
                continue
            yield from _video_renderers(v, depth + 1)
    elif isinstance(node, list):
        for v in node:
            yield from _video_renderers(v, depth + 1)


def _runs_text(x) -> str:  # noqa: ANN001
    if not isinstance(x, dict):
        return ""
    if isinstance(x.get("simpleText"), str):
        return clean_text(x["simpleText"])
    runs = x.get("runs")
    if isinstance(runs, list):
        return clean_text("".join(r.get("text", "") for r in runs if isinstance(r, dict)))
    return ""


# ---------- aygıt eşleştirme ----------
# (anahtar kelime, puan): yüksek puan daha iyi eşleşme
DEVICE_CATEGORIES: dict[str, tuple[tuple[str, int], ...]] = {
    "kulaklik": (("headphone", 3), ("kulaklik", 3), ("headset", 2), ("earphone", 2), ("earbud", 2),
                 ("airpods", 2), ("buds", 2), ("hands free", 1)),
    "hoparlor": (("speaker", 3), ("hoparlor", 3), ("soundbar", 2), ("ses cubugu", 2)),
    "monitor": (("monitor", 3), ("hdmi", 2), ("displayport", 2), ("display audio", 2), ("tv", 2),
                ("nvidia high definition", 1), ("amd high definition", 1), ("intel r display", 1)),
}
_CATEGORY_WORDS = (("kulakl", "kulaklik"), ("headphone", "kulaklik"), ("headset", "kulaklik"),
                   ("hoparl", "hoparlor"), ("speaker", "hoparlor"), ("monitor", "monitor"), ("ekran", "monitor"),
                   ("televizyon", "monitor"), ("tv", "monitor"), ("hdmi", "monitor"))
_DEVICE_STOP = {"ses", "sesi", "cikis", "cikisi", "aygit", "aygiti", "cihaz", "cihazi", "cihazina", "aygitina"}


def device_category(query: str) -> str | None:
    for w in fold(query).replace("'", " ").split():
        for prefix, cat in _CATEGORY_WORDS:
            if w.startswith(prefix) and (len(prefix) > 2 or w in ("tv", "tvye", "tvyi")):
                return cat
    return None


def _query_words(query: str) -> list[str]:
    words = []
    for w in fold(query).split():
        w = w.split("'", 1)[0]
        if w and w not in _DEVICE_STOP:
            words.append(w)
    return words


_HANDS_FREE = ("hands free", "mikrofonlu", "headset")


def _is_category_word(w: str) -> bool:
    return any(w.startswith(p) and (len(p) > 2 or w in ("tv", "tvye", "tvyi")) for p, _ in _CATEGORY_WORDS)


def score_device(query: str, name: str) -> int:
    """Aygıt adının sorguya uygunluğu (0 = eşleşmez). "kulaklık" gibi bir tür sözcüğü varsa türün anahtar
    kelimeleri puanlanır; eller-serbest (mikrofonlu, düşük kaliteli) uç bir puan geride kalır."""
    n = fold(name)
    cat = device_category(query)
    cat_score = 0
    if cat:
        n_sp = f" {n} "
        for kw, pts in DEVICE_CATEGORIES[cat]:
            if (f" {kw}" in n_sp) if len(kw) <= 3 else (kw in n):
                cat_score = max(cat_score, pts)
        if cat == "kulaklik" and cat_score > 1 and any(h in n for h in _HANDS_FREE):
            cat_score -= 1
    words = [w for w in _query_words(query) if not (cat and _is_category_word(w))]
    gen = 0
    if words:
        def has(w: str) -> bool:
            return any(c in n for c in {w, *(fold(v) for v in suffix_variants(w))} if len(c) >= 2)

        joined = " ".join(words)
        if all(has(w) for w in words):
            gen = 4 if fold(joined) in n else 3
        elif phonetic_key(joined) and phonetic_key(joined) in phonetic_key(name):
            gen = 2
    return cat_score + gen


def pick_device(query: str, devices: list[AudioDevice]) -> AudioDevice:
    scored = sorted(((score_device(query, d.name), d) for d in devices), key=lambda x: x[0], reverse=True)
    if not scored or scored[0][0] == 0:
        names = ", ".join(d.name for d in devices) or "hiç aygıt yok"
        raise ToolError("aygıt yok", f"“{query}” ile eşleşen bir ses çıkış aygıtı bulamadım. Aygıtlar: {names}.")
    top = [d for s, d in scored if s == scored[0][0]]
    if len(top) > 1:
        raise ToolError("belirsiz", "Birden fazla aygıt eşleşti: " + ", ".join(d.name for d in top) +
                        ". Hangisini istediğini adıyla söyler misin?")
    return top[0]


# ---------- uygulama eşleştirme ----------
APP_ALIASES: dict[str, frozenset[str]] = {
    "chrome": frozenset({"chrome.exe"}), "krom": frozenset({"chrome.exe"}), "google chrome": frozenset({"chrome.exe"}),
    "edge": frozenset({"msedge.exe"}), "ec": frozenset({"msedge.exe"}), "firefox": frozenset({"firefox.exe"}),
    "opera": frozenset({"opera.exe"}), "brave": frozenset({"brave.exe"}),
    "discord": frozenset({"discord.exe", "discordptb.exe", "discordcanary.exe"}),
    "diskort": frozenset({"discord.exe", "discordptb.exe", "discordcanary.exe"}),
    "spotify": frozenset({"spotify.exe"}), "spotifay": frozenset({"spotify.exe"}),
    "steam": frozenset({"steam.exe", "steamwebhelper.exe"}), "vlc": frozenset({"vlc.exe"}),
    "teams": frozenset({"ms-teams.exe", "teams.exe"}), "zoom": frozenset({"zoom.exe"}),
    "whatsapp": frozenset({"whatsapp.exe", "whatsapp.root.exe"}), "telegram": frozenset({"telegram.exe"}),
    "tarayici": frozenset({"chrome.exe", "msedge.exe", "firefox.exe", "opera.exe", "brave.exe"}),
    "sistem sesleri": frozenset({""}), "sistem": frozenset({""}),
}
_GENITIVE = ("nin", "nun", "in", "un")


def app_query_variants(query: str) -> list[str]:
    out: list[str] = []
    base = query.split("'", 1)[0] if "'" in query else query
    for c in [base, *suffix_variants(base)]:
        f = fold(c)
        if f and f not in out:
            out.append(f)
        for suf in _GENITIVE:
            if f.endswith(suf) and len(f) - len(suf) >= 3:
                stem = f[: -len(suf)]
                if stem not in out:
                    out.append(stem)
    return out


def match_sessions(query: str, sessions: list[AppAudioSession], exe_hints: set[str] | None = None) \
        -> list[AppAudioSession]:
    variants = app_query_variants(query)
    exes = set(exe_hints or ())
    for v in variants:
        exes |= APP_ALIASES.get(v, frozenset())
    out = []
    for s in sessions:
        stem = fold(s.process).removesuffix(" exe")
        display = fold(s.name)
        ok = s.process in exes and (s.process or s.pid == 0)
        for q in variants:
            if ok:
                break
            if not stem:
                continue
            compact, sc = q.replace(" ", ""), stem.replace(" ", "")
            if compact == sc or (len(sc) >= 4 and compact.startswith(sc) and len(compact) - len(sc) <= 4) or \
                    (len(compact) >= 4 and sc.startswith(compact)) or \
                    (len(q) >= 4 and display and q in display) or phonetic_key(q) == phonetic_key(stem):
                ok = True
        if ok:
            out.append(s)
    return out


def session_label(s: AppAudioSession) -> str:
    if not s.process:
        return "Sistem sesleri"
    return clean_text(s.name, 40) if s.name and not s.name.startswith("@") else s.process.removesuffix(".exe")


# ---------- araçlar ----------
class MediaTools:
    def __init__(self, ctx: ToolContext, backends: MediaBackends) -> None:
        self.ctx, self.b = ctx, backends

    def _open(self, target: str) -> None:
        if self.ctx.opener is None:
            raise NotSupportedError()
        self.ctx.opener(target)

    def _wait_for(self, check: Callable[[MediaInfo | None], bool]) -> tuple[MediaInfo | None, bool]:
        """Durum değişene kadar kısa aralıklarla yeniden okur."""
        info: MediaInfo | None = None
        steps = max(1, int(self.b.wait_s / 0.15))
        for _ in range(steps):
            self.b.sleep(0.15)
            try:
                info = self.b.media.current()
            except Exception:  # noqa: BLE001  okunamayan durum = doğrulanamadı
                info = None
            if check(info):
                return info, True
        return info, False

    # --- oynat / duraklat / sonraki ---
    def media_control(self, args: dict) -> ToolOutput:
        action = args["action"]
        m = self.b.media
        if not m.gsmtc_available():
            m.send_key(_KEY_FOR[action])
            what = {"next": "sonraki parça", "previous": "önceki parça", "stop": "durdur"}.get(action, "oynat/duraklat")
            note = " Bu tuş aç/kapa çalışır; müzik zaten durmuşsa yeniden başlatabilir." \
                if action in ("pause", "play") else ""
            return ToolOutput(f"Medya tuşuna bastım ({what}). Hangi programın ne çaldığını okuyamadığım için "
                              f"sonucu doğrulayamadım.{note}", {"action": action, "method": "media_key",
                                                                "verified": False})
        before = m.current()
        if before is None:
            if action not in ("play", "toggle"):
                raise ToolError("medya yok", "Şu anda çalan ya da duraklatılmış bir medya bulamadım.")
            m.send_key("play_pause")
            after, ok = self._wait_for(lambda i: i is not None and i.status == "playing")
            if ok and after:
                return ToolOutput(f"Çalmaya başladı: {describe(after)} ({after.app}).",
                                  {"action": action, "verified": True, "now": _info_dict(after)}, untrusted=True)
            return ToolOutput("Açık bir medya oturumu yoktu; oynat tuşuna bastım ama bir şeyin çalmaya başladığını "
                              "göremedim. Spotify ya da tarayıcıda bir şey açıp yeniden dene.",
                              {"action": action, "verified": False})
        if action in ("pause", "stop") and before.status != "playing":
            return ToolOutput(f"Zaten {_STATUS_TR.get(before.status, 'durmuş')}: {describe(before)}.",
                              {"action": action, "changed": False, "now": _info_dict(before)}, untrusted=True)
        if action == "play" and before.status == "playing":
            return ToolOutput(f"Zaten çalıyor: {describe(before)} ({before.app}).",
                              {"action": action, "changed": False, "now": _info_dict(before)}, untrusted=True)

        method = "gsmtc"
        if not m.control(action):
            m.send_key(_KEY_FOR[action])
            method = "media_key"

        def changed(i: MediaInfo | None) -> bool:
            if i is None:
                return action in ("pause", "stop")
            if action == "play":
                return i.status == "playing"
            if action in ("pause", "stop"):
                return i.status != "playing"
            if action == "toggle":
                return i.status != before.status and i.status in ("playing", "paused", "stopped")
            return (i.title, i.artist) != (before.title, before.artist)

        after, ok = self._wait_for(changed)
        data = {"action": action, "method": method, "verified": ok, "now": _info_dict(after) if after else None}
        if not ok:
            if action == "previous":
                return ToolOutput("Önceki komutunu gönderdim ama parça değişmedi; büyük olasılıkla şarkı başa sarıldı. "
                                  "Önceki şarkı için bir kez daha söyle.", data, untrusted=True)
            return ToolOutput(f"Komutu {before.app} uygulamasına gönderdim ama değişikliği doğrulayamadım.", data,
                              untrusted=True)
        if action in ("next", "previous"):
            return ToolOutput(f"Şimdi çalıyor: {describe(after)}." if after else "Parçayı değiştirdim.", data,
                              untrusted=True)
        if after is None or after.status != "playing":
            verb = "Durdurdum" if action == "stop" else "Duraklattım"
            return ToolOutput(f"{verb}: {describe(before)}.", data, untrusted=True)
        return ToolOutput(f"Devam ediyor: {describe(after)} ({after.app}).", data, untrusted=True)

    def now_playing(self, args: dict) -> ToolOutput:
        m = self.b.media
        if not m.gsmtc_available():
            raise ToolError("gsmtc yok", "Çalan medyayı okuyamıyorum: Windows medya bileşeni (winrt paketi) kurulu "
                                         "değil.")
        info = m.current()
        if info is None:
            return ToolOutput("Şu anda çalan ya da duraklatılmış bir medya yok.", {"session": None})
        return ToolOutput(f"{info.app}: {describe(info)} ({_STATUS_TR.get(info.status, info.status)}).",
                          {"session": _info_dict(info)}, untrusted=True)

    # --- Spotify / YouTube ---
    def prepare_query(self, args: dict) -> dict:
        q = re.sub(r"\s+", " ", _CONTROL_CHARS.sub(" ", args["query"])).strip()
        if not q:
            raise ToolError("boş", "Ne aramam gerektiğini anlayamadım.")
        return {**args, "query": q}

    def spotify_play(self, args: dict) -> ToolOutput:
        q = args["query"]
        desktop = False
        try:
            desktop = bool(self.b.spotify_installed())
        except Exception:  # noqa: BLE001
            log.debug("Spotify kurulu mu okunamadı", exc_info=True)
        if not desktop and self.ctx.apps is not None:
            try:
                desktop = self.ctx.apps.best("spotify") is not None and _is_windows()
            except Exception:  # noqa: BLE001
                desktop = False
        if desktop:
            uri = "spotify:search:" + quote(q, safe="")
            self._open(uri)
            return ToolOutput(f"Spotify'da “{q}” aramasını açtım. Çalmayı kendim başlatamıyorum; ilk sonuca "
                              "tıklayıp oynatman gerekiyor.", {"uri": uri, "app": "desktop", "playing": False})
        url = "https://open.spotify.com/search/" + quote(q, safe="")
        self._open(url)
        return ToolOutput(f"Spotify uygulaması kurulu görünmüyor; “{q}” aramasını tarayıcıda (open.spotify.com) "
                          "açtım. Çalmak için sonuca tıklaman gerekiyor.", {"url": url, "app": "web", "playing": False})

    def youtube_play(self, args: dict) -> ToolOutput:
        q = args["query"]
        results = "https://www.youtube.com/results?search_query=" + quote_plus(q)
        video = None
        try:
            video = parse_first_video(self.b.fetch(results))
        except Exception as e:  # noqa: BLE001  ağ hatası, zaman aşımı, yönlendirme
            log.info("YouTube araması okunamadı: %s", e.__class__.__name__)
        if not video:
            self._open(results)
            return ToolOutput(f"İlk videoyu bulamadım; “{q}” için YouTube arama sonuçlarını açtım.",
                              {"url": results, "video": None})
        url = f"https://www.youtube.com/watch?v={video['video_id']}"
        self._open(url)
        title = video["title"] or "ilk sonuç"
        by = f" ({video['channel']})" if video["channel"] else ""
        return ToolOutput(f"YouTube'da “{title}”{by} videosunu açtım.", {"url": url, "video": video,
                                                                       "results_url": results}, untrusted=True)

    # --- ses çıkış aygıtı ---
    def list_audio(self, args: dict) -> ToolOutput:
        devices = self.b.devices.list_outputs()
        try:
            sessions = self.b.apps.sessions()
        except ToolError:
            sessions = []
        default = next((d.name for d in devices if d.is_default), "yok")
        apps = [{"app": session_label(s), "process": s.process, "volume": s.volume, "muted": s.muted}
                for s in sessions]
        return ToolOutput(f"{len(devices)} ses çıkış aygıtı var, varsayılan: {default}. "
                          f"Ses çalan {len(apps)} uygulama oturumu var.",
                          {"outputs": [{"name": d.name, "default": d.is_default} for d in devices], "apps": apps},
                          untrusted=True)

    def set_audio_output(self, args: dict) -> ToolOutput:
        devices = self.b.devices.list_outputs()
        if not devices:
            raise ToolError("aygıt yok", "Etkin bir ses çıkış aygıtı bulamadım.")
        target = pick_device(args["device"], devices)
        if target.is_default:
            return ToolOutput(f"Ses zaten {target.name} üzerinden çıkıyor.", {"device": target.name, "changed": False})
        self.b.devices.set_default(target.id)
        now = next((d for d in self.b.devices.list_outputs() if d.is_default), None)
        if now is None or now.id != target.id:
            raise ToolError("doğrulanamadı", f"{target.name} aygıtına geçmeyi denedim ama Windows varsayılan aygıtı "
                                             "değiştirmedi.")
        return ToolOutput(f"Ses çıkışını {target.name} yaptım.", {"device": target.name, "changed": True})

    # --- uygulama sesi ---
    def prepare_app_volume(self, args: dict) -> dict:
        mode, value = args["mode"], args["value"]
        if mode == "set" and not 0 <= value <= 100:
            raise ToolError("değer", "Ses yüzdesi 0 ile 100 arasında olmalı.")
        if mode == "change" and value == 0:
            raise ToolError("değer", "Değişim miktarı 0 olamaz.")
        app = args["app"].strip()
        if not app:
            raise ToolError("uygulama", "Hangi uygulamanın sesini değiştireceğimi anlayamadım.")
        return {**args, "app": app}

    def _exe_hints(self, query: str) -> set[str]:
        if self.ctx.apps is None:
            return set()
        try:
            from jarvis.tools.applications import exe_names_for

            entry = self.ctx.apps.best(query)
            return exe_names_for(entry) if entry else set()
        except Exception:  # noqa: BLE001
            return set()

    def set_app_volume(self, args: dict) -> ToolOutput:
        app, mode, value = args["app"], args["mode"], args["value"]
        sessions = self.b.apps.sessions()
        hits = match_sessions(app, sessions, self._exe_hints(app))
        if not hits:
            running = sorted({session_label(s) for s in sessions})
            extra = f" Ses oturumu olan uygulamalar: {', '.join(running)}." if running else ""
            raise ToolError("oturum yok", f"“{app}” için bir ses oturumu bulamadım; uygulama açık değil ya da hiç "
                                          f"ses çalmadı.{extra}")
        label = session_label(hits[0])
        for s in hits:
            if mode in ("mute", "unmute"):
                self.b.apps.set_mute(s.pid, mode == "mute")
            else:
                new = value if mode == "set" else max(0, min(100, s.volume + value))
                self.b.apps.set_volume(s.pid, new)
                if s.muted and new > 0 and (mode == "set" or value > 0):
                    self.b.apps.set_mute(s.pid, False)
        pids = {s.pid for s in hits}
        after = {s.pid: s for s in self.b.apps.sessions() if s.pid in pids}
        if not after:
            raise ToolError("doğrulanamadı", f"{label} sesini değiştirdim ama oturum kapandığı için doğrulayamadım.")
        first = hits[0] if hits[0].pid in after else next(h for h in hits if h.pid in after)
        vol, muted = after[first.pid].volume, all(s.muted for s in after.values())
        data = {"app": label, "sessions": len(after), "volume": vol, "muted": muted}
        if mode == "mute":
            if not muted:
                raise ToolError("doğrulanamadı", f"{label} sesini kapatmayı denedim ama hâlâ açık görünüyor.")
            return ToolOutput(f"{label} sesini kapattım.", data)
        if mode == "unmute":
            if muted:
                raise ToolError("doğrulanamadı", f"{label} sesini açmayı denedim ama hâlâ kapalı görünüyor.")
            return ToolOutput(f"{label} sesini açtım (%{vol}).", data)
        expected = value if mode == "set" else max(0, min(100, first.volume + value))
        if abs(vol - expected) > 1:
            raise ToolError("doğrulanamadı", f"{label} sesini %{expected} yapmayı denedim ama şu an %{vol} görünüyor.")
        verb = "yaptım" if mode == "set" else ("açtım" if value > 0 else "kıstım")
        return ToolOutput(f"{label} sesini %{vol} {verb}.", data)

    # --- Discord ---
    def discord_voice(self, args: dict) -> ToolOutput:
        action = args["action"]
        if not self.b.discord_running():
            raise ToolError("discord yok", "Discord çalışmıyor; önce Discord'u açman gerekiyor.")
        keys = DISCORD_KEYS[action]
        self.b.send_keys(keys)
        combo = "+".join(k.capitalize() for k in keys)
        what = "mikrofonu aç/kapat" if action == "mute" else "sağırlaştır aç/kapat"
        return ToolOutput(f"Discord'a {combo} ({what}) kısayolunu gönderdim. Bu bir aç/kapa tuşu ve Discord'un durumunu "
                          "okuyamıyorum, sonucu doğrulayamadım. Discord'da bu kısayolu değiştirdiysen işe yaramaz.",
                          {"action": action, "keys": combo, "verified": False})


def _is_windows() -> bool:
    from jarvis.tools import registry as regmod

    return regmod.CURRENT_PLATFORM == "win32"


def _info_dict(i: MediaInfo) -> dict:
    return {"title": clean_text(i.title), "artist": clean_text(i.artist, 80), "album": clean_text(i.album, 80),
            "app": i.app, "status": i.status}


def register(reg: ToolRegistry, ctx: ToolContext) -> MediaTools:
    backends = ctx.extra.get("media")
    if backends is None:
        backends = ctx.extra["media"] = default_backends()
    t = MediaTools(ctx, backends)

    reg.register(ToolSpec(
        name="media_control", title="Medya kontrolü", category="medya", platforms=WINDOWS_ONLY,
        description="Çalan müziği/videoyu oynatır, duraklatır, sonraki/önceki parçaya geçer (her oynatıcı). / "
                    "Play, pause, next, previous for the active media player.",
        input_schema=js.obj(action=js.string("İşlem / action", enum=MEDIA_ACTIONS)),
        permission=Permission.SAFE, handler=t.media_control, timeout_s=15))
    reg.register(ToolSpec(
        name="now_playing", title="Ne çalıyor", category="medya", platforms=WINDOWS_ONLY,
        description="Şu an çalan parçayı ve uygulamayı söyler. / What's playing now.",
        input_schema=js.obj(), permission=Permission.READ, handler=t.now_playing, timeout_s=10))
    reg.register(ToolSpec(
        name="spotify_play", title="Spotify'da ara", category="medya", platforms=ALL_PLATFORMS,
        description="Spotify'da şarkı/sanatçı aramasını açar; çalmayı kullanıcı başlatır. / Opens a Spotify search.",
        input_schema=js.obj(query=js.string("Şarkı ya da sanatçı / song or artist", 150, 1)),
        permission=Permission.SAFE, handler=t.spotify_play, prepare=t.prepare_query))
    reg.register(ToolSpec(
        name="youtube_play", title="YouTube'da aç", category="medya", platforms=ALL_PLATFORMS,
        description="YouTube'da arar ve ilk videoyu tarayıcıda açar. / Opens the first YouTube result.",
        input_schema=js.obj(query=js.string("Aranacak / query", 150, 1)),
        permission=Permission.SAFE, handler=t.youtube_play, prepare=t.prepare_query, timeout_s=20))
    reg.register(ToolSpec(
        name="list_audio", title="Ses aygıtları", category="medya", platforms=WINDOWS_ONLY,
        description="Ses çıkış aygıtlarını ve uygulama ses düzeylerini listeler. / Lists outputs and per-app volume.",
        input_schema=js.obj(), permission=Permission.READ, handler=t.list_audio))
    reg.register(ToolSpec(
        name="set_audio_output", title="Ses çıkışını değiştir", category="medya", platforms=WINDOWS_ONLY,
        description="Varsayılan ses çıkışını değiştirir (kulaklık, hoparlör ya da aygıt adı). / Switches output device.",
        input_schema=js.obj(device=js.string("Aygıt: kulaklık, hoparlör, monitör ya da ad / device", 100, 1)),
        permission=Permission.SAFE, handler=t.set_audio_output))
    reg.register(ToolSpec(
        name="set_app_volume", title="Uygulama sesi", category="medya", platforms=WINDOWS_ONLY,
        description="Bir uygulamanın sesini ayarlar/değiştirir/kapatır. value: set=0..100, change=±puan. / "
                    "Per-app volume.",
        input_schema=js.obj(app=js.string("Uygulama / app", 60, 1),
                            mode=js.string("Kip / mode", enum=["set", "change", "mute", "unmute"]),
                            value=js.integer("Değer / value", -100, 100)),
        permission=Permission.SAFE, handler=t.set_app_volume, prepare=t.prepare_app_volume))
    reg.register(ToolSpec(
        name="discord_voice", title="Discord mikrofon/sağırlaştır", category="medya", platforms=WINDOWS_ONLY,
        description="Discord'da mikrofonu ya da sağırlaştırmayı aç/kapa yapar (Ctrl+Shift+M/D). / Toggles Discord "
                    "mute/deafen.",
        input_schema=js.obj(action=js.string("İşlem / action", enum=list(DISCORD_KEYS))),
        permission=Permission.SAFE, handler=t.discord_voice))
    return t
