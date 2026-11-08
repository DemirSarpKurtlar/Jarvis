"""Rutinler bölümü (Otomasyon ekranı): liste, aç/kapat, düzenleyici, silme, çalıştırma, brifing ayarları."""

import asyncio
import time

import pytest
import qasync

from jarvis.app import configure_application
from jarvis.config.settings import SettingsStore
from jarvis.core.assistant import Assistant
from jarvis.interface import theme
from jarvis.interface.main_window import MainWindow
from jarvis.interface.routines_view import BriefingDialog, RoutineEditor, default_args, step_choices
from jarvis.paths import AppPaths
from jarvis.tools import intents


@pytest.fixture
def loop(qapp):
    configure_application(qapp)
    lp = qasync.QEventLoop(qapp, already_running=False)
    asyncio.set_event_loop(lp)
    yield lp
    lp.close()
    asyncio.set_event_loop(None)
    intents.set_routine_matcher(None)


async def wait_for(cond, timeout=5.0):
    t = time.perf_counter()
    while not cond():
        if time.perf_counter() - t > timeout:
            raise AssertionError("zaman aşımı")
        await asyncio.sleep(0.02)


def test_routines_panel(qapp, loop):
    store = SettingsStore(AppPaths.resolve().settings_file)
    store.load()
    a = Assistant(store, sample_interval_s=0.5)
    w = MainWindow(a, theme.load_fonts())

    async def go():
        await a.start()
        w.show()
        w.navigate("otomasyon")
        p = w.automation_view.routines_panel
        await wait_for(lambda: len(p.rows()) == 3)
        assert not p.empty.isVisible()
        routines = p.store
        work = routines.find("Çalışma modu")
        # Aç/kapat: satırdaki onay kutusu
        from PySide6.QtWidgets import QPushButton

        from jarvis.interface.memory_view import Switch
        row = next(r for r in p.rows() if r.property("routine_id") == work.id)
        row.findChild(Switch).setChecked(True)
        assert routines.get(work.id).enabled
        assert intents.parse("çalışma modu").tool == "run_routine"

        # Yeni rutin: düzenleyici
        dlg = p.open_editor(None)
        assert isinstance(dlg, RoutineEditor) and len(dlg.rows) == 1
        dlg.name.setText("Müzik modu")
        dlg.phrases.setText("müzik modu, müzik zamanı")
        row0 = dlg.rows[0]
        row0.tool.setCurrentIndex(row0.tool.findData("set_volume"))
        assert row0.args.text() == '{"level": 0}'
        row0.args.setText('{"level": 250}')
        assert not dlg.save() and "1. adım" in dlg.error.text()
        row0.args.setText('{"level": 25')
        assert not dlg.save() and "JSON" in dlg.error.text()
        row0.args.setText('{"level": 25}')
        r2 = dlg.add_step(None)
        r2.tool.setCurrentIndex(r2.tool.findData("say"))
        r2.args.setText('{"text": "Müzik başlasın"}')
        r3 = dlg.add_step(None)
        dlg._remove(r3)
        assert dlg.save()
        await wait_for(lambda: len(p.rows()) == 4)
        music = routines.find("müzik zamanı")
        assert music.enabled and [s["tool"] for s in music.steps] == ["set_volume", "say"]

        # Düzenleme: mevcut adımlar yüklenir
        dlg = p.open_editor(music.id)
        assert dlg.name.text() == "Müzik modu" and len(dlg.rows) == 2
        assert dlg.rows[1].tool.currentData() == "say"
        dlg.reject()

        # Çalıştır: Linux'ta ses adımı desteklenmiyor, "söyle" adımı ve sonuç dürüstçe gösterilir
        p.refresh()
        row = next(r for r in p.rows() if r.property("routine_id") == music.id)
        run_btn = next(b for b in row.findChildren(QPushButton) if b.text() == "Çalıştır")
        run_btn.click()
        await wait_for(lambda: "rutini" in p.result.text())
        assert "Müzik başlasın" in p.result.text()

        # Silme: onay penceresi, sonra silinir
        box = p.ask_delete(music.id, music.name)
        assert "Müzik modu" in box.text()
        box.reject()
        assert routines.get(music.id) is not None
        p.delete(music.id)
        assert routines.get(music.id) is None and len(p.rows()) == 3

        # Sabah brifingi ayarları
        bd = p.open_briefing()
        assert isinstance(bd, BriefingDialog)
        bd.auto.setChecked(True)
        bd.source.setCurrentIndex(bd.source.findData("bbc"))
        assert a.settings.briefing.auto_enabled and a.settings.briefing.news_source == "bbc"
        assert SettingsStore(AppPaths.resolve().settings_file).load().briefing.auto_enabled
        bd.city.setText("Ankara; rm -rf /")   # ağa gitmeden reddedilir
        bd.save_city()
        await wait_for(lambda: bd.city_btn.isEnabled())
        assert "harf" in bd.status.text() and a.settings.briefing.city == ""
        bd.accept()
        await a.shutdown()

    loop.run_until_complete(go())


def test_editor_helpers(tmp_path):
    from jarvis.tools.defaults import build_registry
    from tests.tool_fakes import make_ctx

    reg = build_registry(make_ctx(tmp_path))
    names = [n for _, n in step_choices(reg)]
    assert names[:4] == ["wait", "say", "open_app", "close_app"]
    assert "run_routine" not in names and "delete_path" not in names
    assert default_args(reg.get("close_app").input_schema) == {"name": "", "force": False}
    assert default_args(reg.get("power_action").input_schema) == {"action": "shutdown", "delay_seconds": 30}
