"""
audio/structure.py
==================
Detectia structurii melodiei in timp real:

    INTRO -> GROOVE -> BUILDUP -> DROP -> CLIMAX -> BREAK -> ... -> OUTRO

Nu exista timecode si nu stim piesa dinainte, deci totul se decide din
evolutia energiei. Semnalele folosite (exact cele cerute):

  * energia globala pe trei orizonturi: 0.35 s (instant), 3 s (referinta),
    15 s (context) - toate normalizate prin AGC, deci independente de
    volumul sistemului
  * panta energiei (regresie liniara pe ultimele secunde) -> build-up
  * cresterea brusca a bass-ului (delta pe o fereastra de 350 ms) -> drop
  * spectral flux fata de media lui mobila -> schimbare de textura
  * RMS absolut -> liniste / break
  * centroid si continutul de inalte -> "riser"-ele urca in frecventa
  * densitatea de onset-uri -> rolele de snare din build-up

Deciziile finale trec printr-o masina de stari cu histerezis (o sectiune
tine minim `min_section_s`), ca luminile sa nu clipeasca intre stari.

Optional: un clasificator scikit-learn antrenat (config
`analysis.structure.ml`) poate fi amestecat peste scorurile euristice.
Fara model, euristica singura functioneaza bine pe muzica electronica.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

SECTIONS = ("SILENCE", "INTRO", "GROOVE", "BUILDUP", "DROP", "CLIMAX", "BREAK", "OUTRO")


class Ring1D:
    """Buffer circular de float cu acces rapid la ultimele N valori."""

    def __init__(self, size: int):
        self.size = max(2, int(size))
        self.buf = np.zeros(self.size, dtype=np.float64)
        self.pos = 0
        self.filled = 0

    def push(self, value: float) -> None:
        self.buf[self.pos] = value
        self.pos = (self.pos + 1) % self.size
        self.filled = min(self.filled + 1, self.size)

    def last(self, n: int) -> np.ndarray:
        n = min(n, self.filled)
        if n <= 0:
            return np.empty(0)
        start = (self.pos - n) % self.size
        if start + n <= self.size:
            return self.buf[start:start + n]
        return np.concatenate((self.buf[start:], self.buf[:(start + n) % self.size]))

    def value_ago(self, n: int) -> float:
        """Valoarea de acum n cadre (0 = cea mai recenta)."""
        if self.filled == 0:
            return 0.0
        n = min(n, self.filled - 1)
        return float(self.buf[(self.pos - 1 - n) % self.size])

    def mean_range(self, start_ago: int, end_ago: int) -> float:
        """Media intre `start_ago` si `end_ago` cadre in urma.

        Exemplu: mean_range(240, 48) = media dintre acum 2.5 s si acum 0.5 s.
        Serveste ca REFERINTA pentru salturi: nu foloseste ultimele cadre,
        deci saltul se masoara fata de trecut, nu fata de el insusi.
        """
        if start_ago <= end_ago or self.filled == 0:
            return 0.0
        seg = self.last(min(start_ago, self.filled))
        if seg.size <= end_ago:
            return float(seg.mean()) if seg.size else 0.0
        seg = seg[:seg.size - end_ago]
        return float(seg.mean()) if seg.size else 0.0

    def max_last(self, n: int) -> float:
        seg = self.last(n)
        return float(seg.max()) if seg.size else 0.0

    def min_last(self, n: int) -> float:
        seg = self.last(n)
        return float(seg.min()) if seg.size else 0.0

    def mean_last(self, n: int) -> float:
        seg = self.last(n)
        return float(seg.mean()) if seg.size else 0.0


@dataclass
class StructureResult:
    section: str = "SILENCE"
    previous: str = "SILENCE"
    changed: bool = False
    age: float = 0.0
    confidence: float = 0.0
    drop_score: float = 0.0
    buildup_score: float = 0.0
    break_score: float = 0.0
    energy_short: float = 0.0
    energy_mid: float = 0.0
    energy_long: float = 0.0
    energy_slope: float = 0.0
    last_drop_age: float = 999.0
    events: list[str] = field(default_factory=list)


class MLSectionClassifier:
    """Invelis peste un model scikit-learn optional (joblib).

    Modelul primeste vectorul de caracteristici de mai jos si intoarce
    probabilitati pentru clasele din SECTIONS. Antrenare: tools/train_sections.py
    """

    FEATURES = ("energy_short", "energy_mid", "energy_long", "slope", "bass",
                "hf", "centroid", "flux", "onset_rate", "bass_jump", "energy_jump")

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.model = None
        self.classes: list[str] = []
        self.load()

    def load(self) -> bool:
        if not self.path.exists():
            log.info("Model ML de sectiuni inexistent (%s) - se foloseste doar euristica.",
                     self.path)
            return False
        try:
            import joblib  # import lenes: dependinta optionala
            bundle = joblib.load(self.path)
            self.model = bundle["model"] if isinstance(bundle, dict) else bundle
            self.classes = list(getattr(self.model, "classes_", []))
            log.info("Model ML de sectiuni incarcat: %s (clase: %s)", self.path, self.classes)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Nu am putut incarca modelul ML (%s): %s", self.path, exc)
            self.model = None
            return False

    def predict(self, features: dict[str, float]) -> dict[str, float]:
        if self.model is None:
            return {}
        try:
            vec = np.array([[features.get(k, 0.0) for k in self.FEATURES]], dtype=np.float64)
            proba = self.model.predict_proba(vec)[0]
            return {str(c): float(p) for c, p in zip(self.classes, proba)}
        except Exception:  # noqa: BLE001
            log.debug("Predictie ML esuata", exc_info=True)
            return {}


class StructureDetector:
    def __init__(self, cfg):
        samplerate = int(cfg.get("audio.samplerate", 48000))
        hop = int(cfg.get("audio.hop_size", 512))
        self.frame_dt = hop / samplerate
        self.frame_rate = 1.0 / self.frame_dt

        sc = cfg.get("analysis.structure", {}) or {}
        self.short_s = float(sc.get("short_s", 0.35))
        self.mid_s = float(sc.get("mid_s", 3.0))
        self.long_s = float(sc.get("long_s", 15.0))
        history_s = float(sc.get("history_s", 40.0))
        self.min_section = float(sc.get("min_section_s", 1.5))
        self.silence_db = float(cfg.get("analysis.silence_db", -55.0))

        d = sc.get("drop", {}) or {}
        self.drop_bass_jump = float(d.get("bass_jump", 0.28))
        self.drop_energy_jump = float(d.get("energy_jump", 0.22))
        self.drop_flux_mult = float(d.get("flux_mult", 1.8))
        self.drop_window = int(float(d.get("window_ms", 350.0)) / 1000.0 * self.frame_rate)
        # ferestrele folosite la masurarea salturilor
        self.jump_fast = max(2, self.drop_window)
        self.jump_ref_start = int(2.5 * self.frame_rate)
        self.jump_ref_end = int(0.5 * self.frame_rate)
        self.drop_cooldown = float(d.get("cooldown_s", 6.0))
        self.drop_context_s = float(d.get("require_context_s", 25.0))
        self.drop_threshold = float(d.get("score_threshold", 0.62))

        b = sc.get("buildup", {}) or {}
        self.buildup_frames = max(4, int(float(b.get("min_s", 2.5)) * self.frame_rate))
        self.buildup_slope_thr = float(b.get("slope_thr", 0.05))
        self.buildup_hf_thr = float(b.get("hf_slope_thr", 0.02))
        self.buildup_threshold = float(b.get("score_threshold", 0.55))
        # valorile de referinta, folosite de slider-ul de sensibilitate din UI
        self._base_drop_threshold = self.drop_threshold
        self._base_buildup_threshold = self.buildup_threshold

        br = sc.get("break", {}) or {}
        self.break_ratio = float(br.get("ratio", 0.62))
        self.break_bass_max = float(br.get("bass_max", 0.32))
        self.break_min_s = float(br.get("min_s", 1.2))

        cl = sc.get("climax", {}) or {}
        self.climax_ratio = float(cl.get("ratio", 0.88))
        self.climax_min_s = float(cl.get("min_s", 3.0))

        self.intro_s = float(sc.get("intro_s", 12.0))
        # cat asteptam dupa aparitia semnalului inainte sa declaram
        # drop/build-up/break: la pornire AGC-ul si mediile inca urca, iar
        # orice inceput de piesa ar arata ca un "drop" urias
        self.warmup_s = float(sc.get("warmup_s", 5.0))
        # cat trebuie sa fie sub prag ca sa fie considerata LINISTE reala
        self.silence_hold = float(sc.get("silence_hold_s", 1.5))
        ou = sc.get("outro", {}) or {}
        self.outro_decline_s = float(ou.get("decline_s", 20.0))
        self.outro_level_max = float(ou.get("level_max", 0.30))

        # --- EMA-uri ---
        self.k_short = 1.0 - np.exp(-self.frame_dt / self.short_s)
        self.k_mid = 1.0 - np.exp(-self.frame_dt / self.mid_s)
        self.k_long = 1.0 - np.exp(-self.frame_dt / self.long_s)
        self.e_short = 0.0
        self.e_mid = 0.0
        self.e_long = 0.0

        # --- istoric ---
        n_hist = int(history_s * self.frame_rate)
        self.h_energy = Ring1D(n_hist)
        self.h_bass = Ring1D(n_hist)
        self.h_hf = Ring1D(n_hist)
        self.h_centroid = Ring1D(n_hist)
        self.h_onset_rate = Ring1D(n_hist)

        # regresie liniara pre-calculata (x fix, doar y se schimba)
        self._x = np.arange(self.buildup_frames, dtype=np.float64) * self.frame_dt
        self._x_mean = self._x.mean()
        self._x_var = float(np.sum((self._x - self._x_mean) ** 2)) or 1.0

        # --- stare ---
        self.section = "SILENCE"
        self.previous = "SILENCE"
        self.section_start = 0.0
        self.last_drop_t = -999.0
        self.last_buildup_t = -999.0
        self.last_break_t = -999.0
        self.audio_start_t = 0.0
        self.had_signal = False
        self._quiet_since = 0.0        # de cand e sub pragul de liniste
        self._signal_lost_t = 0.0      # cand s-a pierdut ultima oara semnalul
        self.confidence = 0.0
        self._break_since = 0.0
        self._climax_since = 0.0

        # --- ML optional ---
        ml = sc.get("ml", {}) or {}
        self.ml_weight = float(ml.get("weight", 0.5))
        self.ml: MLSectionClassifier | None = None
        if ml.get("enabled", False):
            self.ml = MLSectionClassifier(ml.get("model_path", "config/section_model.joblib"))

    # ------------------------------------------------------------------
    def process(self, t: float, loudness: float, rms_db: float, bands: np.ndarray,
                flux_norm: float, centroid_norm: float, onset_rate: float) -> StructureResult:
        bass = float((bands[0] + bands[1]) * 0.5)
        hf = float((bands[4] + bands[5]) * 0.5)

        # --- EMA-uri de energie ---
        self.e_short += self.k_short * (loudness - self.e_short)
        self.e_mid += self.k_mid * (loudness - self.e_mid)
        self.e_long += self.k_long * (loudness - self.e_long)

        self.h_energy.push(self.e_short)
        self.h_bass.push(bass)
        self.h_hf.push(hf)
        self.h_centroid.push(centroid_norm)
        self.h_onset_rate.push(onset_rate)

        result = StructureResult(
            section=self.section, previous=self.previous,
            energy_short=self.e_short, energy_mid=self.e_mid, energy_long=self.e_long,
            last_drop_age=t - self.last_drop_t,
        )

        # --- liniste, cu histerezis in TIMP ---
        # RMS-ul se calculeaza la fiecare 10.7 ms. Intr-o piesa exista mereu
        # pauze scurte (intre kick-uri, taieturi de productie) care coboara
        # sub prag pentru cateva zeci de ms. Fara conditia de durata, fiecare
        # astfel de pauza declara LINISTE -> reset de lumini -> INTRO -> din
        # nou perioada de incalzire. Masurat pe o piesa reala: 4 falsuri in
        # 68 de secunde, cu energia la 63-67%.
        if rms_db < self.silence_db:
            if self._quiet_since <= 0.0:
                self._quiet_since = t
            silent = (t - self._quiet_since) >= self.silence_hold
        else:
            self._quiet_since = 0.0
            silent = False

        if silent:
            if self.section != "SILENCE" and (t - self.section_start) > 1.0:
                self._change("SILENCE", t, result)
            if self.had_signal:
                self._signal_lost_t = t
            self.had_signal = False
            result.section = self.section
            result.age = t - self.section_start
            return result

        if not self.had_signal:
            self.had_signal = True
            # Dupa o pauza SCURTA (intre doua piese) nu are rost sa reluam
            # toata incalzirea de 5 s: AGC-ul si mediile sunt inca valide.
            gap = t - self._signal_lost_t if self._signal_lost_t > 0 else 999.0
            self.audio_start_t = t if gap > 10.0 else t - max(0.0, self.warmup_s - 1.5)
            if self.section == "SILENCE":
                self._change("INTRO", t, result)

        # --- panta energiei (regresie liniara pe fereastra de build-up) ---
        y = self.h_energy.last(self.buildup_frames)
        if y.size >= 4:
            x = self._x[-y.size:]
            xm = x.mean()
            slope = float(np.sum((x - xm) * (y - y.mean())) / max(np.sum((x - xm) ** 2), 1e-9))
        else:
            slope = 0.0
        result.energy_slope = slope

        yc = self.h_centroid.last(self.buildup_frames)
        if yc.size >= 4:
            x = self._x[-yc.size:]
            xm = x.mean()
            hf_slope = float(np.sum((x - xm) * (yc - yc.mean())) / max(np.sum((x - xm) ** 2), 1e-9))
        else:
            hf_slope = 0.0

        # --- salturi (drop) ---
        # Nu se compara cu minimul de pe 350 ms: la 128 BPM kick-ul singur
        # produce oscilatii mari intre doua beat-uri si ar da "drop" la
        # fiecare kick. Se compara media ultimelor ~300 ms cu media dintre
        # acum 2.5 s si acum 0.5 s (trecutul imediat, fara prezent).
        bass_fast = self.h_bass.mean_last(self.jump_fast)
        bass_ref = self.h_bass.mean_range(self.jump_ref_start, self.jump_ref_end)
        bass_jump = bass_fast - bass_ref
        energy_fast = self.h_energy.mean_last(self.jump_fast)
        energy_ref = self.h_energy.mean_range(self.jump_ref_start, self.jump_ref_end)
        energy_jump = energy_fast - energy_ref

        # ============ SCORURI ============
        # BUILD-UP: energie in crestere, spectru care urca, densitate mai mare
        onset_now = self.h_onset_rate.mean_last(int(1.5 * self.frame_rate))
        onset_before = self.h_onset_rate.mean_last(int(6 * self.frame_rate))
        onset_growth = (onset_now - onset_before) / max(onset_before, 0.5)
        s_slope = float(np.clip(slope / max(self.buildup_slope_thr, 1e-6), 0.0, 1.0))
        s_hf = float(np.clip(hf_slope / max(self.buildup_hf_thr, 1e-6), 0.0, 1.0))
        s_dens = float(np.clip(onset_growth, 0.0, 1.0))
        s_nobass = float(np.clip(1.0 - bass / 0.6, 0.0, 1.0))
        buildup_score = 0.45 * s_slope + 0.25 * s_hf + 0.18 * s_dens + 0.12 * s_nobass
        result.buildup_score = buildup_score

        # DROP: bass + energie care sar brusc, textura care se schimba
        ctx = 0.0
        if (t - self.last_buildup_t) < self.drop_context_s:
            ctx = 1.0
        elif (t - self.last_break_t) < self.drop_context_s:
            ctx = 0.8
        d_bass = float(np.clip(bass_jump / max(self.drop_bass_jump, 1e-6), 0.0, 1.0))
        d_energy = float(np.clip(energy_jump / max(self.drop_energy_jump, 1e-6), 0.0, 1.0))
        d_flux = float(np.clip(flux_norm / max(self.drop_flux_mult, 1e-6), 0.0, 1.0))
        drop_score = 0.38 * d_bass + 0.30 * d_energy + 0.17 * d_flux + 0.15 * ctx
        # Un drop este obligatoriu TARE si cu bass: filtru absolut. Fara el,
        # un riser de build-up (energie in crestere, fara bass) ar fi luat
        # drept drop.
        if self.e_short < 0.50 or bass < 0.55:
            drop_score *= 0.25
        result.drop_score = drop_score

        # BREAK: energie mult sub referinta + bass slab.
        # Referinta este maximul dintre media pe 3 s si cea pe 15 s: daca s-ar
        # folosi doar cea pe 3 s, ea coboara odata cu break-ul si dupa ~1 s
        # break-ul "nu mai exista". Contextul lung tine minte cat de tare era
        # piesa inainte.
        break_score = 0.0
        reference = max(self.e_mid, self.e_long)
        if reference > 1e-3:
            ratio = self.e_short / max(reference, 1e-6)
            if ratio < self.break_ratio and bass < self.break_bass_max:
                # conditia e indeplinita -> scorul porneste de la 0.55 si
                # creste cu cat energia e mai jos si bass-ul mai absent
                depth = float(np.clip((self.break_ratio - ratio)
                                      / max(self.break_ratio, 1e-6) * 2.5, 0.0, 1.0))
                bass_ok = float(np.clip((self.break_bass_max - bass)
                                        / max(self.break_bass_max, 1e-6), 0.0, 1.0))
                break_score = 0.55 + 0.30 * depth + 0.15 * bass_ok
        result.break_score = break_score

        # CLIMAX: energie sustinuta aproape de maximul recent
        recent_max = self.h_energy.max_last(int(30 * self.frame_rate))
        climax_level = self.e_short >= self.climax_ratio * max(recent_max, 1e-6) and self.e_short > 0.55

        # --- amestec optional cu modelul ML ---
        ml_proba: dict[str, float] = {}
        if self.ml is not None and self.ml.model is not None:
            ml_proba = self.ml.predict({
                "energy_short": self.e_short, "energy_mid": self.e_mid,
                "energy_long": self.e_long, "slope": slope, "bass": bass, "hf": hf,
                "centroid": centroid_norm, "flux": flux_norm, "onset_rate": onset_rate,
                "bass_jump": bass_jump, "energy_jump": energy_jump,
            })
            if ml_proba:
                w = self.ml_weight
                drop_score = (1 - w) * drop_score + w * ml_proba.get("DROP", 0.0)
                buildup_score = (1 - w) * buildup_score + w * ml_proba.get("BUILDUP", 0.0)
                break_score = (1 - w) * break_score + w * ml_proba.get("BREAK", 0.0)
                result.drop_score = drop_score
                result.buildup_score = buildup_score
                result.break_score = break_score

        # ============ MASINA DE STARI ============
        age = t - self.section_start
        can_change = age >= self.min_section
        warm = (t - self.audio_start_t) >= self.warmup_s
        if not warm:
            # in perioada de incalzire ramanem in INTRO si doar acumulam
            result.section = self.section
            result.age = age
            result.confidence = self.confidence
            return result

        # 1. DROP - are prioritate absoluta, cu cooldown propriu
        if (drop_score >= self.drop_threshold
                and (t - self.last_drop_t) >= self.drop_cooldown
                and self.section != "DROP"):
            self.last_drop_t = t
            self._change("DROP", t, result)
            self.confidence = drop_score
            result.events.append("DROP")

        # 2. iesirea din DROP dupa ~4 secunde
        elif self.section == "DROP" and age > 4.0:
            if climax_level:
                self._change("CLIMAX", t, result)
                self._climax_since = t
            else:
                self._change("GROOVE", t, result)

        # 3. BREAK
        elif (break_score > 0.5 and can_change and self.section not in ("BREAK", "DROP")):
            if self._break_since == 0.0:
                self._break_since = t
            if (t - self._break_since) >= self.break_min_s:
                self.last_break_t = t
                self._change("BREAK", t, result)
                self.confidence = break_score
                self._break_since = 0.0

        # 4. BUILD-UP
        elif (buildup_score >= self.buildup_threshold and can_change
              and self.section not in ("BUILDUP", "DROP")):
            self.last_buildup_t = t
            self._change("BUILDUP", t, result)
            self.confidence = buildup_score

        # 5. CLIMAX sustinut
        elif (climax_level and can_change and self.section == "GROOVE"
              and (t - self.last_drop_t) < 60.0):
            if self._climax_since == 0.0:
                self._climax_since = t
            elif (t - self._climax_since) >= self.climax_min_s:
                self._change("CLIMAX", t, result)

        # 6. INTRO -> GROOVE
        elif self.section == "INTRO" and (t - self.audio_start_t) > self.intro_s:
            self._change("GROOVE", t, result)

        # 7. iesirea din BREAK / BUILDUP / CLIMAX cand conditia dispare
        elif self.section == "BREAK" and break_score < 0.25 and can_change:
            self._change("GROOVE", t, result)
        elif self.section == "BUILDUP" and buildup_score < self.buildup_threshold * 0.6 and can_change:
            self._change("GROOVE", t, result)
        elif self.section == "CLIMAX" and not climax_level and age > self.climax_min_s:
            self._change("GROOVE", t, result)

        # 8. OUTRO: declin lung si nivel mic
        elif can_change and self.section not in ("OUTRO", "INTRO"):
            long_y = self.h_energy.last(int(self.outro_decline_s * self.frame_rate))
            if long_y.size > int(self.outro_decline_s * self.frame_rate * 0.9):
                first_half = float(long_y[:long_y.size // 2].mean())
                second_half = float(long_y[long_y.size // 2:].mean())
                if second_half < self.outro_level_max and second_half < 0.6 * first_half:
                    self._change("OUTRO", t, result)

        # Contoarele de "de cand tine conditia" se reseteaza cand CONDITIA
        # dispare, nu cand se schimba sectiunea (altfel nu s-ar acumula
        # niciodata pana la min_s si BREAK-ul nu ar fi declarat niciodata).
        if break_score <= 0.5:
            self._break_since = 0.0
        if not climax_level:
            self._climax_since = 0.0

        result.section = self.section
        result.previous = self.previous
        result.age = t - self.section_start
        result.confidence = self.confidence
        result.last_drop_age = t - self.last_drop_t
        return result

    # ------------------------------------------------------------------
    def _change(self, new_section: str, t: float, result: StructureResult) -> None:
        if new_section == self.section:
            return
        self.previous = self.section
        self.section = new_section
        self.section_start = t
        result.changed = True
        result.section = new_section
        result.previous = self.previous
        if new_section not in result.events:
            result.events.append(new_section)
        # INFO, nu DEBUG: fara asta nu se poate diagnostica nimic dupa un set
        log.info("SECTIUNE %-8s -> %-8s  drop=%.3f build=%.3f energie=%.0f%%",
                 self.previous, new_section, result.drop_score,
                 result.buildup_score, self.e_short * 100)

    def set_sensitivity(self, value: float) -> None:
        """Sensibilitatea globala din UI (0.5 = lenes, 2.0 = foarte reactiv).

        Scade pragurile de scor pentru drop/build-up si pragul de energie
        al break-ului, proportional cu valoarea primita.
        """
        v = float(np.clip(value, 0.3, 3.0))
        self.drop_threshold = float(np.clip(self._base_drop_threshold / v, 0.15, 0.98))
        self.buildup_threshold = float(np.clip(self._base_buildup_threshold / v, 0.15, 0.98))

    def force_section(self, name: str) -> None:
        """Suprascriere manuala din UI (buton de test)."""
        if name in SECTIONS:
            self.previous = self.section
            self.section = name
            self.section_start = time.monotonic()

    def reset(self) -> None:
        self.e_short = self.e_mid = self.e_long = 0.0
        self.section = "SILENCE"
        self.previous = "SILENCE"
        self.had_signal = False
        self._quiet_since = 0.0
        self.last_drop_t = -999.0
        self.last_buildup_t = -999.0
        self.last_break_t = -999.0
