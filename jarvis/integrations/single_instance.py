"""Tek örnek koruması.

JARVIS ikinci kez başlatılırsa yeni bir kopya açılmaz; çalışan pencere öne
getirilir. İki parça birlikte çalışır:

* `QLockFile` (veri klasöründe `jarvis.lock`): hangi sürecin "asıl" örnek
  olduğuna karar verir. Süreç çökerse kilit, sahibinin PID'i artık yaşamadığı
  için bir sonraki açılışta otomatik geri alınır.
* `QLocalServer` / `QLocalSocket` (Windows'ta adlandırılmış boru): ikinci
  örnek asıl örneğe kısa bir mesaj gönderir (`activate`, `quit`).

Kilit ve sunucu adı veri klasörüne bağlıdır; farklı `JARVIS_HOME` ile
çalışan kopyalar (testler, taşınabilir kullanım) birbirini engellemez.
"""

from __future__ import annotations

import getpass
import hashlib
import logging
import sys
import time
from pathlib import Path

from PySide6.QtCore import QLockFile, QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from jarvis import APP_NAME

log = logging.getLogger(__name__)

MSG_ACTIVATE = "activate"   # pencereyi göster ve öne getir
MSG_QUIT = "quit"           # temizce kapan (kurulum/kaldırma programı kullanır)
KNOWN_MESSAGES = frozenset({MSG_ACTIVATE, MSG_QUIT})
_MAX_MESSAGE = 64


def _user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001  (bazı ortamlarda kullanıcı adı okunamaz)
        return "kullanici"


def server_name_for(key_dir: Path) -> str:
    """Kullanıcıya ve veri klasörüne özgü, kısa ve dosya adı güvenli sunucu adı."""
    raw = f"{_user()}|{str(Path(key_dir).resolve()).lower()}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{APP_NAME}-{digest}"


def allow_foreground_switch() -> None:
    """Windows: asıl örneğin pencereyi öne getirmesine izin ver.

    Windows, arka plandaki bir sürecin kendi penceresini öne almasını engeller.
    Kullanıcının az önce başlattığı (öndeki) ikinci süreç bu hakkı devredebilir.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
    except Exception:  # noqa: BLE001
        log.debug("AllowSetForegroundWindow çağrılamadı", exc_info=True)


class SingleInstance(QObject):
    """Bir veri klasörü için tek JARVIS örneği.

    Kullanım:
        guard = SingleInstance(paths.data_dir)
        if not guard.acquire():
            guard.send(MSG_ACTIVATE)   # çalışan örneği öne getir
            return 0
        guard.message_received.connect(...)
    """

    message_received = Signal(str)

    def __init__(self, key_dir: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        key_dir = Path(key_dir)
        key_dir.mkdir(parents=True, exist_ok=True)
        self.lock_path = key_dir / "jarvis.lock"
        self.server_name = server_name_for(key_dir)
        self._lock = QLockFile(str(self.lock_path))
        # 0: kilit yaşı yüzünden "bayat" sayılmasın (uygulama günlerce açık kalabilir).
        # Sahibi ölmüş kilitler yine de PID kontrolüyle geri alınır.
        self._lock.setStaleLockTime(0)
        self._server: QLocalServer | None = None
        self._buffers: dict[QLocalSocket, bytes] = {}

    @property
    def is_primary(self) -> bool:
        return self._lock.isLocked()

    @property
    def listening(self) -> bool:
        return self._server is not None and self._server.isListening()

    # ---------- asıl örnek ----------
    def acquire(self) -> bool:
        """Kilidi almaya çalışır. Başarılıysa mesaj sunucusunu da açar."""
        if self._lock.isLocked():
            return True
        if not self._lock.tryLock(0):
            err = self._lock.error()
            if err == QLockFile.LockError.LockFailedError:
                return False
            # İzin/disk hatası: kopyayı engellemek uğruna uygulamayı açmamak daha kötü
            log.warning("Tek örnek kilidi alınamadı (%s), yine de devam ediliyor", err.name)
            self._start_server()
            return True
        self._start_server()
        return True

    def _start_server(self) -> None:
        # Önceki çökmeden kalan soket dosyası (Unix) dinlemeyi engellemesin
        QLocalServer.removeServer(self.server_name)
        server = QLocalServer(self)
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        server.newConnection.connect(self._on_new_connection)
        if server.listen(self.server_name):
            self._server = server
            log.debug("Tek örnek sunucusu dinliyor: %s", self.server_name)
        else:
            log.warning("Tek örnek sunucusu açılamadı (%s). İkinci açılış pencereyi öne getiremeyecek.",
                        server.errorString())
            server.deleteLater()

    def _on_new_connection(self) -> None:
        assert self._server is not None
        while self._server.hasPendingConnections():
            sock = self._server.nextPendingConnection()
            self._buffers[sock] = b""
            sock.readyRead.connect(lambda s=sock: self._on_ready_read(s))
            sock.disconnected.connect(lambda s=sock: self._finish(s))
            # Veri bağlantıdan önce gelmiş olabilir
            if sock.bytesAvailable():
                self._on_ready_read(sock)

    def _on_ready_read(self, sock: QLocalSocket) -> None:
        if sock not in self._buffers:
            return
        self._buffers[sock] += bytes(sock.readAll().data())
        if b"\n" in self._buffers[sock] or len(self._buffers[sock]) > _MAX_MESSAGE:
            self._finish(sock)

    def _finish(self, sock: QLocalSocket) -> None:
        data = self._buffers.pop(sock, None)
        if data is None:
            return
        sock.deleteLater()
        message = data.split(b"\n", 1)[0].decode("utf-8", "replace").strip()
        if message in KNOWN_MESSAGES:
            log.info("İkinci açılıştan mesaj: %s", message)
            self.message_received.emit(message)
        elif message:
            log.warning("Tek örnek sunucusuna bilinmeyen mesaj geldi, yok sayıldı")

    def release(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server.deleteLater()
            self._server = None
        if self._lock.isLocked():
            self._lock.unlock()

    # ---------- ikinci örnek ----------
    def send(self, message: str, timeout_s: float = 3.0) -> bool:
        """Çalışan örneğe mesaj gönderir.

        Asıl örnek henüz açılıyor olabilir (kilit alındı ama sunucu dinlemiyor),
        bu yüzden zaman aşımına kadar tekrar denenir. Yalnızca açılışta, olay
        döngüsü başlamadan çağrılır; kısa süre bloklaması sorun değildir.
        """
        if message not in KNOWN_MESSAGES:
            raise ValueError(f"bilinmeyen mesaj: {message}")
        if message == MSG_ACTIVATE:
            allow_foreground_switch()
        deadline = time.monotonic() + timeout_s
        while True:
            sock = QLocalSocket()
            sock.connectToServer(self.server_name)
            if sock.waitForConnected(300):
                sock.write((message + "\n").encode("utf-8"))
                ok = sock.waitForBytesWritten(1000) or sock.bytesToWrite() == 0
                sock.disconnectFromServer()
                if sock.state() != QLocalSocket.LocalSocketState.UnconnectedState:
                    sock.waitForDisconnected(500)
                return ok
            if time.monotonic() >= deadline:
                log.warning("Çalışan JARVIS'e ulaşılamadı (%s)", sock.errorString())
                return False
            time.sleep(0.1)

    def wait_until_released(self, timeout_s: float = 30.0) -> bool:
        """Asıl örnek kapanıp kilidi bırakana kadar bekler (kurulum için)."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._lock.tryLock(100):
                self._lock.unlock()
                return True
            time.sleep(0.1)
        return False
