# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller tarifi: JARVIS (onedir, pencereli).

Derleme (depo kökünden):
    python -m PyInstaller packaging/jarvis.spec --noconfirm --clean --distpath dist --workpath build/pyinstaller

Çıktı: dist/JARVIS/JARVIS.exe (+ dist/JARVIS/_internal/)

İsteğe bağlı paketler (ses: faster-whisper, ctranslate2, onnxruntime, Piper,
openWakeWord, sounddevice; Windows araçları: pywin32, pycaw, comtypes,
send2trash, pypdf, python-docx) KURULUYSA veri dosyaları ve DLL'leri eklenir,
kurulu değilse atlanır. Böylece bu tarif o bağımlılıklar requirements.txt'ye
eklenmeden önce de, eklendikten sonra da değiştirilmeden çalışır.

Ses modelleri (Whisper, Piper sesi, uyandırma kelimesi) exe'ye gömülmez;
kullanıcının veri klasörüne indirilir.
"""

import importlib.util
import os
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import (collect_data_files, collect_dynamic_libs, collect_submodules,
                                     copy_metadata)

ROOT = Path(SPECPATH).resolve().parent  # noqa: F821  (PyInstaller tanımlar)
APP_NAME = "JARVIS"
ICON = ROOT / "jarvis" / "assets" / "icon" / "jarvis.ico"
VERSION = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']',
                    (ROOT / "jarvis" / "__init__.py").read_text(encoding="utf-8"), re.MULTILINE).group(1)


def say(msg):
    print(f"[jarvis.spec] {msg}", flush=True)


def have(module):
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


datas = [(str(ROOT / "jarvis" / "assets"), "jarvis/assets")]
binaries = []
hiddenimports = [
    "qasync",
    "PySide6.QtSvg",          # sol ray ikonları (QSvgRenderer)
    "PySide6.QtNetwork",      # tek örnek (QLocalServer/QLocalSocket)
    # keyring arka uçları giriş noktalarıyla (entry points) yüklenir; statik analiz göremez
    "keyring.backends.Windows",
    "keyring.backends.chainer",
    "keyring.backends.fail",
]


def add_metadata(dist):
    try:
        datas.extend(copy_metadata(dist))
    except Exception as exc:  # noqa: BLE001  (paket kurulu değil)
        say(f"metadata atlandı: {dist} ({exc.__class__.__name__})")


# keyring arka uçlarını giriş noktalarından bulur: metadata olmazsa "güvenli depo yok" der
for dist in ("keyring", "anthropic", "httpx", "pydantic", "platformdirs", "qasync"):
    add_metadata(dist)
if have("win32ctypes"):
    hiddenimports += collect_submodules("win32ctypes")

# (modül, veri dosyaları, DLL'ler, tüm alt modüller, metadata dağıtım adı)
OPTIONAL = [
    # Ses motoru
    ("onnxruntime", True, True, False, "onnxruntime"),
    ("ctranslate2", True, True, False, "ctranslate2"),
    ("faster_whisper", True, False, True, "faster-whisper"),
    ("tokenizers", False, True, False, "tokenizers"),
    ("huggingface_hub", False, False, False, "huggingface-hub"),
    ("av", True, True, False, "av"),
    ("piper", True, True, True, "piper-tts"),
    ("piper_phonemize", True, True, False, "piper-phonemize"),
    ("espeakng_loader", True, True, False, "espeakng-loader"),
    ("openwakeword", True, False, True, "openwakeword"),
    ("sounddevice", False, False, False, "sounddevice"),
    ("_sounddevice_data", True, True, False, None),
    ("numpy", False, False, False, "numpy"),
    ("scipy", False, False, False, "scipy"),
    # Windows araçları
    ("win32com", False, False, False, "pywin32"),
    ("comtypes", False, False, True, "comtypes"),
    ("pycaw", False, False, True, "pycaw"),
    # --- MEDYA --- (projeksiyon tablolarını winrt'nin kendi PyInstaller kancası toplar)
    ("winrt", False, False, False, "winrt-runtime"),
    # --- MEDYA sonu ---
    ("send2trash", False, False, True, "Send2Trash"),
    ("pypdf", False, False, False, "pypdf"),
    ("docx", True, False, False, "python-docx"),
    # --- BELGE ---
    ("openpyxl", False, False, True, "openpyxl"),
    ("et_xmlfile", False, False, False, "et-xmlfile"),
    # --- BELGE sonu ---
]

for module, want_data, want_libs, want_subs, dist in OPTIONAL:
    if not have(module):
        say(f"{module} kurulu değil, atlanıyor")
        continue
    say(f"{module} ekleniyor")
    hiddenimports.append(module)
    if want_data:
        try:
            datas.extend(collect_data_files(module))
        except Exception as exc:  # noqa: BLE001
            say(f"  veri dosyaları alınamadı: {exc}")
    if want_libs:
        try:
            binaries.extend(collect_dynamic_libs(module))
        except Exception as exc:  # noqa: BLE001
            say(f"  DLL'ler alınamadı: {exc}")
    if want_subs:
        try:
            hiddenimports.extend(collect_submodules(module))
        except Exception as exc:  # noqa: BLE001
            say(f"  alt modüller alınamadı: {exc}")
    if dist:
        add_metadata(dist)

# Kullanılmayan büyük Qt modülleri ve geliştirme araçları pakete girmesin
EXCLUDES = [
    "tkinter", "pytest", "_pytest", "pytestqt", "fontTools",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtQuick3D",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DExtras", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.QtGraphs", "PySide6.QtDesigner", "PySide6.QtUiTools",
    "PySide6.QtHelp", "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtLocation",
    "PySide6.QtPositioning", "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtSensors",
    "PySide6.QtSerialPort", "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtSql",
    "PySide6.QtTest", "PySide6.QtTextToSpeech", "PySide6.QtSpatialAudio",
]

# Windows dosya sürümü (exe > Özellikler > Ayrıntılar)
version_file = None
if sys.platform == "win32":
    from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct, StringTable,
                                                     VarFileInfo, VarStruct, VSVersionInfo)

    nums = [int(x) for x in re.match(r"\d+(?:\.\d+)*", VERSION).group(0).split(".")][:4]
    nums += [0] * (4 - len(nums))
    version_file = VSVersionInfo(
        ffi=FixedFileInfo(filevers=tuple(nums), prodvers=tuple(nums)),
        kids=[
            StringFileInfo([StringTable("041F04B0", [  # Türkçe, Unicode
                StringStruct("CompanyName", APP_NAME),
                StringStruct("FileDescription", "JARVIS kişisel asistan"),
                StringStruct("FileVersion", VERSION),
                StringStruct("InternalName", APP_NAME),
                StringStruct("OriginalFilename", f"{APP_NAME}.exe"),
                StringStruct("ProductName", APP_NAME),
                StringStruct("ProductVersion", VERSION),
            ])]),
            VarFileInfo([VarStruct("Translation", [0x041F, 1200])]),
        ],
    )

say(f"sürüm {VERSION}, {len(datas)} veri girdisi, {len(binaries)} ikili, {len(hiddenimports)} gizli içe aktarım")

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # pencereli: konsol penceresi açılmaz
    disable_windowed_traceback=False,
    icon=str(ICON),
    version=version_file,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)
