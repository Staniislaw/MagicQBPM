"""
tools/osc_discover.py
=====================
Aflarea EXACTA a formatului OSC pe care il foloseste MagicQ-ul tau.

Adresele OSC difera intre versiunile MagicQ, iar documentatia nu acopera
toate variantele. In loc sa ghicim, aflam empiric. Doua moduri:


MODUL 1 - ASCULTARE (cel mai sigur)
-----------------------------------
    py -3.12 tools/osc_discover.py listen
    py -3.12 tools/osc_discover.py listen 9000

    In MagicQ: Setup -> View Settings -> (sectiunea Network)
        OSC Mode        = Tx and Rx OSC
        OSC Tx IP       = 127.0.0.1
        OSC Tx Port     = 9000
    Apoi misca un fader de playback / apasa GO in MagicQ.

    Scriptul afiseaza exact ce adresa trimite MagicQ, de exemplu:
        /pb/1  [75]
    Formatul pe care MagicQ il TRIMITE este acelasi pe care il si
    ASCULTA -> il copiezi in config/settings.json la magicq.osc.addresses.


MODUL 2 - SCANARE (daca MagicQ nu are Tx OSC)
---------------------------------------------
    py -3.12 tools/osc_discover.py scan
    py -3.12 tools/osc_discover.py scan 8000

    Trimite pe rand mai multe variante de adresa pentru "fader Playback 1
    la 100%", cu pauza intre ele. Te uiti la faderul PB1 in MagicQ si
    notezi la ce varianta se misca.


MODUL 3 - PORTURI
-----------------
    py -3.12 tools/osc_discover.py ports

    Arata pe ce porturi UDP asculta procesul MagicQ. Daca portul tau OSC
    nu apare acolo, OSC Rx nu este pornit - degeaba schimbi adresele.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ======================================================================
def mode_listen(port: int) -> int:
    """Afiseaza tot ce trimite MagicQ pe OSC."""
    try:
        from pythonosc.osc_packet import OscPacket
    except Exception as exc:  # noqa: BLE001
        print(f"python-osc lipseste: {exc}")
        return 1

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", port))
    except OSError as exc:
        print(f"Nu pot asculta pe portul {port}: {exc}")
        print("Probabil alt program il foloseste deja. Alege alt port.")
        return 1
    sock.settimeout(1.0)

    print(f"\nAscult OSC pe 0.0.0.0:{port}  (CTRL+C pentru oprire)")
    print("In MagicQ seteaza OSC Mode = 'Tx and Rx OSC', OSC Tx IP = 127.0.0.1,")
    print(f"OSC Tx Port = {port}. Apoi misca un fader sau apasa GO.\n")
    print(f"{'sursa':<22} {'adresa':<34} argumente")
    print("-" * 78)

    seen: dict[str, int] = {}
    count = 0
    try:
        while True:
            try:
                data, sender = sock.recvfrom(8192)
            except socket.timeout:
                continue
            count += 1
            src = f"{sender[0]}:{sender[1]}"
            try:
                packet = OscPacket(data)
                for timed in packet.messages:
                    msg = timed.message
                    seen[msg.address] = seen.get(msg.address, 0) + 1
                    print(f"{src:<22} {msg.address:<34} {list(msg.params)}")
            except Exception:  # noqa: BLE001 - nu e OSC valid
                print(f"{src:<22} {'(pachet non-OSC)':<34} {data[:48]!r}")
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()

    print("\n" + "-" * 78)
    print(f"Pachete primite: {count}")
    if seen:
        print("\nAdrese distincte vazute (astea le intelege MagicQ-ul tau):")
        for address, n in sorted(seen.items(), key=lambda kv: -kv[1]):
            print(f"   {address:<40} x{n}")
        print("\nCopiaza-le in config/settings.json la magicq.osc.addresses,")
        print("inlocuind numarul de playback cu {playback}. Exemplu:")
        print('   "pb_level": "/pb/{playback}"')
    else:
        print("\nNU s-a primit nimic. Verifica in MagicQ:")
        print("   - OSC Mode include 'Tx' (Tx OSC sau Tx and Rx OSC)")
        print("   - OSC Tx IP = 127.0.0.1  si  OSC Tx Port =", port)
        print("   - ai miscat efectiv un fader / ai apasat un buton")
    return 0


# ======================================================================
#  Variante de adresa incercate la scanare
# ======================================================================
FADER_VARIANTS: list[tuple[str, object]] = [
    ("/pb/1", 100),
    ("/pb/1", 1.0),
    ("/pb/1/level", 100),
    ("/pb/1/level", 1.0),
    ("/pb/1/fader", 100),
    ("/playback/1", 100),
    ("/pb/1/1", 100),
    ("/magicq/pb/1", 100),
]

GO_VARIANTS: list[tuple[str, object]] = [
    ("/pb/1/go", 1),
    ("/pb/1/go", 1.0),
    ("/pb/1/go", None),
    ("/pb/1/GO", 1),
    ("/playback/1/go", 1),
    ("/pb/1/activate", 1),
]


def mode_scan(port: int, host: str = "127.0.0.1") -> int:
    """Trimite variante de adresa, una cate una."""
    try:
        from pythonosc.udp_client import SimpleUDPClient
    except Exception as exc:  # noqa: BLE001
        print(f"python-osc lipseste: {exc}")
        return 1

    client = SimpleUDPClient(host, port)
    print(f"\nScanez variante de adresa catre {host}:{port}")
    print("UITA-TE LA MagicQ. Noteaza la ce varianta reactioneaza.\n")

    print("--- A. Fader Playback 1 la 100% ---")
    for index, (address, value) in enumerate(FADER_VARIANTS, 1):
        print(f"  {index}. {address:<20} {value!r}")
        client.send_message(address, value)
        time.sleep(2.0)
    # readucem faderul la 0 cu toate variantele, ca sa nu ramana aprins
    for address, value in FADER_VARIANTS:
        client.send_message(address, 0 if isinstance(value, int) else 0.0)
        time.sleep(0.05)

    print("\n--- B. GO pe Playback 1 ---")
    for index, (address, value) in enumerate(GO_VARIANTS, 1):
        print(f"  {index}. {address:<20} {value!r}")
        if value is None:
            client.send_message(address, [])
        else:
            client.send_message(address, value)
        time.sleep(2.0)

    print("\n" + "-" * 66)
    print("Daca a reactionat varianta A3 (/pb/1/level cu 100), atunci in")
    print("config/settings.json pui:")
    print('    "pb_level": "/pb/{playback}/level"')
    print("Daca nu a reactionat NICIUNA, OSC Rx nu este pornit in MagicQ")
    print("sau asculta pe alt port. Ruleaza:")
    print("    py -3.12 tools/osc_discover.py ports")
    return 0


# ======================================================================
def mode_ports() -> int:
    """Ce porturi UDP are deschise MagicQ."""
    print("\n--- Porturi UDP deschise de MagicQ ---")
    pid = None
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq mqqt.exe", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=10).stdout
        for line in out.splitlines():
            parts = [p.strip('"') for p in line.split('","')]
            if len(parts) >= 2 and parts[0].lower().startswith("mqqt"):
                pid = parts[1].strip('"')
                break
    except Exception as exc:  # noqa: BLE001
        print(f"  nu pot citi lista de procese: {exc}")

    if not pid:
        print("  MagicQ (mqqt.exe) nu ruleaza.")
        return 1
    print(f"  MagicQ ruleaza, PID {pid}")

    try:
        netstat = subprocess.run(["netstat", "-ano", "-p", "UDP"],
                                 capture_output=True, text=True, timeout=15).stdout
    except Exception as exc:  # noqa: BLE001
        print(f"  netstat a esuat: {exc}")
        return 1

    ports: list[int] = []
    for line in netstat.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[-1] == pid:
            local = parts[1]
            print(f"    {local}")
            try:
                ports.append(int(local.rsplit(":", 1)[1]))
            except (ValueError, IndexError):
                pass

    known = {6454: "Art-Net", 6553: "retea ChamSys / MagicQ remote",
             5568: "sACN", 21567: "MagicQ Wing"}
    print("\n  Interpretare:")
    for p in sorted(set(ports)):
        print(f"    {p:<6} {known.get(p, 'necunoscut - POATE fi portul OSC')}")

    osc_candidates = [p for p in ports if p not in known]
    if osc_candidates:
        print(f"\n  Incearca portul OSC: {osc_candidates[0]}")
        print(f"  In config/settings.json: magicq.osc.port = {osc_candidates[0]}")
    else:
        print("\n  NU exista niciun port care sa arate a OSC.")
        print("  => OSC Rx NU este pornit in MagicQ. Porneste-l intai:")
        print("     Setup -> View Settings -> sectiunea Network ->")
        print("     'OSC Mode' = Rx OSC   si   'OSC Rx Port' = 8000")
        print("     Apoi salveaza setarile si RULEAZA DIN NOU acest test.")
    return 0


# ======================================================================
def main() -> int:
    args = sys.argv[1:]
    mode = args[0].lower() if args else "ports"
    port = int(args[1]) if len(args) > 1 else (9000 if mode == "listen" else 8000)

    if mode == "listen":
        return mode_listen(port)
    if mode == "scan":
        return mode_scan(port)
    if mode == "ports":
        return mode_ports()
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
