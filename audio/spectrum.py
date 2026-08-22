"""
audio/spectrum.py
=================
Analiza spectrala in timp real (FFT) - inima extractiei de caracteristici.

Pentru fiecare cadru de `hop` esantioane calculeaza:
  * spectrul de amplitudine (rFFT cu fereastra Hann, overlap 75% la hop=512)
  * energia pe cele 6 benzi cerute (Sub / Bass / LowMid / Mid / High / Treble)
  * normalizare automata per banda (AGC) -> valori 0..1 utilizabile in reguli
  * spectral flux (baza pentru onset si detectia de drop)
  * spectral centroid ("agresivitate"), rolloff, flatness
  * RMS, peak si o coloana log-frecventa pentru spectrograma din UI

Totul cu buffere pre-alocate: in bucla realtime nu se aloca memorie noua
(exceptie: rezultatele rFFT, care sunt inevitabile in numpy).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
from scipy.signal import get_window

log = logging.getLogger(__name__)

EPS = 1e-12
BAND_ORDER = ("sub_bass", "bass", "low_mid", "mid", "high", "treble")


@dataclass
class SpectrumFrame:
    """Rezultatul analizei unui cadru."""

    rms: float
    rms_db: float
    peak: float
    loudness: float                 # RMS normalizat prin AGC (0..1)
    bands: np.ndarray               # 6 valori normalizate 0..1 (anvelopa VU)
    bands_slow: np.ndarray          # aceleasi, mediate pe ~1.2 s
    bands_db: np.ndarray            # 6 valori in dB
    bands_power: np.ndarray         # 6 valori putere bruta
    flux: float                     # spectral flux brut
    flux_norm: float                # flux / media mobila (0..~3)
    bass_flux: float                # flux doar sub 250 Hz (kick)
    centroid_hz: float
    centroid_norm: float            # 0..1 pe scala log 100 Hz..8 kHz
    rolloff_hz: float
    flatness: float
    magnitude: np.ndarray           # spectrul complet (amplitudine)
    spectro_col: np.ndarray         # coloana pentru spectrograma (0..1)


class EnvelopeTracker:
    """Urmarire asimetrica de anvelopa (attack rapid / release lent) in dB."""

    def __init__(self, attack_s: float, release_s: float, frame_dt: float,
                 initial: float = -120.0):
        self.value = initial
        self.attack = 1.0 - np.exp(-frame_dt / max(attack_s, 1e-4))
        self.release = 1.0 - np.exp(-frame_dt / max(release_s, 1e-4))

    def update(self, x: float) -> float:
        coef = self.attack if x > self.value else self.release
        self.value += coef * (x - self.value)
        return self.value


class BandAGC:
    """Normalizare adaptiva per banda: dB -> 0..1.

    Urmareste un plafon (peak, attack rapid, release lent) si un prag de
    zgomot (floor, coborare rapida, urcare lenta). Rezultatul e stabil
    indiferent de volumul sistemului sau de masterizarea piesei - exact ce
    trebuie pentru reguli de tipul "IF Bass > 80%".
    """

    def __init__(self, n: int, frame_dt: float, attack_ms: float, release_s: float,
                 floor_db: float, min_range_db: float):
        self.n = n
        self.floor_db = floor_db
        self.min_range = min_range_db
        self.peak = np.full(n, floor_db + min_range_db, dtype=np.float64)
        self.floor = np.full(n, floor_db, dtype=np.float64)
        dt = frame_dt
        self.peak_attack = 1.0 - np.exp(-dt / max(attack_ms / 1000.0, 1e-4))
        self.peak_release = 1.0 - np.exp(-dt / max(release_s, 1e-4))
        self.floor_fall = 1.0 - np.exp(-dt / 1.5)
        self.floor_rise = 1.0 - np.exp(-dt / 30.0)

    def process(self, db: np.ndarray) -> np.ndarray:
        up = db > self.peak
        coef = np.where(up, self.peak_attack, self.peak_release)
        self.peak += coef * (db - self.peak)

        down = db < self.floor
        fcoef = np.where(down, self.floor_fall, self.floor_rise)
        self.floor += fcoef * (db - self.floor)
        np.maximum(self.floor, self.floor_db, out=self.floor)

        hi = np.maximum(self.peak, self.floor + self.min_range)
        norm = (db - self.floor) / np.maximum(hi - self.floor, 1e-6)
        return np.clip(norm, 0.0, 1.0)


class SpectrumAnalyzer:
    def __init__(self, cfg):
        self.samplerate = int(cfg.get("audio.samplerate", 48000))
        self.fft_size = int(cfg.get("audio.fft_size", 2048))
        self.hop = int(cfg.get("audio.hop_size", 512))
        self.frame_dt = self.hop / self.samplerate

        # --- fereastra si buffere ---
        self.window = get_window("hann", self.fft_size, fftbins=True).astype(np.float32)
        self.win_scale = 2.0 / max(np.sum(self.window), EPS)
        self.buffer = np.zeros(self.fft_size, dtype=np.float32)
        self.freqs = np.fft.rfftfreq(self.fft_size, 1.0 / self.samplerate)
        self.n_bins = self.freqs.shape[0]
        self.prev_mag = np.zeros(self.n_bins, dtype=np.float32)

        # --- benzile de frecventa ---
        band_cfg = cfg.get("analysis.bands", {}) or {}
        self.band_names: list[str] = []
        self.band_slices: list[slice] = []
        for name in BAND_ORDER:
            lo, hi = band_cfg.get(name, (20, 20000))
            hi = min(hi, self.samplerate / 2 - 1)
            i0 = int(np.searchsorted(self.freqs, lo, side="left"))
            i1 = int(np.searchsorted(self.freqs, hi, side="right"))
            i1 = max(i1, i0 + 1)
            self.band_names.append(name)
            self.band_slices.append(slice(i0, i1))
        self.n_bands = len(self.band_slices)

        # indexul pana la care consideram "bass" pentru bass-flux (kick)
        self.bass_bin_max = int(np.searchsorted(self.freqs, 250.0, side="right"))
        # ignoram DC si infrasunetele in centroid
        self.centroid_lo = int(np.searchsorted(self.freqs, 20.0, side="left"))

        # --- AGC ---
        agc = cfg.get("analysis.agc", {}) or {}
        self.agc_enabled = bool(agc.get("enabled", True))
        self.band_agc = BandAGC(
            self.n_bands, self.frame_dt,
            attack_ms=float(agc.get("attack_ms", 40.0)),
            release_s=float(agc.get("release_s", 12.0)),
            floor_db=float(agc.get("floor_db", -70.0)),
            min_range_db=float(agc.get("min_range_db", 18.0)),
        )
        self.loud_agc = BandAGC(
            1, self.frame_dt,
            attack_ms=float(agc.get("attack_ms", 40.0)),
            release_s=float(agc.get("release_s", 12.0)),
            floor_db=float(agc.get("floor_db", -70.0)),
            min_range_db=float(agc.get("min_range_db", 18.0)),
        )

        # Anvelopa de afisare/reguli: atac rapid, cadere lenta (ca la orice
        # VU-metru). Fara ea, valoarea benzii cade la ~0 intre doua kick-uri
        # si o regula "bass > 80%" ar porni si opri de zeci de ori pe secunda.
        meter_attack = float(agc.get("meter_attack_ms", 20.0)) / 1000.0
        meter_release = float(agc.get("meter_release_ms", 180.0)) / 1000.0
        self.env_attack = 1.0 - np.exp(-self.frame_dt / max(meter_attack, 1e-4))
        self.env_release = 1.0 - np.exp(-self.frame_dt / max(meter_release, 1e-4))
        self.band_env = np.zeros(self.n_bands, dtype=np.float64)

        # A doua medie, LENTA si simetrica (~1.2 s): raspunde la intrebarea
        # "cat de bass-oasa e sectiunea asta", nu "cat de tare e kick-ul
        # chiar acum". Regulile de tip "IF bass > 80" trebuie sa foloseasca
        # asta (bass_avg), altfel comuta la fiecare kick.
        slow_s = float(agc.get("slow_average_ms", 1200.0)) / 1000.0
        self.env_slow_coef = 1.0 - np.exp(-self.frame_dt / max(slow_s, 1e-4))
        self.band_slow = np.zeros(self.n_bands, dtype=np.float64)

        # --- flux ---
        self.flux_mean = 0.0
        self.flux_coef = 1.0 - np.exp(-self.frame_dt / 2.0)   # medie mobila 2 s

        # --- spectrograma: axa log-frecventa pre-calculata ---
        self.spectro_bins = 128
        self.spectro_freqs = np.logspace(np.log10(25.0),
                                         np.log10(min(20000.0, self.samplerate / 2 - 1)),
                                         self.spectro_bins)
        self.spectro_floor_db = -78.0
        self.spectro_peak_db = -12.0

        log.info("SpectrumAnalyzer: sr=%d fft=%d hop=%d (%.1f cadre/s, rezolutie %.1f Hz)",
                 self.samplerate, self.fft_size, self.hop,
                 1.0 / self.frame_dt, self.samplerate / self.fft_size)

    # ------------------------------------------------------------------
    def process(self, samples: np.ndarray) -> SpectrumFrame:
        """Proceseaza exact `hop` esantioane noi si returneaza caracteristicile."""
        n = samples.shape[0]
        if n >= self.fft_size:
            self.buffer[:] = samples[-self.fft_size:]
        else:
            self.buffer[:-n] = self.buffer[n:]
            self.buffer[-n:] = samples

        # --- nivel in domeniul timp (pe cadrul nou, nu pe toata fereastra) ---
        rms = float(np.sqrt(np.mean(np.square(samples), dtype=np.float64)))
        peak = float(np.max(np.abs(samples))) if n else 0.0
        rms_db = 20.0 * np.log10(max(rms, EPS))

        # --- FFT ---
        windowed = self.buffer * self.window
        spec = np.fft.rfft(windowed)
        mag = (np.abs(spec) * self.win_scale).astype(np.float32)
        power = np.square(mag, dtype=np.float32)

        # --- energie pe benzi ---
        bands_power = np.empty(self.n_bands, dtype=np.float64)
        for i, sl in enumerate(self.band_slices):
            seg = power[sl]
            bands_power[i] = float(np.mean(seg)) if seg.size else 0.0
        bands_db = 10.0 * np.log10(bands_power + EPS)
        if self.agc_enabled:
            bands_norm = self.band_agc.process(bands_db)
            loudness = float(self.loud_agc.process(np.array([rms_db]))[0])
        else:
            bands_norm = np.clip((bands_db + 70.0) / 70.0, 0.0, 1.0)
            loudness = float(np.clip((rms_db + 70.0) / 70.0, 0.0, 1.0))

        coef = np.where(bands_norm > self.band_env, self.env_attack, self.env_release)
        self.band_env += coef * (bands_norm - self.band_env)
        bands_norm = self.band_env.copy()
        self.band_slow += self.env_slow_coef * (bands_norm - self.band_slow)

        # --- spectral flux (doar cresterile => "novelty") ---
        diff = mag - self.prev_mag
        np.maximum(diff, 0.0, out=diff)
        flux = float(np.sum(diff)) / self.n_bins
        bass_flux = float(np.sum(diff[:self.bass_bin_max])) / max(self.bass_bin_max, 1)
        self.prev_mag[:] = mag
        self.flux_mean += self.flux_coef * (flux - self.flux_mean)
        flux_norm = flux / max(self.flux_mean, 1e-9)

        # --- descriptori spectrali ---
        seg_mag = mag[self.centroid_lo:]
        seg_freq = self.freqs[self.centroid_lo:]
        total = float(np.sum(seg_mag))
        if total > 1e-9:
            centroid = float(np.dot(seg_freq, seg_mag) / total)
            cumsum = np.cumsum(seg_mag)
            idx = int(np.searchsorted(cumsum, 0.85 * cumsum[-1]))
            rolloff = float(seg_freq[min(idx, seg_freq.shape[0] - 1)])
            seg_pow = power[self.centroid_lo:] + EPS
            flatness = float(np.exp(np.mean(np.log(seg_pow))) / np.mean(seg_pow))
        else:
            centroid, rolloff, flatness = 0.0, 0.0, 0.0
        # 100 Hz -> 0 ; 8000 Hz -> 1 (perceptual, pe scala log)
        centroid_norm = float(np.clip(
            (np.log2(max(centroid, 20.0)) - np.log2(100.0)) / (np.log2(8000.0) - np.log2(100.0)),
            0.0, 1.0))

        # --- coloana pentru spectrograma (log frecventa, 0..1) ---
        mag_db = 20.0 * np.log10(mag + EPS)
        col = np.interp(self.spectro_freqs, self.freqs, mag_db)
        col = (col - self.spectro_floor_db) / (self.spectro_peak_db - self.spectro_floor_db)
        np.clip(col, 0.0, 1.0, out=col)

        return SpectrumFrame(
            rms=rms, rms_db=rms_db, peak=peak, loudness=loudness,
            bands=bands_norm.astype(np.float32),
            bands_slow=self.band_slow.astype(np.float32),
            bands_db=bands_db.astype(np.float32),
            bands_power=bands_power,
            flux=flux, flux_norm=flux_norm, bass_flux=bass_flux,
            centroid_hz=centroid, centroid_norm=centroid_norm,
            rolloff_hz=rolloff, flatness=flatness,
            magnitude=mag, spectro_col=col.astype(np.float32),
        )

    def reset(self) -> None:
        self.buffer[:] = 0.0
        self.prev_mag[:] = 0.0
        self.flux_mean = 0.0
        self.band_env[:] = 0.0
        self.band_slow[:] = 0.0
