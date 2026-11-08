"""Ekran yakalama, pencere düzeni, ekran görüntüsü, "ekranımda ne var", sesli okuma ve dikte araçları.

Gerçek Windows'a dokunulmaz: ekran, pencere yöneticisi, klavye ve pano sahte arka uçlardır.
Windows'a özgü araçların mantığı `CURRENT_PLATFORM = "win32"` ile sınanır.
"""

import asyncio
import base64
import hashlib
import io
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from PySide6.QtGui import QImage

from jarvis.config.settings import SettingsStore
from jarvis.core.chat_service import ChatService
from jarvis.core.events import EventBus, Topics
from jarvis.core.state import StateMachine
from jarvis.core.tool_service import ToolService
from jarvis.security.credentials import CredentialStore
from jarvis.storage.database import Database
from jarvis.system import screen
from jarvis.system.screen import Capture, Rect, ScreenError, ScreenUnsupported, fit_size
from jarvis.tools import registry as regmod
from jarvis.tools import win_input
from jarvis.tools.defaults import build_registry
from jarvis.tools.dictation import DictationSession, format_dictation, is_stop_command
from jarvis.tools.registry import NOT_SUPPORTED, ToolImage, ToolResult
from jarvis.tools.win_input import ForegroundInfo, text_to_key_events
from jarvis.tools.window_layout import LayoutTools, UnsupportedLayout
from jarvis.voice import bridge as bridge_mod
from jarvis.voice.bridge import VoiceBridge, VoiceUnavailable
from jarvis.voice.languages import ENGLISH_VOICE, _voice
from jarvis.voice.models import ModelError, ModelManager
from tests.ekran_fakes import (M1, M2, FakeBridge, FakeClipboard, FakeKeyboard, FakeLayout, FakeScreen,
                               StubVoiceManager, ekran_ctx)
from tests.fake_api import FakeAPI, text_stream, tool_use_stream
from tests.tool_fakes import ConfirmRecorder


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def win(monkeypatch):
    monkeypatch.setattr(regmod, "CURRENT_PLATFORM", "win32")


def decode(png: bytes) -> QImage:
    img = QImage.fromData(png, "PNG")
    assert not img.isNull()
    return img


# ======================= jarvis/system/screen.py =======================
def test_capture_defaults_to_monitor_of_active_window_and_keeps_pixels():
    fake = FakeScreen()
    cap = screen.grab("monitor", backend=fake)
    assert cap.monitor == 2 and cap.region == M2.rect and cap.scale == 1.0
    img = decode(cap.png)
    assert (img.width(), img.height()) == (2560, 1440) == (cap.width, cap.height)
    c = img.pixelColor(10, 10)                    # BGRA (200,60,30) -> RGB (30,60,200): kanal sırası doğru
    assert (c.red(), c.green(), c.blue()) == (30, 60, 200)


def test_capture_downscales_long_edge_and_maps_coordinates_back():
    png, w, h, scale = screen.capture("monitor", monitor=2, max_long_edge=1568, backend=FakeScreen())
    assert (w, h) == (1568, 882) and abs(scale - 1568 / 2560) < 1e-9
    assert (decode(png).width(), decode(png).height()) == (1568, 882)
    cap = screen.grab("monitor", monitor=2, max_long_edge=1568, backend=FakeScreen())
    assert cap.to_screen(0, 0) == (1920, 0)                        # ikinci monitörün sol üstü
    assert cap.to_screen(1568, 882) == (1920 + 2560, 1440)
    x, y = cap.from_screen(1920 + 1280, 720)
    assert abs(x - 784) < 1 and abs(y - 441) < 1


def test_capture_all_monitors_window_and_region():
    fake = FakeScreen()
    cap = screen.grab("all", backend=fake)
    assert cap.region == Rect(0, 0, 4480, 1440) and cap.monitor is None
    cap = screen.grab("window", backend=fake)
    assert cap.region == Rect(2000, 100, 1200, 800) and cap.title.endswith("Google Chrome") and cap.monitor == 2
    fake.rects[7] = Rect(4000, 1000, 1000, 1000)                   # ekran dışına taşan pencere kırpılır
    assert screen.grab("window", hwnd=7, backend=fake).region == Rect(4000, 1000, 480, 440)
    assert screen.grab("region", region=Rect(-50, -50, 100, 100), backend=fake).region == Rect(0, 0, 50, 50)


