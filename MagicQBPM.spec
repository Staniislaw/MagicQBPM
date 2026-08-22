# -*- mode: python ; coding: utf-8 -*-
"""
Reteta PyInstaller pentru MagicQ BPM Controller.

    py -3.12 -m PyInstaller MagicQBPM.spec --noconfirm

Rezultat:  dist/MagicQBPM/MagicQBPM.exe  + folderul cu biblioteci.

DE CE onedir SI NU onefile:
Cu --onefile, executabilul se dezarhiveaza intr-un folder temporar la
FIECARE pornire. Cu scipy + numpy + Qt inseamna 10-20 de secunde de
asteptare de fiecare data, si un folder temp de sute de MB. Pentru o
unealta de scena, pornirea instantanee conteaza mai mult decat un singur
fisier. Se distribuie folderul intreg (sau o arhiva zip).

CONFIGURAREA NU E IMPACHETATA in exe:
config/ se copiaza LANGA executabil, ca sa poti edita regulile si sa se
poata salva calibrarea. core/config.py detecteaza `sys.frozen` si
foloseste folderul executabilului ca radacina.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

PROJECT = Path(SPECPATH)

# --- biblioteci native care trebuie luate cu tot cu DLL-uri ---
binaries = []
binaries += collect_dynamic_libs("sounddevice")     # PortAudio
binaries += collect_dynamic_libs("pyaudiowpatch")   # PortAudio cu loopback

datas = []
datas += collect_data_files("sounddevice")
datas += collect_data_files("soundcard")            # definitiile cffi

hiddenimports = [
    # backend-uri audio incarcate dinamic (import in interiorul functiilor)
    "soundcard", "soundcard.mediafoundation",
    "pyaudiowpatch",
    "sounddevice",
    # uneltele, apelate ca subcomenzi (--doctor, --calibrate, ...)
    "tools.doctor", "tools.calibrate_palettes", "tools.record_analysis",
    "tools.loopback_check", "tools.selftest", "tools.keyboard_test",
    "tools.magicq_test", "tools.osc_discover",
    # transporturi optionale
    "pythonosc", "pythonosc.udp_client", "pythonosc.osc_packet",
    "mido", "mido.backends.rtmidi", "rtmidi",
    "keyboard",
    # scipy: submodule folosite prin import lenes
    "scipy.signal", "scipy.ndimage", "scipy.special._cdflib",
]

# ce NU are rost sa intre: reduce mult marimea
excludes = [
    "matplotlib", "tkinter", "PIL", "pandas", "IPython", "jupyter",
    "notebook", "pytest", "sphinx", "setuptools", "pip",
    "PyQt6.QtWebEngineCore", "PyQt6.QtWebEngineWidgets", "PyQt6.Qt3DCore",
    "PyQt6.QtBluetooth", "PyQt6.QtQuick", "PyQt6.QtQml", "PyQt6.QtMultimedia",
    "PyQt6.QtPositioning", "PyQt6.QtSensors", "PyQt6.QtSerialPort",
    "PyQt6.QtNetworkAuth", "PyQt6.QtDesigner", "PyQt6.QtHelp", "PyQt6.QtSql",
    "PyQt6.QtTest", "PyQt6.QtPdf", "PyQt6.QtCharts", "PyQt6.QtDataVisualization",
]

a = Analysis(
    ["main.py"],
    pathex=[str(PROJECT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MagicQBPM",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,          # consola ramane: acolo se vad calibrarea si erorile
    disable_windowed_traceback=False,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="MagicQBPM",
)
