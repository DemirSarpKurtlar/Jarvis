"""Medya ve ses araçları: medya kontrolü, ne çalıyor, Spotify/YouTube, ses çıkışı, uygulama sesi,
Discord. Gerçek Windows'a dokunulmaz; arka uçların sahteleri `ctx.extra["media"]` ile verilir."""

import asyncio
import ctypes
import json

import pytest

from jarvis.security.permission_manager import Permission, PermissionManager
from jarvis.tools import registry as regmod
from jarvis.tools.defaults import build_registry
from jarvis.tools.media import (MediaBackends, match_sessions, parse_first_video, pick_device, score_device)
from jarvis.tools.media_win import (INPUT, KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, VK, AppAudioSession,
                                    AudioDevice, MediaInfo, build_key_inputs, pretty_app)
from jarvis.tools.registry import NOT_SUPPORTED, ToolError
from jarvis.tools.applications import AppIndex
from tests.tool_fakes import FAKE_APPS, make_ctx

MEDIA_TOOLS = {"media_control", "now_playing", "spotify_play", "youtube_play", "list_audio", "set_audio_output",
               "set_app_volume", "discord_voice"}


def run(coro):
    return asyncio.run(coro)


# ---------- sahteler ----------
class FakeMedia:
    def __init__(self, info=None, gsmtc=True, control_ok=True, effective=True, playlist=None):
        self.info, self.gsmtc, self.control_ok, self.effective = info, gsmtc, control_ok, effective
        self.playlist = playlist or ["Şımarık", "Dudu", "Kuzu Kuzu"]
        self.calls, self.keys = [], []

    def gsmtc_available(self):
        return self.gsmtc

    def current(self):
        return self.info

    def _apply(self, action):
        if not self.effective or self.info is None:
            return
        i = self.info
        if action in ("play", "pause", "toggle", "stop"):
            status = {"play": "playing", "pause": "paused", "stop": "stopped",
                      "toggle": "paused" if i.status == "playing" else "playing"}[action]
            self.info = MediaInfo(i.title, i.artist, i.album, i.app, status)
        else:
            idx = self.playlist.index(i.title) if i.title in self.playlist else 0
            idx = (idx + (1 if action == "next" else -1)) % len(self.playlist)
            self.info = MediaInfo(self.playlist[idx], i.artist, i.album, i.app, i.status)

    def control(self, action):
        self.calls.append(action)
        if self.control_ok:
            self._apply(action)
        return self.control_ok

    def send_key(self, key):
        self.keys.append(key)
        action = {"play_pause": "toggle", "next": "next", "previous": "previous", "stop": "stop"}[key]
        self._apply(action)


class FakeDevices:
    def __init__(self, names, default=0, obey=True):
        self.devices = [AudioDevice(f"{{id-{i}}}", n, i == default) for i, n in enumerate(names)]
        self.obey, self.set_calls = obey, []

    def list_outputs(self):
        return list(self.devices)

    def set_default(self, device_id):
        self.set_calls.append(device_id)
        if self.obey:
            self.devices = [AudioDevice(d.id, d.name, d.id == device_id) for d in self.devices]


class FakeAppAudio:
    def __init__(self, sessions, obey=True):
        self.list, self.obey = {s.pid: s for s in sessions}, obey

    def sessions(self):
        return list(self.list.values())

    def set_volume(self, pid, percent):
        s = self.list[pid]
        if self.obey:
            self.list[pid] = AppAudioSession(s.pid, s.process, s.name, percent, s.muted)

    def set_mute(self, pid, muted):
        s = self.list[pid]
        if self.obey:
            self.list[pid] = AppAudioSession(s.pid, s.process, s.name, s.volume, muted)


SESSIONS = [
    AppAudioSession(0, "", "@%SystemRoot%\\System32\\AudioSrv.Dll,-202", 100, False),
    AppAudioSession(101, "chrome.exe", "", 80, False),
    AppAudioSession(102, "chrome.exe", "", 60, False),
    AppAudioSession(201, "discord.exe", "", 70, True),
    AppAudioSession(301, "spotify.exe", "Spotify", 90, False),
]

PLAYING = MediaInfo("Şımarık", "Tarkan", "Ölürüm Sana", "Spotify", "playing")