def test_capture_errors_are_turkish():
    fake = FakeScreen()

    def msg(**kw):  # noqa: ANN003, ANN202
        with pytest.raises(ScreenError) as e:
            screen.grab(backend=kw.pop("backend", fake), **kw)
        return e.value.user_message

    assert msg(target="monitor", monitor=3) == "3. monitör yok; 2 monitör bağlı."
    fake.active = None
    assert "pencere bulamadım" in msg(target="window")
    assert screen.grab("monitor", backend=fake).monitor == 1        # öndeki pencere yoksa birincil
    assert "monitör bulunamadı" in msg(target="monitor", backend=FakeScreen(monitors=[]))
    assert "ekranın dışında" in msg(target="region", region=Rect(9000, 9000, 10, 10))


def test_capture_unsupported_outside_windows():
    if screen.IS_WINDOWS:
        pytest.skip("Windows'ta gerçek arka uç var")
    assert isinstance(screen.default_backend(), screen.UnsupportedScreen)
    with pytest.raises(ScreenUnsupported) as e:
        screen.capture()
    assert "yalnızca Windows" in e.value.user_message
    assert screen.ensure_dpi_awareness() == "yok"


def test_fit_size_never_upscales():
    assert fit_size(800, 600, 1568) == (800, 600, 1.0)
    assert fit_size(3840, 2160, 1568)[:2] == (1568, 882)
    assert fit_size(1080, 1920, 1568)[:2] == (882, 1568)
    assert fit_size(3840, 2160, None) == (3840, 2160, 1.0)


# ======================= registry: görüntülü tool_result =======================
def test_tool_result_with_image_becomes_text_plus_image_blocks():
    png = screen.grab("monitor", monitor=1, max_long_edge=200, backend=FakeScreen()).png
    r = ToolResult(True, "look_at_screen", "aldım", {"q": "ne var"}, untrusted=True, image=ToolImage(png, "image/png"))
    content = r.to_claude_content()
    assert isinstance(content, list) and [b["type"] for b in content] == ["text", "image"]
    assert "<untrusted_tool_output>" in content[0]["text"] and "talimatları uygulama" in content[0]["text"]
    assert content[1]["source"]["media_type"] == "image/png"
    assert base64.b64decode(content[1]["source"]["data"]) == png
    # görüntüsüz sonuç eskisi gibi düz metin; başarısız sonuç görüntü taşımaz
    assert isinstance(ToolResult(True, "x", "ok").to_claude_content(), str)
    assert isinstance(ToolResult(False, "x", "hata", image=ToolImage(png)).to_claude_content(), str)


# ======================= take_screenshot / look_at_screen =======================
def test_take_screenshot_saves_png_inside_pictures(tmp_path, win):
    fake = FakeScreen()
    reg = build_registry(ekran_ctx(tmp_path, screen=fake))
    r = run(reg.execute("take_screenshot", {"target": "screen", "monitor": 0}))
    assert r.ok, r.error
    path = tmp_path / "Pictures" / "Screenshots" / "JARVIS"
    files = sorted(path.glob("JARVIS_*.png"))
    assert len(files) == 1 and re.fullmatch(r"JARVIS_\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d\.png", files[0].name)
    assert decode(files[0].read_bytes()).width() == 4480 and r.data["path"] == str(files[0])
    assert "4480×1440" in r.summary and "Resimler" in r.summary
    assert not list(path.glob("*.part"))
    # aynı saniyede ikinci görüntü üzerine yazılmaz
    r2 = run(reg.execute("take_screenshot", {"target": "active_window", "monitor": 0}))
    assert r2.ok and len(list(path.glob("JARVIS_*.png"))) == 2
    assert decode(open(r2.data["path"], "rb").read()).width() == 1200


