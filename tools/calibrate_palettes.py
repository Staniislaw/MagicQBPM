"""
tools/calibrate_palettes.py
===========================
Calibrarea grilelor de palete din MagicQ, ca aplicatia sa poata apasa
automat butoanele de GROUP / COLOUR / BEAM / POSITION.

    py -3.12 tools/calibrate_palettes.py
    py -3.12 tools/calibrate_palettes.py colour beam     (doar unele)
    py -3.12 tools/calibrate_palettes.py --show          (arata calibrarea)
    py -3.12 tools/calibrate_palettes.py --test colour 7 (verifica o casuta)
    py -3.12 tools/calibrate_palettes.py --test-exec      (plimba cursorul pe
                                                           toate butoanele Execute)
    py -3.12 tools/calibrate_palettes.py --test-exec strobe flash_par

Nu trebuie sa introduci 100 de coordonate: casutele MagicQ sunt perfect
uniforme, deci se retin doar centrul PRIMEI casute si al ULTIMEI, plus
numarul de coloane si randuri. Restul se interpoleaza.

Coordonatele se salveaza RELATIV la coltul ferestrei MagicQ, deci raman
valide daca muti fereastra. NU raman valide daca schimbi layout-ul
ferestrelor sau redimensionezi MagicQ - atunci recalibrezi.

Mod de lucru pentru fiecare fereastra:
   1. pui mouse-ul pe centrul primei casute (G1 / C1 / B1 / P1)
   2. apesi SPACE
   3. pui mouse-ul pe centrul ULTIMEI casute (dreapta-jos, ex. C25)
   4. apesi SPACE
   (ESC sari peste fereastra curenta)
"""

from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

VK_SPACE = 0x20
VK_ESCAPE = 0x1B

#: ferestrele calibrabile: nume -> (eticheta, cols, rows impliciti)
WINDOWS = {
    "exec": ("EXECUTE (grila mare cu toate butoanele)", 11, 7),
    "group": ("GROUP  (G1, G2, ...)", 5, 5),
    "colour": ("COLOUR (C1, C2, ...)", 5, 5),
    "beam": ("BEAM   (B1, B2, ...)", 5, 5),
    "position": ("POSITION (P1, P2, ...)", 5, 5),
}


def _key_down(vk: int) -> bool:
    return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)


def wait_for_key() -> str:
    """Asteapta SPACE sau ESC, oriunde ar fi focusul. Returneaza 'space'/'esc'."""
    while _key_down(VK_SPACE) or _key_down(VK_ESCAPE):
        time.sleep(0.05)                      # asteptam eliberarea tastei
    while True:
        if _key_down(VK_SPACE):
            while _key_down(VK_SPACE):
                time.sleep(0.02)
            return "space"
        if _key_down(VK_ESCAPE):
            while _key_down(VK_ESCAPE):
                time.sleep(0.02)
            return "esc"
        time.sleep(0.02)