def yt_html(items, var=True):
    data = {"contents": {"twoColumnSearchResultsRenderer": {"primaryContents": {"sectionListRenderer": {
        "contents": [{"itemSectionRenderer": {"contents": items}}]}}}}}
    prefix = "var ytInitialData = " if var else 'window["ytInitialData"] = '
    return f"<html><script>{prefix}{json.dumps(data)};</script></html>"


def video(vid, title="Lofi Girl radyo", channel="Lofi Girl"):
    return {"videoRenderer": {"videoId": vid, "title": {"runs": [{"text": title}]},
                              "ownerText": {"runs": [{"text": channel}]}, "lengthText": {"simpleText": "1:02:03"}}}


class Env:
    def __init__(self, tmp_path, media=None, devices=None, apps=None, discord=True, spotify=True, fetch=None):
        self.media = media or FakeMedia(PLAYING)
        self.devices = devices or FakeDevices(["Speakers (Realtek(R) Audio)", "Headphones (WH-1000XM4 Stereo)"])
        self.apps = apps or FakeAppAudio(SESSIONS)
        self.sent, self.fetched = [], []
        self.fetch_result = fetch

        def fetcher(url):
            self.fetched.append(url)
            if isinstance(self.fetch_result, Exception):
                raise self.fetch_result
            return self.fetch_result or ""

        self.backends = MediaBackends(self.media, self.devices, self.apps, self.sent.append, lambda: discord,
                                      lambda: spotify, fetcher, sleep=lambda s: None, wait_s=0.3)
        self.ctx = make_ctx(tmp_path)
        self.ctx.extra["media"] = self.backends
        self.reg = build_registry(self.ctx)

    @property
    def opened(self):
        return self.ctx.opener.opened

    def run(self, name, args, **kw):
        return run(self.reg.execute(name, args, **kw))


@pytest.fixture
def win(monkeypatch):
    monkeypatch.setattr(regmod, "CURRENT_PLATFORM", "win32")


# ---------- kayıt ----------
def test_tools_registered_with_short_descriptions(tmp_path, win):
    env = Env(tmp_path)
    for name in MEDIA_TOOLS:
        spec = env.reg.get(name)
        assert spec is not None, name
        assert len(spec.description) < 200, name
        assert spec.permission in (Permission.READ, Permission.SAFE), name
    tools = {t["name"] for t in env.reg.to_claude_tools()}
    assert MEDIA_TOOLS <= tools


def test_windows_only_tools_unsupported_elsewhere(tmp_path, monkeypatch):
    monkeypatch.setattr(regmod, "CURRENT_PLATFORM", "linux")
    env = Env(tmp_path)
    for name, args in (("media_control", {"action": "pause"}), ("now_playing", {}), ("list_audio", {}),
                       ("set_audio_output", {"device": "kulaklık"}), ("discord_voice", {"action": "mute"}),
                       ("set_app_volume", {"app": "chrome", "mode": "set", "value": 30})):
        r = env.run(name, args)
        assert not r.ok and r.error == NOT_SUPPORTED, name
    assert env.media.calls == [] and env.sent == []
    # Spotify/YouTube her sistemde (tarayıcıyla) çalışır
    assert env.run("youtube_play", {"query": "lofi"}).ok


def test_default_backends_on_linux_say_not_supported(tmp_path, win):
    ctx = make_ctx(tmp_path)
    reg = build_registry(ctx)          # varsayılan (gerçek) arka uçlar: Linux'ta "desteklenmiyor"
    r = run(reg.execute("media_control", {"action": "pause"}))
    assert not r.ok and "desteklenmiyor" in r.error
    r = run(reg.execute("list_audio", {}))
    assert not r.ok and "desteklenmiyor" in r.error


def test_no_tool_needs_confirmation_but_read_only_blocks_changes(tmp_path, win):
    env = Env(tmp_path)
    r = env.run("media_control", {"action": "pause"})            # onay geri çağrısı yok: yine de çalışır
    assert r.ok and r.decision == "izin"
    env.reg.permissions = PermissionManager(read_only=True)
    r = env.run("discord_voice", {"action": "mute"})
    assert not r.ok and env.sent == []
    assert env.run("now_playing", {}).ok                           # okuma serbest


