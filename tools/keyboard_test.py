"""
tools/keyboard_test.py
======================
Testarea controlului MagicQ prin tastatura, pas cu pas.

    py -3.12 tools/keyboard_test.py           interactiv
    py -3.12 tools/keyboard_test.py --dry     arata ce ar trimite, fara sa trimita
    py -3.12 tools/keyboard_test.py --pb 4    testeaza playback-urile 1..4 (implicit 4)

ATENTIE: testul chiar apasa taste in MagicQ, deci va face GO / FLASH pe
playback-urile tale. Ruleaza-l cand nu esti in mijlocul unui show.

Testul separa cele doua cauze posibile de esec:

  ETAPA 1 - "ajung tastele in MagicQ?"
      Trimite cifrele 1 2 3 si te intreaba daca le vezi in linia de comanda
      MagicQ (casuta Input Display, jos in mijloc). Daca NU le vezi,
      problema este focusul ferestrei, nu maparile.

  ETAPA 2 - "e MagicQ in modul corect?"
      Trimite Q / W / E / R (GO pe playback-urile 1-4). Astea functioneaza
      DOAR daca in MagicQ ai setat:
          Setup -> View Settings -> MagicQ Keyboard Mode = "Playback shortcuts"

  ETAPA 3 - flash (toggle la 100%): tastele \\ Z X C pentru PB1-4.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DRY = "--dry" in sys.argv


def ask(question: str) -> bool:
    while True:
        answer = input(f"      {question} [d/n] ").strip().lower()
        if answer in ("d", "da", "y", "yes"):
            return True
        if answer in ("n", "nu", "no", ""):
            return False


def main() -> int:
    import logging
    logging.basicConfig(level=logging.WARNING, format="%(levelname)-7s %(message)s")

    from core.config import load_settings
    from magicq.actions import Action, ActionType
    from magicq.keyboard import KeyboardTransport, find_magicq_window

    n_pb = 4
    if "--pb" in sys.argv:
        try:
            n_pb = int(sys.argv[sys.argv.index("--pb") + 1])
        except (IndexError, ValueError):
            pass

    cfg = load_settings()
    kb = KeyboardTransport(cfg, None)

    print("\n" + "=" * 70)
    print("  TEST CONTROL MagicQ PRIN TASTATURA")
    print("=" * 70)

    hwnd = find_magicq_window(kb.process_name, kb.title_regex)
    print(f"  Proces cautat    : {kb.process_name}")
    print(f"  Fereastra MagicQ : {hwnd if hwnd else 'NEGASITA'}")
    if not hwnd:
        print("\n  MagicQ nu ruleaza. Porneste-l si reia.")
        return 1
    print(f"  Focus automat    : {'DA' if kb.focus else 'NU (risc: tastele ajung aiurea)'}")
    print(f"  Pauza intre taste: {kb.key_delay * 1000:.0f} ms | tinere {kb.hold * 1000:.0f} ms")
    if DRY:
        print("\n  MOD --dry: NU se trimite nimic, doar se afiseaza.")
    kb.connect()

    def send(sequence: str, label: str) -> None:
        print(f"    >>> {label:<38} tasta: {sequence!r}")
        if DRY:
            return
        kb.send_sequence(sequence)

    # ---------------- ETAPA 1 ----------------
    print("\n  --- ETAPA 1: ajung tastele in MagicQ? ---")
    print("  Trimit cifrele 1 2 3. Uita-te in MagicQ, in casuta de jos")
    print("  (Input Display / linia de comanda).")
    if not DRY:
        input("      ENTER ca sa incep... ")
    for digit in ("1", "2", "3"):
        send(digit, f"cifra {digit}")
        time.sleep(0.35)

    keys_arrive = True
    if not DRY:
        keys_arrive = ask("Ai vazut 1 2 3 aparand in MagicQ?")
        if not keys_arrive:
            print("\n  => Tastele NU ajung in MagicQ. Cauze posibile:")
            print("     - alta fereastra fura focusul; verifica focus_window=true")
            print("     - MagicQ ruleaza ca administrator, iar Python nu")
            print("       (ruleaza si PowerShell ca administrator)")
            print("     - antivirus / anti-cheat blocheaza SendInput")
            print("\n  Restul testului nu are sens pana nu se rezolva asta.")
            if not ask("Continui oricum?"):
                return 1
        else:
            print("      OK: tastele ajung. Apasa ESC in MagicQ ca sa golesti linia.")

    # ---------------- ETAPA 2 ----------------
    print(f"\n  --- ETAPA 2: GO pe playback-urile 1-{n_pb} ---")
    print("  Necesita: Setup -> View Settings -> MagicQ Keyboard Mode")
    print("            = 'Playback shortcuts'")
    if not DRY:
        input("      ENTER ca sa incep... ")

    go_ok: list[int] = []
    for pb in range(1, n_pb + 1):
        action = Action(ActionType.PB_GO, {"playback": pb}, "test")
        seq = kb._resolve_binding(kb.bindings.get("pb_go"), action)
        send(seq, f"GO pe Playback {pb}")
        time.sleep(0.8)
        if not DRY and ask(f"A pornit Playback {pb}?"):
            go_ok.append(pb)

    # ---------------- ETAPA 3 ----------------
    print(f"\n  --- ETAPA 3: FLASH (toggle 100%) pe playback-urile 1-{n_pb} ---")
    if not DRY:
        input("      ENTER ca sa incep... ")

    flash_ok: list[int] = []
    for pb in range(1, n_pb + 1):
        action = Action(ActionType.PB_FLASH, {"playback": pb}, "test")
        seq = kb._resolve_binding(kb.bindings.get("pb_flash"), action)
        send(seq, f"FLASH ON  Playback {pb}")
        time.sleep(0.9)
        send(seq, f"FLASH OFF Playback {pb}")
        time.sleep(0.4)
        if not DRY and ask(f"A clipit Playback {pb} (aprins ~1 s, apoi stins)?"):
            flash_ok.append(pb)

    # ---------------- ETAPA 4 ----------------
    print(f"\n  --- ETAPA 4: RELEASE pe playback-urile 1-{n_pb} ---")
    for pb in range(1, n_pb + 1):
        action = Action(ActionType.PB_RELEASE, {"playback": pb}, "test")
        seq = kb._resolve_binding(kb.bindings.get("pb_release"), action)
        send(seq, f"RELEASE Playback {pb}")
        time.sleep(0.5)

    # ---------------- raport ----------------
    if DRY:
        print("\n  (mod --dry: fara raport)")
        return 0

    print("\n" + "-" * 70)
    print(f"  GO functioneaza pe    : {go_ok or 'NICIUNUL'}")
    print(f"  FLASH functioneaza pe : {flash_ok or 'NICIUNUL'}")

    if not go_ok and keys_arrive:
        print("\n  Tastele ajung, dar GO nu face nimic")
        print("  => aproape sigur MagicQ Keyboard Mode NU este 'Playback shortcuts'.")
        print("     Setup -> View Settings -> cauta 'MagicQ Keyboard Mode'.")
    elif go_ok and len(go_ok) < n_pb:
        lipsa = [p for p in range(1, n_pb + 1) if p not in go_ok]
        print(f"\n  Playback-urile {lipsa} nu au raspuns.")
        print("  => probabil nu au nimic programat, sau sunt pe alta pagina.")
    elif go_ok:
        print("\n  Totul functioneaza. Poti rula aplicatia:")
        print("     py -3.12 main.py --rules config/rules_4pb.json "
              "--transport keyboard")
    return 0


if __name__ == "__main__":
    sys.exit(main())
