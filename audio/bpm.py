"""
audio/bpm.py
============
Estimarea tempo-ului (BPM) in timp real, continuu.

Algoritm (rulat o data la ~250 ms, nu la fiecare cadru):

  1. anvelopa de novelty (spectral flux) pe ultimele 8 s, la 93.75 Hz
  2. eliminarea componentei lente (scadem media mobila de 0.5 s) +
     redresare -> ramane doar "pulsul"
  3. autocorelatie prin FFT (O(n log n))
  4. scor pe o grila de tempo-uri: acf(lag) + 0.5*acf(2*lag) + 0.25*acf(3*lag)
     -> suma armonica: intareste perioada reala si rezolva ambiguitatea
     de octava (64 vs 128 vs 256 BPM)
  5. ponderare cu un prior log-normal centrat pe 128 BPM (muzica de club)
  6. interpolare parabolica pentru precizie sub-lag (~0.1 BPM)
  7. pliere in intervalul preferat (80..175) + mediana peste ultimele N
     estimari + histerezis, ca sa nu "sara" BPM-ul pe ecran

Suporta si tap tempo manual si BPM fixat din configurare.
"""

from __future__ import annotations

import logging
from collections import deque

import numpy as np
from scipy.ndimage import uniform_filter1d

log = logging.getLogger(__name__)


