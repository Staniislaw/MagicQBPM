"""
audio/onset.py
==============
Detectia onset-urilor (inceputul fiecarui sunet: kick, snare, hi-hat,
atacul unui sintetizator).

Metoda: spectral flux + prag adaptiv median.

  novelty[n] = suma cresterilor de amplitudine intre cadrul n-1 si n
  prag[n]    = mediana(novelty pe ultima ~1.2 s) * mult + delta
  onset      = flanc crescator peste prag, cu perioada refractara

Se declanseaza pe FLANCUL CRESCATOR, nu pe maximul local: un maxim local
ar cere sa asteptam cadrul urmator (+10.7 ms latenta). Pentru lumini,
reactia imediata conteaza mai mult decat precizia de sub-cadru.

Iesirea alimenteaza: BPM (audio/bpm.py), beat tracker (audio/beat.py) si
regulile de tip "ON ONSET".
"""

from __future__ import annotations

from collections import deque

import numpy as np


class OnsetDetector:
    def __init__(self, cfg):
        samplerate = int(cfg.get("audio.samplerate", 48000))
        hop = int(cfg.get("audio.hop_size", 512))
        self.frame_dt = hop / samplerate
        self.frame_rate = 1.0 / self.frame_dt

        oc = cfg.get("analysis.onset", {}) or {}
        self.mult = float(oc.get("threshold_mult", 1.55))
        self.delta = float(oc.get("threshold_delta", 0.012))
        self.min_interval = float(oc.get("min_interval_ms", 55.0)) / 1000.0
        window_s = float(oc.get("window_s", 1.2))

        self.window = max(8, int(window_s * self.frame_rate))
        self.history: deque[float] = deque(maxlen=self.window)
        self.recent_onsets: deque[float] = deque(maxlen=64)

        self.prev_novelty = 0.0
        self.last_onset_t = -10.0
        self.threshold = 0.0
        self.strength = 0.0
        # anvelopa de novelty folosita de BPM (mereu pozitiva, netezita usor)
        self.envelope = 0.0
        self._env_coef = 1.0 - np.exp(-self.frame_dt / 0.02)

    def process(self, novelty: float, t: float) -> tuple[bool, float]:
        """`novelty` = spectral flux brut. Returneaza (onset, putere_relativa)."""
        self.history.append(float(novelty))

        if len(self.history) >= 8:
            med = float(np.median(self.history))
            self.threshold = med * self.mult + self.delta
        else:
            self.threshold = max(self.delta, float(novelty) * 2.0)

        self.envelope += self._env_coef * (novelty - self.envelope)

        is_onset = False
        strength = novelty / max(self.threshold, 1e-9)
        rising = novelty > self.prev_novelty
        if (novelty > self.threshold and rising
                and (t - self.last_onset_t) >= self.min_interval):
            is_onset = True
            self.last_onset_t = t
            self.recent_onsets.append(t)

        self.prev_novelty = float(novelty)
        self.strength = strength
        return is_onset, strength

    def onset_rate(self, t: float, window_s: float = 2.0) -> float:
        """Onset-uri pe secunda in ultimele `window_s` (indicator de densitate:
        creste in build-up, scade in break)."""
        if not self.recent_onsets:
            return 0.0
        cutoff = t - window_s
        count = sum(1 for x in self.recent_onsets if x >= cutoff)
        return count / window_s

    def reset(self) -> None:
        self.history.clear()
        self.recent_onsets.clear()
        self.prev_novelty = 0.0
        self.last_onset_t = -10.0
        self.envelope = 0.0
