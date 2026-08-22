"""
audio/beat.py
=============
Urmarirea beat-ului si a downbeat-ului (primul timp din masura).

Tempo-ul (perioada) vine de la audio/bpm.py; aici se rezolva FAZA, adica
exact cand cade urmatorul beat. Se foloseste o bucla de tip PLL:

  * pastram momentul prezis al urmatorului beat: `next_beat`
  * cand timpul il depaseste -> emitem BEAT si adaugam o perioada
  * cand apare un onset aproape de un beat prezis, corectam faza cu o
    fractiune din eroare (alpha), ponderat cu puterea onset-ului
  * o corectie sistematica (mereu in acelasi sens) ajusteaza si perioada
    cu maxim +/-2%, ca sa urmarim DJ-ul care schimba pitch-ul

Beat-urile sunt PREZISE, nu detectate post-factum: pe un beat prezis
lumina poate fi trimisa cu cateva ms inainte, deci sincronizarea vizuala
este perfecta chiar daca lantul audio are 20 ms latenta.

Downbeat: ipoteza 4/4. Tinem 4 acumulatoare (unul per pozitie in masura)
in care adunam energia de bass la fiecare beat, cu uitare exponentiala.
Pozitia cu cea mai multa energie de kick devine "1"-ul masurii.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class BeatInfo:
    beat: bool = False
    downbeat: bool = False
    index: int = 0          # numarul beat-ului de la pornire
    in_bar: int = 0         # 0..beats_per_bar-1 (0 = downbeat)
    bar: int = 0
    phase: float = 0.0      # 0..1 in interiorul beat-ului curent
    time_to_beat: float = 0.0
    period: float = 0.0
    locked: bool = False


class BeatTracker:
    def __init__(self, cfg):
        bc = cfg.get("analysis.beat", {}) or {}
        self.alpha = float(bc.get("phase_alpha", 0.18))
        self.max_correction = float(bc.get("max_correction", 0.35))
        self.beats_per_bar = int(bc.get("beats_per_bar", 4))
        self.downbeat_decay = float(bc.get("downbeat_decay", 0.92))
        self.min_confidence = float(bc.get("min_confidence", 0.25))

        self.period = 0.0
        self.next_beat = 0.0
        self.last_beat_t = 0.0
        self.index = 0
        self.bar = 0
        self.downbeat_offset = 0
        self.locked = False
        self.confidence = 0.0

        self._bar_acc = np.zeros(self.beats_per_bar, dtype=np.float64)
        self._err_acc = 0.0
        self._err_count = 0
        self._base_period = 0.0
        self._acc_err = 0.0        # eroare de faza acumulata intre beat-uri
        self._acc_weight = 0.0
        self._misses = 0           # beat-uri consecutive fara onset apropiat

    # ------------------------------------------------------------------
    def set_tempo(self, bpm: float, confidence: float) -> None:
        self.confidence = confidence
        if bpm <= 0:
            self.period = 0.0
            self.locked = False
            return
        new_period = 60.0 / bpm
        if self.period <= 0:
            self.period = new_period
            self._base_period = new_period
        elif abs(new_period - self._base_period) / self._base_period > 0.005:
            # tempo nou din estimator: pastram faza, schimbam perioada
            if self.next_beat > 0:
                self.next_beat = self.last_beat_t + new_period
            self.period = new_period
            self._base_period = new_period

    # ------------------------------------------------------------------
    def process(self, t: float, onset: bool, onset_strength: float,
                bass_energy: float) -> BeatInfo:
        info = BeatInfo(period=self.period)
        if self.period <= 0 or self.confidence < self.min_confidence:
            self.locked = False
            info.locked = False
            info.index = self.index
            return info

        # prima initializare a fazei: pornim de la primul onset puternic
        if self.next_beat <= 0.0:
            if onset:
                self.next_beat = t + self.period
                self.last_beat_t = t
                self.index = 0
                self._bar_acc[:] = 0.0
            info.index = self.index
            return info

        # --- colectarea erorilor de faza din onset-uri ---
        # Corectia NU se aplica imediat: intr-un build-up cu role de snare
        # apar 10-16 onset-uri pe secunda si aplicarea directa ar trage faza
        # in toate partile (se pierdeau beat-uri). Se acumuleaza o eroare
        # medie ponderata si se aplica O SINGURA data, la fiecare beat,
        # limitata la 15% din perioada.
        if onset:
            prev_beat = self.next_beat - self.period
            err_next = t - self.next_beat
            err_prev = t - prev_beat
            err = err_prev if abs(err_prev) <= abs(err_next) else err_next
            if abs(err) < self.max_correction * self.period:
                weight = float(np.clip(onset_strength / 2.0, 0.2, 1.0))
                self._acc_err += weight * err
                self._acc_weight += weight

        # --- emiterea beat-ului ---
        if t >= self.next_beat:
            # daca am ramas mult in urma (pauza de analiza), resincronizam
            if t - self.next_beat > 2 * self.period:
                self.next_beat = t + self.period
            else:
                self.next_beat += self.period
            self.index += 1
            self.last_beat_t = t
            self.locked = True

            # corectia de faza acumulata de la beat-ul precedent
            if self._acc_weight > 0:
                mean_err = self._acc_err / self._acc_weight
                correction = float(np.clip(self.alpha * mean_err,
                                           -0.15 * self.period, 0.15 * self.period))
                self.next_beat += correction
                self._err_acc += mean_err
                self._err_count += 1
                self._misses = 0
            else:
                # niciun onset langa beat: probabil faza e complet gresita
                self._misses += 1
                if self._misses >= 4:
                    self.next_beat = 0.0      # re-initializare la urmatorul onset
                    self._misses = 0
            self._acc_err = 0.0
            self._acc_weight = 0.0

            # adaptarea fina a perioadei (DJ care schimba pitch-ul)
            if self._err_count >= 8:
                mean_err = self._err_acc / self._err_count
                # eroare medie pozitiva = beat-urile reale vin mai tarziu =>
                # perioada noastra este prea scurta
                adjust = float(np.clip(mean_err * 0.05,
                                       -0.02 * self.period, 0.02 * self.period))
                candidate = self.period + adjust
                if abs(candidate - self._base_period) / self._base_period <= 0.03:
                    self.period = candidate
                self._err_acc = 0.0
                self._err_count = 0

            slot = self.index % self.beats_per_bar
            self._bar_acc *= self.downbeat_decay
            self._bar_acc[slot] += max(bass_energy, 0.0)
            self.downbeat_offset = int(np.argmax(self._bar_acc))

            info.beat = True
            in_bar = (self.index - self.downbeat_offset) % self.beats_per_bar
            info.in_bar = in_bar
            info.downbeat = (in_bar == 0)
            if info.downbeat:
                self.bar += 1
        else:
            info.in_bar = (self.index - self.downbeat_offset) % self.beats_per_bar

        info.index = self.index
        info.bar = self.bar
        info.locked = self.locked
        elapsed = t - self.last_beat_t
        info.phase = float(np.clip(elapsed / self.period, 0.0, 1.0)) if self.period else 0.0
        info.time_to_beat = max(self.next_beat - t, 0.0)
        info.period = self.period
        return info

    # ------------------------------------------------------------------
    def resync(self, t: float) -> None:
        """Forteaza un downbeat acum (buton 'SYNC' in UI)."""
        if self.period > 0:
            self.next_beat = t + self.period
            self.last_beat_t = t
            self.index = 0
            self.downbeat_offset = 0
            self.bar = 0
            self._bar_acc[:] = 0.0

    def reset(self) -> None:
        self.period = 0.0
        self.next_beat = 0.0
        self.index = 0
        self.bar = 0
        self.locked = False
        self._bar_acc[:] = 0.0
        self._err_acc = 0.0
        self._err_count = 0
        self._acc_err = 0.0
        self._acc_weight = 0.0
        self._misses = 0