class TempoEstimator:
    def __init__(self, cfg):
        samplerate = int(cfg.get("audio.samplerate", 48000))
        hop = int(cfg.get("audio.hop_size", 512))
        self.frame_dt = hop / samplerate
        self.frame_rate = 1.0 / self.frame_dt

        bc = cfg.get("analysis.bpm", {}) or {}
        self.bpm_min = float(bc.get("min", 60.0))
        self.bpm_max = float(bc.get("max", 200.0))
        self.prefer_min = float(bc.get("prefer_min", 80.0))
        self.prefer_max = float(bc.get("prefer_max", 175.0))
        self.window_s = float(bc.get("window_s", 8.0))
        self.update_interval = float(bc.get("update_interval_s", 0.25))
        self.prior_center = float(bc.get("prior_center", 128.0))
        self.prior_width = float(bc.get("prior_width", 0.9))
        self.smoothing = int(bc.get("smoothing", 7))
        self.lock_tol = float(bc.get("lock_tolerance", 0.02))
        manual = bc.get("manual_bpm")
        self.manual_bpm = float(manual) if manual else None

        self.n_frames = max(64, int(self.window_s * self.frame_rate))
        self.novelty = np.zeros(self.n_frames, dtype=np.float32)
        self.filled = 0
        self.pos = 0

        # grila de tempo candidate (rezolutie 0.25 BPM)
        self.bpm_grid = np.arange(self.bpm_min, self.bpm_max + 0.25, 0.25)
        self.lag_grid = 60.0 * self.frame_rate / self.bpm_grid
        # prior log-normal
        self.prior = np.exp(-0.5 * (np.log2(self.bpm_grid / self.prior_center)
                                    / self.prior_width) ** 2)
        self.smooth_len = max(1, int(round(0.5 * self.frame_rate)))

        self.candidates: deque[float] = deque(maxlen=max(3, self.smoothing))
        self.bpm = 0.0
        self.confidence = 0.0
        self.raw_bpm = 0.0
        self._last_update = -1.0
        self._taps: deque[float] = deque(maxlen=8)
        self.changed = False

    # ------------------------------------------------------------------
    def push(self, novelty: float) -> None:
        """Adauga o valoare de novelty (un cadru de analiza)."""
        self.novelty[self.pos] = novelty
        self.pos = (self.pos + 1) % self.n_frames
        self.filled = min(self.filled + 1, self.n_frames)

    def _ordered(self) -> np.ndarray:
        if self.filled < self.n_frames:
            return self.novelty[:self.pos]
        return np.concatenate((self.novelty[self.pos:], self.novelty[:self.pos]))

    # ------------------------------------------------------------------
    def update(self, t: float, force: bool = False) -> bool:
        """Recalculeaza BPM-ul daca a trecut intervalul. True = s-a schimbat."""
        self.changed = False
        if self.manual_bpm:
            if self.bpm != self.manual_bpm:
                self.bpm = self.manual_bpm
                self.confidence = 1.0
                self.changed = True
            return self.changed
        if not force and (t - self._last_update) < self.update_interval:
            return False
        self._last_update = t
        # avem nevoie de cel putin 4 s de istoric pentru o estimare stabila
        if self.filled < int(4.0 * self.frame_rate):
            return False

        x = self._ordered().astype(np.float64)
        # 2. scoatem trendul lent si redresam
        baseline = uniform_filter1d(x, size=self.smooth_len, mode="nearest")
        x = x - baseline
        np.maximum(x, 0.0, out=x)
        std = float(np.std(x))
        if std < 1e-9:
            return False
        x /= std

        # 3. autocorelatie prin FFT
        n = x.shape[0]
        nfft = 1 << int(np.ceil(np.log2(2 * n)))
        spec = np.fft.rfft(x, n=nfft)
        acf = np.fft.irfft(np.abs(spec) ** 2, n=nfft)[:n]
        if acf[0] <= 1e-12:
            return False
        acf /= acf[0]
        # normalizare pe numarul de termeni suprapusi (altfel lag-urile mari
        # sunt penalizate artificial)
        counts = np.arange(n, 0, -1, dtype=np.float64)
        acf *= n / counts

        # 4-5. scor armonic + prior
        idx = np.arange(n, dtype=np.float64)
        score = np.interp(self.lag_grid, idx, acf, left=0.0, right=0.0)
        score += 0.5 * np.interp(self.lag_grid * 2, idx, acf, left=0.0, right=0.0)
        score += 0.25 * np.interp(self.lag_grid * 3, idx, acf, left=0.0, right=0.0)
        # penalizam si jumatatea de perioada (contra dublarii tempo-ului)
        score += 0.35 * np.interp(self.lag_grid * 0.5, idx, acf, left=0.0, right=0.0)
        score *= self.prior

        best = int(np.argmax(score))
        best_score = float(score[best])
        if best_score <= 0:
            return False

        # 6. interpolare parabolica pe grila de tempo
        bpm_raw = float(self.bpm_grid[best])
        if 0 < best < len(score) - 1:
            y0, y1, y2 = score[best - 1], score[best], score[best + 1]
            denom = (y0 - 2 * y1 + y2)
            if abs(denom) > 1e-12:
                delta = 0.5 * (y0 - y2) / denom
                bpm_raw += float(np.clip(delta, -1.0, 1.0)) * 0.25

        mean_score = float(np.mean(score))
        self.confidence = float(np.clip((best_score / max(mean_score, 1e-9) - 1.0) / 4.0, 0.0, 1.0))
        self.raw_bpm = bpm_raw

        # 7. pliere in intervalul preferat
        bpm_folded = self._fold(bpm_raw)
        self.candidates.append(bpm_folded)
        median = float(np.median(self.candidates))

        if self.bpm <= 0:
            self.bpm = median
            self.changed = True
        else:
            rel = abs(median - self.bpm) / max(self.bpm, 1e-6)
            if rel > self.lock_tol:
                # schimbam doar daca majoritatea estimarilor recente confirma
                agree = sum(1 for c in self.candidates
                            if abs(c - median) / max(median, 1e-6) < self.lock_tol)
                if agree >= max(2, len(self.candidates) // 2):
                    self.bpm = median
                    self.changed = True
            else:
                # ajustare fina, fara salturi vizibile
                self.bpm += 0.25 * (median - self.bpm)
        return self.changed

    def _fold(self, bpm: float) -> float:
        for _ in range(4):
            if bpm < self.prefer_min:
                bpm *= 2.0
            elif bpm > self.prefer_max:
                bpm /= 2.0
            else:
                break
        return float(np.clip(bpm, self.bpm_min, self.bpm_max))

    # ------------------------------------------------------------------
    def tap(self, t: float) -> float | None:
        """Tap tempo manual: apeleaza la fiecare apasare de buton."""
        if self._taps and (t - self._taps[-1]) > 2.5:
            self._taps.clear()
        self._taps.append(t)
        if len(self._taps) < 3:
            return None
        intervals = np.diff(np.array(self._taps))
        period = float(np.median(intervals))
        if period <= 0:
            return None
        bpm = self._fold(60.0 / period)
        self.bpm = bpm
        self.confidence = 1.0
        self.candidates.clear()
        self.candidates.append(bpm)
        self.changed = True
        return bpm

    def set_manual(self, bpm: float | None) -> None:
        self.manual_bpm = float(bpm) if bpm else None
        if self.manual_bpm:
            self.bpm = self.manual_bpm
            self.confidence = 1.0

    def reset(self) -> None:
        self.novelty[:] = 0.0
        self.filled = 0
        self.pos = 0
        self.candidates.clear()
        self.bpm = 0.0
        self.confidence = 0.0
