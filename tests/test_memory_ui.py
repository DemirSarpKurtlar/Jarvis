"""Aşama 4: Hafıza ekranı (ekransız Qt)."""

import asyncio
import time

import pytest
import qasync

from jarvis.app import configure_application
from jarvis.config.settings import SettingsStore
from jarvis.core.assistant import Assistant
from jarvis.core.events import Topics
from jarvis.interface import theme
from jarvis.interface.main_window import ComingSoonPage, MainWindow
from jarvis.interface.memory_view import DeleteDialog, MemoryEditDialog, MemoryView
from jarvis.memory.store import ACTIVE, Candidate
from jarvis.paths import AppPaths


@pytest.fixture
def loop(qapp):
    configure_application(qapp)
    lp = qasync.QEventLoop(qapp, already_running=False)
    asyncio.set_event_loop(lp)
    yield lp
    lp.close()
    asyncio.set_event_loop(None)


@pytest.fixture
def ui(qapp, loop):
    store = SettingsStore(AppPaths.resolve().settings_file)
    store.load()
    a = Assistant(store, sample_interval_s=0.5)
    w = MainWindow(a, theme.load_fonts())
    w.show()
    yield a, w, loop
    loop.run_until_complete(asyncio.sleep(0.05))
    a.db.close()
    w.deleteLater()


async def wait_for(cond, timeout=5.0):
    t = time.perf_counter()
    while not cond():
        if time.perf_counter() - t > timeout:
            raise AssertionError("koşul zaman aşımına uğradı")
        await asyncio.sleep(0.02)


def add(a, content, category="preference", **kw):
    return a.memory.store.add(Candidate(content=content, category=category, **kw)).memory


def test_memory_page_replaces_coming_soon(ui):
    a, w, loop = ui
    assert isinstance(w.pages["hafiza"], MemoryView) and not isinstance(w.pages["hafiza"], ComingSoonPage)
    w.navigate("hafiza")
    loop.run_until_complete(w.memory_view.refresh())
    v = w.memory_view
    assert v.rows == [] and v.empty.isVisible() and "bunu hatırla" in v.empty.text()
    assert not v.card.isVisible()
    assert v.switch.isChecked() and v.switch_label.text() == "Hafıza açık"


def test_rows_filters_and_importance(ui):
    a, w, loop = ui
    add(a, "Cevapları kısa ve net tercih ediyor.", importance=4, source="konuşma:1")
    add(a, "Akşamları en çok Steam ve Discord'u açıyor.", "app", importance=3)
    add(a, "Çalışma modu deyince Discord kapanıyor, VS Code açılıyor.", "routine", importance=5)
    w.navigate("hafiza")
    v = w.memory_view
    loop.run_until_complete(v.refresh())
    assert [r.memory.category for r in v.rows] == ["routine", "preference", "app"]   # önem sırası
    pref = [r for r in v.rows if r.memory.category == "preference"][0]
    assert pref.category.text() == "Tercih" and pref.importance.value == 4
    assert "konuşmasından" in pref.meta.text()
    v.set_filter("app")
    assert [r.memory.category for r in v.rows] == ["app"]
    v.set_filter("task")
    assert v.rows == [] and v.empty.isVisible() and v.empty.text() == "Bu kategoride kayıt yok."
    v.filter_buttons[None].click()
    assert len(v.rows) == 3
    img = w.grab().toImage()
    assert not img.isNull()


def test_pending_card_remember_and_discard(ui):
    a, w, loop = ui
    blood = add(a, "Kan grubu A Rh+.", "personal")                       # hassas: onay bekler
    guess = add(a, "Muhtemelen yazılımcı.", "personal", is_inference=True, source="çıkarım")
    w.navigate("hafiza")
    v = w.memory_view
    loop.run_until_complete(v.refresh())
    assert v.card.isVisible() and v.rows == []
    assert "Kan grubu A Rh+." in v.card.text.text() or "Muhtemelen" in v.card.text.text()
    assert "2 bilgi onay bekliyor" in v.card.title.text()
    first = v.card.memory
    reason = "sağlıkla ilgili" if first.id == blood.id else "çıkardım"
    assert reason in v.card.text.text()

    v.card.remember_btn.click()
    loop.run_until_complete(wait_for(lambda: a.memory.store.get(first.id).status == ACTIVE and len(v.rows) == 1))
    second = v.card.memory
    assert second.id != first.id and v.card.isVisible()
    v.card.discard_btn.click()
    loop.run_until_complete(wait_for(lambda: not v.card.isVisible()))
    assert a.memory.store.get(second.id) is None and len(v.rows) == 1
    assert {blood.id, guess.id} == {first.id, second.id}


def test_edit_dialog_corrects_memory_and_rejects_secrets(ui):
    a, w, loop = ui
    m = add(a, "Adı Deniz.", "personal", importance=3)
    w.navigate("hafiza")
    v = w.memory_view
    loop.run_until_complete(v.refresh())
    v.rows[0].edit_btn.click()
    dlg = v._dialog
    assert isinstance(dlg, MemoryEditDialog) and dlg.content.toPlainText() == "Adı Deniz."
    dlg.content.setPlainText("Şifresi Kedi2024!")
    dlg.ok.click()
    loop.run_until_complete(wait_for(lambda: dlg.ok.isEnabled()))
    assert "gizli bilgileri" in dlg.result.text() and a.memory.store.get(m.id).content == "Adı Deniz."
    dlg.content.setPlainText("Adı Demir.")
    dlg.importance.setCurrentIndex(4)
    dlg.temporary.setChecked(True)
    dlg.ok.click()
    loop.run_until_complete(wait_for(lambda: v.rows and v.rows[0].memory.content == "Adı Demir."))
    got = a.memory.store.get(m.id)
    assert got.importance == 5 and got.expires_at is not None and got.source == "kullanıcı"
    assert "Senin eklediğin" in v.rows[0].meta.text() and "geçici" in v.rows[0].meta.text()


def test_delete_requires_confirmation(ui):
    a, w, loop = ui
    m = add(a, "Steam kullanıyor.", "app")
    w.navigate("hafiza")
    v = w.memory_view
    loop.run_until_complete(v.refresh())
    v.rows[0].delete_btn.click()
    dlg = v._dialog
    assert isinstance(dlg, DeleteDialog) and dlg.isVisible()
    dlg.cancel.click()
    loop.run_until_complete(asyncio.sleep(0.05))
    assert a.memory.store.get(m.id) is not None
    v.rows[0].delete_btn.click()
    v._dialog.ok.click()
    loop.run_until_complete(wait_for(lambda: v.rows == []))
    assert a.memory.store.get(m.id) is None and v.empty.isVisible()


def test_toggle_persists_and_shows_off_note(ui):
    a, w, loop = ui
    w.navigate("hafiza")
    v = w.memory_view
    v.switch.click()
    assert SettingsStore(a.store.path).load().memory.enabled is False
    assert v.switch_label.text() == "Hafıza kapalı" and v.off_note.isVisible()
    v.switch.click()
    assert SettingsStore(a.store.path).load().memory.enabled is True and not v.off_note.isVisible()


def test_pending_event_shows_banner_with_shortcut(ui):
    a, w, loop = ui
    m = add(a, "Kan grubu A Rh+.", "personal")
    a.bus.publish(Topics.MEMORY_PENDING, m)
    assert w.panel.banner.isVisible() and w.panel.banner.action.text() == "Hafıza"
    w.panel.banner.action.click()
    assert w.stack.currentWidget() is w.memory_view and not w.panel.banner.isVisible()
    loop.run_until_complete(wait_for(lambda: w.memory_view.card.isVisible()))
