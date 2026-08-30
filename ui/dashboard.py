"""
ui/dashboard.py
===============
Fereastra principala.

Afiseaza tot ce s-a cerut: BPM, beat, drop, sectiunea curenta, meterele
Bass/Mid/Treble (si Sub/LowMid/High), spectrograma, waveform, RMS, FPS,
latenta si statusul conexiunii cu MagicQ - plus tabelul de reguli cu
declansare manuala si un jurnal de actiuni.

UI-ul ruleaza in firul principal Qt si NU atinge niciodata direct
modulele de analiza: citeste doar snapshot-uri din SharedState si
evenimente din EventBus. Astfel, chiar daca interfata incetineste
(fereastra mutata, ecran blocat), analiza si comenzile catre MagicQ merg
mai departe fara intreruperi.
"""

from __future__ import annotations

import logging
import time

import numpy as np
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QGridLayout, QHBoxLayout, QHeaderView,
                             QLabel, QMainWindow, QPlainTextEdit, QPushButton, QSlider,
                             QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout,
                             QWidget)

from core.bus import EventBus, EventType
from core.state import BAND_LABELS, SharedState
from ui.widgets import (ACCENT, BPMWidget, GREEN, LevelBar, MUTED, MetersWidget, ORANGE,
                        RED, SectionWidget, SpectrogramWidget, StatWidget, StatusLed,
                        TEXT, WaveformWidget, YELLOW)

log = logging.getLogger(__name__)

STYLE = """
QMainWindow, QWidget { background-color: #121216; color: #e4e4eb; }
QPushButton {
    background-color: #24242c; border: 1px solid #3a3a46; border-radius: 4px;
    padding: 6px 12px; color: #e4e4eb; font-weight: 600;
}
QPushButton:hover { background-color: #2f2f3a; }
QPushButton:pressed { background-color: #1a1a20; }
QPushButton#panic { background-color: #7a1f2b; border-color: #ff4646; }
QPushButton#panic:hover { background-color: #a12b3a; }
QPushButton#auto { background-color: #1f5a3a; border-color: #3cdc82; }
QPushButton#manual { background-color: #6a5a12; border-color: #fac83c; }
QPushButton#bpmsync { background-color: #1e3f5a; border-color: #00c8ff; }
QPushButton#bpmsync:hover { background-color: #2a5578; }
QTableWidget {
    background-color: #1c1c22; gridline-color: #2e2e38; border: 1px solid #2e2e38;
    selection-background-color: #2a4a5a;
}
QHeaderView::section {
    background-color: #24242c; color: #9a9aa8; border: 0px; padding: 4px;
    font-size: 10px; font-weight: bold;
}
QPlainTextEdit {
    background-color: #16161c; border: 1px solid #2e2e38; color: #b8b8c4;
    font-family: Consolas; font-size: 11px;
}
QSlider::groove:horizontal { height: 4px; background: #2e2e38; border-radius: 2px; }
QSlider::handle:horizontal {
    background: #00c8ff; width: 12px; margin: -5px 0; border-radius: 6px;
}
QLabel { color: #9a9aa8; }
QComboBox { background: #24242c; border: 1px solid #3a3a46; padding: 4px; }
"""


