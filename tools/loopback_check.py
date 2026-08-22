"""
tools/loopback_check.py
=======================
Verificarea capturii audio, cu muzica ta.

    py -3.12 tools/loopback_check.py            (10 secunde, backend automat)
    py -3.12 tools/loopback_check.py 20         (20 de secunde)
    py -3.12 tools/loopback_check.py 10 soundcard

DA DRUMUL LA MUZICA inainte de a rula (Spotify / YouTube / Winamp).
Scriptul afiseaza in timp real nivelul, benzile si BPM-ul detectat, ca sa
vezi imediat daca:
  * backend-ul de loopback prinde sunetul redat de PC
  * nivelul este suficient (nu sub -50 dB)
  * BPM-ul se stabilizeaza

Daca nu vezi nimic (RMS langa -90 dB), incearca alt backend sau
seteaza device-ul corect in config/settings.json.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BAR = "#"


def main() -> int:
    import logging

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)-7s %(name)-16s %(message)s")

    from audio.bpm import TempoEstimator
    from audio.capture import AudioCapture, available_loopback_backends
    from audio.onset import OnsetDetector
    from audio.spectrum import SpectrumAnalyzer
    from core.config import load_settings

    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0
    backend = sys.argv[2] if len(sys.argv) > 2 else None

    cfg = load_settings()
    if backend:
        cfg.set("audio.sources.loopback.backend", backend)

    print("\nBackend-uri disponibile:", ", ".join(available_loopback_backends()) or "NICIUNUL")
    print("Porneste muzica acum...\n")

    capture = AudioCapture(cfg)
    capture.start()
    print(f"Backend folosit: {capture.loopback_backend or 'sounddevice'}")
    print(f"Surse active   : {[s.name for s in capture.sources]}\n")

    spectrum = SpectrumAnalyzer(cfg)
    onset = OnsetDetector(cfg)
    tempo = TempoEstimator(cfg)

    t0 = time.monotonic()
    frames = 0
    onsets = 0
    peak_rms = -120.0
    last_print = 0.0

    try:
        while time.monotonic() - t0 < duration:
            chunk = capture.read()
            if chunk is None:
                time.sleep(0.002)
                continue
            t = time.monotonic()
            sf = spectrum.process(chunk)
            frames += 1
            peak_rms = max(peak_rms, sf.rms_db)
            silent = sf.rms_db < float(cfg.get("analysis.silence_db", -55.0))
            novelty = 0.0 if silent else sf.flux_norm
            hit, _ = onset.process(novelty, t)
            onsets += int(hit and not silent)
            tempo.push(novelty)
            tempo.update(t)

            if t - last_print >= 0.2:
                last_print = t
                bars = " ".join(f"{name}{BAR * int(v * 10):<10s}"
                                for name, v in zip("SBLMHT", sf.bands))
                print(f"\r{sf.rms_db:6.1f} dB | {bars} | BPM {tempo.bpm:6.1f} "
                      f"({tempo.confidence * 100:3.0f}%) | lat {capture.latency_ms():5.1f} ms",
                      end="", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        capture.stop()

    elapsed = time.monotonic() - t0
    print("\n\n--- REZULTAT ---")
    print(f"  cadre de analiza : {frames} in {elapsed:.1f} s "
          f"({frames / max(elapsed, 1e-9):.1f}/s, asteptat ~93.8/s)")
    print(f"  nivel maxim      : {peak_rms:.1f} dBFS")
    print(f"  onset-uri        : {onsets}")
    print(f"  BPM final        : {tempo.bpm:.1f} (incredere {tempo.confidence * 100:.0f}%)")
    print(f"  overflow-uri     : {capture.total_overflows()}")

    if peak_rms < -60:
        print("\n  ATENTIE: nu s-a auzit nimic. Verifica:")
        print("    - muzica chiar ruleaza pe device-ul IMPLICIT de iesire?")
        print("    - incearca alt backend: py -3.12 tools/loopback_check.py 10 pyaudiowpatch")
        print("    - ruleaza 'py -3.12 main.py --list-devices' si seteaza device-ul in")
        print("      config/settings.json -> audio.sources.loopback.device")
        return 1
    if frames < elapsed * 80:
        print("\n  ATENTIE: se pierd cadre. Mareste audio.block_size la 512 in settings.json.")
        return 1
    print("\n  Captura functioneaza corect.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