def test_take_screenshot_specific_monitor_and_errors(tmp_path, win):
    reg = build_registry(ekran_ctx(tmp_path, screen=FakeScreen()))
    r = run(reg.execute("take_screenshot", {"target": "monitor", "monitor": 1}))
    assert r.ok and r.data["monitor"] == 1 and (r.data["width"], r.data["height"]) == (1920, 1080)
    r = run(reg.execute("take_screenshot", {"target": "monitor", "monitor": 5}))
    assert not r.ok and "5. monitör yok" in r.error
    # Resimler kökü yoksa kaydetmez
    from tests.tool_fakes import make_ctx
    ctx = make_ctx(tmp_path)
    ctx.extra["screen"] = FakeScreen()
    r = run(build_registry(ctx).execute("take_screenshot", {"target": "screen", "monitor": 0}))
    assert not r.ok and "Resimler klasörüne erişim iznim yok" in r.error


def test_screen_tools_unsupported_os(tmp_path, monkeypatch):
    monkeypatch.setattr(regmod, "CURRENT_PLATFORM", "linux")
    reg = build_registry(ekran_ctx(tmp_path, screen=FakeScreen()))
    for name, args in (("take_screenshot", {"target": "screen", "monitor": 0}),
                       ("look_at_screen", {"question": "ne var", "target": "monitor"}),
                       ("arrange_windows", {"action": "minimize_all", "app": "", "second_app": "", "monitor": 0}),
                       ("read_text_aloud", {"source": "clipboard"}),
                       ("dictation", {"action": "start"})):
        r = run(reg.execute(name, args, ConfirmRecorder(True)))
        assert not r.ok and r.error == NOT_SUPPORTED, name
    names = {t["name"] for t in reg.to_claude_tools()}
    assert "take_screenshot" not in names and "speak_in_language" in names


def test_real_backends_say_unsupported_on_linux(tmp_path, win):
    if screen.IS_WINDOWS:
        pytest.skip("Windows'ta gerçek arka uçlar var")
    from tests.tool_fakes import make_ctx
    reg = build_registry(make_ctx(tmp_path))     # gerçek (desteklenmeyen) arka uçlar
    r = run(reg.execute("take_screenshot", {"target": "screen", "monitor": 0}))
    assert not r.ok and "yalnızca Windows" in r.error
    r = run(reg.execute("read_text_aloud", {"source": "clipboard"}))
    assert not r.ok and r.error == NOT_SUPPORTED
    r = run(reg.execute("arrange_windows", {"action": "maximize", "app": "", "second_app": "", "monitor": 0}))
    assert not r.ok and r.error == NOT_SUPPORTED


def test_look_at_screen_returns_downscaled_image_not_logged(tmp_path, win):
    ctx = ekran_ctx(tmp_path, screen=FakeScreen())
    db = Database(tmp_path / "a.db")
    from jarvis.security.audit_logger import AuditLogger
    reg = build_registry(ctx, audit=AuditLogger(db))
    r = run(reg.execute("look_at_screen", {"question": "bu hata ne", "target": "monitor"}))
    assert r.ok and r.untrusted and r.image is not None
    assert (r.image.width, r.image.height) == (1568, 882) and decode(r.image.data).width() == 1568
    assert r.data["question"] == "bu hata ne" and r.data["screen_region"] == [1920, 0, 2560, 1440]
    row = db.query("SELECT * FROM audit_log")[0]
    dumped = json.dumps(dict(row), default=str)
    assert len(dumped) < 2000 and "iVBOR" not in dumped           # PNG denetim kaydına yazılmadı
    assert not list((tmp_path / "Pictures").rglob("*.png"))      # diske de yazılmadı


