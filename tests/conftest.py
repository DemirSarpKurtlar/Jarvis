"""Ortak test ayarları: ekransız Qt, her test için ayrı JARVIS_HOME ve bellekte anahtar deposu."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import keyring  # noqa: E402
import keyring.errors  # noqa: E402
import pytest  # noqa: E402
from keyring.backend import KeyringBackend  # noqa: E402


class MemoryKeyring(KeyringBackend):
    """Gerçek Windows Credential Manager'a hiç dokunmayan test deposu."""

    priority = 10

    def __init__(self) -> None:
        super().__init__()
        self.data: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):  # noqa: ANN001, ANN201
        return self.data.get((service, username))

    def set_password(self, service, username, password):  # noqa: ANN001, ANN201
        self.data[(service, username)] = password

    def delete_password(self, service, username):  # noqa: ANN001, ANN201
        if (service, username) not in self.data:
            raise keyring.errors.PasswordDeleteError("yok")
        del self.data[(service, username)]


@pytest.fixture(autouse=True)
def jarvis_home(tmp_path, monkeypatch, request):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "jarvis_home"))
    if not request.node.get_closest_marker("live"):
        monkeypatch.delenv("JARVIS_ANTHROPIC_API_KEY", raising=False)
    return tmp_path / "jarvis_home"


@pytest.fixture(autouse=True)
def memory_keyring(request):
    if request.node.get_closest_marker("live"):
        # Canlı testler gerçek anahtar deposundaki (Windows Credential Manager) anahtarı kullanır
        yield None
        return
    previous = keyring.get_keyring()
    kr = MemoryKeyring()
    keyring.set_keyring(kr)
    yield kr
    keyring.set_keyring(previous)