class Dashboard(QMainWindow):
    def __init__(self, cfg, state: SharedState, bus: EventBus, engine, router, rule_engine):
        super().__init__()
        self.cfg = cfg
        self.state = state
        self.bus = bus
        self.engine = engine
        self.router = router
        self.rule_engine = rule_engine

        self.setWindowTitle("MagicQ Audio Reactive Controller")
        self.resize(1500, 940)
        self.setStyleSheet(STYLE)

        self._sub = bus.subscribe(maxsize=2048)
        self._log_lines = int(cfg.get("ui.log_lines", 400))
        self._rule_rows: dict[str, int] = {}
        self._last_ui_t = time.monotonic()
        self._ui_fps = 0.0
        self._panel = None

        self._build_ui()
        self._populate_rules()

        fps = max(10, int(cfg.get("ui.fps", 60)))
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(int(1000 / fps))

    # ==================================================================
    #  Constructia interfetei
    # ==================================================================
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        root.addLayout(self._build_toolbar())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_left())
        splitter.addWidget(self._build_right())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([900, 600])
        root.addWidget(splitter, 1)

    # ------------------------------------------------------------------
    def _build_toolbar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setSpacing(6)

        self.btn_run = QPushButton("PAUZA ANALIZA")
        self.btn_run.clicked.connect(self._toggle_analysis)
        bar.addWidget(self.btn_run)

        self.btn_mode = QPushButton("AUTO")
        self.btn_mode.setObjectName("auto")
        self.btn_mode.clicked.connect(self._toggle_mode)
        bar.addWidget(self.btn_mode)

        btn_panic = QPushButton("PANIC")
        btn_panic.setObjectName("panic")
        btn_panic.clicked.connect(self._panic)
        bar.addWidget(btn_panic)

        btn_tap = QPushButton("TAP")
        btn_tap.clicked.connect(lambda: self.engine.tap_tempo())
        bar.addWidget(btn_tap)

        btn_sync = QPushButton("SYNC")
        btn_sync.setToolTip("Forteaza downbeat-ul acum (intern)")
        btn_sync.clicked.connect(lambda: self.engine.resync_beat())
        bar.addWidget(btn_sync)

        self.btn_bpm_sync = QPushButton("BPM -> MagicQ")
        self.btn_bpm_sync.setObjectName("bpmsync")
        self.btn_bpm_sync.setToolTip(
            "Trimite o rafala de tap-uri pe butonul de tap tempo din MagicQ,\n"
            "in ritmul muzicii care se aude ACUM.\n"
            "Nu schimba nimic altceva - nici lumini, nici culori.\n"
            "Merge si in modul MANUAL.")
        self.btn_bpm_sync.clicked.connect(self._sync_bpm_to_magicq)
        bar.addWidget(self.btn_bpm_sync)

        btn_panel = QPushButton("PANOU BPM")
        btn_panel.setToolTip("Deschide panoul separat cu BPM si combinatii de culori")
        btn_panel.clicked.connect(self._open_panel)
        bar.addWidget(btn_panel)

        btn_reload = QPushButton("RELOAD REGULI")
        btn_reload.clicked.connect(self._reload_rules)
        bar.addWidget(btn_reload)

        bar.addSpacing(16)
        bar.addWidget(QLabel("Sensibilitate"))
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setMinimum(30)
        self.slider.setMaximum(250)
        self.slider.setValue(int(float(self.cfg.get("rules.sensitivity", 1.0)) * 100))
        self.slider.setFixedWidth(130)
        self.slider.valueChanged.connect(self._on_sensitivity)
        bar.addWidget(self.slider)
        self.lbl_sens = QLabel("1.00")
        bar.addWidget(self.lbl_sens)

        bar.addStretch(1)

        self.led_audio = StatusLed("AUDIO")
        bar.addWidget(self.led_audio)
        self.leds: dict[str, StatusLed] = {}
        for name in ("osc", "midi", "keyboard", "mouse"):
            led = StatusLed(name.upper())
            self.leds[name] = led
            bar.addWidget(led)
        return bar

    # ------------------------------------------------------------------
    def _build_left(self) -> QWidget:
        panel = QWidget()
        grid = QGridLayout(panel)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(8)

        self.w_bpm = BPMWidget()
        self.w_section = SectionWidget()
        self.w_stats = StatWidget()
        grid.addWidget(self.w_bpm, 0, 0)
        grid.addWidget(self.w_section, 0, 1)
        grid.addWidget(self.w_stats, 0, 2)

        self.w_meters = MetersWidget(list(BAND_LABELS))
        grid.addWidget(self.w_meters, 1, 0, 1, 2)

        rms_box = QWidget()
        rms_layout = QVBoxLayout(rms_box)
        rms_layout.setContentsMargins(0, 0, 0, 0)
        rms_layout.setSpacing(8)
        self.w_rms = LevelBar("RMS", GREEN)
        self.w_loud = LevelBar("ENERGIE (AGC)", ACCENT)
        self.w_bright = LevelBar("BRIGHTNESS / CENTROID", ORANGE)
        rms_layout.addWidget(self.w_rms)
        rms_layout.addWidget(self.w_loud)
        rms_layout.addWidget(self.w_bright)
        grid.addWidget(rms_box, 1, 2)

        self.w_spectro = SpectrogramWidget()
        grid.addWidget(self.w_spectro, 2, 0, 1, 3)

        self.w_wave = WaveformWidget()
        grid.addWidget(self.w_wave, 3, 0, 1, 3)

        grid.setRowStretch(2, 3)
        grid.setRowStretch(3, 1)
        grid.setColumnStretch(0, 2)
        grid.setColumnStretch(1, 2)
        grid.setColumnStretch(2, 1)
        return panel

    # ------------------------------------------------------------------
    def _build_right(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("REGULI")
        title.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        header.addWidget(title)
        header.addStretch(1)
        self.lbl_rules = QLabel("")
        header.addWidget(self.lbl_rules)
        layout.addLayout(header)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["", "Regula", "Conditie", "Actiuni", "N", ""])
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(True)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        head.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        head.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        head.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 28)
        self.table.setColumnWidth(1, 150)
        self.table.setColumnWidth(4, 40)
        self.table.setColumnWidth(5, 54)
        layout.addWidget(self.table, 3)

        test_row = QHBoxLayout()
        test_row.addWidget(QLabel("Test sectiune:"))
        self.combo_section = QComboBox()
        self.combo_section.addItems(["DROP", "BUILDUP", "BREAK", "CLIMAX", "INTRO",
                                     "OUTRO", "GROOVE"])
        test_row.addWidget(self.combo_section)
        btn_force = QPushButton("SIMULEAZA")
        btn_force.clicked.connect(self._force_section)
        test_row.addWidget(btn_force)
        test_row.addStretch(1)
        layout.addLayout(test_row)

        log_title = QLabel("JURNAL")
        log_title.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        layout.addWidget(log_title)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(self._log_lines)
        layout.addWidget(self.log_view, 2)
        return panel

    # ------------------------------------------------------------------
    def _populate_rules(self) -> None:
        rules = self.rule_engine.rules
        self.table.setRowCount(len(rules))
        self._rule_rows.clear()
        for row, rule in enumerate(rules):
            self._rule_rows[rule.name] = row

            check = QCheckBox()
            check.setChecked(rule.enabled)
            check.stateChanged.connect(
                lambda st, name=rule.name: self.rule_engine.set_rule_enabled(
                    name, st == Qt.CheckState.Checked.value))
            holder = QWidget()
            hl = QHBoxLayout(holder)
            hl.setContentsMargins(6, 0, 0, 0)
            hl.addWidget(check)
            self.table.setCellWidget(row, 0, holder)

            self.table.setItem(row, 1, self._item(rule.name, TEXT))
            self.table.setItem(row, 2, self._item(rule.condition_text(), MUTED))
            self.table.setItem(row, 3, self._item(rule.actions_text(), MUTED))
            self.table.setItem(row, 4, self._item("0", MUTED, center=True))

            btn = QPushButton("TEST")
            btn.setFixedHeight(22)
            btn.clicked.connect(lambda _=False, name=rule.name:
                                self.rule_engine.trigger_rule(name))
            self.table.setCellWidget(row, 5, btn)
            self.table.setRowHeight(row, 26)
        self.lbl_rules.setText(f"{len(rules)} incarcate")

    @staticmethod
    def _item(text: str, color: QColor, center: bool = False) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setForeground(color)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        if center:
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        return item

    # ==================================================================
    #  Bucla de actualizare
    # ==================================================================
    def _tick(self) -> None:
        now = time.monotonic()
        dt = now - self._last_ui_t
        self._last_ui_t = now
        if dt > 0:
            self._ui_fps = 0.9 * self._ui_fps + 0.1 * (1.0 / dt)

        g = self.state.graphics()
        snap = g["snapshot"]

        # --- BPM / beat ---
        self.w_bpm.update_values(snap.bpm, snap.bpm_confidence, snap.beat_in_bar,
                                 g["beat_flash_t"], g["downbeat_flash_t"],
                                 snap.bpm_confidence > 0.3 and snap.beat_index > 0)

        # --- sectiune ---
        self.w_section.update_values(snap.section, snap.section_age, snap.drop_score,
                                     snap.buildup_score, g["drop_flash_t"])

        # --- metere ---
        self.w_meters.update_values(snap.bands)
        self.w_rms.update_value(snap.rms, f"{snap.rms:.3f}  ({snap.rms_db:6.1f} dBFS)")
        self.w_loud.update_value(snap.loudness, f"{snap.loudness * 100:5.1f} %")
        self.w_bright.update_value(snap.centroid_norm, f"{snap.centroid_hz:6.0f} Hz")

        # --- grafice ---
        self.w_spectro.update_data(g["spectrogram"])
        self.w_wave.update_data(g["waveform"], g["beat_flash_t"])

        # --- diagnostic ---
        lat_color = GREEN if snap.latency_ms < 50 else (YELLOW if snap.latency_ms < 90 else RED)
        cpu_color = GREEN if snap.cpu_load < 0.5 else (YELLOW if snap.cpu_load < 0.8 else RED)
        self.w_stats.update_rows([
            ("Analiza FPS", f"{snap.analysis_fps:6.1f}", TEXT),
            ("UI FPS", f"{self._ui_fps:6.1f}", TEXT),
            ("Latenta", f"{snap.latency_ms:6.1f} ms", lat_color),
            ("CPU analiza", f"{snap.cpu_load * 100:5.1f} %", cpu_color),
            ("Onset/s", f"{snap.onset_rate:6.1f}", TEXT),
            ("Comenzi", f"{self.router.sent:6d}", TEXT),
            ("Esuate", f"{self.router.failed:6d}",
             RED if self.router.failed else MUTED),
        ])

        # --- LED-uri ---
        if snap.silence:
            self.led_audio.set_state("warn", "liniste / semnal prea slab")
        elif self.engine.paused:
            self.led_audio.set_state("off", "analiza pe pauza")
        else:
            self.led_audio.set_state("ok", self.engine.source_summary())

        status = self.router.status_map()
        for name, led in self.leds.items():
            info = status.get(name, {})
            if not info.get("connected"):
                led.set_state("off", info.get("detail", "inactiv"))
            elif info.get("errors"):
                led.set_state("warn", f"{info['detail']} ({info['errors']} erori)")
            elif time.monotonic() - info.get("last_send", 0) < 0.4:
                led.set_state("active", info.get("detail", ""))
            else:
                led.set_state("ok", info.get("detail", ""))

        self._drain_events()

    # ------------------------------------------------------------------
    def _drain_events(self) -> None:
        for event in self._sub.drain(200):
            t = event.type
            if t is EventType.RULE_FIRED:
                name = event.data.get("rule", "")
                row = self._rule_rows.get(name)
                if row is not None:
                    rule = self.rule_engine.rules[row]
                    item = self.table.item(row, 4)
                    if item:
                        item.setText(str(rule.fired_count))
                    for col in range(1, 5):
                        cell = self.table.item(row, col)
                        if cell:
                            cell.setBackground(QColor(40, 70, 90))
                self._log(f"REGULA  {name} -> {event.data.get('actions', '')}", ACCENT)
            elif t is EventType.ACTION_SENT:
                self._log(f"  ->    {event.data.get('action')} "
                          f"[{event.data.get('transport')}]", GREEN)
            elif t is EventType.ACTION_FAILED:
                self._log(f"  !!    {event.data.get('action')} - "
                          f"{event.data.get('reason')}", RED)
            elif t is EventType.SECTION_CHANGE:
                self._log(f"SECTIUNE {event.data.get('previous')} -> "
                          f"{event.data.get('section')}", ORANGE)
            elif t is EventType.DROP:
                # scorul ajuta la reglarea pragurilor din reguli
                self._log(f"DROP    scor {event.data.get('drop_score', 0):.3f}  "
                          f"(prag de declansare {self.engine.structure.drop_threshold:.2f})",
                          RED)
            elif t is EventType.BUILDUP:
                self._log(f"BUILDUP scor {event.data.get('buildup_score', 0):.3f}",
                          YELLOW)
            elif t is EventType.BPM_CHANGE:
                self._log(f"BPM     {event.data.get('bpm', 0):.1f} "
                          f"(incredere {event.data.get('confidence', 0) * 100:.0f}%)", MUTED)
            elif t is EventType.AUDIO_ERROR:
                self._log(f"AUDIO   {event.data.get('source')}: "
                          f"{event.data.get('message')}", RED)
            elif t is EventType.LOG:
                self._log(f"{event.data.get('level', 'INFO')}: "
                          f"{event.data.get('message', '')}", YELLOW)

    def _log(self, message: str, color: QColor = MUTED) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"{stamp}  {message}")

    # ==================================================================
    #  Actiuni din interfata
    # ==================================================================
    def _toggle_analysis(self) -> None:
        paused = not self.engine.paused
        self.engine.pause(paused)
        self.btn_run.setText("PORNESTE ANALIZA" if paused else "PAUZA ANALIZA")
        self._log("Analiza " + ("pusa pe pauza." if paused else "reluata."), YELLOW)

    def _toggle_mode(self) -> None:
        auto = not self.rule_engine.auto_mode
        self.rule_engine.set_auto_mode(auto)
        self.router.set_manual_mode(not auto)
        self.btn_mode.setText("AUTO" if auto else "MANUAL")
        self.btn_mode.setObjectName("auto" if auto else "manual")
        self.btn_mode.setStyleSheet("")     # forteaza re-aplicarea stilului
        self.setStyleSheet(STYLE)
        self._log("Mod " + ("AUTOMAT" if auto else "MANUAL (nu se trimite nimic)"), YELLOW)

    def _open_panel(self) -> None:
        """Panoul separat BPM & Culori (o singura instanta)."""
        from ui.bpm_panel import BpmColorPanel
        if getattr(self, "_panel", None) is None or not self._panel.isVisible():
            self._panel = BpmColorPanel(self.cfg, self.state, self.router)
            self._panel.show()
            self._log("Panou BPM & Culori deschis.", ACCENT)
        else:
            self._panel.raise_()
            self._panel.activateWindow()

    def _sync_bpm_to_magicq(self) -> None:
        """Sincronizeaza Speed Master-ul MagicQ cu BPM-ul detectat acum.

        Trimite N apasari pe butonul de tap tempo, la intervalul exact al
        beat-ului curent. NU atinge nimic altceva - nici playback-uri, nici
        culori, nici efecte. Functioneaza si in modul MANUAL, fiind o
        comanda data explicit de utilizator.
        """
        from magicq.actions import Action, ActionType

        snapshot = self.state.snapshot
        bpm = snapshot.bpm
        if bpm <= 20:
            self._log("BPM -> MagicQ: nu am inca un tempo detectat. "
                      "Da drumul la muzica si asteapta 5-10 s.", RED)
            return
        if snapshot.bpm_confidence < 0.35:
            self._log(f"BPM -> MagicQ: tempo nesigur ({bpm:.1f}, incredere "
                      f"{snapshot.bpm_confidence * 100:.0f}%). Trimit oricum.", YELLOW)

        result = self.router.tap_burst(bpm, self.router.tap_buttons(),
                                       int(self.cfg.get("magicq.tap_count", 8)))
        ok = "lipsesc" not in result and "niciun" not in result
        self._log(f"BPM -> MagicQ: {result}", ACCENT if ok else RED)

    def _panic(self) -> None:
        self.router.panic()
        self._log("PANIC - toate playback-urile eliberate.", RED)

    def _on_sensitivity(self, value: int) -> None:
        sens = value / 100.0
        self.lbl_sens.setText(f"{sens:.2f}")
        self.engine.structure.set_sensitivity(sens)

    def _force_section(self) -> None:
        name = self.combo_section.currentText()
        self.engine.force_section(name)
        event = {"DROP": EventType.DROP, "BUILDUP": EventType.BUILDUP,
                 "BREAK": EventType.BREAK, "CLIMAX": EventType.CLIMAX,
                 "INTRO": EventType.INTRO, "OUTRO": EventType.OUTRO,
                 "GROOVE": EventType.GROOVE}.get(name)
        if event:
            self.bus.emit(event, confidence=1.0, simulated=True)
        self.bus.emit(EventType.SECTION_CHANGE, section=name, previous="MANUAL",
                      confidence=1.0)

    def _reload_rules(self) -> None:
        from core.config import load_rules_file
        from core.rules import load_rules
        try:
            data = load_rules_file()
            rules = load_rules(data, self.cfg.section("rules"))
        except Exception as exc:  # noqa: BLE001
            self._log(f"Reincarcarea regulilor a esuat: {exc}", RED)
            return
        self.rule_engine.replace_rules(rules)
        self._populate_rules()
        self._log(f"Reguli reincarcate ({len(rules)}).", GREEN)

    # ------------------------------------------------------------------
    def closeEvent(self, event) -> None:  # noqa: N802
        log.info("Se inchide interfata; se opresc firele de executie.")
        try:
            self.timer.stop()
            self.rule_engine.stop()
            self.router.panic()
            self.router.stop()
            self.engine.stop()
            self._sub.close()
        except Exception:  # noqa: BLE001
            log.debug("Eroare la oprire", exc_info=True)
        event.accept()
