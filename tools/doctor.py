"""
tools/doctor.py
===============
Verifica daca aplicatia e gata de rulat pe PC-ul asta.

    py -3.12 tools/doctor.py

De rulat prima data pe un PC nou. Verifica pe rand: Python, pachetele,
captura audio, MagicQ, tastatura si calibrarea mouse-ului - si spune
exact ce mai trebuie facut, cu comanda de rulat.

Ce se muta odata cu folderul (nu trebuie refacut):
    config/rules_*.json      regulile
    config/palettes.json     combinatiile de culori
    exec_buttons             maparea butoanelor (daca folosesti acelasi show)

Ce NU se muta (trebuie refacut pe fiecare PC):
    magicq.mouse.grids       coordonatele grilei Execute
    magicq.mouse.calibrated_size
    -> depind de rezolutie si de marimea ferestrei MagicQ
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OK, WARN, BAD = "  OK  ", " ATENTIE", " LIPSA"
_todo: list[str] = []


def line(status: str, label: str, detail: str = "") -> None:
    print(f"  [{status:^8}] {label:<34} {detail}")


def todo(text: str) -> None:
    _todo.append(text)


def main() -> int:
    print("\n" + "=" * 72)
    print("  VERIFICARE INSTALARE")
    print("=" * 72 + "\n")

    # ---------------- 1. Python ----------------
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    good = sys.version_info[:2] >= (3, 10)
    line(OK if good else BAD, "Python", version)
    if not good:
        todo("Instaleaza Python 3.12 de pe python.org")

    # ---------------- 2. pachete ----------------
    print()
    required = {"numpy": "numpy", "scipy": "scipy", "sounddevice": "sounddevice",
                "PyQt6.QtWidgets": "PyQt6", "pythonosc": "python-osc"}
    optional = {"soundcard": "soundcard", "pyaudiowpatch": "PyAudioWPatch",
                "mido": "mido", "keyboard": "keyboard"}
    missing_req, missing_opt = [], []
    for module, package in required.items():
        try:
            __import__(module)
            line(OK, f"pachet {package}")
        except Exception:  # noqa: BLE001
            line(BAD, f"pachet {package}", "obligatoriu")
            missing_req.append(package)
    for module, package in optional.items():
        try:
            __import__(module)
            line(OK, f"pachet {package}", "optional")
        except Exception:  # noqa: BLE001
            line(WARN, f"pachet {package}", "optional, lipseste")
            missing_opt.append(package)
    if missing_req or missing_opt:
        todo("py -3.12 -m pip install -r requirements.txt")

    # ---------------- 3. audio ----------------
    print()
    try:
        from audio.capture import available_loopback_backends
        backends = available_loopback_backends()
        if backends:
            line(OK, "captura sunet din PC", ", ".join(backends))
        else:
            line(BAD, "captura sunet din PC", "niciun backend")
            todo("py -3.12 -m pip install soundcard")
    except Exception as exc:  # noqa: BLE001
        line(BAD, "captura sunet din PC", str(exc)[:40])
        backends = []

    # ---------------- 4. configurare ----------------
    print()
    from core.config import load_settings
    cfg = load_settings()
    line(OK, "config/settings.json", str(cfg.path))

    rules_dir = ROOT / "config"
    rule_files = sorted(p.name for p in rules_dir.glob("rules*.json"))
    line(OK if rule_files else BAD, "fisiere de reguli", ", ".join(rule_files) or "niciunul")

    buttons = {k: v for k, v in (cfg.get("magicq.mouse.exec_buttons", {}) or {}).items()
               if not k.startswith("_")}
    if buttons:
        line(OK, "butoane Execute mapate", f"{len(buttons)} butoane")
    else:
        line(WARN, "butoane Execute mapate", "niciunul")
        todo("Completeaza magicq.mouse.exec_buttons in config/settings.json")

    # ---------------- 5. MagicQ ----------------
    print()
    from magicq.keyboard import (client_area, describe_magicq_windows,
                                 enable_dpi_awareness, find_magicq_window)
    enable_dpi_awareness()
    process = cfg.get("magicq.keyboard.window_process", "mqqt.exe")
    hwnd = find_magicq_window(process, cfg.get("magicq.keyboard.window_title_regex", "^MagicQ"))
    area = client_area(hwnd) if hwnd else None
    if hwnd and area and area[2] > 0:
        line(OK, "MagicQ ruleaza", f"fereastra {area[2]}x{area[3]}")
        for row in describe_magicq_windows(process):
            print(f"              {row}")
    elif hwnd:
        line(WARN, "MagicQ ruleaza", "dar e minimizat")
        todo("Restaureaza fereastra MagicQ (nu o lasa minimizata)")
    else:
        line(BAD, "MagicQ ruleaza", "negasit")
        todo("Porneste MagicQ PC inainte de calibrare")

    # ---------------- 6. calibrarea mouse-ului ----------------
    print()
    grids = cfg.get("magicq.mouse.grids", {}) or {}
    calibrated = cfg.get("magicq.mouse.calibrated_size")
    if "exec" not in grids:
        line(BAD, "grila Execute calibrata", "NU")
        todo("py -3.12 tools/calibrate_palettes.py exec")
    else:
        grid = grids["exec"]
        first, last = grid.get("first", [0, 0]), grid.get("last", [0, 0])
        if min(first + last) < 0:
            line(BAD, "grila Execute calibrata", "coordonate negative - invalida")
            todo("py -3.12 tools/calibrate_palettes.py exec")
        elif calibrated and area and tuple(calibrated) != (area[2], area[3]):
            line(WARN, "grila Execute calibrata",
                 f"pentru {calibrated[0]}x{calibrated[1]}, acum {area[2]}x{area[3]}")
            todo("Fereastra MagicQ are alta marime decat la calibrare. "
                 "Fie o pui la loc, fie: py -3.12 tools/calibrate_palettes.py exec")
        else:
            line(OK, "grila Execute calibrata",
                 f"{grid.get('cols')}x{grid.get('rows')} casute")

    mouse_on = cfg.get("magicq.mouse.enabled")
    line(OK if mouse_on else WARN, "transport mouse",
         "activat" if mouse_on else "dezactivat in settings.json")
    if not mouse_on:
        todo('Pune "enabled": true la magicq.mouse in config/settings.json')

    priority = cfg.get("magicq.priority", [])
    line(OK if "mouse" in priority else WARN, "ordine transporturi",
         " > ".join(priority))
    if "mouse" not in priority:
        todo('Adauga "mouse" in magicq.priority')

    # ---------------- rezumat ----------------
    print("\n" + "-" * 72)
    if not _todo:
        print("  Totul e pregatit. Porneste cu:\n")
        print("     py -3.12 main.py --rules config/rules_execute.json")
        print("     py -3.12 main.py --panel                (doar BPM + culori)\n")
        return 0

    print(f"  MAI AI DE FACUT {len(_todo)} LUCRURI, in ordinea asta:\n")
    for i, item in enumerate(_todo, 1):
        print(f"   {i}. {item}")
    print("\n  Dupa fiecare pas, ruleaza din nou:  py -3.12 tools/doctor.py")
    print("-" * 72 + "\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