# ---------- medya kontrolü ----------
def test_pause_playing_is_verified(tmp_path, win):
    env = Env(tmp_path)
    r = env.run("media_control", {"action": "pause"})
    assert r.ok and "Duraklattım" in r.summary and "Tarkan" in r.summary
    assert r.data["verified"] and r.data["method"] == "gsmtc"
    assert env.media.calls == ["pause"] and env.media.keys == []
    assert r.untrusted and "untrusted_tool_output" in r.to_claude_content()


def test_pause_when_already_paused_does_not_toggle(tmp_path, win):
    env = Env(tmp_path, media=FakeMedia(MediaInfo("Dudu", "Tarkan", "", "Spotify", "paused")))
    r = env.run("media_control", {"action": "pause"})
    assert r.ok and "Zaten duraklatıldı" in r.summary
    assert env.media.calls == [] and env.media.keys == []


def test_play_when_already_playing(tmp_path, win):
    env = Env(tmp_path)
    r = env.run("media_control", {"action": "play"})
    assert r.ok and "Zaten çalıyor" in r.summary and env.media.calls == []


def test_resume_paused(tmp_path, win):
    env = Env(tmp_path, media=FakeMedia(MediaInfo("Dudu", "Tarkan", "", "Spotify", "paused")))
    r = env.run("media_control", {"action": "play"})
    assert r.ok and "Devam ediyor" in r.summary and r.data["verified"]


def test_next_reports_new_track(tmp_path, win):
    env = Env(tmp_path)
    r = env.run("media_control", {"action": "next"})
    assert r.ok and "Şimdi çalıyor: Tarkan - Dudu" in r.summary
    assert r.data["now"]["title"] == "Dudu"


def test_gsmtc_refuses_falls_back_to_media_key(tmp_path, win):
    env = Env(tmp_path, media=FakeMedia(PLAYING, control_ok=False))
    r = env.run("media_control", {"action": "next"})
    assert r.ok and env.media.keys == ["next"] and r.data["method"] == "media_key" and r.data["verified"]


def test_unverified_change_is_reported_honestly(tmp_path, win):
    env = Env(tmp_path, media=FakeMedia(PLAYING, effective=False))
    r = env.run("media_control", {"action": "next"})
    assert r.ok and "doğrulayamadım" in r.summary and not r.data["verified"]
    r = env.run("media_control", {"action": "previous"})
    assert "başa sarıldı" in r.summary


def test_no_session_pause_is_error(tmp_path, win):
    env = Env(tmp_path, media=FakeMedia(None))
    r = env.run("media_control", {"action": "pause"})
    assert not r.ok and "medya bulamadım" in r.error
    assert env.media.keys == []


def test_no_session_play_presses_key_but_does_not_claim_success(tmp_path, win):
    env = Env(tmp_path, media=FakeMedia(None))
    r = env.run("media_control", {"action": "play"})
    assert r.ok and env.media.keys == ["play_pause"]
    assert "göremedim" in r.summary and not r.data["verified"]


def test_without_gsmtc_uses_media_key_unverified(tmp_path, win):
    env = Env(tmp_path, media=FakeMedia(None, gsmtc=False))
    r = env.run("media_control", {"action": "pause"})
    assert r.ok and env.media.keys == ["play_pause"] and "doğrulayamadım" in r.summary
    assert "aç/kapa" in r.summary and r.data["verified"] is False
    r = env.run("media_control", {"action": "previous"})
    assert env.media.keys[-1] == "previous"


def test_backend_failure_is_error(tmp_path, win):
    class Broken(FakeMedia):
        def current(self):
            raise ToolError("x", "Windows medya denetimi yanıt vermedi.")

    env = Env(tmp_path, media=Broken(PLAYING))
    r = env.run("media_control", {"action": "pause"})
    assert not r.ok and "yanıt vermedi" in r.error


def test_now_playing(tmp_path, win):
    env = Env(tmp_path)
    r = env.run("now_playing", {})
    assert r.ok and r.summary == "Spotify: Tarkan - Şımarık (çalıyor)."
    assert r.untrusted and r.data["session"]["artist"] == "Tarkan"
    env.media.info = None
    assert "çalan ya da duraklatılmış bir medya yok" in env.run("now_playing", {}).summary
    env.media.gsmtc = False
    r = env.run("now_playing", {})
    assert not r.ok and "winrt" in r.error


