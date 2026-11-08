"""Medya/ses yerel komutları: Claude'suz Türkçe ayrıştırma ve uçtan uca yerel çalıştırma."""

import asyncio

import pytest

from jarvis.tools import registry as regmod
from jarvis.tools import schema as js
from jarvis.tools.defaults import build_registry
from jarvis.tools.intents import parse
from tests.test_media import Env
from tests.tool_fakes import make_ctx

CASES = [
    # oynat / duraklat / sonraki / önceki
    ("müziği durdur", "media_control", {"action": "pause"}),
    ("Jarvis, müziği duraklat", "media_control", {"action": "pause"}),
    ("şarkıyı durdurur musun", "media_control", {"action": "pause"}),
    ("durdur", "media_control", {"action": "pause"}),
    ("müziği kapat", "media_control", {"action": "pause"}),
    ("devam et", "media_control", {"action": "play"}),
    ("müziği devam ettir", "media_control", {"action": "play"}),
    ("kaldığı yerden devam et", "media_control", {"action": "play"}),
    ("oynat", "media_control", {"action": "play"}),
    ("müzik çal", "media_control", {"action": "play"}),
    ("sonraki şarkı", "media_control", {"action": "next"}),
    ("Sonraki şarkıya geç.", "media_control", {"action": "next"}),
    ("sıradaki", "media_control", {"action": "next"}),
    ("şarkıyı geç", "media_control", {"action": "next"}),
    ("şarkıyı atla", "media_control", {"action": "next"}),
    ("bu şarkıyı değiştir", "media_control", {"action": "next"}),
    ("önceki", "media_control", {"action": "previous"}),
    ("önceki şarkı", "media_control", {"action": "previous"}),
    ("bir önceki şarkıya dön", "media_control", {"action": "previous"}),
    ("şu an ne çalıyor", "now_playing", {}),
    ("bu şarkı ne", "now_playing", {}),
    ("hangi şarkı çalıyor", "now_playing", {}),
    # Spotify / YouTube
    ("Spotify'da Tarkan çal", "spotify_play", {"query": "tarkan"}),
    ("spotifyda sezen aksu çalar mısın", "spotify_play", {"query": "sezen aksu"}),
    ("Tarkan'ı Spotify'da çal", "spotify_play", {"query": "tarkan"}),
    ("YouTube'da lo-fi aç", "youtube_play", {"query": "lo-fi"}),
    ("youtubeda kedi videoları aç", "youtube_play", {"query": "kedi videoları"}),
    ("YouTube'da Barış Manço izle", "youtube_play", {"query": "barış manço"}),
    # ses çıkışı
    ("kulaklığa geç", "set_audio_output", {"device": "kulaklık"}),
    ("hoparlöre geç", "set_audio_output", {"device": "hoparlör"}),
    ("sesi kulaklığa ver", "set_audio_output", {"device": "kulaklık"}),
    ("ses çıkışını hoparlör yap", "set_audio_output", {"device": "hoparlör"}),
    ("sesi monitöre ver", "set_audio_output", {"device": "monitör"}),
    ("ses aygıtlarını listele", "list_audio", {}),
    ("hangi ses cihazları var", "list_audio", {}),
    # uygulama sesi
    ("Discord'un sesini kıs", "set_app_volume", {"app": "discord", "mode": "change", "value": -20}),
    ("Chrome'un sesini %30 yap", "set_app_volume", {"app": "chrome", "mode": "set", "value": 30}),
    ("chromeun sesini yüzde otuz yap", "set_app_volume", {"app": "chromeun", "mode": "set", "value": 30}),
    ("Spotify'ın sesini 10 artır", "set_app_volume", {"app": "spotify", "mode": "change", "value": 10}),
    ("Spotify'ın sesini kapat", "set_app_volume", {"app": "spotify", "mode": "mute", "value": 0}),
    ("Chrome'u sessize al", "set_app_volume", {"app": "chrome", "mode": "mute", "value": 0}),
    ("Discord'un sesini aç", "set_app_volume", {"app": "discord", "mode": "unmute", "value": 0}),
    # Discord
    ("Discord'da mikrofonumu kapat", "discord_voice", {"action": "mute"}),
    ("discordda mikrofonu aç", "discord_voice", {"action": "mute"}),
    ("discord'da beni sustur", "discord_voice", {"action": "mute"}),
    ("sağırlaştır", "discord_voice", {"action": "deafen"}),
    ("Discord'da sağırlaştır", "discord_voice", {"action": "deafen"}),
]

# Başka kurallara ait ya da yerelde işlenmemesi gereken cümleler
NOT_MEDIA = [
    ("sesi kapat", "set_mute"),
    ("sesi yüzde 30 yap", "set_volume"),
    ("Chrome'a geç", "switch_to_app"),
    ("Spotify'ı aç", "open_app"),
    ("Spotify'ı kapat", "close_app"),
    ("youtube'da kedi ara", "web_search_in_browser"),
    ("youtube'u aç", "open_url"),
    ("devam", None),                          # sohbette "devam" = anlatmaya devam et
    ("sistemin sesini 30 yap", None),
    ("Tarkan çal", None),                     # hangi servis olduğu belirsiz: Claude'a
    ("müziği durdur ve Spotify'ı kapat", None),
    ("sesi biraz kıs", "change_volume"),
]


@pytest.mark.parametrize("text,tool,args", CASES)
def test_media_intents(text, tool, args):
    hit = parse(text)
    assert hit is not None, text
    assert hit.tool == tool, (text, hit)
    assert hit.args == args, (text, hit.args)


@pytest.mark.parametrize("text,tool", NOT_MEDIA)
def test_not_media(text, tool):
    hit = parse(text)
    assert (hit.tool if hit else None) == tool, (text, hit)


def test_intent_args_match_tool_schemas(tmp_path):
    reg = build_registry(make_ctx(tmp_path))
    for text, tool, _ in CASES:
        hit = parse(text)
        spec = reg.get(hit.tool)
        assert spec is not None, tool
        js.validate(spec.input_schema, hit.args)


def test_local_intent_runs_tool_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(regmod, "CURRENT_PLATFORM", "win32")
    env = Env(tmp_path)
    for text in ("müziği durdur", "kulaklığa geç", "Chrome'un sesini %30 yap", "Discord'da mikrofonumu kapat"):
        hit = parse(text)
        r = asyncio.run(env.reg.execute(hit.tool, hit.args, source="yerel"))
        assert r.ok, (text, r.error)
    assert env.media.info.status == "paused"
    assert next(d for d in env.devices.devices if d.is_default).name.startswith("Headphones")
    assert env.apps.list[101].volume == 30
    assert env.sent == [("ctrl", "shift", "m")]
