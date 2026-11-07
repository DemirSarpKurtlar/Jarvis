import json

import pytest
from pydantic import ValidationError

from jarvis.config.settings import Settings, SettingsStore
from jarvis.paths import AppPaths


def store_at(tmp_path):
    return SettingsStore(tmp_path / "settings.json")


def test_defaults_match_decisions():
    s = Settings()
    assert s.ai.model_light == "claude-haiku-5-5"
    assert s.ai.model_main == "claude-sonnet-5-5"
    assert s.ai.model_advanced == "claude-opus-5-5"
    assert s.budget.monthly_limit_usd == 5.0
    assert s.budget.on_limit == "fallback_light"
    assert s.ai.web_search_enabled is True
    assert s.voice.wake_words == ["hey_jarvis", "jarvis"]


def test_first_load_creates_file(tmp_path):
    st = store_at(tmp_path)
    st.load()
    assert st.path.exists()
    assert st.load_warning is None


def test_roundtrip_persists_changes(tmp_path):
    st = store_at(tmp_path)
    st.load()
    st.settings.voice.volume = 33
    st.settings.budget.monthly_limit_usd = 2.5
    st.save()
    st2 = store_at(tmp_path)
    s2 = st2.load()
    assert s2.voice.volume == 33
    assert s2.budget.monthly_limit_usd == 2.5


def test_invalid_assignment_rejected():
    s = Settings()
    with pytest.raises(ValidationError):
        s.voice.volume = 150
    with pytest.raises(ValidationError):
        s.budget.on_limit = "harca"


def test_corrupt_json_is_backed_up_not_swallowed(tmp_path):
    st = store_at(tmp_path)
    st.path.write_text("{ bu json değil", encoding="utf-8")
    s = st.load()
    assert s == Settings()
    assert st.load_warning and "yedeklendi" in st.load_warning
    backups = list(tmp_path.glob("settings.bozuk-*.json"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{ bu json değil"


def test_out_of_range_value_is_backed_up(tmp_path):
    st = store_at(tmp_path)
    st.path.write_text(json.dumps({"voice": {"volume": 999}}), encoding="utf-8")
    st.load()
    assert st.load_warning is not None
    assert st.settings.voice.volume == 70


def test_unknown_keys_ignored(tmp_path):
    st = store_at(tmp_path)
    st.path.write_text(json.dumps({"gelecek_ozellik": 1, "voice": {"volume": 10, "eski": True}}), encoding="utf-8")
    s = st.load()
    assert st.load_warning is None
    assert s.voice.volume == 10


def test_no_temp_files_left_after_save(tmp_path):
    st = store_at(tmp_path)
    st.load()
    for _ in range(5):
        st.save()
    assert [p.name for p in tmp_path.iterdir()] == ["settings.json"]


def test_app_paths_use_jarvis_home(jarvis_home):
    p = AppPaths.resolve()
    assert p.config_dir == jarvis_home.resolve() / "config"
    assert p.log_dir.exists() and p.data_dir.exists()