def test_pretty_app_names():
    assert pretty_app("SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify") == "Spotify"
    assert pretty_app("Chrome") == "Chrome"
    assert pretty_app("308046B0AF4A39CB") == "Firefox"
    assert pretty_app("") == "bilinmeyen uygulama"


# ---------- Spotify ----------
def test_spotify_desktop_search(tmp_path, win):
    env = Env(tmp_path)
    r = env.run("spotify_play", {"query": "Tarkan Şımarık"})
    assert r.ok and env.opened == ["spotify:search:Tarkan%20%C5%9E%C4%B1mar%C4%B1k"]
    assert "Çalmayı kendim başlatamıyorum" in r.summary and r.data["playing"] is False


def test_spotify_web_fallback(tmp_path, win):
    env = Env(tmp_path, spotify=False)
    env.ctx.apps = AppIndex([e for e in FAKE_APPS if "spotify" not in e.name.lower()])
    r = env.run("spotify_play", {"query": "sezen aksu"})
    assert r.ok and env.opened == ["https://open.spotify.com/search/sezen%20aksu"]
    assert "kurulu görünmüyor" in r.summary


def test_spotify_query_is_sanitised(tmp_path, win):
    env = Env(tmp_path)
    r = env.run("spotify_play", {"query": "a\x07b/../c"})
    assert r.ok and env.opened == ["spotify:search:a%20b%2F..%2Fc"]
    assert not env.run("spotify_play", {"query": "\x01\x02"}).ok
    assert not env.run("spotify_play", {"query": ""}).ok
    assert not env.run("spotify_play", {"query": "x" * 151}).ok


# ---------- YouTube ----------
def test_youtube_opens_first_video(tmp_path, win):
    html = yt_html([{"adSlotRenderer": {"videoRenderer": {"videoId": "REKLAMxxxxx"}}}, video("rFZHOHl-L8A"),
                    video("sF80I-TQiW0")])
    env = Env(tmp_path, fetch=html)
    r = env.run("youtube_play", {"query": "lo-fi"})
    assert r.ok and env.opened == ["https://www.youtube.com/watch?v=rFZHOHl-L8A"]
    assert env.fetched == ["https://www.youtube.com/results?search_query=lo-fi"]
    assert "Lofi Girl radyo" in r.summary and r.untrusted


def test_youtube_falls_back_to_results_page(tmp_path, win):
    env = Env(tmp_path, fetch=ToolError("ağ", "bağlantı yok"))
    r = env.run("youtube_play", {"query": "kedi videoları"})
    assert r.ok and env.opened == ["https://www.youtube.com/results?search_query=kedi+videolar%C4%B1"]
    assert "İlk videoyu bulamadım" in r.summary
    env.fetch_result = "<html>onay sayfası</html>"
    env.run("youtube_play", {"query": "x"})
    assert env.opened[-1].startswith("https://www.youtube.com/results?")


def test_parse_first_video_variants():
    assert parse_first_video(yt_html([video("n61ULEU7CO0")], var=False))["video_id"] == "n61ULEU7CO0"
    # geçersiz kimlik atlanır
    assert parse_first_video(yt_html([video("kötü<script>"), video("n61ULEU7CO0")]))["video_id"] == "n61ULEU7CO0"
    # JSON bozuksa düz metin yedeği
    broken = 'var ytInitialData = {bozuk "videoRenderer":{"videoId":"abcdefghijk","x":1}'
    assert parse_first_video(broken) == {"video_id": "abcdefghijk", "title": "", "channel": "", "length": ""}
    assert parse_first_video("") is None and parse_first_video("<html></html>") is None
    # kontrol karakterleri ve aşırı uzun başlık temizlenir
    v = parse_first_video(yt_html([video("n61ULEU7CO0", title="a\x00b" + "x" * 300)]))
    assert "\x00" not in v["title"] and len(v["title"]) <= 120


# ---------- ses çıkışı ----------
def test_switch_to_headphones_verified(tmp_path, win):
    env = Env(tmp_path)
    r = env.run("set_audio_output", {"device": "kulaklık"})
    assert r.ok and r.summary == "Ses çıkışını Headphones (WH-1000XM4 Stereo) yaptım."
    assert env.devices.set_calls == ["{id-1}"]
    r = env.run("set_audio_output", {"device": "hoparlör"})
    assert r.ok and "Speakers" in r.summary


