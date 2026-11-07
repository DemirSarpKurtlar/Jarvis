"""Tek örnek koruması: kilit, mesajlaşma, bayat kilit, uçtan uca ikinci açılış."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from jarvis.integrations.single_instance import (MSG_ACTIVATE, MSG_QUIT, SingleInstance,
                                                 server_name_for)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def guards(qapp, tmp_path):
    made = []

    def make(sub="veri"):
        g = SingleInstance(tmp_path / sub)
        made.append(g)
        return g

    yield make
    for g in made:
        g.release()


def test_second_instance_cannot_acquire_and_activates_first(qtbot, guards):
    first, second = guards(), guards()
    assert first.acquire() and first.is_primary and first.listening
    assert not second.acquire()
    with qtbot.waitSignal(first.message_received, timeout=3000) as sig:
        assert second.send(MSG_ACTIVATE, timeout_s=2)
    assert sig.args == [MSG_ACTIVATE]


def test_quit_message_is_delivered(qtbot, guards):
    first, second = guards(), guards()
    first.acquire()
    with qtbot.waitSignal(first.message_received, timeout=3000) as sig:
        second.send(MSG_QUIT, timeout_s=2)
    assert sig.args == [MSG_QUIT]


def test_unknown_message_is_rejected(guards):
    with pytest.raises(ValueError):
        guards().send("rm -rf")


def test_different_data_dirs_do_not_collide(guards):
    a, b = guards("a"), guards("b")
    assert a.server_name != b.server_name
    assert a.acquire() and b.acquire()


def test_release_lets_next_instance_start(guards):
    first, second = guards(), guards()
    first.acquire()
    assert not second.acquire()
    assert not second.wait_until_released(timeout_s=0.3)
    first.release()
    assert second.wait_until_released(timeout_s=2)
    assert second.acquire()


def test_send_without_running_instance_fails_fast(guards):
    g = guards()
    assert g.send(MSG_ACTIVATE, timeout_s=0.3) is False


def test_lock_of_crashed_process_is_reclaimed(guards, tmp_path):
    """Çöken (kilidi bırakmadan ölen) sürecin kilidi bir sonraki açılışta geri alınır."""
    lock = tmp_path / "veri" / "jarvis.lock"
    lock.parent.mkdir(parents=True)
    code = ("import os, sys; from PySide6.QtCore import QLockFile; "
            "l = QLockFile(sys.argv[1]); assert l.tryLock(0); os._exit(0)")
    subprocess.run([sys.executable, "-c", code, str(lock)], check=True, timeout=60)
    assert lock.exists()
    assert guards().acquire()


def test_server_name_is_stable_and_safe(tmp_path):
    n1, n2 = server_name_for(tmp_path), server_name_for(tmp_path / ".." / tmp_path.name)
    assert n1 == n2
    assert n1.startswith("JARVIS-") and all(c.isalnum() or c == "-" for c in n1)


def test_second_launch_and_kapat_end_to_end(jarvis_home, monkeypatch):
    """Gerçek uygulama: ikinci açılış çalışanı öne getirir, --kapat temizce kapatır."""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import instance_check
    finally:
        sys.path.pop(0)
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    log = jarvis_home / "logs" / "jarvis.log"
    assert instance_check.main(["--log", str(log), "--timeout", "60", "--", sys.executable, "-m", "jarvis"]) == 0
    text = log.read_text(encoding="utf-8")
    assert "çalışan pencere öne getiriliyor" in text
    assert text.count("Arayüz hazır") == 1  # ikinci kopya arayüz açmadı
    assert os.environ["JARVIS_HOME"] == str(jarvis_home)
