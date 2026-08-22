"""
main.py
=======
Punctul de intrare al aplicatiei.

    py -3.12 main.py                 porneste normal (interfata grafica)
    py -3.12 main.py --list-devices  listeaza device-urile audio
    py -3.12 main.py --headless      fara interfata (doar consola)
    py -3.12 main.py --simulate 128  fara microfon/boxe: click-track intern
    py -3.12 main.py --no-magicq     analizeaza, dar nu trimite comenzi
    py -3.12 main.py --selftest      verifica lantul DSP si iese
    py -3.12 main.py --doctor        verifica instalarea pe PC-ul asta
    py -3.12 main.py --calibrate     calibreaza grila Execute
    py -3.12 main.py --test-exec     verifica butoanele, fara click

Firele de executie pornite (toate independente):

    [PortAudio callback]  captura -> ring buffer          (prioritate maxima)
    [AnalysisEngine]      DSP -> SharedState + EventBus
    [RuleEngine]          evenimente/expresii -> actiuni
    [MagicQRouter]        actiuni -> OSC / MIDI / taste / mouse
    [Qt main thread]      interfata, 60 FPS
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from pathlib import Path

# permite rularea din orice director
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.bus import EventBus, EventType          # noqa: E402
from core.config import ROOT as ROOT_APP           # noqa: E402
from core.config import (RULES_PATH, SETTINGS_PATH, load_rules_file,  # noqa: E402
                         load_settings, setup_logging)
from core.rules import load_rules                  # noqa: E402
from core.state import SharedState                 # noqa: E402

log = logging.getLogger("main")


# ======================================================================
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Controller MagicQ reactiv la muzica (analiza audio in timp real)")
    p.add_argument("--config", default=str(SETTINGS_PATH), help="cale catre settings.json")
    p.add_argument("--rules", default=None,
                   help="cale catre fisierul de reguli "
                        "(implicit: cel din settings.json -> rules.file)")
    p.add_argument("--list-devices", action="store_true", help="listeaza device-urile audio")
    p.add_argument("--headless", action="store_true", help="fara interfata grafica")
    p.add_argument("--panel", action="store_true",
                   help="doar panoul mic cu BPM si combinatii de culori")
    p.add_argument("--simulate", nargs="?", const=128.0, type=float, default=None,
                   metavar="BPM", help="sursa audio sintetica pentru teste")
    p.add_argument("--no-magicq", action="store_true", help="nu trimite comenzi catre MagicQ")
    p.add_argument("--transport", choices=["osc", "midi", "keyboard", "mouse"], default=None,
                   help="forteaza un singur transport (ex: --transport keyboard "
                        "pentru MagicQ PC in Demo Mode)")
    p.add_argument("--selftest", action="store_true", help="test intern al lantului DSP")
    # Uneltele, ca subcomenzi: intr-un .exe nu exista tools/*.py de rulat
    p.add_argument("--doctor", action="store_true",
                   help="verifica instalarea si spune ce mai lipseste")
    p.add_argument("--calibrate", nargs="*", metavar="FEREASTRA", default=None,
                   help="calibreaza grilele de butoane (implicit: exec)")
    p.add_argument("--test-exec", nargs="*", metavar="BUTON", default=None,
                   help="plimba cursorul peste butoanele Execute, fara click")
    p.add_argument("--record", nargs="?", const=120.0, type=float, default=None,
                   metavar="SECUNDE", help="inregistreaza analiza pentru diagnostic")
    p.add_argument("--check-audio", nargs="?", const=10.0, type=float, default=None,
                   metavar="SECUNDE", help="verifica captura audio, cu muzica pornita")
    p.add_argument("--debug", action="store_true", help="log detaliat")
    return p.parse_args()


# ======================================================================
def cmd_list_devices() -> int:
    from audio.capture import available_loopback_backends, list_devices
    try:
        devices = list_devices()
    except Exception as exc:  # noqa: BLE001
        print(f"Eroare: {exc}")
        return 1

    print("\n=== DEVICE-URI AUDIO (PortAudio) ===\n")
    for dev in devices:
        print(" ", dev)

    print("\n=== BACKEND-URI PENTRU SUNETUL REDAT DE PC (loopback) ===")
    backends = available_loopback_backends()
    if backends:
        print("  disponibile, in ordinea de incercare:", ", ".join(backends))
    else:
        print("  NICIUNUL. Instaleaza:  py -3.12 -m pip install soundcard")

    try:
        import soundcard as sc
        print("\n  Boxe vazute de 'soundcard' (pentru loopback):")
        default = sc.default_speaker().name
        for spk in sc.all_speakers():
            mark = "  <- implicit" if spk.name == default else ""
            print(f"    - {spk.name}{mark}")
    except Exception as exc:  # noqa: BLE001
        print(f"  (soundcard indisponibil: {exc})")

    print("\nIn config/settings.json alegi sursa asa:")
    print('  "audio": {"sources": {"loopback": {"device": "Speakers",')
    print('                                     "backend": "auto"}}}')
    print('  (device = o parte din nume; backend = auto|soundcard|'
          'pyaudiowpatch|sounddevice)')
    return 0


# ======================================================================
def build_app(cfg, args):
    """Creeaza toate componentele si le porneste. Returneaza obiectele."""
    from audio.capture import SyntheticCapture
    from audio.engine import AnalysisEngine
    from core.rules import RuleEngine
    from magicq.router import MagicQRouter

    samplerate = int(cfg.get("audio.samplerate", 48000))
    hop = int(cfg.get("audio.hop_size", 512))
    frame_rate = samplerate / hop
    wave_decim = 4

    state = SharedState(
        spectro_bins=128,
        spectro_cols=int(float(cfg.get("ui.spectrogram_seconds", 8.0)) * frame_rate),
        wave_samples=int(float(cfg.get("ui.waveform_seconds", 3.0)) * samplerate / wave_decim),
        hist_frames=int(30 * frame_rate),
        wave_decim=wave_decim,
    )
    bus = EventBus()

    capture = SyntheticCapture(cfg, bpm=args.simulate) if args.simulate else None
    engine = AnalysisEngine(cfg, state, bus, capture=capture)

    router = MagicQRouter(cfg, bus)
    if args.no_magicq:
        router.enabled = False
        log.warning("--no-magicq: comenzile catre MagicQ sunt dezactivate.")
    else:
        router.connect()

    try:
        rules_data = load_rules_file(args.rules)
        rules = load_rules(rules_data, cfg.section("rules"))
    except Exception as exc:  # noqa: BLE001
        log.error("Nu am putut incarca regulile (%s). Se porneste fara reguli.", exc)
        rules = []
    log.info("Reguli incarcate: %d", len(rules))

    rule_engine = RuleEngine(cfg, state, bus, router, rules)

    router.start()
    engine.start()

    if getattr(args, "panel", False):
        # MOD PANOU: analiza merge (pentru BPM), dar motorul de reguli NU
        # porneste deloc si router-ul e pe manual. Spre MagicQ pleaca doar
        # ce apesi tu in panou (actiunile marcate cu "force").
        router.set_manual_mode(True)
        log.warning("Mod PANOU: regulile sunt OPRITE. Catre MagicQ pleaca "
                    "doar culorile si tap-urile apasate de tine in panou.")
    else:
        rule_engine.start()
    return state, bus, engine, router, rule_engine


def install_panic_hotkey(router) -> None:
    """CTRL+ALT+P = PANIC global (necesita pachetul `keyboard`)."""
    try:
        import keyboard as kb  # type: ignore
        kb.add_hotkey("ctrl+alt+p", router.panic)
        log.info("Hotkey global de urgenta: CTRL+ALT+P (PANIC)")
    except Exception as exc:  # noqa: BLE001
        log.debug("Hotkey global indisponibil: %s", exc)


# ======================================================================
def run_gui(cfg, args) -> int:
    from PyQt6.QtWidgets import QApplication
    from ui.dashboard import Dashboard

    app = QApplication(sys.argv)
    app.setApplicationName("MagicQ Audio Reactive Controller")

    state, bus, engine, router, rule_engine = build_app(cfg, args)
    install_panic_hotkey(router)

    if args.panel:
        from ui.bpm_panel import BpmColorPanel
        window = BpmColorPanel(cfg, state, router)
        log.info("Panou BPM & Culori pornit (fara interfata mare).")
    else:
        window = Dashboard(cfg, state, bus, engine, router, rule_engine)
        log.info("Interfata pornita.")
    window.show()
    code = app.exec()

    rule_engine.stop()
    router.stop()
    engine.stop()
    time.sleep(0.3)
    return code


# ======================================================================
def run_headless(cfg, args) -> int:
    state, bus, engine, router, rule_engine = build_app(cfg, args)
    install_panic_hotkey(router)
    sub = bus.subscribe([EventType.SECTION_CHANGE, EventType.RULE_FIRED,
                         EventType.ACTION_SENT, EventType.ACTION_FAILED,
                         EventType.AUDIO_ERROR])
    stopping = False

    def handle_signal(_sig, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, handle_signal)
    print("Mod headless. CTRL+C pentru oprire.\n")

    last_print = 0.0
    try:
        while not stopping:
            for event in sub.drain(64):
                print(f"  {event}")
            now = time.monotonic()
            if now - last_print >= 1.0:
                last_print = now
                s = state.snapshot
                print(f"\rBPM {s.bpm:6.1f} | {s.section:8s} | "
                      f"bass {s.bands[1] * 100:3.0f}% mid {s.bands[3] * 100:3.0f}% "
                      f"treble {s.bands[5] * 100:3.0f}% | RMS {s.rms:.3f} | "
                      f"lat {s.latency_ms:4.1f} ms | cmd {router.sent}",
                      end="", flush=True)
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    print("\nSe opreste...")
    rule_engine.stop()
    router.panic()
    router.stop()
    engine.stop()
    time.sleep(0.3)
    return 0


# ======================================================================
def main() -> int:
    # Inainte de orice API de coordonate: procesul trebuie sa fie DPI-aware,
    # la fel ca unealta de calibrare, altfel click-urile de mouse cad langa
    # tinta pe sistemele cu scalare != 100%.
    try:
        from magicq.keyboard import enable_dpi_awareness
        dpi_mode = enable_dpi_awareness()
    except Exception:  # noqa: BLE001
        dpi_mode = "n/a"

    args = parse_args()
    cfg = load_settings(args.config)
    if args.debug:
        cfg.set("logging.level", "DEBUG")
    setup_logging(cfg)

    log.info("=" * 62)
    log.info("MagicQ Audio Reactive Controller")
    # Fara --rules se ia setul din configurare. ATENTIE: valoarea veche
    # implicita era config/rules.json - setul generic pe playback-uri, care
    # apasa PB1-PB10 chiar daca show-ul tau nu are nimic acolo.
    if not args.rules:
        args.rules = cfg.get("rules.file") or str(RULES_PATH)
    rules_path = Path(args.rules)
    if not rules_path.is_absolute():
        rules_path = ROOT_APP / rules_path
    if not rules_path.exists():
        log.error("Fisierul de reguli %s nu exista.", rules_path)
        fallback = ROOT_APP / "config" / "rules_execute.json"
        if fallback.exists():
            rules_path = fallback
            log.warning("Se foloseste %s in loc.", fallback.name)
    args.rules = str(rules_path)

    log.info("config: %s | reguli: %s", args.config, args.rules)
    log.info("DPI awareness: %s", dpi_mode)
    log.info("=" * 62)

    if args.transport:
        # un singur transport: restul sunt dezactivate explicit
        cfg.set("magicq.priority", [args.transport])
        for name in ("osc", "midi", "keyboard", "mouse"):
            cfg.set(f"magicq.{name}.enabled", name == args.transport)
        cfg.set("magicq.auto_keyboard_fallback", args.transport == "keyboard")
        log.info("Transport fortat: %s", args.transport.upper())

    if args.list_devices:
        return cmd_list_devices()

    # ---- unelte (functioneaza si din .exe) ----
    if args.doctor:
        from tools.doctor import main as doctor_main
        return doctor_main()
    if args.calibrate is not None:
        from tools import calibrate_palettes
        sys.argv = [sys.argv[0]] + (args.calibrate or ["exec"])
        return calibrate_palettes.main()
    if args.test_exec is not None:
        from tools import calibrate_palettes
        sys.argv = [sys.argv[0], "--test-exec"] + list(args.test_exec)
        return calibrate_palettes.main()
    if args.record is not None:
        from tools import record_analysis
        sys.argv = [sys.argv[0], str(args.record)]
        return record_analysis.main()
    if args.check_audio is not None:
        from tools import loopback_check
        sys.argv = [sys.argv[0], str(args.check_audio)]
        return loopback_check.main()

    if args.selftest:
        from tools.selftest import run_selftest
        return run_selftest(cfg)

    if args.headless or not cfg.get("ui.enabled", True):
        return run_headless(cfg, args)

    try:
        return run_gui(cfg, args)
    except ImportError as exc:
        log.error("PyQt6 nu este disponibil (%s). Se porneste in mod headless.", exc)
        return run_headless(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
