"""
tools/magicq_test.py
====================
Verificarea conexiunii cu MagicQ, pas cu pas.

    py -3.12 tools/magicq_test.py             interactiv (recomandat prima data)
    py -3.12 tools/magicq_test.py --auto      trimite tot, fara intrebari
    py -3.12 tools/magicq_test.py --keyboard  testeaza si tastatura

Ce face: trimite pe rand cate o comanda si te intreaba daca ai vazut ceva in
MagicQ. La final iti spune exact ce merge si ce trebuie schimbat in
config/settings.json.

INAINTE DE RULARE:
  1. Porneste MagicQ PC.
  2. Setup -> View Settings -> Network:
         OSC Mode     = Rx OSC   (sau Tx and Rx OSC)
         OSC Rx Port  = 8000
  3. Ai nevoie de PLAYBACK-URI PROGRAMATE (macar PB1 si PB2 cu ceva vizibil).
     Aplicatia nu creeaza lumini - doar apasa butoanele tale.
  4. Lasa MagicQ vizibil pe ecran ca sa vezi faderele/butoanele reactionand.
"""

from __future__ import annotations

import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

AUTO = "--auto" in sys.argv
TEST_KEYBOARD = "--keyboard" in sys.argv


def ask(question: str) -> bool:
    """Intrebare da/nu. In modul --auto raspunde mereu 'nu stiu' (None -> False)."""
    if AUTO:
        time.sleep(1.2)
        return False
    while True:
        answer = input(f"    {question} [d/n/s=sari peste] ").strip().lower()
        if answer in ("d", "da", "y", "yes"):
            return True
        if answer in ("n", "nu", "no"):
            return False
        if answer in ("s", "skip", ""):
            return False