def test_claude_tool_loop_sends_screenshot_as_image_block(tmp_path, win):
    """Uçtan uca: Claude look_at_screen çağırır, ikinci istekte tool_result içinde görüntü bloğu gider."""
    api = FakeAPI([tool_use_stream("look_at_screen", {"question": "ekranımda ne var", "target": "monitor"}),
                   text_stream(["Ekranında bir tarayıcı açık."])])
    store = SettingsStore(tmp_path / "settings.json")
    store.load()
    bus, state, db = EventBus(), StateMachine(), Database(tmp_path / "j.db")
    CredentialStore().set_anthropic_key("sk-ant-api03-" + "E" * 40)
    tools = ToolService(store, bus, state, db, ekran_ctx(tmp_path, screen=FakeScreen()))
    chat = ChatService(store, bus, state, db, ThreadPoolExecutor(2),
                       client_factory=lambda k, max_retries=0: api.client(k, max_retries=0))
    chat.tools = tools

    async def go():
        await chat.start()
        return await chat.send("ekranımda ne var?")

    done = run(go())
    assert done.text.endswith("tarayıcı açık.")
    msgs = [r for r in api.requests if r["path"] == "/v1/messages"]
    assert "look_at_screen" in [t["name"] for t in msgs[0]["body"]["tools"]]
    result = msgs[1]["body"]["messages"][-1]["content"][0]
    assert result["type"] == "tool_result" and result["is_error"] is False
    assert [b["type"] for b in result["content"]] == ["text", "image"]
    png = base64.b64decode(result["content"][1]["source"]["data"])
    assert decode(png).width() == 1568
    # konuşma geçmişine yalnızca metin girer (görüntü sonraki isteklerde tekrar gönderilmez)
    assert "iVBOR" not in json.dumps(db.query("SELECT * FROM messages") if _has_table(db, "messages") else [],
                                     default=str)


