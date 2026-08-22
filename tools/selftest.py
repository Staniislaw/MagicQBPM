"""
tools/selftest.py
=================
Test intern al lantului DSP, fara placa de sunet si fara MagicQ.

Genereaza o piesa sintetica de ~34 s cu structura cunoscuta:

    0-8 s   INTRO    kick slab + pad
    8-16 s  BUILD-UP riser care urca in frecventa, fara bass, snare din ce
                     in ce mai dese
    16-27 s DROP     kick puternic + bass + spectru plin
    27-34 s BREAK    doar pad, fara bass

apoi o trece prin exact aceleasi module ca la rulare reala si verifica:

    * BPM-ul detectat (trebuie sa fie 128 +/- 3)
    * numarul de beat-uri emise
    * detectarea DROP-ului
    * incarcarea si evaluarea regulilor din config/rules.json
    * parsarea formelor scurte ("Flash 4", "Speed=180", ...)

Rulare:  py -3.12 main.py --selftest
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ======================================================================
def make_track(sr: int = 48000, bpm: float = 128.0) -> np.ndarray:
    """Piesa sintetica cu structura cunoscuta."""
    rng = np.random.default_rng(1234)
    duration = 34.0
    n = int(duration * sr)
    t = np.arange(n) / sr
    period = 60.0 / bpm
    beat_phase = np.mod(t, period)
    eighth_phase = np.mod(t, period / 2)

    kick = np.sin(2 * np.pi * 52 * beat_phase) * np.exp(-beat_phase * 26.0)
    sub = np.sin(2 * np.pi * 38 * beat_phase) * np.exp(-beat_phase * 12.0)
    hat = rng.standard_normal(n) * np.exp(-eighth_phase * 150.0) * 0.25
    pad = 0.10 * (np.sin(2 * np.pi * 220 * t) + np.sin(2 * np.pi * 330 * t))
    noise = rng.standard_normal(n) * 0.5

    out = np.zeros(n, dtype=np.float64)

    def seg(a: float, b: float) -> slice:
        return slice(int(a * sr), int(b * sr))

    # INTRO
    s = seg(0, 8)
    out[s] = 0.35 * kick[s] + pad[s] * 0.8 + 0.05 * hat[s]

    # BUILD-UP: riser (frecventa care urca) + densitate de snare in crestere
    s = seg(8, 16)
    local = t[s] - 8.0
    sweep_f = 300 * np.power(2.0, local / 8.0 * 4.5)        # 300 Hz -> ~6.8 kHz
    riser = 0.28 * np.sin(2 * np.pi * np.cumsum(sweep_f) / sr) * (local / 8.0)
    roll_rate = 4 + 12 * (local / 8.0)
    roll_phase = np.mod(local * roll_rate, 1.0)
    snare = noise[s] * np.exp(-roll_phase * 18.0) * 0.35 * (local / 8.0)
    out[s] = riser + snare + pad[s] * 0.5 + 0.10 * kick[s]

    # DROP: totul la maxim
    s = seg(16, 27)
    out[s] = (1.15 * kick[s] + 0.85 * sub[s] + 0.35 * hat[s] + pad[s] * 1.2
              + 0.05 * noise[s])

    # BREAK
    s = seg(27, 34)
    out[s] = pad[s] * 0.55 + 0.04 * hat[s]

    out *= 0.55
    return out.astype(np.float32)


# ======================================================================
def run_selftest(cfg=None) -> int:
    from core.config import load_rules_file, load_settings
    from core.rules import compile_expression, eval_expression, load_rules
    from core.state import Snapshot
    from magicq.shorthand import parse_shorthand, parse_shorthand_list

    cfg = cfg or load_settings()
    sr = int(cfg.get("audio.samplerate", 48000))
    hop = int(cfg.get("audio.hop_size", 512))

    from audio.beat import BeatTracker
    from audio.bpm import TempoEstimator
    from audio.onset import OnsetDetector
    from audio.spectrum import SpectrumAnalyzer
    from audio.structure import StructureDetector

    print("\n" + "=" * 66)
    print("  SELFTEST - lant DSP + reguli")
    print("=" * 66)

    audio = make_track(sr, 128.0)
    spectrum = SpectrumAnalyzer(cfg)
    onset = OnsetDetector(cfg)
    tempo = TempoEstimator(cfg)
    beat = BeatTracker(cfg)
    structure = StructureDetector(cfg)

    n_frames = len(audio) // hop
    beats = 0
    beats_locked = 0        # beat-uri dupa ce tempo-ul e stabil (t > 8 s)
    downbeats = 0
    onsets = 0
    sections: list[tuple[float, str]] = []
    bpm_track: list[float] = []
    t_proc0 = time.perf_counter()

    for i in range(n_frames):
        chunk = audio[i * hop:(i + 1) * hop]
        t = (i + 1) * hop / sr          # timp simulat

        sf = spectrum.process(chunk)
        # acelasi lant ca in audio/engine.py: flux normalizat + poarta de liniste
        silent = sf.rms_db < float(cfg.get("analysis.silence_db", -55.0))
        novelty = 0.0 if silent else sf.flux_norm
        is_onset, strength = onset.process(novelty, t)
        is_onset = is_onset and not silent
        onsets += int(is_onset)
        tempo.push(novelty)
        tempo.update(t)
        beat.set_tempo(tempo.bpm, tempo.confidence)
        binfo = beat.process(t, is_onset, strength, float(sf.bands[1]))
        beats += int(binfo.beat)
        if t > 8.0:
            beats_locked += int(binfo.beat)
        downbeats += int(binfo.downbeat)
        st = structure.process(t, sf.loudness, sf.rms_db, sf.bands,
                               sf.flux_norm, sf.centroid_norm, onset.onset_rate(t))
        if st.changed:
            sections.append((t, st.section))
        if t > 6.0:
            bpm_track.append(tempo.bpm)

    proc_time = time.perf_counter() - t_proc0
    audio_time = len(audio) / sr
    realtime_factor = audio_time / max(proc_time, 1e-9)

    median_bpm = float(np.median([b for b in bpm_track if b > 0])) if bpm_track else 0.0
    # tempo-ul are nevoie de ~4-6 s de istoric, deci numaram doar dupa 8 s
    expected_beats = (audio_time - 8.0) * 128.0 / 60.0
    detected_sections = [s for _, s in sections]

    print(f"\n  Audio analizat        : {audio_time:.1f} s in {proc_time:.2f} s "
          f"({realtime_factor:.0f}x realtime)")
    print(f"  Timp mediu / cadru    : {proc_time / n_frames * 1000:.3f} ms "
          f"(buget {hop / sr * 1000:.1f} ms)")
    print(f"  BPM detectat (median) : {median_bpm:.2f}   (asteptat 128.00)")
    print(f"  Incredere BPM         : {tempo.confidence * 100:.0f}%")
    print(f"  Onset-uri             : {onsets}")
    print(f"  Beat-uri emise        : {beats} (dupa lock: {beats_locked}, "
          f"asteptat ~{expected_beats:.0f})")
    print(f"  Downbeat-uri          : {downbeats}")
    print("  Sectiuni detectate    :")
    for t, name in sections:
        print(f"      {t:6.1f} s  ->  {name}")

    checks: list[tuple[str, bool, str]] = []
    checks.append(("BPM in 128 +/- 3", abs(median_bpm - 128.0) <= 3.0,
                   f"{median_bpm:.2f}"))
    checks.append(("Beat-uri detectate (+/-12%)",
                   abs(beats_locked - expected_beats) <= 0.12 * expected_beats + 2,
                   f"{beats_locked} vs {expected_beats:.0f}"))
    checks.append(("BREAK detectat", "BREAK" in detected_sections,
                   ", ".join(detected_sections) or "-"))
    checks.append(("DROP detectat", "DROP" in detected_sections,
                   ", ".join(detected_sections) or "-"))
    checks.append(("BUILDUP detectat", "BUILDUP" in detected_sections,
                   ", ".join(detected_sections) or "-"))
    checks.append(("Analiza mai rapida decat realtime", realtime_factor > 5.0,
                   f"{realtime_factor:.0f}x"))

    # ---------------- reguli ----------------
    print("\n  --- Reguli ---")
    try:
        rules = load_rules(load_rules_file(), cfg.section("rules"))
        print(f"  Reguli incarcate      : {len(rules)}")
        for rule in rules[:12]:
            print(f"      [{'x' if rule.enabled else ' '}] {rule.name:26s} "
                  f"{rule.condition_text():34s} -> {rule.actions_text()}")
        checks.append(("rules.json se incarca", len(rules) > 0, f"{len(rules)} reguli"))
    except Exception as exc:  # noqa: BLE001
        print(f"  EROARE la incarcarea regulilor: {exc}")
        checks.append(("rules.json se incarca", False, str(exc)))

    # ---------------- expresii ----------------
    snap = Snapshot(bpm=150.0, bands=(0.9, 0.85, 0.4, 0.5, 0.75, 0.3),
                    rms=0.95, section="DROP", energy_short=0.9)
    variables = snap.rule_vars()
    expr_tests = [
        ("BPM > 140", True),
        ("Bass > 80", True),
        ("RMS > 0.9", True),
        ("High Frequency > 70", True),
        ("mid > 90", False),
        ('section == "DROP"', True),
        ("is_drop and bass > 50", True),
        ("bpm > 100 and (bass > 80 or treble > 90)", True),
    ]
    ok_expr = True
    for text, expected in expr_tests:
        try:
            result = eval_expression(compile_expression(text), variables)
        except Exception as exc:  # noqa: BLE001
            result = f"EROARE: {exc}"
        good = result is expected
        ok_expr = ok_expr and good
        print(f"      {'OK ' if good else 'ESEC'}  {text:38s} = {result}")
    checks.append(("Evaluarea expresiilor", ok_expr, ""))

    # ---------------- forme scurte ----------------
    print("\n  --- Actiuni in forma scurta ---")
    ok_short = True
    for text in ("Flash 4", "Increase Speed", "Release Flash", "Playback 5",
                 "Color FX", "Speed=180", "Exec 1/3", "Key ctrl+f4",
                 "Flash 2 + Speed=150"):
        try:
            actions = parse_shorthand_list(text, "test")
            print(f"      {text:22s} -> {', '.join(a.describe() for a in actions)}")
        except Exception as exc:  # noqa: BLE001
            print(f"      {text:22s} -> EROARE: {exc}")
            ok_short = False
    checks.append(("Parsarea formelor scurte", ok_short, ""))

    # ---------------- raport ----------------
    print("\n" + "-" * 66)
    failed = 0
    for name, ok, detail in checks:
        status = "  OK  " if ok else " ESEC "
        failed += 0 if ok else 1
        print(f"  [{status}] {name:38s} {detail}")
    print("-" * 66)
    if failed:
        print(f"  {failed} verificari au esuat.\n")
    else:
        print("  Toate verificarile au trecut.\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run_selftest())
