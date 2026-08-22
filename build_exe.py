"""
build_exe.py
============
Construieste executabilul pentru PC-uri fara Python.

    py -3.12 build_exe.py

Rezultat:  dist/MagicQBPM/  - se copiaza intreg pe celalalt PC si se da
dublu-click pe MagicQBPM.exe

Ce face in plus fata de PyInstaller simplu:
  * copiaza config/ LANGA executabil (nu in interiorul lui), ca regulile
    si culorile sa ramana editabile si calibrarea sa se poata salva
  * NU copiaza settings.json-ul tau (contine calibrarea acestui PC) -
    duce doar sablonul; aplicatia isi genereaza singura settings.json
  * scrie CITESTE-MA.txt cu pasii pentru PC-ul nou
  * face scurtaturi .bat pentru calibrare, doctor si panou
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist" / "MagicQBPM"

BATCH_FILES = {
    "1 - VERIFICA INSTALAREA.bat": (
        "@echo off\r\n"
        "title Verificare instalare\r\n"
        "MagicQBPM.exe --doctor\r\n"
        "pause\r\n"),
    "2 - CALIBREAZA BUTOANELE.bat": (
        "@echo off\r\n"
        "title Calibrare grila Execute\r\n"
        "echo Deschide MagicQ cu fereastra EXECUTE vizibila, apoi apasa o tasta.\r\n"
        "pause\r\n"
        "MagicQBPM.exe --calibrate exec\r\n"
        "pause\r\n"),
    "3 - VERIFICA BUTOANELE.bat": (
        "@echo off\r\n"
        "title Test butoane Execute\r\n"
        "MagicQBPM.exe --test-exec\r\n"
        "pause\r\n"),
    "PORNESTE.bat": (
        "@echo off\r\n"
        "title MagicQ BPM Controller\r\n"
        "MagicQBPM.exe --rules config\\rules_execute.json\r\n"),
    "PORNESTE PANOU BPM.bat": (
        "@echo off\r\n"
        "title Panou BPM si culori\r\n"
        "MagicQBPM.exe --panel\r\n"),
}

READ_ME = """\
MagicQ BPM Controller
=====================

PC NOU, PRIMA DATA - in ordinea asta:

  1. Porneste MagicQ PC cu show-ul tau si cu fereastra EXECUTE deschisa.
     Pune fereastra MagicQ la marimea la care o vei folosi si NU o mai
     redimensiona dupa aceea (mutatul pe alt monitor e permis).

  2. Dublu-click pe:   1 - VERIFICA INSTALAREA.bat
     Iti spune ce mai lipseste.

  3. Dublu-click pe:   2 - CALIBREAZA BUTOANELE.bat
     Pui mouse-ul pe butonul din STANGA-SUS al grilei Execute si apesi
     SPACE, apoi pe cel din DREAPTA-JOS si iar SPACE.
     ATENTIE: nu da click, doar tine mouse-ul deasupra si apasa SPACE.

  4. Dublu-click pe:   3 - VERIFICA BUTOANELE.bat
     Cursorul se plimba peste toate butoanele, FARA sa apese. Verifica
     daca nimereste. Daca nu, reia pasul 3.

  5. Dublu-click pe:   PORNESTE.bat

Daca butoanele din show-ul tau stau altfel decat in maparea implicita,
editeaza  config\\settings.json  la  magicq.mouse.exec_buttons  -
fiecare buton e [rand, coloana], numarate de la 1.


CE PORNESTE CE
--------------
  PORNESTE.bat             aplicatia completa, cu reguli automate
  PORNESTE PANOU BPM.bat   doar BPM + combinatii de culori,
                           NU trimite nimic fara click-ul tau


UNDE SE REGLEAZA
----------------
  config\\rules_execute.json   ce se intampla la drop, break, buildup
  config\\palettes.json        combinatiile de culori din panou
  config\\settings.json        praguri de analiza, calibrare, butoane

