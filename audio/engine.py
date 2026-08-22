"""
audio/engine.py
===============
Firul de analiza: leaga captura de toate modulele DSP si publica
rezultatele in starea partajata + pe busul de evenimente.

           +-------------+   hop=512      +------------------+
  audio -> | AudioCapture| -------------> | SpectrumAnalyzer |
           +-------------+                +--------+---------+
                                                   |
                    +------------------------------+------------------+
                    |               |              |                  |
                    v               v              v                  v
              OnsetDetector   TempoEstimator  StructureDetector   (nivel/benzi)
                    |               |              |                  |
                    +-------+-------+              |                  |
                            v                      |                  |
                      BeatTracker  ----------------+------------------+
                                                   |
                                       Snapshot -> SharedState (UI)
                                       Events   -> EventBus (reguli)

Ruleaza cu prioritate ridicata si fara alocari inutile. La 48 kHz / hop
512 avem 93.75 cadre/s, adica un buget de 10.6 ms per cadru; consumul
real este de ordinul a 0.3-0.8 ms (vezi indicatorul CPU din UI).
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time

import numpy as np

from audio.beat import BeatTracker
from audio.bpm import TempoEstimator
from audio.capture import AudioCapture
from audio.onset import OnsetDetector
from audio.spectrum import SpectrumAnalyzer
from audio.structure import StructureDetector
from core.bus import EventBus, EventType
from core.state import SharedState, Snapshot

log = logging.getLogger(__name__)

# harta sectiune -> tip de eveniment
SECTION_EVENTS = {
    "INTRO": EventType.INTRO,
    "BUILDUP": EventType.BUILDUP,
    "DROP": EventType.DROP,
    "BREAK": EventType.BREAK,
    "CLIMAX": EventType.CLIMAX,
    "OUTRO": EventType.OUTRO,
    "GROOVE": EventType.GROOVE,
}


def _boost_thread_priority() -> None:
    """Prioritate 'above normal' pentru firul de analiza (Windows)."""
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetCurrentThread()
        kernel32.SetThreadPriority(handle, 1)  # THREAD_PRIORITY_ABOVE_NORMAL
    except Exception:  # noqa: BLE001 - pe alte platforme pur si simplu ignoram
        pass


class AnalysisEngine(threading.Thread):
    def __init__(self, cfg, state: SharedState, bus: EventBus, capture=None):
        super().__init__(name="AnalysisEngine", daemon=True)
        self.cfg = cfg
        self.state = state
        self.bus = bus
        self.capture = capture if capture is not None else AudioCapture(cfg, self._on_capture_error)

        self.samplerate = int(cfg.get("audio.samplerate", 48000))
        self.hop = int(cfg.get("audio.hop_size", 512))
        self.frame_dt = self.hop / self.samplerate
        self.silence_db = float(cfg.get("analysis.silence_db", -55.0))

        self.spectrum = SpectrumAnalyzer(cfg)
        self.onset = OnsetDetector(cfg)
        self.tempo = TempoEstimator(cfg)
        self.beat = BeatTracker(cfg)
        self.structure = StructureDetector(cfg)

        self._stop = threading.Event()
        self._paused = threading.Event()
        self.frame_index = 0
        self.fps = 0.0
        self.cpu_load = 0.0
        self._fps_t0 = 0.0
        self._fps_frames = 0
        self._proc_time_acc = 0.0
        self.last_error = ""
        self.started_ok = False
        self._last_bpm_change = -999.0

    # ------------------------------------------------------------------
    def _on_capture_error(self, source: str, message: str) -> None:
        self.bus.emit(EventType.AUDIO_ERROR, source=source, message=message)

    # ------------------------------------------------------------------
    def run(self) -> None:
        _boost_thread_priority()
        try:
            self.capture.start()
            self.started_ok = True
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            log.error("Captura audio nu a putut porni: %s", exc)
            self.bus.emit(EventType.AUDIO_ERROR, source="capture", message=str(exc))
            return

        for warning in getattr(self.capture, "warnings", []):
            self.bus.emit(EventType.LOG, level="WARNING", message=warning)

        log.info("Motor de analiza pornit (%.2f cadre/s, latenta captura %.1f ms)",
                 1.0 / self.frame_dt, self.capture.latency_ms())
        self._fps_t0 = time.monotonic()
        was_silent = True

        while not self._stop.is_set():
            frame = self.capture.read()
            if frame is None:
                # nu exista date noi: dormim o fractiune de hop (nu ardem CPU)
                time.sleep(self.frame_dt * 0.25)
                continue
            if self._paused.is_set():
                continue

            t0 = time.perf_counter()
            t = time.monotonic()

            # ---------- 1. spectru ----------
            sf = self.spectrum.process(frame)

            # ---------- 2. onset ----------
            # se foloseste fluxul NORMALIZAT (flux / media mobila): pragurile
            # devin independente de volum. In liniste nu emitem onset-uri,
            # altfel raportul de normalizare amplifica zgomotul de fond.
            silent_now = sf.rms_db < self.silence_db
            is_onset, onset_strength = self.onset.process(
                0.0 if silent_now else sf.flux_norm, t)
            if silent_now:
                is_onset = False
            onset_rate = self.onset.onset_rate(t)

            # ---------- 3. tempo ----------
            self.tempo.push(0.0 if silent_now else sf.flux_norm)
            bpm_changed = self.tempo.update(t)
            if bpm_changed:
                self._last_bpm_change = t
                log.info("BPM %.1f (incredere %.0f%%)",
                         self.tempo.bpm, self.tempo.confidence * 100)

            # ---------- 4. beat ----------
            self.beat.set_tempo(self.tempo.bpm, self.tempo.confidence)
            binfo = self.beat.process(t, is_onset, onset_strength, float(sf.bands[1]))

            # ---------- 5. structura ----------
            st = self.structure.process(
                t=t, loudness=sf.loudness, rms_db=sf.rms_db, bands=sf.bands,
                flux_norm=sf.flux_norm, centroid_norm=sf.centroid_norm,
                onset_rate=onset_rate,
            )

            # ---------- 6. snapshot ----------
            silent = st.section == "SILENCE"
            snapshot = Snapshot(
                t=t, frame=self.frame_index,
                rms=sf.rms, rms_db=sf.rms_db, peak=sf.peak, loudness=sf.loudness,
                bands=tuple(float(x) for x in sf.bands),
                bands_slow=tuple(float(x) for x in sf.bands_slow),
                bands_db=tuple(float(x) for x in sf.bands_db),
                centroid_hz=sf.centroid_hz, centroid_norm=sf.centroid_norm,
                rolloff_hz=sf.rolloff_hz, flatness=sf.flatness,
                flux=sf.flux, flux_norm=sf.flux_norm,
                bpm=self.tempo.bpm, bpm_confidence=self.tempo.confidence,
                bpm_age=t - self._last_bpm_change,
                beat=binfo.beat, downbeat=binfo.downbeat, beat_index=binfo.index,
                beat_in_bar=binfo.in_bar, bar_index=binfo.bar, beat_phase=binfo.phase,
                time_to_beat=binfo.time_to_beat,
                onset=is_onset, onset_strength=onset_strength, onset_rate=onset_rate,
                section=st.section, section_age=st.age, section_confidence=st.confidence,
                drop_score=st.drop_score, buildup_score=st.buildup_score,
                energy_short=st.energy_short, energy_mid=st.energy_mid,
                energy_long=st.energy_long, energy_slope=st.energy_slope,
                last_drop_age=st.last_drop_age, silence=silent,
                latency_ms=self.capture.latency_ms(),
                analysis_fps=self.fps, cpu_load=self.cpu_load,
                dropped_blocks=self.capture.total_overflows(),
            )
            self.state.update(snapshot, sf.spectro_col, frame)

            # ---------- 7. evenimente ----------
            if is_onset:
                self.bus.emit(EventType.ONSET, strength=onset_strength, t=t)
            if binfo.beat:
                self.bus.emit(EventType.BEAT, index=binfo.index, in_bar=binfo.in_bar,
                              bpm=self.tempo.bpm, t=t)
                if binfo.downbeat:
                    self.bus.emit(EventType.DOWNBEAT, bar=binfo.bar, bpm=self.tempo.bpm, t=t)
            if bpm_changed:
                self.bus.emit(EventType.BPM_CHANGE, bpm=self.tempo.bpm,
                              confidence=self.tempo.confidence)
            if st.changed:
                self.bus.emit(EventType.SECTION_CHANGE, section=st.section,
                              previous=st.previous, confidence=st.confidence)
                ev = SECTION_EVENTS.get(st.section)
                if ev is not None:
                    self.bus.emit(ev, confidence=st.confidence,
                                  drop_score=st.drop_score, buildup_score=st.buildup_score)
            if silent and not was_silent:
                self.bus.emit(EventType.SILENCE)
            elif not silent and was_silent:
                self.bus.emit(EventType.SIGNAL)
            was_silent = silent

            # ---------- 8. metrici ----------
            self.frame_index += 1
            self._proc_time_acc += time.perf_counter() - t0
            self._fps_frames += 1
            elapsed = t - self._fps_t0
            if elapsed >= 0.5:
                self.fps = self._fps_frames / elapsed
                self.cpu_load = (self._proc_time_acc / max(elapsed, 1e-6))
                self._fps_frames = 0
                self._proc_time_acc = 0.0
                self._fps_t0 = t

        self.capture.stop()
        log.info("Motor de analiza oprit dupa %d cadre.", self.frame_index)

    # ------------------------------------------------------------------
    #  Comenzi din UI
    # ------------------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()

    def pause(self, value: bool = True) -> None:
        if value:
            self._paused.set()
        else:
            self._paused.clear()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def tap_tempo(self) -> float | None:
        return self.tempo.tap(time.monotonic())

    def resync_beat(self) -> None:
        self.beat.resync(time.monotonic())

    def set_manual_bpm(self, bpm: float | None) -> None:
        self.tempo.set_manual(bpm)

    def reset_analysis(self) -> None:
        self.spectrum.reset()
        self.onset.reset()
        self.tempo.reset()
        self.beat.reset()
        self.structure.reset()

    def force_section(self, name: str) -> None:
        self.structure.force_section(name)

    def source_summary(self) -> str:
        stats = self.capture.stats()
        if not stats:
            return "fara surse"
        parts = []
        for name, s in stats.items():
            parts.append(f"{name}: {'OK' if s.active else 'STOP'} "
                         f"({s.stream_latency_ms:.0f} ms)")
        return " | ".join(parts)
