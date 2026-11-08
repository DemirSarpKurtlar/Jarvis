"""Oyun başlatıcıları: Riot, Epic ve Xbox kayıtlarının örnek dosyalardan okunması ve oyunların adla açılması."""

import asyncio
import json

import pytest

from jarvis.tools import game_launchers as gl
from jarvis.tools import registry as regmod
from jarvis.tools.applications import AppEntry, AppIndex
from jarvis.tools.defaults import build_registry
from jarvis.tools.registry import ToolError
from tests.tool_fakes import FAKE_APPS, make_ctx


@pytest.fixture
def riot(tmp_path):
    root = tmp_path / "Riot Games"
    client = tmp_path / "Riot Client" / "RiotClientServices.exe"
    client.parent.mkdir(parents=True)
    client.write_bytes(b"MZ")
    root.mkdir()
    (root / "RiotClientInstalls.json").write_text(json.dumps({
        "associated_client": {str(tmp_path / "VALORANT" / "live"): str(client)},
        "rc_default": str(client), "rc_live": str(client)}), encoding="utf-8")
    game = tmp_path / "VALORANT" / "live"
    game.mkdir(parents=True)
    for product, install in (("valorant.live", game), ("league_of_legends.live", tmp_path / "kaldirildi"),
                             ("bacon.live", None), ("riot_client", None), ("valorant.pbe", game)):
        d = root / "Metadata" / product
        d.mkdir(parents=True)
        if install is not None:
            (d / f"{product}.product_settings.yaml").write_text(
                f'product_install_full_path: "{install.as_posix()}"\nshortcut_name: VALORANT.lnk\n', encoding="utf-8")
    return root, client


def test_riot_client_path_and_installed_products(riot, tmp_path):
    root, client = riot
    assert gl.riot_client_path(root) == client
    assert gl.riot_products(root) == [("bacon", "live"), ("valorant", "live"), ("valorant", "pbe")]
    names = [e.name for e in gl.riot_entries(root)]
    assert names == ["Legends of Runeterra", "VALORANT", "VALORANT (pbe)"]
    e = gl.riot_entries(root)[1]
    assert e.target == "riotclient://valorant/live" and e.kind == "game" and e.source == "riot"
    assert "valorant-win64-shipping.exe" in e.exe_names


def test_riot_missing_client_and_bad_json(tmp_path, monkeypatch):
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "pf"))
    root = tmp_path / "Riot Games"
    root.mkdir()
    (root / "RiotClientInstalls.json").write_text("{bozuk", encoding="utf-8")
    assert gl.riot_client_path(root) is None
    with pytest.raises(ToolError) as e:
        gl.launch_riot("riotclient://valorant/live", root=root, popen=lambda *a, **k: None)
    assert "Riot Client bulunamadı" in e.value.user_message
    assert gl.riot_products(tmp_path / "yok") == []


def test_launch_riot_uses_argument_list_without_shell(riot):
    root, client = riot
    calls = []
    gl.launch_riot("riotclient://valorant/live", root=root, popen=lambda args, **kw: calls.append((args, kw)))
    assert calls == [([str(client), "--launch-product=valorant", "--launch-patchline=live"],
                      {"shell": False, "close_fds": True})]
    for bad in ("riotclient://valorant/live --x", "riotclient://VALO&calc/live", "steam://x", "riotclient://a"):
        with pytest.raises(ToolError):
            gl.launch_riot(bad, root=root, popen=lambda *a, **k: calls.append(a))
    assert len(calls) == 1


def _item(folder, name, **fields):
    data = {"DisplayName": name, "AppName": name.replace(" ", ""), "CatalogNamespace": "ns1",
            "CatalogItemId": "abc123", "AppCategories": ["public", "games", "applications"],
            "LaunchExecutable": "Game/Binaries/Win64/Game.exe"}
    data.update(fields)
    (folder / f"{name}.item").write_text(json.dumps(data), encoding="utf-8")


def test_epic_manifests(tmp_path):
    m = tmp_path / "Manifests"
    m.mkdir()
    _item(m, "Fortnite", AppName="Fortnite", CatalogNamespace="fn", CatalogItemId="4fe75bbc5a674f4f9b356b5c90567da5",
          LaunchExecutable="FortniteGame/Binaries/Win64/FortniteLauncher.exe")
    _item(m, "Rocket League", AppName="Sugar")
    _item(m, "Bir DLC", AppName="DlcX", MainGameAppName="Sugar")
    _item(m, "Unreal Engine", AppName="UE_5", AppCategories=["engines"])
    _item(m, "Yarım", bIsIncompleteInstall=True)
    _item(m, "Kötü", CatalogNamespace="ns?x=1&y")
    (m / "bozuk.item").write_text("{", encoding="utf-8")
    entries = gl.epic_entries(m)
    assert [e.name for e in entries] == ["Fortnite", "Rocket League"]
    fn = entries[0]
    assert fn.target == ("com.epicgames.launcher://apps/fn%3A4fe75bbc5a674f4f9b356b5c90567da5%3AFortnite"
                         "?action=launch&silent=true")
    assert fn.kind == "game" and fn.exe_names == ("fortnitelauncher.exe",)


