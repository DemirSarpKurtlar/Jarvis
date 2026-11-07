import logging

import pytest

from jarvis.core.errors import InvalidStateTransition, add_error_listener, remove_error_listener, report_error
from jarvis.core.events import EventBus
from jarvis.core.logging_setup import redact, setup_logging
from jarvis.core.state import ALLOWED_TRANSITIONS, AssistantState as S, StateMachine


# ---------- Durum makinesi ----------
def test_full_voice_cycle():
    sm = StateMachine()
    for st in (S.WAKE_DETECTED, S.LISTENING, S.PROCESSING, S.EXECUTING, S.SPEAKING, S.LISTENING, S.PROCESSING, S.STANDBY):
        sm.transition(st)
    assert sm.state is S.STANDBY


def test_invalid_transition_raises():
    sm = StateMachine()
    with pytest.raises(InvalidStateTransition):
        sm.transition(S.SPEAKING)
    assert sm.state is S.STANDBY


def test_error_reachable_from_every_state_and_reset():
    for start in S:
        sm = StateMachine()
        sm._state = start  # test için doğrudan kur
        sm.fail("x")
        assert sm.state is S.ERROR
        sm.reset()
        assert sm.state is S.STANDBY


def test_every_state_has_transitions_defined():
    assert set(ALLOWED_TRANSITIONS) == set(S)


def test_listener_called_and_errors_isolated():
    sm = StateMachine()
    seen = []
    sm.add_listener(lambda ch: 1 / 0)
    sm.add_listener(lambda ch: seen.append((ch.old, ch.new, ch.detail)))
    sm.transition(S.LISTENING, "elle")
    assert seen == [(S.STANDBY, S.LISTENING, "elle")]


def test_interrupt_while_speaking_returns_to_listening():
    sm = StateMachine()
    sm.transition(S.PROCESSING)
    sm.transition(S.SPEAKING)
    sm.transition(S.LISTENING)  # kullanıcı sözü kesti
    assert sm.state is S.LISTENING


# ---------- Olay veriyolu ----------
def test_bus_publish_subscribe_unsubscribe():
    bus = EventBus()
    got = []
    unsub = bus.subscribe("t", got.append)
    bus.publish("t", 1)
    unsub()
    bus.publish("t", 2)
    assert got == [1]


def test_bus_handler_error_does_not_stop_others(caplog):
    bus = EventBus()
    got = []
    bus.subscribe("t", lambda _: 1 / 0)
    bus.subscribe("t", got.append)
    with caplog.at_level(logging.ERROR):
        bus.publish("t", "x")
    assert got == ["x"]
    assert "Olay dinleyicisi hata verdi" in caplog.text


def test_publish_threadsafe_requires_loop():
    with pytest.raises(RuntimeError):
        EventBus().publish_threadsafe("t")


# ---------- Hata dinleyicileri ----------
def test_error_listener_receives_and_survives_bad_listener():
    got = []
    bad = lambda m: 1 / 0  # noqa: E731
    add_error_listener(bad)
    add_error_listener(got.append)
    try:
        report_error("sorun")
    finally:
        remove_error_listener(bad)
        remove_error_listener(got.append)
    assert got == ["sorun"]


# ---------- Loglama ve gizli bilgi maskeleme ----------
@pytest.mark.parametrize("raw", [
    "anahtar sk-ant-api03-AbCdEfGh1234567890_xyz kullanıldı",
    'headers={"x-api-key": "sk-ant-api03-ZZZZZZZZZZZZ"}',
    "Authorization: Bearer abcdefghijklmnop",
    "api_key=supersecretvalue123",
    "password: hunter2hunter2",
])
def test_redact_hides_secrets(raw):
    out = redact(raw)
    for secret in ("AbCdEfGh1234567890", "ZZZZZZZZZZZZ", "abcdefghijklmnop", "supersecretvalue123", "hunter2hunter2"):
        assert secret not in out


def test_redact_keeps_normal_text():
    assert redact("Not Defteri açıldı, CPU %23") == "Not Defteri açıldı, CPU %23"


def test_log_file_never_contains_key_even_in_traceback(tmp_path):
    log_file = tmp_path / "j.log"
    setup_logging(log_file, "DEBUG")
    log = logging.getLogger("test.gizli")
    key = "sk-ant-api03-GIZLIANAHTAR1234567890"
    log.info("istek gönderiliyor: %s", key)
    try:
        raise ValueError(f"API reddetti: {key}")
    except ValueError:
        log.exception("hata")
    for h in logging.getLogger().handlers:
        h.flush()
    text = log_file.read_text(encoding="utf-8")
    assert "GIZLIANAHTAR" not in text
    assert "sk-ant-***" in text
    assert "ValueError" in text
