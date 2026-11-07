"""Ortak test ayarları: ekransız Qt ve her test için ayrı JARVIS_HOME."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def jarvis_home(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "jarvis_home"))
    return tmp_path / "jarvis_home"
