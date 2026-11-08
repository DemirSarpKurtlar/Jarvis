"""Bilgisayarı kullanma: yerel Türkçe komutlar ve "dur" ile durdurma (Assistant üzerinden)."""

import asyncio

import pytest

from jarvis.config.settings import SettingsStore
from jarvis.core.assistant import Assistant
from jarvis.paths import AppPaths
from jarvis.tools.intents import parse
from tests.fake_api import FakeAPI
from tests.tool_fakes import make_ctx


@pytest.mark.parametrize("text,goal", [
    ("bilgisayarı sen kullan: Spotify'da Tarkan çal", "Spotify'da Tarkan çal"),
    ("Jarvis, bilgisayarımı kullanarak Chrome'da hava durumuna bak", "Chrome'da hava durumuna bak"),
    ("BİLGİSAYARI SEN KULLAN: İndirilenler klasörünü aç", "İndirilenler klasörünü aç"),
    ("fareyi ve klavyeyi kullanarak Paint'te daire çiz", "Paint'te daire çiz"),
    ("ekrana bakarak Steam'de indirmeleri duraklat", "Steam'de indirmeleri duraklat"),
    ("Valorant'ı aç ve takımlı ölüm kalım maçı başlat", "Valorant'ı aç ve takımlı ölüm kalım maçı başlat"),
    ("volarantı açıp sıraya gir", "volarantı açıp sıraya gir"),
    ("League of Legends'ı aç, ARAM modunu seç", "League of Legends'ı aç, ARAM modunu seç"),
])
def test_computer_use_intents(text, goal):
    hit = parse(text)
    assert hit is not None and hit.tool == "use_computer" and hit.args == {"goal": goal}


@pytest.mark.parametrize("text,tool", [
    ("Valorant'ı aç", "open_app"), ("Valorant oyna", "launch_game"), ("bilgisayarı kapat", "power_action"),
    ("Spotify'ı aç ve şarkı çal", None), ("bilgisayar nasıl hızlandırılır", None),
])
def test_other_commands_unchanged(text, tool):
    hit = parse(text)
    assert (hit.tool if hit else None) == tool


def test_typed_dur_stops_running_session_before_other_handlers(tmp_path):
    store = SettingsStore(AppPaths.resolve().settings_file)
    store.load()
    api = FakeAPI()
    a = Assistant(store, sample_interval_s=0.2, data_dir=tmp_path / "data", tool_context=make_ctx(tmp_path),
                  client_factory=lambda key, max_retries=0: api.client(key, max_retries=0))

    class Session:
        reason = None

        def request_stop(self, reason):  # noqa: ANN001, ANN201
            self.reason = reason

    a.chat.computer.session = Session()
    notices = []
    from jarvis.core.events import Topics

    a.bus.subscribe(Topics.NOTICE, notices.append)
    assert asyncio.run(a._handle_text("dur")) is None
    assert a.chat.computer.session.reason == "dur" and notices
    assert api.requests == []                          # Claude'a gitmedi
    a.db.close()