def test_xbox_games_marked_from_package_roots(tmp_path):
    gdk = tmp_path / "Forza"
    gdk.mkdir()
    (gdk / "MicrosoftGame.config").write_text("<Game/>", encoding="utf-8")
    packages = {
        "Microsoft.624F8B84B80_3.414.967.0_x64__8wekyb3d8bbwe": str(gdk),
        "Microsoft.MinecraftUWP_1.21.0.0_x64__8wekyb3d8bbwe": r"C:\XboxGames\Minecraft\Content",
        "Microsoft.WindowsCalculator_11.2.0.0_x64__8wekyb3d8bbwe": str(tmp_path / "calc"),
        "bozukad": str(gdk),
    }
    fams = gl.xbox_game_families(packages)
    assert fams == {"Microsoft.624F8B84B80_8wekyb3d8bbwe", "Microsoft.MinecraftUWP_8wekyb3d8bbwe"}
    entries = [AppEntry("Forza Horizon 5", "shell:AppsFolder\\Microsoft.624F8B84B80_8wekyb3d8bbwe!Game", "apps_folder"),
               AppEntry("Hesap Makinesi", "shell:AppsFolder\\Microsoft.WindowsCalculator_8wekyb3d8bbwe!App",
                        "apps_folder")]
    marked = gl.mark_xbox_games(entries, fams)
    assert [e.kind for e in marked] == ["game", "app"]
    assert gl.package_family("a_b") is None


GAMES = FAKE_APPS + [
    AppEntry("VALORANT", "riotclient://valorant/live", "riot", ("valorant", "valo"), kind="game"),
    AppEntry("VALORANT", r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Riot Games\VALORANT.lnk", "start_menu"),
    AppEntry("Fortnite", "com.epicgames.launcher://apps/fn%3A4fe%3AFortnite?action=launch&silent=true", "epic",
             kind="game"),
    AppEntry("League of Legends", "riotclient://league_of_legends/live", "riot", ("lol", "league", "lig of legends"),
             kind="game"),
    AppEntry("Forza Horizon 5", "shell:AppsFolder\\Microsoft.624F8B84B80_8wekyb3d8bbwe!Game", "apps_folder"),
]


@pytest.mark.parametrize("query,expected", [
    ("valorant", "VALORANT"), ("Valorant'ı", "VALORANT"), ("valoran", "VALORANT"), ("volarant", "VALORANT"),
    ("valorantı", "VALORANT"), ("fortnayt", "Fortnite"), ("fortnite", "Fortnite"), ("lol", "League of Legends"),
    ("lig of lecends", "League of Legends"),
])
def test_phonetic_game_names(query, expected):
    e = AppIndex(GAMES).best(query, kind="game")
    assert e is not None and e.name == expected


def test_riot_entry_preferred_over_start_menu_shortcut():
    assert AppIndex(GAMES).best("valorant").source == "riot"


def test_launch_game_opens_riot_epic_and_store_games(tmp_path, monkeypatch):
    monkeypatch.setattr(regmod, "CURRENT_PLATFORM", "win32")
    ctx = make_ctx(tmp_path, GAMES)
    riot_calls = []
    ctx.extra["riot_launcher"] = riot_calls.append
    reg = build_registry(ctx)
    run = lambda name, args: asyncio.run(reg.execute(name, args))  # noqa: E731
    r = run("launch_game", {"name": "valorant"})
    assert r.ok and riot_calls == ["riotclient://valorant/live"] and "VALORANT" in r.summary
    assert run("launch_game", {"name": "fortnayt"}).ok
    assert ctx.opener.opened[-1].startswith("com.epicgames.launcher://apps/fn%3A")
    assert run("launch_game", {"name": "forza horizon"}).ok          # oyun işaretsiz Mağaza oyunu da açılır
    assert ctx.opener.opened[-1].startswith("shell:AppsFolder\\Microsoft.624F8B84B80")
    assert run("open_app", {"name": "valorant"}).ok and len(riot_calls) == 2
    listed = run("list_games", {"limit": 50})
    assert {"VALORANT", "Fortnite", "League of Legends"} <= set(listed.data)


def test_vocabulary_prefers_launcher_games():
    vocab = AppIndex(GAMES).vocabulary()
    for name in ("VALORANT", "Fortnite", "League of Legends"):
        assert name in vocab[:8]