def _has_table(db, name):  # noqa: ANN001, ANN202
    return bool(db.query("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)))


# ======================= arrange_windows =======================
def layout_reg(tmp_path, layout):  # noqa: ANN001, ANN201
    return build_registry(ekran_ctx(tmp_path, layout=layout))


def arrange(reg, action, app="", second="", monitor=0):  # noqa: ANN001, ANN201
    return run(reg.execute("arrange_windows", {"action": action, "app": app, "second_app": second,
                                               "monitor": monitor}))


CHROME = (101, "Yeni sekme - Google Chrome", 11, Rect(100, 100, 800, 600))
CODE = (102, "main.py - jarvis - Visual Studio Code", 12, Rect(300, 200, 900, 700))
NOTES = (103, "notlar.txt - Not Defteri", 13, Rect(50, 50, 400, 300))


def test_side_by_side_by_app_names(tmp_path, win):
    lay = FakeLayout([NOTES, CHROME, CODE], active=103)
    r = arrange(layout_reg(tmp_path, lay), "side_by_side", "chrome", "vs code")
    assert r.ok and r.data["ok"], r.summary
    assert lay.rect[101] == Rect(0, 0, 960, 1040) and lay.rect[102] == Rect(960, 0, 960, 1040)
    assert "Google Chrome" in r.summary and "Visual Studio Code" in r.summary
    assert lay.rect[103] == NOTES[3]                                   # diğer pencereye dokunulmadı


def test_side_by_side_without_names_uses_top_two_and_same_window_error(tmp_path, win):
    lay = FakeLayout([CHROME, CODE], active=101)
    r = arrange(layout_reg(tmp_path, lay), "side_by_side")
    assert r.ok and lay.rect[101].left == 0 and lay.rect[102].left == 960
    lay = FakeLayout([CHROME], active=101)
    r = arrange(layout_reg(tmp_path, lay), "side_by_side", "chrome", "")
    assert not r.ok and "ikinci bir pencere" in r.error


def test_maximize_minimize_restore_active_window(tmp_path, win):
    lay = FakeLayout([CHROME, CODE], active=102)
    reg = layout_reg(tmp_path, lay)
    r = arrange(reg, "maximize")
    assert r.ok and lay.st[102] == "maximized" and "Visual Studio Code" in r.summary and "büyüttüm" in r.summary
    assert arrange(reg, "minimize", "chrome").ok and lay.st[101] == "minimized"
    assert arrange(reg, "restore", "google chrome").ok and lay.st[101] == "normal"


def test_state_change_not_happening_is_reported_honestly(tmp_path, win):
    lay = FakeLayout([CHROME], active=101)
    lay.stuck.add(101)
    tools = LayoutTools(ekran_ctx(tmp_path), lay, lambda e, n: set(), sleep=lambda s: None, wait_s=0.05)
    out = tools.arrange({"action": "maximize", "app": "", "second_app": "", "monitor": 0})
    assert out.data["ok"] is False and "durumu değişmedi" in out.summary


def test_left_and_right_half_and_min_size_limit(tmp_path, win):
    lay = FakeLayout([CHROME, CODE], active=101)
    reg = layout_reg(tmp_path, lay)
    r = arrange(reg, "left_half")
    assert r.ok and r.data["ok"] and lay.rect[101] == Rect(0, 0, 960, 1040) and "sol yarısına" in r.summary
    lay.rect[102] = Rect(2000, 100, 900, 700)                          # ikinci monitörde
    r = arrange(reg, "right_half", "visual studio code")
    assert r.ok and lay.rect[102] == Rect(1920 + 1280, 0, 1280, 1400)
    lay.min_size[101] = (1200, 0)                                      # uygulama daha dar olmuyor
    tools = LayoutTools(ekran_ctx(tmp_path), lay, lambda e, n: set(), sleep=lambda s: None, wait_s=0.05)
    out = tools.arrange({"action": "right_half", "app": "chrome", "second_app": "", "monitor": 0})
    assert out.data["ok"] is False and "tam oturmadı" in out.summary


def test_minimize_all_then_restore_all(tmp_path, win):
    lay = FakeLayout([CHROME, CODE, NOTES])
    lay.st[103] = "minimized"                                          # zaten küçüktü: geri açılmamalı
    reg = layout_reg(tmp_path, lay)
    r = arrange(reg, "minimize_all")
    assert r.ok and r.data["minimized"] == 2 and all(lay.st[h] == "minimized" for h in (101, 102))
    r = arrange(reg, "restore_all")
    assert r.ok and r.data["restored"] == 2 and lay.st[101] == lay.st[102] == "normal" and lay.st[103] == "minimized"
    r = arrange(reg, "restore_all")
    assert r.ok and r.data["restored"] == 0 and "önce" in r.summary


def test_move_to_other_monitor_keeps_relative_place_and_maximized_state(tmp_path, win):
    lay = FakeLayout([CHROME, CODE], active=101)
    reg = layout_reg(tmp_path, lay)
    r = arrange(reg, "move_to_monitor", "chrome")
    assert r.ok and r.data == {"monitor": 2, "ok": True} and lay.monitor_of(101) == 2
    got = lay.rect[101]
    assert got.width == round(800 * 2560 / 1920) and got.left == 1920 + round(100 / 1920 * 2560)
    r = arrange(reg, "move_to_monitor", "chrome")                       # sıradaki: yeniden birinciye
    assert r.ok and lay.monitor_of(101) == 1
    lay.st[102] = "maximized"
    r = arrange(reg, "move_to_monitor", "visual studio code", monitor=2)
    assert r.ok and lay.st[102] == "maximized" and lay.monitor_of(102) == 2
    r = arrange(reg, "move_to_monitor", "chrome", monitor=1)
    assert r.ok and "zaten 1. monitörde" in r.summary
    r = arrange(reg, "move_to_monitor", "chrome", monitor=4)
    assert not r.ok and "4. monitör yok" in r.error


def test_layout_errors(tmp_path, win):
    lay = FakeLayout([CHROME], monitors=[M1], active=101)
    reg = layout_reg(tmp_path, lay)
    r = arrange(reg, "move_to_monitor", "chrome")
    assert not r.ok and "tek monitör" in r.error
    r = arrange(reg, "maximize", "spotify")
    assert not r.ok and "Açık bir Spotify penceresi bulamadım" in r.error
    lay = FakeLayout([], active=None)
    r = arrange(layout_reg(tmp_path, lay), "maximize")
    assert not r.ok and "pencere bulamadım" in r.error
    r = arrange(layout_reg(tmp_path, lay), "minimize_all")
    assert r.ok and "Küçültülecek açık pencere yok" in r.summary
    with pytest.raises(regmod.NotSupportedError):
        UnsupportedLayout().windows()


# ======================= read_text_aloud / speak_in_language =======================
def speech_reg(tmp_path, kb=None, clip=None, bridge=None):  # noqa: ANN001, ANN201
    clip = clip or FakeClipboard()
    kb = kb or FakeKeyboard(clipboard=clip)
    kb.clipboard = clip
    ctx = ekran_ctx(tmp_path, keyboard=kb, clipboard=clip)
    if bridge is not None:
        ctx.extra["voice"] = bridge
    return build_registry(ctx), kb, clip


def test_read_selection_copies_reads_restores_and_speaks(tmp_path, win):
    br = FakeBridge()
    reg, kb, clip = speech_reg(tmp_path, bridge=br)
    r = run(reg.execute("read_text_aloud", {"source": "selection"}))
    assert r.ok and r.untrusted
    assert kb.copies == [False]                         # Ctrl+C (terminal değil)
    assert br.spoken == [("Seçili cümle burada.", "tr")]
    assert clip.text == "eski pano" and len(clip.restored) == 1        # pano eski hâline döndü
    assert r.data["text"] == "Seçili cümle burada." and "okudum" in r.summary


def test_read_selection_in_terminal_uses_ctrl_insert(tmp_path, win):
    clip = FakeClipboard()
    kb = FakeKeyboard(ForegroundInfo(9, 77, "Windows PowerShell", "CASCADIA_HOSTING_WINDOW_CLASS"), clip)
    reg, kb, clip = speech_reg(tmp_path, kb, clip, FakeBridge())
    assert run(reg.execute("read_text_aloud", {"source": "selection"})).ok
    assert kb.copies == [True]


def test_read_selection_refuses_jarvis_focus_and_empty_selection(tmp_path, win, monkeypatch):
    import os
    monkeypatch.setattr("jarvis.tools.speech_tools.COPY_WAIT_S", 0.05)
    clip = FakeClipboard()
    kb = FakeKeyboard(ForegroundInfo(1, os.getpid(), "JARVIS", "Qt"), clip)
    reg, kb, clip = speech_reg(tmp_path, kb, clip, FakeBridge())
    r = run(reg.execute("read_text_aloud", {"source": "selection"}))
    assert not r.ok and "Odak şu an JARVIS'te" in r.error and kb.copies == []
    kb.fg = ForegroundInfo(5, 4242, "Belge", "Notepad")
    kb.selection = None                                  # hiçbir şey seçili değil: pano değişmez
    r = run(reg.execute("read_text_aloud", {"source": "selection"}))
    assert not r.ok and "seçili bir metin bulamadım" in r.error and clip.restored == []


def test_sensitive_clipboard_is_never_read(tmp_path, win):
    clip = FakeClipboard("Parola123!", sensitive=True)
    br = FakeBridge()
    reg, kb, clip = speech_reg(tmp_path, clip=clip, bridge=br)
    r = run(reg.execute("read_text_aloud", {"source": "clipboard"}))
    assert not r.ok and "parola" in r.error.lower() and br.spoken == []
    r = run(reg.execute("read_text_aloud", {"source": "selection"}))
    assert not r.ok and br.spoken == [] and len(clip.restored) == 1    # yine de geri yüklendi


def test_read_clipboard_without_voice_shows_text(tmp_path, win):
    clip = FakeClipboard("Toplantı yarın saat onda.")
    reg, kb, clip = speech_reg(tmp_path, clip=clip, bridge=FakeBridge(can_speak=False))
    r = run(reg.execute("read_text_aloud", {"source": "clipboard"}))
    assert r.ok and kb.copies == [] and "sesli okuyamadım" in r.summary and "Toplantı yarın" in r.summary
    assert "Hoparlör bulunamadı" in r.summary
    clip.text = "   "
    r = run(reg.execute("read_text_aloud", {"source": "clipboard"}))
    assert not r.ok and "Panoda okunacak bir metin yok" in r.error


def test_speak_in_language(tmp_path, win):
    br = FakeBridge()
    reg, *_ = speech_reg(tmp_path, bridge=br)
    r = run(reg.execute("speak_in_language", {"text": "How are you?", "language": "en"}))
    assert r.ok and br.spoken == [("How are you?", "en")] and "İngilizce" in r.summary
    r = run(reg.execute("speak_in_language", {"text": "x", "language": "de"}))
    assert not r.ok and "izin verilen" in r.error                    # şema: yalnızca en/tr
    reg2, *_ = speech_reg(tmp_path / "b", bridge=FakeBridge(can_speak=False))
    r = run(reg2.execute("speak_in_language", {"text": "Hello", "language": "en"}))
    assert not r.ok and "Sesli söyleyemiyorum" in r.error


def test_win_input_unicode_events_cover_turkish_and_emoji():
    ev = text_to_key_events("çğıİşü\n😀")
    units = [s for vk, s, fl in ev if fl == win_input.KEYEVENTF_UNICODE]
    assert "".join(map(chr, units[:6])) == "çğıİşü"
    assert (win_input.VK_RETURN, 0, 0) in ev
    assert len([1 for vk, s, fl in ev if fl & win_input.KEYEVENTF_UNICODE]) == 2 * (6 + 2)   # emoji = 2 birim
    assert all(fl & win_input.KEYEVENTF_KEYUP for _, _, fl in ev[1::2])                      # her basışa bırakma
    assert text_to_key_events("a\x07b") == text_to_key_events("ab")                          # denetim karakteri yok


# ======================= dikte =======================
def test_dictation_formatting_and_stop_words():
    assert format_dictation("Merhaba virgül nasılsın soru işareti") == "Merhaba, nasılsın?"
    assert format_dictation("Merhaba.") == "Merhaba"                              # otomatik son nokta atılır
    assert format_dictation("bugün hava güzel", "Merhaba") == " bugün hava güzel"
    assert format_dictation("ilk satır yeni satır ikinci satır nokta") == "İlk satır\nİkinci satır."
    assert format_dictation("istanbul", "Selam?") == " İstanbul"
    assert format_dictation("tırnak aç merhaba tırnak kapat dedi") == '"Merhaba" dedi'
    assert format_dictation("Merhaba, virgül dünya") == "Merhaba, dünya"
    assert format_dictation("not iki nokta üst üste süt al") == "Not: süt al"
    for s in ("Dikte bitti.", "Jarvis, yazmayı bitir.", "dikte modunu kapat", "Dikteyi durdur"):
        assert is_stop_command(s), s
    for s in ("dikte bitti mi", "yazmayı bitirdim ama", "bitti"):
        assert not is_stop_command(s), s


def test_dictation_session_types_and_tracks_tail():
    kb = FakeKeyboard()
    s = DictationSession(kb)
    assert s.handle("Merhaba virgül") == "typed" and s.handle("nasılsın soru işareti") == "typed"
    assert "".join(kb.typed) == "Merhaba, nasılsın?"
    assert s.handle("iyiyim nokta") == "typed" and kb.typed[-1] == " İyiyim."     # soru işaretinden sonra büyük harf
    assert s.handle("Dikte bitti.") == "stop" and s.handle("  ") == "empty"
    assert s.typed_chars == len("".join(kb.typed)) and s.utterances == 3
    kb.fail_typing = regmod.ToolError("x", "Odaktaki pencereye yazamadım.")
    with pytest.raises(regmod.ToolError):
        s.handle("devam")


def test_dictation_tool_needs_confirmation_unless_voice(tmp_path, win):
    br = FakeBridge()
    reg, kb, _ = speech_reg(tmp_path, bridge=br)
    no = ConfirmRecorder(False)
    r = run(reg.execute("dictation", {"action": "start"}, no))
    assert not r.ok and not br.dictation_active and "odaktaki pencereye" in no.prompts[0].message
    r = run(reg.execute("dictation", {"action": "start"}, ConfirmRecorder(True)))
    assert r.ok and br.dictation_active and isinstance(br.sessions[0], DictationSession)
    assert br.sessions[0].keyboard is kb
    r = run(reg.execute("dictation", {"action": "start"}, ConfirmRecorder(True)))
    assert r.ok and "zaten açık" in r.summary
    r = run(reg.execute("dictation", {"action": "stop"}))             # durdurmak onay istemez
    assert r.ok and not br.dictation_active and r.summary == "Dikte modu kapandı."
    assert run(reg.execute("dictation", {"action": "stop"})).summary == "Dikte modu zaten kapalı."
    reg2, *_ = speech_reg(tmp_path / "b")                              # ses motoru yok
    r = run(reg2.execute("dictation", {"action": "start"}, ConfirmRecorder(True)))
    assert not r.ok and "ses motorunun" in r.error


# ======================= ses köprüsü: İngilizce ses indirme =======================
class _Resp:
    def __init__(self, data: bytes) -> None:
        self._b = io.BytesIO(data)
        self.headers = {"Content-Length": str(len(data))}

    def read(self, n: int = -1) -> bytes:
        return self._b.read(n)

    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, *a) -> None:  # noqa: ANN002
        pass


def _fake_voice(monkeypatch):  # noqa: ANN001, ANN202
    onnx, cfg = b"ONNX" * 1000, b'{"audio": {"sample_rate": 22050}}'
    v = _voice("en", "en_US-test-medium", "İngilizce ses (test)", "test", len(onnx), hashlib.md5(onnx).hexdigest(),
               len(cfg), hashlib.md5(cfg).hexdigest())
    monkeypatch.setattr(bridge_mod, "LANGUAGE_VOICES", {"en": v})
    return v, {v.onnx.url: onnx, v.config.url: cfg}


def test_english_voice_downloads_once_with_progress(tmp_path, monkeypatch):
    v, blobs = _fake_voice(monkeypatch)
    calls = []

    def opener(req, timeout):  # noqa: ANN001, ANN202
        calls.append(req.full_url)
        return _Resp(blobs[req.full_url])

    loaded = []
    vm = StubVoiceManager()
    b = VoiceBridge(vm, manager_factory=lambda: ModelManager(tmp_path / "models", opener=opener),
                    tts_factory=lambda p: loaded.append(p) or f"tts:{p.name}")
    assert b.speak("How are you?", "en") is True
    assert vm.spoken == [("How are you?", "tts:en_US-test-medium.onnx")]
    assert (tmp_path / "models" / "piper" / "en_US-test-medium.onnx").read_bytes() == blobs[v.onnx.url]
    assert len(calls) == 2 and any(p.finished for p in vm.progress)
    assert b.speak("Again", "en") and len(calls) == 2 and len(loaded) == 1   # indirme ve yükleme bir kez
    assert b.speak("Merhaba", "tr") and vm.spoken[-1] == ("Merhaba", None)  # Türkçe: ana ses


def test_english_voice_download_failure_is_honest(tmp_path, monkeypatch):
    _fake_voice(monkeypatch)

    def offline(req, timeout):  # noqa: ANN001, ANN202
        raise ConnectionError("yok")

    b = VoiceBridge(StubVoiceManager(), manager_factory=lambda: ModelManager(tmp_path / "m", opener=offline))
    with pytest.raises(ModelError) as e:
        b.speak("Hello", "en")
    assert "indirilemedi" in e.value.user_message and "internet" in e.value.user_message
    with pytest.raises(VoiceUnavailable):
        b.voice_for("de")
    vm = StubVoiceManager()
    vm.components = None
    assert VoiceBridge(vm).speak("Hello", "en") is False       # ses motoru yok: indirme bile denenmez


def test_english_voice_metadata_is_permissive_male_voice():
    assert ENGLISH_VOICE.key == "en_US-norman-medium" and "public domain" in ENGLISH_VOICE.license
    assert ENGLISH_VOICE.onnx.url.startswith("https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/norman/")
    assert ENGLISH_VOICE.onnx.md5 and ENGLISH_VOICE.onnx.size == 63531379 and ENGLISH_VOICE.config.kind == "json"


def test_bridge_dictation_runs_on_loop_thread():
    vm = StubVoiceManager()
    seen = []

    def start(session):  # noqa: ANN001, ANN202
        seen.append((session, threading.current_thread().name))
        vm.dictation = session

    vm.start_dictation = start
    vm.stop_dictation = lambda: bool(vm.dictation) and not setattr(vm, "dictation", None)
    b = VoiceBridge(vm)
    with pytest.raises(VoiceUnavailable):
        b.start_dictation("x")                                # döngü yok

    async def go():
        vm._loop = asyncio.get_running_loop()
        await asyncio.get_running_loop().run_in_executor(None, b.start_dictation, "oturum")
        assert b.dictation_active
        return await asyncio.get_running_loop().run_in_executor(None, b.stop_dictation)

    assert run(go()) is True and seen[0][0] == "oturum" and seen[0][1] == threading.main_thread().name
    assert not b.dictation_active
