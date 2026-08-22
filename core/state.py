"""
core/state.py
=============
Starea partajata intre firul de analiza (scriitor) si UI (cititor).

Modelul este "snapshot sub lock": firul de analiza scrie un obiect
imutabil o data la fiecare cadru, iar UI-ul citeste ultimul snapshot la
rata lui proprie (60 FPS). UI-ul nu blocheaza niciodata audio-ul mai
mult de cateva microsecunde si nu poate corupe datele.

Istoricele grafice (spectrograma, waveform, BPM) sunt buffere circulare
numpy pre-alocate - fara alocari in bucla realtime.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np

BAND_NAMES = ("sub_bass", "bass", "low_mid", "mid", "high", "treble")
BAND_LABELS = ("Sub", "Bass", "LowMid", "Mid", "High", "Treble")


@dataclass(frozen=True)
class Snapshot:
    """Fotografia completa a analizei la un moment dat."""

    # --- timp ---
    t: float = 0.0
    frame: int = 0

    # --- nivel ---
    rms: float = 0.0            # 0..1 liniar
    rms_db: float = -120.0
    peak: float = 0.0
    loudness: float = 0.0       # 0..1, RMS normalizat cu AGC

    # --- benzi (6) ---
    bands: tuple[float, ...] = (0.0,) * 6        # energie normalizata 0..1
    bands_slow: tuple[float, ...] = (0.0,) * 6   # aceleasi, mediate pe ~1.2 s
    bands_db: tuple[float, ...] = (-120.0,) * 6  # energie in dB

    # --- spectru ---
    centroid_hz: float = 0.0
    centroid_norm: float = 0.0   # 0..1 (log 100 Hz .. 8 kHz) - "agresivitate"
    rolloff_hz: float = 0.0
    flatness: float = 0.0        # 0=tonal, 1=zgomot
    flux: float = 0.0
    flux_norm: float = 0.0

    # --- ritm ---
    bpm: float = 0.0
    bpm_confidence: float = 0.0
    bpm_age: float = 999.0       # secunde de la ultima schimbare de tempo
    beat: bool = False           # True doar in cadrul in care cade beat-ul
    downbeat: bool = False
    beat_index: int = 0
    beat_in_bar: int = 0
    bar_index: int = 0
    beat_phase: float = 0.0      # 0..1 pozitia in interiorul beat-ului
    time_to_beat: float = 0.0
    onset: bool = False
    onset_strength: float = 0.0
    onset_rate: float = 0.0      # onset-uri / secunda

    # --- structura ---
    section: str = "UNKNOWN"
    section_age: float = 0.0
    section_confidence: float = 0.0
    drop_score: float = 0.0
    buildup_score: float = 0.0
    energy_short: float = 0.0
    energy_mid: float = 0.0
    energy_long: float = 0.0
    energy_slope: float = 0.0    # panta energiei (per secunda)
    last_drop_age: float = 999.0
    silence: bool = True

    # --- diagnostic ---
    latency_ms: float = 0.0
    analysis_fps: float = 0.0
    cpu_load: float = 0.0        # fractiune din bugetul de timp per cadru
    dropped_blocks: int = 0

    def as_dict(self) -> dict[str, Any]:
        d = {f: getattr(self, f) for f in self.__dataclass_fields__}
        for name, value in zip(BAND_NAMES, self.bands):
            d[name] = value
        return d

    def rule_vars(self) -> dict[str, Any]:
        """Variabilele expuse expresiilor din rules.json.

        Conventie (documentata in README):
          * benzile si energiile sunt in PROCENTE  0..100
          * rms / loudness / *_score / phase sunt 0..1
          * bpm in BPM, *_db in dB, *_hz in Hz
        """
        b = self.bands
        s = self.bands_slow
        return {
            "bpm": self.bpm,
            "bpm_conf": self.bpm_confidence,
            "bpm_age": self.bpm_age,
            "beat": self.beat,
            "downbeat": self.downbeat,
            "beat_index": self.beat_index,
            "beat_in_bar": self.beat_in_bar + 1,   # 1..4, mai natural in reguli
            "bar": self.bar_index,
            "phase": self.beat_phase,
            "onset": self.onset,
            "onset_strength": self.onset_strength,
            "onset_rate": self.onset_rate,

            "rms": self.rms,
            "rms_db": self.rms_db,
            "peak": self.peak,
            "loudness": self.loudness * 100.0,

            "sub": b[0] * 100.0,
            "sub_bass": b[0] * 100.0,
            "bass": b[1] * 100.0,
            "low_mid": b[2] * 100.0,
            "mid": b[3] * 100.0,
            "high": b[4] * 100.0,
            "treble": b[5] * 100.0,
            "highs": (b[4] + b[5]) * 50.0,          # media High+Treble in %
            "lows": (b[0] + b[1]) * 50.0,

            # Versiunile MEDIATE (~1.2 s). Astea trebuie folosite in reguli de
            # tip prag ("IF bass > 80"): valorile instantanee de mai sus cad
            # aproape la zero intre doua kick-uri si regula ar comuta continuu.
            "sub_avg": s[0] * 100.0,
            "bass_avg": s[1] * 100.0,
            "low_mid_avg": s[2] * 100.0,
            "mid_avg": s[3] * 100.0,
            "high_avg": s[4] * 100.0,
            "treble_avg": s[5] * 100.0,
            "highs_avg": (s[4] + s[5]) * 50.0,
            "lows_avg": (s[0] + s[1]) * 50.0,

            "centroid": self.centroid_hz,
            "brightness": self.centroid_norm * 100.0,
            "flatness": self.flatness,
            "flux": self.flux_norm * 100.0,

            "section": self.section,
            "section_age": self.section_age,
            # comparatii comode: is_drop, is_buildup, is_break, ...
            "is_drop": self.section == "DROP",
            "is_buildup": self.section == "BUILDUP",
            "is_break": self.section == "BREAK",
            "is_climax": self.section == "CLIMAX",
            "is_intro": self.section == "INTRO",
            "is_outro": self.section == "OUTRO",
            "is_groove": self.section == "GROOVE",
            "is_silence": self.section == "SILENCE",
            "energy": self.energy_short * 100.0,
            "energy_mid": self.energy_mid * 100.0,
            "energy_long": self.energy_long * 100.0,
            "energy_slope": self.energy_slope,
            "drop_score": self.drop_score,
            "buildup_score": self.buildup_score,
            "drop_age": self.last_drop_age,
            "silence": self.silence,
        }


class History:
    """Buffer circular 1D pentru grafice (waveform, BPM, RMS)."""

    def __init__(self, size: int, dtype=np.float32):
        self.size = int(size)
        self.buf = np.zeros(self.size, dtype=dtype)
        self.pos = 0
        self.filled = 0

    def push(self, value: float) -> None:
        self.buf[self.pos] = value
        self.pos = (self.pos + 1) % self.size
        self.filled = min(self.filled + 1, self.size)

    def extend(self, values: np.ndarray) -> None:
        n = len(values)
        if n >= self.size:
            self.buf[:] = values[-self.size:]
            self.pos = 0
            self.filled = self.size
            return
        end = self.pos + n
        if end <= self.size:
            self.buf[self.pos:end] = values
        else:
            first = self.size - self.pos
            self.buf[self.pos:] = values[:first]
            self.buf[:n - first] = values[first:]
        self.pos = end % self.size
        self.filled = min(self.filled + n, self.size)

    def ordered(self) -> np.ndarray:
        """Datele in ordine cronologica (cel mai vechi -> cel mai nou)."""
        if self.filled < self.size:
            return self.buf[:self.pos].copy()
        return np.concatenate((self.buf[self.pos:], self.buf[:self.pos]))


class Spectrogram2D:
    """Buffer circular 2D (bins x coloane) pentru spectrograma."""

    def __init__(self, bins: int, cols: int):
        self.bins = int(bins)
        self.cols = int(cols)
        self.buf = np.zeros((self.bins, self.cols), dtype=np.float32)
        self.pos = 0

    def push(self, column: np.ndarray) -> None:
        if column.shape[0] != self.bins:
            return
        self.buf[:, self.pos] = column
        self.pos = (self.pos + 1) % self.cols

    def ordered(self) -> np.ndarray:
        return np.concatenate((self.buf[:, self.pos:], self.buf[:, :self.pos]), axis=1)


class SharedState:
    """Container thread-safe pentru snapshot + istoricele grafice."""

    def __init__(self, spectro_bins: int = 128, spectro_cols: int = 750,
                 wave_samples: int = 36000, hist_frames: int = 1200,
                 wave_decim: int = 4):
        # `wave_decim`: waveform-ul se pastreaza decimat (afisarea nu are
        # nevoie de rezolutie completa, iar copierea la 60 FPS devine ieftina)
        self.wave_decim = max(1, int(wave_decim))
        self._lock = threading.Lock()
        self._snapshot = Snapshot()
        self.spectrogram = Spectrogram2D(spectro_bins, spectro_cols)
        self.waveform = History(wave_samples)
        self.rms_history = History(hist_frames)
        self.bpm_history = History(hist_frames)
        self.beat_flash_t = 0.0
        self.downbeat_flash_t = 0.0
        self.drop_flash_t = 0.0
        self.started_at = time.monotonic()

    # ---------------- scriere (firul de analiza) ----------------
    def update(self, snapshot: Snapshot, spectro_col: np.ndarray | None = None,
               samples: np.ndarray | None = None) -> None:
        now = snapshot.t
        with self._lock:
            self._snapshot = snapshot
            if spectro_col is not None:
                self.spectrogram.push(spectro_col)
            if samples is not None:
                self.waveform.extend(samples[::self.wave_decim])
            self.rms_history.push(snapshot.rms)
            self.bpm_history.push(snapshot.bpm)
            if snapshot.beat:
                self.beat_flash_t = now
            if snapshot.downbeat:
                self.downbeat_flash_t = now
            if snapshot.section == "DROP" and snapshot.section_age < 0.1:
                self.drop_flash_t = now

    def patch(self, **fields: Any) -> None:
        with self._lock:
            self._snapshot = replace(self._snapshot, **fields)

    # ---------------- citire (UI) ----------------
    @property
    def snapshot(self) -> Snapshot:
        with self._lock:
            return self._snapshot

    def graphics(self) -> dict[str, Any]:
        with self._lock:
            return {
                "snapshot": self._snapshot,
                "spectrogram": self.spectrogram.ordered(),
                "waveform": self.waveform.ordered(),
                "rms": self.rms_history.ordered(),
                "bpm": self.bpm_history.ordered(),
                "beat_flash_t": self.beat_flash_t,
                "downbeat_flash_t": self.downbeat_flash_t,
                "drop_flash_t": self.drop_flash_t,
            }


@dataclass
class TransportStatus:
    """Starea unui transport catre MagicQ, afisata in UI."""

    name: str
    available: bool = False       # biblioteca instalata + configurat
    connected: bool = False       # canal deschis
    last_send: float = 0.0
    sent: int = 0
    errors: int = 0
    detail: str = ""
    supported: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_idle(self) -> bool:
        return self.last_send == 0.0 or (time.monotonic() - self.last_send) > 5.0