Toate sunt fisiere text - se editeaza cu Notepad. In fiecare, la inceput,
e explicat ce se poate schimba.


DACA CEVA NU MERGE
------------------
  MagicQBPM.exe --doctor              verifica tot
  MagicQBPM.exe --check-audio 10      aude muzica? (cu muzica pornita)
  MagicQBPM.exe --record 120          inregistreaza analiza pe 2 minute
  MagicQBPM.exe --list-devices        ce device-uri audio exista

Jurnalul complet:  logs\\magicq_audio.log

Oprire de urgenta: butonul PANIC din interfata, sau CTRL+ALT+P oriunde.
"""


def _exe_is_running() -> bool:
    """True daca MagicQBPM.exe e pornit (ar bloca suprascrierea)."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq MagicQBPM.exe", "/NH"],
            capture_output=True, text=True, timeout=10).stdout
        return "MagicQBPM.exe" in out
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    print("=" * 66)
    print("  BUILD MagicQ BPM Controller")
    print("=" * 66)

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("\n  PyInstaller lipseste. Instaleaza-l cu:")
        print("     py -3.12 -m pip install pyinstaller")
        return 1

    # Un exe pornit tine fisierele blocate, iar PyInstaller esueaza cu o
    # eroare de permisiuni greu de citit. Verificam intai.
    if _exe_is_running():
        print("\n  MagicQBPM.exe RULEAZA ACUM.")
        print("  Inchide fereastra aplicatiei (si panoul BPM, daca e deschis)")
        print("  si porneste build-ul din nou. Altfel fisierele sunt blocate.\n")
        return 1

    for folder in (ROOT / "build", ROOT / "dist"):
        if folder.exists():
            print(f"  Sterg {folder.name}/ ...")
            shutil.rmtree(folder, ignore_errors=True)
            if folder.exists():
                print(f"\n  Nu pot sterge {folder}/ - probabil un fisier e "
                      "deschis sau folderul e deschis in Explorer.\n")
                return 1

    print("\n  Rulez PyInstaller (dureaza cateva minute)...\n")
    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "MagicQBPM.spec", "--noconfirm"],
        cwd=ROOT)
    if result.returncode != 0:
        print("\n  BUILD ESUAT.")
        return result.returncode
    if not DIST.exists():
        print(f"\n  Nu gasesc {DIST}")
        return 1

    # ---- configurarea, LANGA exe (nu in interiorul lui) ----
    print("\n  Copiez configurarea langa executabil...")
    target = DIST / "config"
    target.mkdir(exist_ok=True)
    # Se duc DOAR fisierele folosite. Celelalte rules_*.json sunt exemple
    # pentru alte moduri de control (playback-uri, palete) si daca ajung
    # langa exe pot fi incarcate din greseala - au apasat PB1-PB10 pe un
    # show care nu avea nimic acolo.
    wanted = ["rules_execute.json", "palettes.json", "settings.example.json"]
    copied = 0
    for name in wanted:
        src = ROOT / "config" / name
        if src.exists():
            shutil.copy2(src, target / name)
            copied += 1
        else:
            print(f"     ATENTIE: lipseste {name}")
    print(f"     {copied} fisiere: {', '.join(wanted)}")
    print("     (settings.json se genereaza pe PC-ul nou din sablon)")

    (DIST / "logs").mkdir(exist_ok=True)

    for name, content in BATCH_FILES.items():
        (DIST / name).write_text(content, encoding="cp1252")
    print(f"     {len(BATCH_FILES)} scurtaturi .bat")

    (DIST / "CITESTE-MA.txt").write_text(READ_ME, encoding="cp1252")

    size = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file())
    print("\n" + "-" * 66)
    print(f"  GATA:  {DIST}")
    print(f"  Marime: {size / 1024 / 1024:.0f} MB")
    print("\n  Copiaza folderul intreg pe celalalt PC si citeste CITESTE-MA.txt")
    print("-" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