def main() -> int:
    import logging
    logging.basicConfig(level=logging.WARNING, format="%(levelname)-7s %(message)s")

    from core.config import load_settings
    from magicq.keyboard import (client_area, describe_magicq_windows,
                                 enable_dpi_awareness, find_magicq_window)
    from magicq.mouse import MouseTransport, cursor_pos

    mode = enable_dpi_awareness()

    cfg = load_settings()
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]

    process = cfg.get("magicq.mouse.window_process", "mqqt.exe")
    regex = cfg.get("magicq.mouse.window_title_regex", "^MagicQ")
    hwnd = find_magicq_window(process, regex)

    grids = dict(cfg.get("magicq.mouse.grids", {}) or {})

    # ---------------- --show ----------------
    if "--show" in flags:
        print("\n  Ferestre MagicQ:")
        for line in describe_magicq_windows(process):
            print("   ", line)
        area_now = client_area(hwnd) if hwnd else None
        calib = cfg.get("magicq.mouse.calibrated_size")
        if area_now:
            print(f"\n  Zona client acum: {area_now[2]}x{area_now[3]}")
        print(f"  La calibrare    : {calib[0]}x{calib[1]}" if calib
              else "  La calibrare    : necunoscut (calibrare veche)")
        if area_now and calib and tuple(calib) != (area_now[2], area_now[3]):
            print("  !!! MARIMEA DIFERA - coordonatele nu mai sunt exacte.")

        print("\n  Calibrare curenta:")
        if not grids:
            print("    (niciuna)")
        invalid = False
        for name, g in grids.items():
            first, last = g.get("first", [0, 0]), g.get("last", [0, 0])
            bad = min(first[0], first[1], last[0], last[1]) < 0
            invalid = invalid or bad
            flag = "   <== INVALID (coordonate negative)" if bad else ""
            print(f"    {name:<9} prima {first}  ultima {last}  "
                  f"{g.get('cols')}x{g.get('rows')}{flag}")
        if invalid:
            print("\n  Coordonatele negative inseamna ca la calibrare s-a folosit")
            print("  fereastra GRESITA ca referinta (probabil 'MagicQ Visualiser').")
            print("  Bug reparat - dar trebuie sa RECALIBREZI:")
            print("      py -3.12 tools/calibrate_palettes.py")
        targets = cfg.get("magicq.mouse.targets", {}) or {}
        if targets:
            print("\n  Tinte simple:")
            for name, xy in targets.items():
                print(f"    {name:<12} {xy}")
        return 0

    if not hwnd:
        print("\n  MagicQ nu ruleaza (proces mqqt.exe). Porneste-l si reia.")
        return 1

    # ---------------- --test-exec ----------------
    if "--test-exec" in flags:
        mouse = MouseTransport(cfg, None)
        mouse.enabled = True
        mouse.connect()
        from magicq.mouse import move_cursor
        names = args or [k for k in mouse.exec_buttons if not k.startswith("_")]
        old = cursor_pos()
        print(f"\n  Verific {len(names)} butoane Execute. Cursorul se plimba,")
        print("  FARA sa dea click. Urmareste in MagicQ daca nimereste.\n")
        for n in names:
            rc = mouse.resolve_exec({"name": n})
            if rc is None:
                print(f"    {n:<20} NEDEFINIT")
                continue
            pos = mouse.grid_position_rc("exec", rc[0], rc[1])
            if pos is None:
                print(f"    {n:<20} r{rc[0]} c{rc[1]}  -> grila 'exec' necalibrata")
                continue
            origin = mouse._window_origin() or (0, 0)
            print(f"    {n:<20} r{rc[0]} c{rc[1]}  -> {pos}")
            move_cursor(origin[0] + pos[0], origin[1] + pos[1])
            time.sleep(0.8)
        move_cursor(*old)
        print("\n  Gata.")
        return 0

    # ---------------- --test ----------------
    if "--test" in flags:
        if len(args) < 2:
            print("  Foloseste: --test <fereastra> <numar>   ex: --test colour 7")
            return 1
        window, item = args[0], int(args[1])
        mouse = MouseTransport(cfg, None)
        mouse.enabled = True
        mouse.connect()
        pos = mouse.grid_position(window, item)
        if pos is None:
            print(f"  Grila '{window}' nu e calibrata.")
            return 1
        print(f"\n  {window.upper()}{item} ar fi la {pos} (relativ la fereastra).")
        print("  Mut cursorul acolo timp de 3 secunde, FARA sa dau click.")
        print("  Verifica daca e pe casuta corecta.")
        origin = mouse._window_origin() or (0, 0)
        from magicq.mouse import move_cursor
        old = cursor_pos()
        move_cursor(origin[0] + pos[0], origin[1] + pos[1])
        time.sleep(3.0)
        move_cursor(*old)
        print("  Gata.")
        return 0

    # ---------------- calibrare ----------------
    area = client_area(hwnd)
    if area is None:
        print("  Nu pot citi pozitia ferestrei MagicQ.")
        return 1
    rect_origin = (area[0], area[1])
    client_w, client_h = area[2], area[3]

    print("\n  Ferestre MagicQ gasite:")
    for line in describe_magicq_windows(process):
        print("   ", line)
    print(f"\n  DPI awareness: {mode}")

    wanted = [w for w in args if w in WINDOWS] or list(WINDOWS)

    print("\n" + "=" * 68)
    print("  CALIBRARE PALETE MagicQ")
    print("=" * 68)
    print(f"  Zona client MagicQ: origine {rect_origin}, marime {client_w}x{client_h}")
    print("  NU redimensiona fereastra MagicQ dupa calibrare (muta-o cat vrei).")
    print("  Adu MagicQ in fata, cu paginile de palete vizibile.")
    print("  Pentru fiecare fereastra: mouse pe casuta ceruta + SPACE.")
    print("  ESC = sari peste fereastra curenta.\n")
    print("  ATENTIE: nu da CLICK, doar tine mouse-ul deasupra si apasa SPACE.")
    input("  ENTER ca sa incepem... ")

    for name in wanted:
        label, def_cols, def_rows = WINDOWS[name]
        print(f"\n  --- {label} ---")
        try:
            cols = int(input(f"      cate COLOANE are grila? [{def_cols}] ") or def_cols)
            rows = int(input(f"      cate RANDURI vizibile?  [{def_rows}] ") or def_rows)
        except ValueError:
            cols, rows = def_cols, def_rows

        if name == "exec":
            first_label = "butonul din STANGA-SUS (randul 1, coloana 1)"
            last_label = f"butonul din DREAPTA-JOS (randul {rows}, coloana {cols})"
        else:
            initial = {"group": "G", "colour": "C", "beam": "B", "position": "P"}[name]
            first_label = initial + "1"
            last_label = initial + str(cols * rows)

        print(f"      1) mouse pe CENTRUL casutei {first_label}, apoi SPACE")
        if wait_for_key() == "esc":
            print("      (sarit)")
            continue
        first = _relative(cursor_pos(), rect_origin)
        print(f"         {first_label} = {first}")

        print(f"      2) mouse pe CENTRUL casutei {last_label} "
              f"(ultima, dreapta-jos), apoi SPACE")
        if wait_for_key() == "esc":
            print("      (sarit)")
            continue
        last = _relative(cursor_pos(), rect_origin)
        print(f"         {last_label} = {last}")

        if last[0] <= first[0] or last[1] <= first[1]:
            print("      ATENTIE: ultima casuta nu e la dreapta-jos fata de prima.")
            print("      Grila NU a fost salvata. Reia fereastra asta.")
            continue

        grids[name] = {"first": list(first), "last": list(last),
                       "cols": cols, "rows": rows}
        dx = (last[0] - first[0]) / max(cols - 1, 1)
        dy = (last[1] - first[1]) / max(rows - 1, 1)
        print(f"      OK: pas {dx:.1f} x {dy:.1f} px")

    # ---------------- butonul CLEAR (optional) ----------------
    targets = dict(cfg.get("magicq.mouse.targets", {}) or {})
    print("\n  --- Butonul CLEAR (optional, dar recomandat) ---")
    print("      Paletele scriu in PROGRAMATOR, iar programatorul are")
    print("      prioritate peste playback-uri pana la CLEAR.")
    print("      Mouse pe butonul CLEAR din MagicQ + SPACE, sau ESC ca sa sari.")
    if wait_for_key() == "space":
        targets["clear"] = list(_relative(cursor_pos(), rect_origin))
        print(f"      CLEAR = {targets['clear']}")

    # ---------------- salvare ----------------
    cfg.set("magicq.mouse.grids", grids)
    cfg.set("magicq.mouse.targets", targets)
    cfg.set("magicq.mouse.calibrated_size", [client_w, client_h])
    cfg.set("magicq.mouse.enabled", True)
    priority = list(cfg.get("magicq.priority", []) or [])
    if "mouse" not in priority:
        priority.append("mouse")
        cfg.set("magicq.priority", priority)
    cfg.save()

    print("\n" + "-" * 68)
    print(f"  Salvat in {cfg.path}")
    print(f"  Grile calibrate: {', '.join(sorted(grids)) or 'niciuna'}")
    print(f"  Marime fereastra la calibrare: {client_w}x{client_h}")
    print("  Transportul de mouse a fost ACTIVAT.")
    print("\n  Verifica o casuta (doar muta cursorul, fara click):")
    print("     py -3.12 tools/calibrate_palettes.py --test beam 4")
    return 0


def _window_origin(hwnd: int) -> tuple[int, int] | None:
    from magicq.mouse import RECT
    rect = RECT()
    if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return int(rect.left), int(rect.top)


def _relative(point: tuple[int, int], origin: tuple[int, int]) -> tuple[int, int]:
    return point[0] - origin[0], point[1] - origin[1]


if __name__ == "__main__":
    sys.exit(main())
