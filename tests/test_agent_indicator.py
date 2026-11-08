"""Bilgisayar kullanılırken ekrandaki "Esc Esc ile durdur" göstergesi."""

from jarvis.agent.service import AgentStatus
from jarvis.core.events import EventBus, Topics
from jarvis.interface.agent_indicator import AgentIndicator


def test_indicator_shows_progress_and_hides_when_done(qapp):
    bus = EventBus()
    ind = AgentIndicator(bus)
    assert ind.window is None                         # olay gelmeden pencere oluşturulmaz
    bus.publish(Topics.AGENT_STATUS, AgentStatus(True, "hedef", 3, "3. adım: Oyna'ya tıklıyorum"))
    assert ind.visible
    assert "Esc Esc" in ind.window.title.text() and "3. adım" in ind.window.detail.text()
    flags = ind.window.windowFlags()
    from PySide6.QtCore import Qt

    assert flags & Qt.WindowType.WindowTransparentForInput and flags & Qt.WindowType.WindowDoesNotAcceptFocus
    bus.publish(Topics.AGENT_STATUS, AgentStatus(False, "hedef", 3, "Bitti", "done"))
    assert not ind.visible
    ind.shutdown()
    bus.publish(Topics.AGENT_STATUS, AgentStatus(True, "hedef", 1, "x"))
    assert ind.window is None
