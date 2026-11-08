"""Bilgisayar kullanan ajan: ölçekleme, tuş adları, güvenlik kuralları, görüntü işlemleri, eylem yürütücü."""

import base64
import threading

import numpy as np
import pytest

from jarvis.agent import imaging, safety
from jarvis.agent.actions import ActionError, ActionExecutor, Interrupted, SafetyStop
from jarvis.agent.backends import ForegroundInfo
from jarvis.agent.geometry import CoordinateError, Rect, ScreenScaler, fit_size
from jarvis.agent.keys import KeyError_, parse_combo, parse_modifiers
from tests.agent_fakes import make_desktop

VALORANT_FG = ForegroundInfo("VALORANT  ", "UnrealWindow", "valorant-win64-shipping.exe", 7)


# ---------- ölçekleme ----------
@pytest.mark.parametrize("w,h,expected", [
    (2560, 1440, (1366, 768)), (3840, 2160, (1366, 768)), (1920, 1080, (1366, 768)),
    (1280, 720, (1280, 720)), (1366, 768, (1366, 768)), (1080, 1920, (768, 1366)),
])
def test_fit_size_downscales_to_target(w, h, expected):
    assert fit_size(w, h) == expected


def test_fit_size_respects_api_limits():
    w, h = fit_size(7680, 4320, long_edge=5000)
    assert max(w, h) <= 2576 and w * h <= 3_750_000


@pytest.mark.parametrize("mon", [Rect(0, 0, 2560, 1440), Rect(-1920, 0, 1920, 1080), Rect(2560, -300, 3840, 2160),
                                 Rect(0, 0, 1366, 768)])
