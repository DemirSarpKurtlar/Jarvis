"""Konuşma tanıma doğruluğu: Whisper ipucu (uygulama adları + Türkçe komutlar), ipucu yankısı,
uydurma süzgeci ve yabancı adların Türkçe okunuşla eşleştirilmesi ("Rokit Lig" -> Rocket League)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import numpy as np
import pytest

from tests.tool_fakes import FAKE_APPS, make_ctx
from tests.voice_fakes import FakeBackend
from jarvis.tools import intents
from jarvis.tools.applications import (BUILTIN_APPS, AppEntry, AppIndex, AppTools, phonetic_score,
                                       vocabulary_names)
from jarvis.tools.registry import ToolError
from jarvis.tools.text import consonant_skeleton, phonetic_key
from jarvis.voice.stt import WhisperSTT, choose, clean_transcript
from jarvis.voice.vocabulary import (BASE_PHRASES, DEFAULT_MAX_TOKENS, VOCAB_INTRO, PromptCache, build_prompt,
                                     estimate_tokens, is_prompt_echo)


# ---------------- ipucu metni ----------------
def test_prompt_contains_turkish_commands_and_app_names():
    p = build_prompt(["Rocket League", "Discord", "Steam"])
    assert p.startswith(VOCAB_INTRO + " Rocket League, Discord, Steam.")
    for phrase in BASE_PHRASES:
        assert phrase in p
    assert p.endswith(BASE_PHRASES[-1])                 # komut cümleleri modele en yakın yerde
    assert "Not Defteri'ni aç" in p and "Sesi yüzde 30 yap" in p and "Chrome'u kapat" in p
    assert estimate_tokens(p) <= DEFAULT_MAX_TOKENS


def test_prompt_without_vocabulary_is_just_commands():
    assert build_prompt([]) == " ".join(BASE_PHRASES)
    assert VOCAB_INTRO not in build_prompt(["", "   "])


def test_prompt_is_bounded_and_keeps_priority_order():
    names = [f"Oyun Numara {i}" for i in range(500)]
    p = build_prompt(names, max_tokens=120)
    assert estimate_tokens(p) <= 120
    assert "Oyun Numara 0," in p and "Oyun Numara 499" not in p
    # Whisper ipucunun yalnızca son 223 belirtecini kullanır: varsayılan bütçe bunun altında kalmalı
    assert estimate_tokens(build_prompt(names)) <= 223


def test_prompt_with_real_token_counter_and_tiny_budget():
    words = lambda s: len(s.split())  # noqa: E731 - kelime = belirteç sayan sahte tokenizer
    p = build_prompt(["A B C D E F G H", "Kısa", "Discord"], max_tokens=words(" ".join(BASE_PHRASES)) + 6,
                     count_tokens=words)
    assert "Kısa" in p and "Discord" in p and "A B C" not in p   # sığmayan uzun ad atlanır, kısalar girer
    assert build_prompt(["Discord"], max_tokens=5, count_tokens=words) == " ".join(BASE_PHRASES)


def test_prompt_dedups_cleans_and_skips_overlong_names():
    p = build_prompt(["Discord", "DISCORD", "discord", "Rocket League™", "Euro, Truck", "X" * 41])
    assert p.count("iscord") + p.count("ISCORD") == 2      # adlarda bir kez + komutlarda bir kez
    assert "Rocket League," in p and "™" not in p
    assert "Euro Truck" in p and "X" * 41 not in p


def test_prompt_cache_rebuilds_on_change_and_survives_provider_errors(caplog):
    names = ["Discord"]
    cache = PromptCache()
    assert "Discord," not in cache.get(lambda: names) and "Discord." in cache.get(lambda: names)
    names.append("Rocket League")
    assert "Rocket League" in cache.get(lambda: list(names))

    def broken():  # noqa: ANN202
        raise RuntimeError("dizin bozuk")
    assert cache.get(broken) == " ".join(BASE_PHRASES)
    assert cache.get(None) == " ".join(BASE_PHRASES)


@pytest.mark.parametrize("text,echo", [
    ("Not Defteri'ni aç.", False),                      # ipucunda geçse de kısa komut geçerli
    ("Rocket League'i aç.", False),
    ("Hesap makinesini aç. Sesi yüzde 30 yap. Chrome'u kapat.", True),
    ("Bilgisayardaki uygulamalar ve oyunlar: Steam", True),
    ("Bugün hava nasıl, yarın yağmur yağacak mı acaba?", False),
    ("", False),
])
def test_prompt_echo_detection(text, echo):
    prompt = build_prompt(["Rocket League", "Steam"])
    assert is_prompt_echo(text, prompt) is echo


# ---------------- WhisperSTT ipucunu modele veriyor mu ----------------
class FakeWhisperModel:
    def __init__(self, outputs: list[str], tokenizer=None) -> None:  # noqa: ANN001
        self.outputs = list(outputs)
        self.calls: list[dict] = []
        if tokenizer is not None:
            self.hf_tokenizer = tokenizer

    def transcribe(self, audio, **kw):  # noqa: ANN001, ANN201
        """Çıktı: metin, (metin, no_speech_prob, avg_logprob) ya da None (hiç bölüt yok)."""
        self.calls.append(kw)
        out = self.outputs.pop(0)
        if out is None:
            return iter([]), SimpleNamespace(language="tr")
        text, nsp, alp = (out, 0.01, -0.2) if isinstance(out, str) else out
        seg = SimpleNamespace(text=text, no_speech_prob=nsp, avg_logprob=alp)
        return iter([seg]), SimpleNamespace(language="tr")


class WordTokenizer:
    def encode(self, text, add_special_tokens=False):  # noqa: ANN001, ANN201
        return SimpleNamespace(ids=text.split())


def make_stt(outputs, **kw) -> tuple[WhisperSTT, FakeWhisperModel]:  # noqa: ANN001
    stt = WhisperSTT(__import__("pathlib").Path("yok"), choose("small", "cpu"), **kw)
    model = FakeWhisperModel(outputs, kw.pop("tokenizer", None) if "tokenizer" in kw else None)
    stt._model = model
    return stt, model


def test_transcribe_passes_prompt_with_installed_app_names():
    stt, model = make_stt([" Rocket League'i aç."], vocabulary=lambda: ["Rocket League", "Discord"], use_prompt=True)
    tr = stt.transcribe(np.zeros(16000, np.int16))
    assert tr.text == "Rocket League'i aç."
    kw = model.calls[0]
    assert kw["language"] == "tr" and kw["beam_size"] == 5
    assert "Rocket League" in kw["initial_prompt"] and "Not Defteri'ni aç." in kw["initial_prompt"]
    assert "hotwords" not in kw or kw["hotwords"] is None   # ipucu tek yerden: initial_prompt


def test_vocabulary_attached_later_is_used():
    stt, model = make_stt([" a b", " c d"], use_prompt=True)
    stt.transcribe(np.zeros(1600, np.float32))
    assert VOCAB_INTRO not in model.calls[0]["initial_prompt"]
    stt.set_vocabulary(lambda: ["Valorant"])
    stt.transcribe(np.zeros(1600, np.float32))
    assert "Valorant" in model.calls[1]["initial_prompt"]


def test_prompt_can_be_disabled(monkeypatch):
    stt, model = make_stt([" Merhaba"], vocabulary=lambda: ["Steam"], use_prompt=False)
    stt.transcribe(np.zeros(1600, np.float32))
    assert model.calls[0]["initial_prompt"] is None
    monkeypatch.setenv("JARVIS_STT_PROMPT", "off")
    assert WhisperSTT(__import__("pathlib").Path("yok"), choose("small", "cpu")).initial_prompt() is None
    monkeypatch.setenv("JARVIS_STT_PROMPT", "")
    assert WhisperSTT(__import__("pathlib").Path("yok"), choose("small", "cpu")).initial_prompt()


def test_prompt_echo_is_retried_without_prompt():
    stt, model = make_stt([" Hesap makinesini aç. Sesi yüzde 30 yap. Chrome'u kapat.", " Saat kaç?"],
                          use_prompt=True)
    tr = stt.transcribe(np.zeros(16000, np.int16))
    assert tr.text == "Saat kaç?"
    assert model.calls[0]["initial_prompt"] and model.calls[1]["initial_prompt"] is None


def test_prompted_text_in_silence_is_dropped():
    """Gerçek ölçüm: Whisper small ipucuyla 2 sn sessizlikte "Hesap'ı kapat." yazdı (nsp=0.84, alp=-0.86).
    Şüpheli çıktı ipucusuz doğrulanır; orada konuşma yoksa boş döner."""
    stt, model = make_stt([(" Hesap'ı kapat.", 0.84, -0.86), None], use_prompt=True)
    tr = stt.transcribe(np.zeros(32000, np.int16))
    assert tr.text == "" and len(model.calls) == 2 and model.calls[1]["initial_prompt"] is None


def test_suspect_but_real_speech_keeps_prompted_text():
    stt, model = make_stt([(" Rocket League'i aç.", 0.55, -0.4), (" Roket gibi aç.", 0.3, -0.6)], use_prompt=True)
    assert stt.transcribe(np.zeros(16000, np.int16)).text == "Rocket League'i aç."   # ipucunun düzelttiği metin kalır
    stt, model = make_stt([(" Discord'u aç.", 0.2, -0.3)], use_prompt=True)
    assert stt.transcribe(np.zeros(16000, np.int16)).text == "Discord'u aç." and len(model.calls) == 1


def test_prompt_budget_uses_model_tokenizer():
    stt, _ = make_stt([], vocabulary=lambda: [f"Ad{i}" for i in range(400)], use_prompt=True)
    stt._model.hf_tokenizer = WordTokenizer()        # kelime başına 1 belirteç
    prompt = stt.initial_prompt()
    assert prompt is not None and len(prompt.split()) <= DEFAULT_MAX_TOKENS
    assert "Ad0," in prompt and "Ad399" not in prompt


# ---------------- uydurma süzgeci kısa komutları atmamalı ----------------
@pytest.mark.parametrize("text,expected", [
    ("Aç.", "Aç."),
    ("Dur", "Dur"),
    ("Evet.", "Evet."),
    ("Saat kaç?", "Saat kaç?"),
    ("Napıyon?", "Napıyon?"),
    ("Not Defteri'ni aç.", "Not Defteri'ni aç."),
    ("YouTube'da altyazıyı aç.", "YouTube'da altyazıyı aç."),
    ("Altyazıları kapat", "Altyazıları kapat"),
    ("Abonelikleri aç", "Abonelikleri aç"),
    ("Jarvis, Rocket League'i aç.", "Rocket League'i aç."),
    ("Altyazı M.K.", ""),
    ("Alt yazı M.K.", ""),
    ("Altyazı", ""),
    ("Abone ol.", ""),
    ("Abone olmayı unutmayın!", ""),
    ("İzlediğiniz için teşekkürler.", ""),
    ("Thanks for watching!", ""),
    ("Jarvis.", ""),
    ("a", ""),
])
def test_clean_transcript_keeps_short_commands(text, expected):
    assert clean_transcript(text) == expected


# ---------------- uygulama dizininden ad listesi ----------------
def test_vocabulary_ranks_games_first_and_filters_noise():
    entries = list(BUILTIN_APPS) + [
        AppEntry("Discord", "d.lnk", "start_menu"),
        AppEntry("Rocket League", "steam://rungameid/252950", "steam", kind="game"),
        AppEntry("Microsoft Visual C++ 2015 Redistributable (x64)", "", "uninstall", launchable=False),
        AppEntry("7-Zip", "", "uninstall", launchable=False),
        AppEntry("ODBC Data Sources (64-bit)", "odbc", "apps_folder"),
        AppEntry("Windows PowerShell ISE", "ise", "apps_folder"),
        AppEntry("NVIDIA Driver Helper", "x", "start_menu"),
        AppEntry("Uninstall Discord", "u.lnk", "start_menu"),
        AppEntry("msedge", "msedge.exe", "app_paths"),
        AppEntry("discord", "x", "apps_folder"),                       # tekrar (büyük/küçük harf)
        AppEntry("Bir Çok Uzun Adlı Yardımcı Araç Paketi Sürüm", "x", "start_menu"),
        AppEntry("VALORANT", "v.lnk", "start_menu", kind="game"),
    ]
    names = vocabulary_names(entries)
    assert names[:2] == ["VALORANT", "Rocket League"]
    assert names.index("Discord") < names.index("Not Defteri")       # kurulan uygulamalar yerleşiklerden önce
    for bad in ("7-Zip", "ODBC Data Sources (64-bit)", "Windows PowerShell ISE", "NVIDIA Driver Helper",
                "Uninstall Discord", "msedge", "Bir Çok Uzun Adlı Yardımcı Araç Paketi Sürüm"):
        assert bad not in names
    assert sum(1 for n in names if n.lower() == "discord") == 1
    assert len(vocabulary_names(entries, limit=3)) == 3


def test_app_index_vocabulary_never_triggers_discovery():
    calls = []

    def discover():  # noqa: ANN202
        calls.append(1)
        return [AppEntry("Rocket League", "steam://rungameid/252950", "steam", kind="game")]

    idx = AppIndex(discover=discover)
    assert idx.vocabulary() == [] and calls == []                    # dizin hazır değil: taramaz, bekletmez
    idx.entries()
    assert idx.vocabulary() == ["Rocket League"] and calls == [1]


# ---------------- Türkçe okunuşla eşleştirme ----------------
PHONETIC_APPS = list(BUILTIN_APPS) + [
    AppEntry("Rocket League", "steam://rungameid/252950", "steam", kind="game"),
    AppEntry("VALORANT", "v.lnk", "start_menu", kind="game"),
    AppEntry("Counter-Strike 2", "steam://rungameid/730", "steam", kind="game"),
    AppEntry("Discord", "d.lnk", "start_menu"),
    AppEntry("Google Chrome", "c.lnk", "start_menu"),
    AppEntry("Spotify", "s.lnk", "apps_folder"),
    AppEntry("Steam", "st.lnk", "start_menu"),
    AppEntry("WhatsApp", "w", "apps_folder"),
    AppEntry("Microsoft Excel", "e.lnk", "start_menu"),
    AppEntry("Twitch", "t.lnk", "start_menu"),
    AppEntry("Zoom", "z.lnk", "start_menu"),
    AppEntry("Telegram", "tg.lnk", "start_menu"),
    AppEntry("Notion", "n.lnk", "start_menu"),
    AppEntry("Visual Studio Code", "code.exe", "start_menu"),
]


@pytest.mark.parametrize("query,expected", [
    ("Rokit Lig", "Rocket League"),
    ("rokit lig", "Rocket League"),
    ("rokıt lig", "Rocket League"),
    ("Rokıt Lig'i", "Rocket League"),
    ("rokit ligi", "Rocket League"),
    ("Roket Lig", "Rocket League"),
    ("Rocket League", "Rocket League"),
    ("diskort", "Discord"),
    ("diskordu", "Discord"),
    ("Diskord", "Discord"),
    ("valorant", "VALORANT"),
    ("valorınt", "VALORANT"),
    ("kroom", "Google Chrome"),
    ("krom", "Google Chrome"),
    ("spotifay", "Spotify"),
    ("stim", "Steam"),
    ("vatsap", "WhatsApp"),
    ("eksel", "Microsoft Excel"),
    ("tviç", "Twitch"),
    ("zum", "Zoom"),
    ("telegrım", "Telegram"),
    ("kaunter strayk 2", "Counter-Strike 2"),
    ("not defterini", "Not Defteri"),
    ("hesap makinesi", "Hesap Makinesi"),
    # yanlış eşleşme olmamalı: emin değilse None
    ("Rokit digi", None),
    ("not after night", None),
    ("lig", None),
    ("oyun", None),
    ("kart", None),
    ("disk", None),
    ("discovery", None),
    ("tim", None),
    ("word", None),
    ("ses", None),
    ("battlefield", None),
])
def test_phonetic_app_matching_table(query, expected):
    idx = AppIndex(PHONETIC_APPS)
    got = idx.best(query)
    assert (got.name if got else None) == expected


def test_phonetic_keys():
    assert phonetic_key("Rocket League") == phonetic_key("Rokıt Lig") == "roket lik" or \
        consonant_skeleton(phonetic_key("Rocket League")) == consonant_skeleton(phonetic_key("Rokıt Lig"))
    assert phonetic_key("Discord") == phonetic_key("diskort") == "diskort"
    assert phonetic_key("Chrome") == phonetic_key("krom") == "krom"
    assert phonetic_key("Twitch") == phonetic_key("tviç")
    assert phonetic_key("Counter-Strike 2").endswith(" 2")
    assert phonetic_score("rokit lig", "rocket league") >= 0.86
    assert phonetic_score("rokit", "rocket league") < 0.78          # kelime sayısı farklı: iskelet kuralı yok
    assert phonetic_score("", "steam") == 0.0


def test_ambiguous_phonetic_match_returns_none():
    # İki aday aynı okunuşta: tahmin etme, çağıran "şunlardan birini mi demek istedin" diye sorar
    idx = AppIndex([AppEntry("Diskort", "a", "start_menu"), AppEntry("Discord", "b", "start_menu")])
    assert idx.best("diskord") is None
    idx = AppIndex([AppEntry("Rocket League", "a", "steam", kind="game"), AppEntry("Roket Lig", "b", "start_menu")])
    assert idx.best("rokit lig") is None
    assert idx.best("Rocket League").name == "Rocket League"          # birebir ad her zaman kazanır
    # Aynı oyunun Steam kaydı + Başlat menüsü kısayolu rakip sayılmaz
    idx2 = AppIndex([AppEntry("Rocket League", "steam://rungameid/252950", "steam", kind="game"),
                     AppEntry("Rocket League", "r.lnk", "start_menu")])
    assert idx2.best("rokit lig") is not None and idx2.best("rokit lig").name == "Rocket League"
    assert [e.name for e, _ in idx.find("rokit lig")] == ["Roket Lig", "Rocket League"] or \
        {e.name for e, _ in idx.find("rokit lig")} == {"Roket Lig", "Rocket League"}


def test_voice_style_commands_resolve_end_to_end(tmp_path):
    """Konuşma tanımanın yazdığı Türkçe okunuşlu komut -> yerel ayrıştırıcı -> doğru uygulama."""
    apps = AppTools(make_ctx(tmp_path, PHONETIC_APPS))
    cases = {"Rokit Lig'i aç.": "Rocket League", "Jarvis, diskordu aç": "Discord", "Kroom'u aç": "Google Chrome",
             "Valorant'ı aç": "VALORANT", "Not defterini aç.": "Not Defteri", "Stim'i aç": "Steam"}
    for text, expected in cases.items():
        it = intents.parse(text)
        assert it is not None and it.tool == "open_app", text
        assert apps.prepare_open(it.args)["_entry"].name == expected, text
    game = intents.parse("Rokit Lig oyununu aç")
    assert game is not None and game.tool == "launch_game"
    assert apps.prepare_game(game.args)["_entry"].name == "Rocket League"
    with pytest.raises(ToolError) as e:
        apps.prepare_open(intents.parse("Rokit digi aç").args)       # STT tamamen yanlış duyduysa sor
    assert "Rocket League" in e.value.user_message


@pytest.mark.parametrize("text,tool", [
    ("Diskordu aç.", "open_app"),                 # eskiden "disk" ile başladığı için disk_info oluyordu
    ("Pillars of Eternity'yi aç", "open_app"),
    ("Rampage'i aç", "open_app"),
    ("disk durumu", "disk_info"),
    ("diskte ne kadar yer kaldı", "disk_info"),
    ("disklerim dolu mu", "disk_info"),
    ("RAM kullanımı ne kadar", "memory_status"),
    ("ramim yetiyor mu", "memory_status"),
    ("pilim ne kadar", "battery_status"),
    ("pil durumu", "battery_status"),
    ("şarj kaç", "battery_status"),
])
def test_app_names_are_not_taken_for_system_queries(text, tool):
    it = intents.parse(text)
    assert it is not None and it.tool == tool, (text, it)


@pytest.mark.parametrize("text,args", [
    ("Sesi yüzde elli yap.", {"level": 50}),            # Whisper ipucuyla sayıları yazıyla da verebiliyor
    ("sesi yüzde yetmiş beş yap", {"level": 75}),
    ("sesi otuza çek", {"level": 30}),
    ("sesi yüzde yüz yap", {"level": 100}),
    ("ses seviyesini yirmiye ayarla", {"level": 20}),
    ("sesi yüzde dörde düşür", {"level": 4}),
    ("sesi yüzde sıfır yap", {"level": 0}),
    ("sesi on artır", {"delta": 10}),
    ("sesi biraz kıs", {"delta": -10}),               # "biraz" sayı sanılmamalı
    ("sesi yüz elli yap", None),                        # 150 değil: emin değilse yerelde işleme
])
def test_volume_number_words(text, args):
    it = intents.parse(text)
    if args is None:
        assert it is None
    else:
        assert it is not None and it.args == args, (text, it)


# ---------------- bağlantı: Assistant -> VoiceManager -> WhisperSTT ----------------
class VocabSTT:
    label = "sahte"

    def __init__(self) -> None:
        self.vocabulary = None

    def set_vocabulary(self, fn) -> None:  # noqa: ANN001
        self.vocabulary = fn

    def transcribe(self, audio):  # noqa: ANN001, ANN201
        raise AssertionError("çağrılmamalı")


def test_voice_manager_attaches_vocabulary_to_stt(tmp_path):
    from jarvis.config.settings import SettingsStore
    from jarvis.core.events import EventBus
    from jarvis.core.state import StateMachine
    from jarvis.voice.audio_io import MicrophoneStream
    from jarvis.voice.components import VoiceComponents
    from jarvis.voice.vad import EnergyVad
    from jarvis.voice.voice_manager import VoiceManager
    from tests.voice_fakes import StaticLoader

    store = SettingsStore(tmp_path / "s.json")
    store.load()
    store.settings.voice.microphone_enabled = False
    stt = VocabSTT()
    comps = VoiceComponents(EnergyVad(-70), lambda: MicrophoneStream(FakeBackend()), None, stt=stt, stt_label="x")

    def vocab():  # noqa: ANN202
        return ["Rocket League"]

    async def send(text):  # noqa: ANN001, ANN202
        return None

    vm = VoiceManager(store, EventBus(), StateMachine(), SimpleNamespace(busy=False), StaticLoader(comps),
                      sleep=lambda s: None, send=send, vocabulary=vocab)

    async def go():  # noqa: ANN202
        await vm.start()
        await vm.shutdown()

    asyncio.run(go())
    assert stt.vocabulary is vocab


def test_assistant_feeds_installed_apps_to_voice(tmp_path):
    from jarvis.config.settings import SettingsStore
    from jarvis.core.assistant import Assistant
    from jarvis.paths import AppPaths

    store = SettingsStore(AppPaths.resolve().settings_file)
    store.load()
    a = Assistant(store, data_dir=tmp_path / "d", tool_context=make_ctx(tmp_path))
    try:
        assert a.voice._vocabulary == a._voice_vocabulary
        names = a._voice_vocabulary()
        assert names[:3] == ["Hades", "Counter-Strike 2", "Euro Truck Simulator 2"]     # oyunlar önce
        assert "Discord" in names and "Spotify" in names and "7-Zip" not in names
        assert set(names) <= {e.name for e in FAKE_APPS}
        a.tool_service.ctx.apps = None
        assert a._voice_vocabulary() == []
    finally:
        a.db.close()


# ---------------- gerçek modellerle (isteğe bağlı) ----------------
@pytest.mark.models
def test_real_whisper_prompt_fixes_app_names():
    """Piper'ın Türkçe sesiyle Türkçe okunuşlu komutlar -> Whisper small (CPU) ipucuyla -> doğru uygulama.
    Ölçüm (temiz ses): ipucusuz "Rokek TGH.", "Diskordu aç." (disk_info!), "Jomu kapat."; ipuculu doğru."""
    from tests import model_assets
    from tests.test_voice_models import _speech_16k
    from jarvis.voice.tts import PiperTTS

    tts = PiperTTS(model_assets.turkish_voice())
    wdir = model_assets.whisper()
    index = AppIndex(PHONETIC_APPS)
    stt = WhisperSTT(wdir, choose(wdir.name, "cpu"), vocabulary=index.vocabulary, use_prompt=True)
    stt.load()
    for spoken, tool, expected in (("Roket Lig'i aç.", "open_app", "Rocket League"),
                                   ("Diskord'u aç.", "open_app", "Discord"),
                                   ("Not defterini aç.", "open_app", "Not Defteri"),
                                   ("Krom'u kapat.", "close_app", "Google Chrome")):
        tr = stt.transcribe(_speech_16k(tts, spoken))
        it = intents.parse(tr.text)
        assert it is not None and it.tool == tool, (spoken, tr.text)
        assert index.best(it.args["name"]).name == expected, (spoken, tr.text)
    assert stt.transcribe(np.zeros(32000, np.int16)).text == ""       # ipucu sessizlikte komut uydurmasın
