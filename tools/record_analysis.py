"""
tools/record_analysis.py
========================
Inregistreaza ce AUDE aplicatia, ca sa se poata diagnostica ce nu merge.

    py -3.12 tools/record_analysis.py 120        (2 minute)
    py -3.12 tools/record_analysis.py 180        (3 minute)

Da drumul la o piesa reprezentativa si lasa-l sa ruleze. NU trimite nimic
catre MagicQ - doar asculta si scrie.

Rezultat:
    logs/analysis.csv   o linie la fiecare 100 ms cu toate valorile
    plus un rezumat in consola: BPM, stabilitate, sectiuni, drop-uri cu
    scorurile lor.

Cu fisierul asta se poate spune exact:
   * daca BPM-ul e corect si stabil
   * daca drop-urile sunt detectate si cu ce scor (deci ce prag trebuie pus)
   * daca sectiunile se schimba prea des sau prea rar
"""

from __future__ import annotations

import csv
import sys
import time
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from core.config import ROOT  # noqa: E402


def main() -> int:
    import logging
    logging.basicConfig(level=logging.WARNING, format="%(levelname)-7s %(message)s")

    import numpy as np

    from audio.beat import BeatTracker
    from audio.bpm import TempoEstimator
    from audio.capture import AudioCapture
    from audio.onset import OnsetDetector
    from audio.spectrum import SpectrumAnalyzer
    from audio.structure import StructureDetector
    from core.config import load_settings

    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 120.0
    cfg = load_settings()
    silence_db = float(cfg.get("analysis.silence_db", -55.0))

    out_path = ROOT / "logs" / "analysis.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    capture = AudioCapture(cfg)
    capture.start()
    spectrum = SpectrumAnalyzer(cfg)
    onset = OnsetDetector(cfg)
    tempo = TempoEstimator(cfg)
    beat = BeatTracker(cfg)
    structure = StructureDetector(cfg)

    print(f"\n  Inregistrez {duration:.0f} s. Da drumul la muzica ACUM.")
    print(f"  Backend: {capture.loopback_backend}")
    print("  (CTRL+C ca sa opresc mai devreme)\n")

    sections: list[tuple[float, str, float, float]] = []
    bpm_track: list[float] = []
    beats = 0
    onsets = 0
    rows: list[dict] = []
    t0 = time.monotonic()
    last_print = 0.0
    last_row = 0.0

    try:
        while time.monotonic() - t0 < duration:
            chunk = capture.read()
            if chunk is None:
                time.sleep(0.002)
                continue
            now = time.monotonic()
            t = now - t0
            sf = spectrum.process(chunk)
            silent = sf.rms_db < silence_db
            novelty = 0.0 if silent else sf.flux_norm
            hit, strength = onset.process(novelty, now)
            hit = hit and not silent
            onsets += int(hit)
            tempo.push(novelty)
            tempo.update(now)
            beat.set_tempo(tempo.bpm, tempo.confidence)
            binfo = beat.process(now, hit, strength, float(sf.bands[1]))
            beats += int(binfo.beat)
            st = structure.process(now, sf.loudness, sf.rms_db, sf.bands,
                                   sf.flux_norm, sf.centroid_norm, onset.onset_rate(now))
            if tempo.bpm > 0:
                bpm_track.append(tempo.bpm)
            if st.changed:
                sections.append((t, st.section, st.drop_score, st.buildup_score))
                marker = "  <<<" if st.section in ("DROP", "BUILDUP") else ""
                print(f"   {t:6.1f}s  {st.section:<8} drop={st.drop_score:.3f} "
                      f"build={st.buildup_score:.3f} energie={st.energy_short*100:3.0f}%{marker}")

            if t - last_row >= 0.1:
                last_row = t
                rows.append({
                    "t": round(t, 2), "bpm": round(tempo.bpm, 2),
                    "bpm_conf": round(tempo.confidence, 3),
                    "section": st.section,
                    "drop_score": round(st.drop_score, 4),
                    "buildup_score": round(st.buildup_score, 4),
                    "energy": round(st.energy_short * 100, 1),
                    "energy_mid": round(st.energy_mid * 100, 1),
                    "rms_db": round(sf.rms_db, 1),
                    "sub": round(sf.bands[0] * 100, 1), "bass": round(sf.bands[1] * 100, 1),
                    "mid": round(sf.bands[3] * 100, 1), "high": round(sf.bands[4] * 100, 1),
                    "treble": round(sf.bands[5] * 100, 1),
                    "bass_avg": round(sf.bands_slow[1] * 100, 1),
                    "centroid": round(sf.centroid_hz, 0),
                    "onset_rate": round(onset.onset_rate(now), 1),
                })
            if t - last_print >= 1.0:
                last_print = t
                print(f"\r   {t:5.0f}s  BPM {tempo.bpm:6.1f} ({tempo.confidence*100:3.0f}%)  "
                      f"{st.section:<8} drop={st.drop_score:.2f}  "
                      f"bass={sf.bands[1]*100:3.0f}%  RMS {sf.rms_db:6.1f} dB   ",
                      end="", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        capture.stop()

    elapsed = time.monotonic() - t0
    if rows:
        with open(out_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    print("\n\n" + "=" * 66)
    print("  REZUMAT")
    print("=" * 66)
    arr = np.array(bpm_track) if bpm_track else np.zeros(1)
    print(f"  Durata               : {elapsed:.0f} s")
    print(f"  BPM median           : {np.median(arr):.1f}")
    print(f"  BPM min/max          : {arr.min():.1f} / {arr.max():.1f}")
    stable = float(np.mean(np.abs(arr - np.median(arr)) < 2)) * 100 if bpm_track else 0
    print(f"  Stabilitate BPM      : {stable:.0f}% din timp la +/-2 BPM de median")
    print(f"  Incredere finala     : {tempo.confidence*100:.0f}%")
    print(f"  Beat-uri emise       : {beats}  (asteptat ~{elapsed*np.median(arr)/60:.0f})")
    print(f"  Onset-uri            : {onsets}")

    drops = [s for s in sections if s[1] == "DROP"]
    builds = [s for s in sections if s[1] == "BUILDUP"]
    print(f"\n  Schimbari de sectiune: {len(sections)} "
          f"(o data la {elapsed/max(len(sections),1):.0f} s)")
    print(f"  DROP-uri             : {len(drops)}")
    for t, _, ds, _ in drops:
        print(f"      {t:6.1f}s  scor {ds:.3f}")
    print(f"  BUILD-UP-uri         : {len(builds)}")

    if drops:
        scores = [d[2] for d in drops]
        print(f"\n  => Scorurile drop-urilor tale: min {min(scores):.3f}  "
              f"max {max(scores):.3f}")
        print(f"     Pentru strobe la cele mai bune, pune in regula 6:")
        print(f'        "if": "drop_score > {np.median(scores):.2f}"')
    else:
        print("\n  => NICIUN DROP detectat. Coboara pragul in config/settings.json:")
        print("        analysis.structure.drop.score_threshold : 0.62 -> 0.50")
        print("     sau mareste sensibilitatea din interfata.")

    print(f"\n  Detalii complete: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