def test_coordinate_round_trip_within_one_screenshot_pixel(mon):
    sc = ScreenScaler.fit(mon)
    for x, y in [(0, 0), (sc.width - 1, sc.height - 1), (sc.width // 2, sc.height // 3), (17, 401)]:
        px, py = sc.to_physical([x, y])
        assert mon.contains(px, py)
        assert sc.to_screenshot(px, py) == (x, y)
    # fiziksel -> ekran görüntüsü -> fiziksel: en çok bir ölçek adımı sapma
    for px, py in [(mon.left + 5, mon.top + 9), (mon.right - 1, mon.bottom - 1), (mon.left + mon.width // 2, mon.top)]:
        x, y = sc.to_screenshot(px, py)
        bx, by = sc.to_physical([x, y])
        assert abs(bx - px) <= sc.scale_x + 1 and abs(by - py) <= sc.scale_y + 1


def test_out_of_screen_and_bad_coordinates_rejected():
    sc = ScreenScaler.fit(Rect(0, 0, 2560, 1440))
    for bad in ([1366, 10], [-1, 5], [5], "10,10", [True, 3], None):
        with pytest.raises(CoordinateError):
            sc.to_physical(bad)


def test_zoom_region_maps_to_physical_crop():
    sc = ScreenScaler.fit(Rect(-2560, 0, 2560, 1440))
    r = sc.region_to_physical([683, 384, 0, 0])          # ters sıra da kabul
    assert (r.left, r.top) == (0, 0) and abs(r.width - 1280) <= 2 and abs(r.height - 720) <= 2
    with pytest.raises(CoordinateError):
        sc.region_to_physical([10, 10, 11, 11])
    with pytest.raises(CoordinateError):
        sc.region_to_physical([1, 2, 3])


# ---------- tuşlar ----------
def test_parse_key_combos():
    c = parse_combo("ctrl+shift+Escape")
    assert c.modifiers == (0xA2, 0xA0) and c.key.vk == 0x1B
    assert parse_combo("Return").key.vk == 0x0D
    assert parse_combo("alt+Tab").modifiers == (0xA4,)
    assert parse_combo("F5").key.vk == 0x74
    assert parse_combo("Page_Down").key.extended
    assert parse_combo("ctrl+s").key.vk == ord("S")
    assert parse_combo("ctrl++").key.char == "+"
    assert parse_combo("ğ").key == parse_combo("ğ").key and parse_combo("ğ").key.vk == 0   # Unicode yolu
    assert parse_combo("shift").key is None
    assert parse_modifiers("ctrl+shift") == (0xA2, 0xA0)
    for bad in ("", "hyper+x", "notakey", "ctrl+banana"):
        with pytest.raises(KeyError_):
            parse_combo(bad)
    with pytest.raises(KeyError_):
        parse_modifiers("x")


# ---------- güvenlik ----------
@pytest.mark.parametrize("fg", [
    ForegroundInfo("", "", "consent.exe"),
    ForegroundInfo("Windows Güvenliği", "ApplicationFrameWindow", "applicationframehost.exe"),
    ForegroundInfo("Windows Security", "x", "x.exe"),
    ForegroundInfo("Komut İstemi", "ConsoleWindowClass", "conhost.exe"),
    ForegroundInfo("Çalıştır", "#32770", "explorer.exe"),
    ForegroundInfo("Ziraat Bankası İnternet Şubesi - Google Chrome", "Chrome_WidgetWin_1", "chrome.exe"),
    ForegroundInfo("Ödeme - Steam", "x", "steamwebhelper.exe"),
    ForegroundInfo("Windows PowerShell", "x", "powershell.exe"),
])
def test_sensitive_windows_detected(fg):
    assert safety.sensitive_reason(fg)


@pytest.mark.parametrize("fg", [
    ForegroundInfo("VALORANT", "UnrealWindow", "valorant-win64-shipping.exe"),
    ForegroundInfo("Riot Client", "RCLIENT", "riotclientux.exe"),
    ForegroundInfo("Temple Run 2", "x", "templerun.exe"),
    ForegroundInfo("Belgesiz - Not Defteri", "Notepad", "notepad.exe"),
])
def test_normal_windows_allowed(fg):
    assert safety.sensitive_reason(fg) is None


@pytest.mark.parametrize("text", ["sk-ant-api03-abcdefghijklmnop", "4111 1111 1111 1111", "TR33 0006 1005 1978 6457 8413 26",
                                  "Xk9#mQ2$vL8pR4!z", "ghp_abcdefghijklmnopqrstuvwxyz0123"])
def test_secret_text_refused(text):
    assert safety.secret_reason(text)


def test_login_window_blocks_credential_like_text():
    fg = ForegroundInfo("Riot Client - Oturum aç", "x", "riotclientux.exe")
    assert safety.secret_reason("benimsifrem123", fg)
    assert safety.secret_reason("takımlı ölüm kalım", fg) is None   # boşluklu normal metin


@pytest.mark.parametrize("text", ["takımlı ölüm kalım", "valorant", "Tarkan Şımarık", "hava durumu İstanbul", "1234"])
def test_normal_text_allowed(text):
    assert safety.secret_reason(text, ForegroundInfo("Google - Chrome")) is None


@pytest.mark.parametrize("goal", ["maçı benim yerime oyna", "düşmanlara nişan al ve ateş et", "Valorantta rank kas",
                                  "oyunda düşmanları öldür", "maçı kazan", "aimbot gibi oyna"])
def test_gameplay_goals_refused(goal):
    reason = safety.gameplay_goal_reason(goal)
    assert reason and "hile" in reason


@pytest.mark.parametrize("goal", ["Valorant'ı aç ve takımlı ölüm kalım maçı başlat", "Spotify'da Tarkan çal",
                                  "League of Legends'ı açıp sıraya gir", "Chrome'da hava durumuna bak",
                                  "yürütülen görevleri listele"])
def test_menu_goals_allowed(goal):
    assert safety.gameplay_goal_reason(goal) is None


def test_dangerous_shortcuts_refused():
    assert safety.key_reason(parse_combo("super+r"))
    assert safety.key_reason(parse_combo("ctrl+alt+Delete"))
    assert safety.key_reason(parse_combo("ctrl+shift+Escape"))
    assert safety.key_reason(parse_combo("alt+F4")) is None


def test_game_window_blocks_movement_and_holding():
    assert safety.game_action_reason("hold_key", VALORANT_FG)
    assert safety.game_action_reason("left_mouse_down", VALORANT_FG)
    assert safety.game_action_reason("key", VALORANT_FG, parse_combo("w"))
    assert safety.game_action_reason("key", VALORANT_FG, parse_combo("space"))
    assert safety.game_action_reason("key", VALORANT_FG, parse_combo("Escape")) is None
    assert safety.game_action_reason("left_click", VALORANT_FG) is None
    assert safety.game_action_reason("hold_key", ForegroundInfo(process="notepad.exe")) is None


# ---------- görüntü ----------
def test_png_fallback_and_jpeg_encoding():
    frame = np.zeros((40, 60, 3), np.uint8)
    frame[:, :30] = 200
    media, data = imaging.encode(frame, prefer_jpeg=False)
    assert media == "image/png" and data.startswith(b"\x89PNG")
    media, data = imaging.encode(frame)
    assert media in ("image/jpeg", "image/png") and len(data) > 100


def test_resize_and_numpy_fallback_sizes():
    frame = np.random.default_rng(1).integers(0, 255, (1440, 2560, 3), dtype=np.uint8)
    assert imaging.resize(frame, 1366, 768).shape == (768, 1366, 3)
    assert imaging._numpy_resize(frame, 1366, 768).shape == (768, 1366, 3)


def test_change_ratio_global_and_local():
    a = np.zeros((360, 640, 3), np.uint8)
    b = a.copy()
    ta, tb = imaging.thumbnail(a), imaging.thumbnail(b)
    assert imaging.change_ratio(ta, tb) == 0.0
    b[0:40, 0:60] = 255                       # sol üstte küçük değişim
    tb = imaging.thumbnail(b)
    assert 0 < imaging.change_ratio(ta, tb) < 0.1
    assert imaging.change_ratio(ta, tb, around=(0.05, 0.05)) > 0.2
    assert imaging.change_ratio(ta, tb, around=(0.9, 0.9)) == 0.0
    assert imaging.change_ratio(None, tb) == 1.0


# ---------- eylem yürütücü ----------
def _executor(monitors=None, fg=None, static=False):
    d = make_desktop(monitors, static, fg)
    mon = d.screen.monitors()[0]
    return d, ActionExecutor(d, ScreenScaler.fit(mon), threading.Event())


def _decode_size(media_b64: tuple[str, str]) -> tuple[int, int]:
    from PySide6.QtGui import QImage

    img = QImage.fromData(base64.b64decode(media_b64[1]))
    return img.width(), img.height()


def test_screenshot_is_downscaled_image():
    d, ex = _executor()
    res = ex.run("screenshot", {})
    assert res.image is not None and _decode_size(res.image) == (1366, 768)
    assert isinstance(res.content(), list) and res.content()[0]["type"] == "image"
    assert res.thumb is not None


def test_black_screenshot_adds_fullscreen_hint():
    d, ex = _executor()
    d.screen.frame_fn = lambda mon, n: np.zeros((mon.height, mon.width, 3), np.uint8)
    res = ex.run("screenshot", {})
    assert "pencereli tam ekran" in res.note and res.content()[1]["type"] == "text"


def test_zoom_crops_full_resolution_and_fills_budget():
    d, ex = _executor()
    res = ex.run("zoom", {"region": [0, 0, 683, 384]})
    w, h = _decode_size(res.image)
    assert abs(w - 1366) <= 2 and abs(h - 768) <= 2   # çeyrek ekran, tam bütçeye büyütüldü
    with pytest.raises(ActionError):
        ex.run("zoom", {"region": [0, 0, 1, 1]})


def test_click_maps_to_physical_pixels_on_second_monitor():
    d, ex = _executor(monitors=[Rect(-2560, 0, 2560, 1440)])
    ex.run("left_click", {"coordinate": [683, 384]})
    moves = [e for e in d.input.events if e[0] == "move"]
    assert moves[-1] == ("move", -2560 + 1280, 720)
    assert d.input.clicks() == [("button", "left", True), ("button", "left", False)]
    ex.run("double_click", {"coordinate": [0, 0], "text": "ctrl"})
    ev = d.input.events
    assert ("key", 0xA2, True) in ev and ev[-1] == ("key", 0xA2, False)   # değiştirici en son bırakılır
    assert len(d.input.clicks()) == 6


def test_type_sends_unicode_turkish_text_and_newline():
    d, ex = _executor()
    ex.run("type", {"text": "Şımarık ğüİç\nok"})
    assert d.input.typed() == "Şımarık ğüİçok"
    assert ("key", 0x0D, True) in d.input.events


def test_type_secret_refused_and_nothing_typed():
    d, ex = _executor()
    with pytest.raises(ActionError, match="yazmıyorum"):
        ex.run("type", {"text": "sk-ant-api03-gizlianahtar1234"})
    assert d.input.events == []


def test_key_combo_press_and_release_order_and_repeat():
    d, ex = _executor()
    ex.run("key", {"text": "ctrl+s"})
    assert d.input.events == [("key", 0xA2, True), ("key", ord("S"), True), ("key", ord("S"), False),
                              ("key", 0xA2, False)]
    d.input.events.clear()
    ex.run("key", {"text": "Down", "repeat": 3})
    assert sum(1 for e in d.input.events if e == ("key", 0x28, True)) == 3
    ex.run("key", {"text": "Escape"})
    assert d.kill_switch.injected == 1           # kendi Esc'imiz acil durdurmayı tetiklemesin
    with pytest.raises(ActionError):
        ex.run("key", {"text": "super+r"})
    with pytest.raises(ActionError):
        ex.run("key", {"text": "Down", "repeat": 500})


def test_scroll_cursor_position_and_mouse_buttons():
    d, ex = _executor()
    ex.run("scroll", {"scroll_direction": "down", "scroll_amount": 5, "coordinate": [100, 100]})
    assert ("wheel", -5, 0) in d.input.events
    ex.run("scroll", {"scroll_direction": "left", "scroll_amount": 2})
    assert ("wheel", 0, -2) in d.input.events
    assert ex.run("cursor_position", {}).text == "X=100,Y=100"
    ex.run("left_mouse_down", {})
    ex.release_all()
    assert d.input.events[-1] == ("button", "left", False)
    with pytest.raises(ActionError):
        ex.run("scroll", {"scroll_direction": "sideways", "scroll_amount": 1})
    with pytest.raises(ActionError):
        ex.run("teleport", {})


def test_wait_and_hold_are_interruptible_and_release_keys():
    d, ex = _executor()
    d.clock.on_sleep = lambda c: ex.stop.set() if c.slept > 1.0 else None
    with pytest.raises(Interrupted):
        ex.run("hold_key", {"text": "shift", "duration": 30})
    assert d.input.events[-1] == ("key", 0xA0, False)
    assert d.clock.slept < 2.0


def test_drag_releases_button_when_stopped():
    d, ex = _executor()
    d.clock.on_sleep = lambda c: ex.stop.set()
    with pytest.raises(Interrupted):
        ex.run("left_click_drag", {"start_coordinate": [10, 10], "coordinate": [500, 400]})
    assert d.input.clicks()[-1] == ("button", "left", False)


def test_deadline_stops_long_wait():
    d, ex = _executor()
    ex.deadline = d.clock.monotonic() + 5
    with pytest.raises(Interrupted) as e:
        ex.run("wait", {"duration": 300})
    assert e.value.args[0] == "time" and d.clock.slept <= 5.2


def test_sensitive_foreground_stops_and_uac_process_detected():
    d, ex = _executor(fg=ForegroundInfo("", "", "consent.exe"))
    with pytest.raises(SafetyStop):
        ex.guard_foreground()
    d2, ex2 = _executor()
    d2.probe.running.add("consent.exe")
    with pytest.raises(SafetyStop, match="UAC"):
        ex2.guard_foreground()


def test_game_window_guard_in_executor():
    d, ex = _executor(fg=VALORANT_FG)
    with pytest.raises(ActionError, match="hile"):
        ex.run("hold_key", {"text": "w", "duration": 2})
    with pytest.raises(ActionError, match="hile"):
        ex.run("key", {"text": "a"})
    ex.run("left_click", {"coordinate": [683, 700]})   # menüde tıklama serbest
    assert d.input.clicks()


def test_win32_module_imports_and_is_windows_only():
    from jarvis.agent import win32

    assert win32.default_desktop() is None or win32.IS_WINDOWS