def test_switch_already_default(tmp_path, win):
    env = Env(tmp_path)
    r = env.run("set_audio_output", {"device": "hoparlöre"})
    assert r.ok and "zaten" in r.summary and env.devices.set_calls == []


def test_switch_not_verified_is_error(tmp_path, win):
    env = Env(tmp_path, devices=FakeDevices(["Speakers (Realtek)", "Kulaklık (AirPods)"], obey=False))
    r = env.run("set_audio_output", {"device": "kulaklığa"})
    assert not r.ok and "değiştirmedi" in r.error


def test_switch_unknown_and_ambiguous(tmp_path, win):
    env = Env(tmp_path, devices=FakeDevices(["Speakers (Realtek)", "LG ULTRAGEAR (NVIDIA High Definition Audio)"]))
    r = env.run("set_audio_output", {"device": "kulaklık"})
    assert not r.ok and "bulamadım" in r.error and "LG ULTRAGEAR" in r.error
    env = Env(tmp_path, devices=FakeDevices(["Speakers (Realtek)", "Headphones (A)", "Headphones (B)"]))
    r = env.run("set_audio_output", {"device": "kulaklık"})
    assert not r.ok and "Birden fazla" in r.error


def test_device_scoring():
    devs = [AudioDevice("1", "Hoparlör (Realtek(R) Audio)", True), AudioDevice("2", "Kulaklık (WH-1000XM4)", False),
            AudioDevice("3", "Kulaklık Mikrofonlu Seti (WH-1000XM4 Hands-Free)", False),
            AudioDevice("4", "LG TV (NVIDIA High Definition Audio)", False)]
    assert pick_device("kulaklık", devs).id == "2"           # eller-serbest uç değil
    assert pick_device("kulaklığa", devs).id == "2"
    assert pick_device("hoparlöre", devs).id == "1"
    assert pick_device("monitör", devs).id == "4"
    assert pick_device("televizyona", devs).id == "4"
    assert pick_device("nvidia", devs).id == "4"
    assert pick_device("WH-1000XM4 Hands-Free", devs).id == "3"
    # bluetooth kulaklığın iki ucu: "Headphones" yüksek kaliteli olan, "Headset" değil
    bt = [AudioDevice("a", "Headset (Buds Hands-Free AG Audio)", False), AudioDevice("b", "Headphones (Buds)", False)]
    assert pick_device("kulaklık", bt).id == "b"
    assert score_device("kulaklık", "Speakers (Realtek)") == 0


def test_list_audio(tmp_path, win):
    env = Env(tmp_path)
    r = env.run("list_audio", {})
    assert r.ok and "2 ses çıkış aygıtı" in r.summary and "Speakers" in r.summary
    assert {a["app"] for a in r.data["apps"]} == {"Sistem sesleri", "chrome", "discord", "Spotify"}
    assert r.untrusted


# ---------- uygulama sesi ----------
def test_set_app_volume_all_sessions(tmp_path, win):
    env = Env(tmp_path)
    r = env.run("set_app_volume", {"app": "Chrome'un", "mode": "set", "value": 30})
    assert r.ok and r.summary == "chrome sesini %30 yaptım." and r.data["sessions"] == 2
    assert env.apps.list[101].volume == 30 and env.apps.list[102].volume == 30
    assert env.apps.list[301].volume == 90


def test_change_app_volume_unmutes_when_raising(tmp_path, win):
    env = Env(tmp_path)
    r = env.run("set_app_volume", {"app": "discordun", "mode": "change", "value": -20})
    assert r.ok and "%50 kıstım" in r.summary and env.apps.list[201].muted
    r = env.run("set_app_volume", {"app": "diskort", "mode": "change", "value": 10})
    assert r.ok and "%60 açtım" in r.summary and not env.apps.list[201].muted


def test_mute_and_unmute_app(tmp_path, win):
    env = Env(tmp_path)
    r = env.run("set_app_volume", {"app": "spotify", "mode": "mute", "value": 0})
    assert r.ok and r.summary == "Spotify sesini kapattım." and env.apps.list[301].muted
    r = env.run("set_app_volume", {"app": "Spotify", "mode": "unmute", "value": 0})
    assert r.ok and "açtım (%90)" in r.summary
    r = env.run("set_app_volume", {"app": "krom", "mode": "mute", "value": 0})
    assert r.ok and env.apps.list[101].muted and env.apps.list[102].muted


