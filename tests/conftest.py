"""Ortak test ayarları: ekransız Qt, her test için ayrı JARVIS_HOME ve bellekte anahtar deposu."""

import os
import sys

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
    # Ses motoru testlerde gerçek mikrofona dokunmaz ve model indirmez (ses testleri sahte parça verir)
    monkeypatch.setenv("JARVIS_VOICE", "off")
    if not request.node.get_closest_marker("live"):
        monkeypatch.delenv("JARVIS_ANTHROPIC_API_KEY", raising=False)
    return tmp_path / "jarvis_home"


@pytest.fixture(autouse=True)
def collect_qt_garbage(request):
    """Qt testinden sonra pencereleri ve döngüsel çöpü burada, güvenli bir anda temizle.

    Pencereler (ör. dinleme göstergesi) olay veriyolu → abone → asistan → veriyolu döngüsüyle
    test bittikten sonra da yaşar. Toplanmazsa Python onları sonraki testin ortasında, Qt bir
    pencere çizerken silebilir: Linux CI'da rastgele "Segmentation fault" böyle çıkıyordu."""
    yield
    if "qapp" not in request.fixturenames and "qtbot" not in request.fixturenames:
        return
    import gc

    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is not None:
        # Python'un göremediği Qt ebeveyn bağları yüzünden pencereler döngüsel çöp olarak bile
        # toplanamayabiliyor: test bitince hepsini burada kapatıp gerçekten sil.
        for w in QApplication.topLevelWidgets():
            try:
                w.hide()
                w.deleteLater()
            except RuntimeError:     # C++ nesnesi zaten silinmiş
                pass
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    gc.collect()


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


# --- Aşama 5: Windows'a özgü testler yalnızca Windows'ta çalışır ---
def pytest_collection_modifyitems(config, items):  # noqa: ANN001, ANN201
    """`windows` işaretli testler başka sistemlerde kendiliğinden atlanır.
    `models` testleri model yoksa kendi içinde atlanır (tests/model_assets.py)."""
    if sys.platform == "win32":
        return
    skip = pytest.mark.skip(reason="yalnızca Windows'ta çalışır")
    for item in items:
        if item.get_closest_marker("windows"):
            item.add_marker(skip)
# --- Aşama 5 sonu ---