def main() -> int:
    import logging

    logging.basicConfig(level=logging.WARNING, format="%(levelname)-7s %(message)s")

    from core.bus import EventBus
    from core.config import load_settings
    from magicq.actions import Action, ActionType
    from magicq.keyboard import find_magicq_window
    from magicq.router import MagicQRouter

    cfg = load_settings()
    host = cfg.get("magicq.osc.host")
    port = int(cfg.get("magicq.osc.port"))

    print("\n" + "=" * 68)
    print("  TEST CONEXIUNE MagicQ")
    print("=" * 68)
    print(f"  OSC tinta        : {host}:{port}")
    print(f"  Prioritate       : {' > '.join(cfg.get('magicq.priority'))}")

    # ---------- 1. fereastra MagicQ ----------
    process = cfg.get("magicq.keyboard.window_process", "mqqt.exe")
    regex = cfg.get("magicq.keyboard.window_title_regex", "^MagicQ")
    hwnd = find_magicq_window(process, regex)
    print(f"  Proces cautat    : {process}")
    print(f"  Fereastra MagicQ : "
          f"{'GASITA (handle %s)' % hwnd if hwnd else 'NEGASITA - MagicQ nu ruleaza'}")
    if not hwnd:
        print("\n  MagicQ nu pare sa ruleze. Porneste-l si reia testul.")
        print("  (OSC merge si fara fereastra vizibila, dar tastatura nu.)")
        if not AUTO and not ask("Continui oricum?"):
            return 1

    # ---------- 2. portul OSC ----------
    # Verificare esentiala: OSC este UDP, deci trimiterea "reuseste" mereu,
    # chiar daca nimeni nu asculta. Fara acest test, raportul ar arata
    # "0 esuate" cand de fapt niciun pachet nu ajunge nicaieri.
    print("\n  --- Verificare port OSC ---")
    port_liber = False
    if host in ("127.0.0.1", "localhost"):
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            probe.bind(("127.0.0.1", port))
            probe.close()
            port_liber = True
        except OSError:
            print(f"  OK: portul {port} este ocupat - MagicQ pare sa asculte acolo.")
    else:
        print(f"  MagicQ e pe alt PC ({host}) - portul local nu spune nimic.")

    if port_liber:
        print(f"\n  >>> PROBLEMA GASITA: nimic nu asculta pe UDP {port}.")
        print("      Pachetele pleaca, dar nu le primeste nimeni. Orice test de")
        print("      mai jos ar arata fals 'trimis cu succes'.\n")
        print("      In MagicQ:  Setup -> View Settings -> sectiunea Network")
        print(f"                  OSC Mode    = Rx OSC  (sau Tx and Rx OSC)")
        print(f"                  OSC Rx Port = {port}")
        print("                  apoi salveaza setarile.\n")
        print("      Verifica pe ce porturi asculta MagicQ acum:")
        print("          py -3.12 tools/osc_discover.py ports\n")
        if not AUTO and not ask("Continui testul oricum (util doar pentru tastatura)?"):
            return 1

    # ---------- 3. conectare ----------
    bus = EventBus()
    router = MagicQRouter(cfg, bus)
    results = router.connect()
    print("\n  --- Transporturi ---")
    for name, ok in results.items():
        status = "CONECTAT" if ok else "indisponibil"
        detail = router.transports[name].status.detail
        print(f"    {name:9s} {status:13s} {detail}")
    router.start()

    # ---------- 4. comenzile de test ----------
    tests: list[tuple[str, Action, str]] = [
        ("Fader Playback 1 la 100%",
         Action(ActionType.PB_LEVEL, {"playback": 1, "level": 100}, "test"),
         "faderul PB1 a urcat la maxim?"),
        ("Fader Playback 1 la 0%",
         Action(ActionType.PB_LEVEL, {"playback": 1, "level": 0}, "test"),
         "faderul PB1 a coborat?"),
        ("GO pe Playback 1",
         Action(ActionType.PB_GO, {"playback": 1}, "test"),
         "cue stack-ul de pe PB1 a pornit?"),
        ("FLASH pe Playback 2 (0.8 s, cu auto-release)",
         Action(ActionType.PB_FLASH, {"playback": 2, "duration": 0.8}, "test"),
         "PB2 a clipit si s-a stins singur?"),
        ("RELEASE pe Playback 1",
         Action(ActionType.PB_RELEASE, {"playback": 1}, "test"),
         "PB1 a fost eliberat?"),
        ("Execute page 1 / item 1",
         Action(ActionType.EXEC, {"page": 1, "item": 1}, "test"),
         "butonul 1 din Execute Window a fost apasat?"),
        ("Comanda RPC 'RELEASE ALL'",
         Action(ActionType.RPC, {"command": "RELEASE ALL"}, "test"),
         "s-au eliberat playback-urile?"),
    ]
    if TEST_KEYBOARD:
        tests.append((
            "TASTATURA: GO pe Playback 1 (fortat prin taste)",
            Action(ActionType.PB_GO, {"playback": 1}, "test", transport="keyboard"),
            "a reactionat MagicQ la combinatia de taste?"))

    print("\n  --- Comenzi de test ---")
    print("  Uita-te in MagicQ dupa fiecare comanda.\n")
    working: list[str] = []
    broken: list[str] = []

    for label, action, question in tests:
        print(f"  >>> {label}")
        router.send(action)
        time.sleep(1.4)          # lasam timp pentru auto-release-ul flash-ului
        if ask(question):
            working.append(label)
        else:
            broken.append(label)

    time.sleep(0.5)
    router.panic()
    time.sleep(0.3)
    router.stop()

    # ---------- 5. raport ----------
    print("\n" + "-" * 68)
    print(f"  Comenzi trimise : {router.sent}")
    print(f"  Esuate          : {router.failed}")
    if not AUTO:
        print(f"\n  AU MERS ({len(working)}):")
        for item in working:
            print(f"    + {item}")
        if broken:
            print(f"\n  NU AU MERS ({len(broken)}):")
            for item in broken:
                print(f"    - {item}")
            print("\n  Ce verifici pentru cele care nu au mers:")
            print("   1. In MagicQ: Setup -> View Settings -> Network -> OSC Mode = Rx OSC")
            print(f"      si OSC Rx Port = {port}")
            print("   2. Playback-ul respectiv chiar are ceva programat?")
            print("   3. Adresa OSC difera la versiunea ta: modifica sablonul in")
            print("      config/settings.json -> magicq.osc.addresses")
            print("      (sterge o adresa care nu exista -> actiunea trece automat")
            print("       pe MIDI sau pe tastatura)")
            print("   4. Firewall Windows: permite trafic UDP local pentru Python.")
    print("-" * 68 + "\n")
    return 0 if router.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