def test_app_volume_errors(tmp_path, win):
    env = Env(tmp_path)
    r = env.run("set_app_volume", {"app": "Steam", "mode": "set", "value": 30})
    assert not r.ok and "ses oturumu bulamadım" in r.error and "discord" in r.error
    assert not env.run("set_app_volume", {"app": "chrome", "mode": "set", "value": -5}).ok
    assert not env.run("set_app_volume", {"app": "chrome", "mode": "change", "value": 0}).ok
    assert not env.run("set_app_volume", {"app": "chrome", "mode": "set", "value": 101}).ok
    assert not env.run("set_app_volume", {"app": "  ", "mode": "mute", "value": 0}).ok


def test_app_volume_not_applied_is_error(tmp_path, win):
    env = Env(tmp_path, apps=FakeAppAudio(SESSIONS, obey=False))
    r = env.run("set_app_volume", {"app": "chrome", "mode": "set", "value": 30})
    assert not r.ok and "şu an %80" in r.error
    r = env.run("set_app_volume", {"app": "chrome", "mode": "mute", "value": 0})
    assert not r.ok and "hâlâ açık" in r.error


def test_match_sessions():
    assert {s.pid for s in match_sessions("Discord'un", SESSIONS)} == {201}
    assert {s.pid for s in match_sessions("chromeun", SESSIONS)} == {101, 102}
    assert {s.pid for s in match_sessions("sistem sesleri", SESSIONS)} == {0}
    assert {s.pid for s in match_sessions("tarayıcının", SESSIONS)} == {101, 102}
    assert match_sessions("zoom", SESSIONS) == []
    assert match_sessions("a", SESSIONS) == []
    # uygulama dizini ipucu: "VS Code" -> code.exe
    code = [AppAudioSession(9, "code.exe", "", 50, False)]
    assert match_sessions("vs code", code, {"code.exe"})[0].pid == 9


# ---------- Discord ----------
def test_discord_mute_sends_shortcut_honestly(tmp_path, win):
    env = Env(tmp_path)
    r = env.run("discord_voice", {"action": "mute"})
    assert r.ok and env.sent == [("ctrl", "shift", "m")]
    assert "doğrulayamadım" in r.summary and "Ctrl+Shift+M" in r.summary and r.data["verified"] is False
    r = env.run("discord_voice", {"action": "deafen"})
    assert env.sent[-1] == ("ctrl", "shift", "d")


def test_discord_not_running(tmp_path, win):
    env = Env(tmp_path, discord=False)
    r = env.run("discord_voice", {"action": "mute"})
    assert not r.ok and "Discord çalışmıyor" in r.error and env.sent == []


def test_discord_call_is_audited(tmp_path, win):
    class Audit:
        def __init__(self):
            self.rows = []

        def record(self, **kw):
            self.rows.append(kw)

    env = Env(tmp_path)
    env.reg.audit = Audit()
    env.run("discord_voice", {"action": "mute"})
    assert env.reg.audit.rows[-1]["tool"] == "discord_voice" and env.reg.audit.rows[-1]["ok"]


# ---------- SendInput ----------
def test_input_struct_layout():
    assert ctypes.sizeof(INPUT) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)


def test_build_key_inputs_order_and_flags():
    arr = build_key_inputs(("ctrl", "shift", "m"), scan=lambda vk: vk + 1)
    seq = [(a.ki.wVk, bool(a.ki.dwFlags & KEYEVENTF_KEYUP)) for a in arr]
    assert seq == [(VK["ctrl"], False), (VK["shift"], False), (VK["m"], False),
                   (VK["m"], True), (VK["shift"], True), (VK["ctrl"], True)]
    assert all(a.type == 1 and a.ki.wScan == a.ki.wVk + 1 for a in arr)
    media = build_key_inputs(("play_pause",))
    assert len(media) == 2 and media[0].ki.dwFlags & KEYEVENTF_EXTENDEDKEY
    with pytest.raises(ValueError):
        build_key_inputs(("f13",))
