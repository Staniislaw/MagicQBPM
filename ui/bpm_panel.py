"""
ui/bpm_panel.py
===============
Panou separat, mic, cu doua lucruri:

  * BPM-ul detectat din muzica, mare si vizibil de la distanta
  * combinatii de culori alese, aplicate DOAR cand apesi tu pe ele

NU face NIMIC automat. Nu schimba culori singur, nu urmareste masurile,
nu reactioneaza la muzica. Singurele lucruri care pleaca spre MagicQ sunt
cele pe care le apesi: o combinatie de culori, sau butonul de BPM.

Se deschide din interfata principala (butonul "PANOU BPM") sau singur:

    py -3.12 main.py --panel --rules config/rules_execute.json

Combinatiile vin din config/palettes.json - nu sunt aleatoare, ci alese
cromatic (analog / complement / triadic). Fiecare are TREI culori:

    cap1 = IntHybrid140SR   cap2 = IntBeamQ60   par = SlimPARProPix

Aplicarea inseamna 6 click-uri: grup + culoare, de trei ori.

Panoul trimite comenzi cu "force", deci functioneaza si cand aplicatia e
pe MANUAL: e o unealta manuala, nu un automatism.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import numpy as np
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QBrush
from PyQt6.QtWidgets import (QFrame, QGridLayout, QLabel, QMainWindow, QPushButton,
                             QVBoxLayout, QWidget)

from core.state import SharedState
from ui.widgets import ACCENT, GREEN, MUTED, RED, TEXT, YELLOW

log = logging.getLogger(__name__)

PANEL_STYLE = """
QMainWindow, QWidget { background-color: #121216; color: #e4e4eb; }
QPushButton {
    background-color: #24242c; border: 1px solid #3a3a46; border-radius: 4px;
    padding: 7px 10px; color: #e4e4eb; font-weight: 600;
}
QPushButton:hover { background-color: #2f2f3a; }
QPushButton#sync { background-color: #1e3f5a; border-color: #00c8ff; }
QPushButton#sync:hover { background-color: #2a5578; }
QLabel { color: #9a9aa8; }
"""


class SchemeButton(QPushButton):
    """Buton cu trei pastile colorate: cap 1 | cap 2 | par."""

    def __init__(self, scheme: dict, parent=None):
        super().__init__(parent)
        self.scheme = scheme
        self.active = False
        self.setMinimumHeight(46)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"{scheme.get('tip', '')}\n"
                        f"cap 1 (Hybrid) : {scheme.get('cap1', '?')}\n"
                        f"cap 2 (BeamQ60): {scheme.get('cap2', '?')}\n"
                        f"par            : {scheme.get('par', '?')}")

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rgb = self.scheme.get("rgb", ["#888888"] * 3)

        # trei pastile: cap 1 | cap 2 | par
        for i, hexcol in enumerate(rgb[:3]):
            x = 8 + i * 22
            painter.setBrush(QBrush(QColor(hexcol)))
            painter.setPen(QPen(QColor(0, 0, 0, 120), 1))
            painter.drawEllipse(x, self.height() // 2 - 8, 16, 16)

        painter.setPen(TEXT if self.active else QColor(200, 200, 210))
        font = QFont("Segoe UI", 10, QFont.Weight.Bold if self.active else QFont.Weight.Normal)
        painter.setFont(font)
        painter.drawText(78, 0, self.width() - 84, self.height(),
                         int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                         self.scheme["nume"])
        painter.setPen(MUTED)
        painter.setFont(QFont("Segoe UI", 7))
        painter.drawText(78, self.height() // 2, self.width() - 84, self.height() // 2 - 2,
                         int(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft),
                         self.scheme.get("tip", ""))
        if self.active:
            painter.setPen(QPen(ACCENT, 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(1, 1, self.width() - 2, self.height() - 2, 4, 4)
        painter.end()


class BigBPM(QFrame):
    """Afisaj mare de BPM + bara de incredere + indicator de beat."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.bpm = 0.0
        self.confidence = 0.0
        self.beat_in_bar = 0
        self.last_beat = 0.0
        self.setMinimumHeight(120)

    def update_values(self, bpm, confidence, beat_in_bar, last_beat) -> None:
        self.bpm, self.confidence = bpm, confidence
        self.beat_in_bar, self.last_beat = beat_in_bar, last_beat
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(28, 28, 34)))
        painter.drawRoundedRect(0, 0, w, h, 6, 6)

        painter.setFont(QFont("Consolas", 46, QFont.Weight.Bold))
        painter.setPen(TEXT if self.confidence > 0.35 else MUTED)
        text = f"{self.bpm:.1f}" if self.bpm > 20 else "--"
        painter.drawText(0, 6, w - 12, h - 44,
                         int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), text)
        painter.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        painter.setPen(MUTED)
        painter.drawText(14, 14, 80, 24, int(Qt.AlignmentFlag.AlignLeft), "BPM")

        # bara de incredere
        painter.setBrush(QBrush(QColor(50, 50, 60)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(14, h - 30, w - 28, 6, 3, 3)
        color = GREEN if self.confidence > 0.6 else (YELLOW if self.confidence > 0.3 else RED)
        painter.setBrush(QBrush(color))
        painter.drawRoundedRect(14, h - 30, int((w - 28) * float(np.clip(self.confidence, 0, 1))),
                                6, 3, 3)
        painter.setPen(MUTED)
        painter.setFont(QFont("Segoe UI", 8))
        painter.drawText(14, h - 22, 200, 18, int(Qt.AlignmentFlag.AlignLeft),
                         f"incredere {self.confidence * 100:.0f}%")

        # 4 pastile de beat
        now = time.monotonic()
        for i in range(4):
            cx = w - 100 + i * 24
            glow = max(0.0, 1.0 - (now - self.last_beat) / 0.18) if i == self.beat_in_bar else 0.0
            base = RED if i == 0 else ACCENT
            col = QColor(base)
            col.setAlpha(int(60 + 195 * glow) if i == self.beat_in_bar else 50)
            painter.setBrush(QBrush(col))
            painter.setPen(QPen(base if i == self.beat_in_bar else QColor(60, 60, 70), 1))
            r = 7 + 2 * glow
            painter.drawEllipse(int(cx - r), int(h - 30 - r), int(2 * r), int(2 * r))
        painter.end()


class BpmColorPanel(QMainWindow):
    def __init__(self, cfg, state: SharedState, router, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.state = state
        self.router = router
        self.schemes: list[dict] = []
        self.buttons: list[SchemeButton] = []
        self.load_error = ""
        self.palettes_path: Path | None = None
        self.index = -1

        self.setWindowTitle("BPM & Culori")
        self.setStyleSheet(PANEL_STYLE)
        self.resize(360, 680)

        self._load_schemes()
        self._build()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(33)

    # ------------------------------------------------------------------
    def _load_schemes(self) -> None:
        """Incarca combinatiile de culori din config/palettes.json.

        Calea se ia din core.config.ROOT, NU relativ la fisierul asta:
        intr-un .exe, `__file__` arata spre folderul temporar de extractie,
        unde nu exista config/ - si panoul ar aparea gol, fara nicio culoare.
        """
        from core.config import ROOT as APP_ROOT

        path = Path(self.cfg.get("ui.palettes_file", "config/palettes.json"))
        if not path.is_absolute():
            path = APP_ROOT / path
        self.palettes_path = path
        self.load_error = ""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.schemes = [s for s in data.get("scheme", []) if s.get("nume")]
            if not self.schemes:
                self.load_error = f"{path.name} nu contine nicio combinatie"
        except FileNotFoundError:
            self.load_error = f"lipseste {path}"
        except Exception as exc:  # noqa: BLE001
            self.load_error = f"{path.name}: {exc}"
        if self.load_error:
            log.error("Combinatii de culori: %s", self.load_error)
            self.schemes = []

    def _build(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        self.lbl_mode = QLabel()
        self.lbl_mode.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        self.lbl_mode.setWordWrap(True)
        root.addWidget(self.lbl_mode)
        self._refresh_mode_label()

        self.w_bpm = BigBPM()
        root.addWidget(self.w_bpm)

        btn_sync = QPushButton("BPM  ->  MagicQ")
        btn_sync.setObjectName("sync")
        btn_sync.setToolTip("Trimite tap-uri in ritmul muzicii. Nu schimba altceva.")
        btn_sync.clicked.connect(self._sync_bpm)
        root.addWidget(btn_sync)

        title = QLabel("COMBINATII        cap1 | cap2 | par")
        title.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        root.addWidget(title)

        if self.schemes:
            grid = QGridLayout()
            grid.setSpacing(4)
            for i, scheme in enumerate(self.schemes):
                btn = SchemeButton(scheme)
                btn.clicked.connect(lambda _=False, n=i: self.apply_scheme(n))
                grid.addWidget(btn, i // 2, i % 2)
                self.buttons.append(btn)
            root.addLayout(grid)
        else:
            # Fara asta, panoul ar aparea pur si simplu gol si n-ai avea de
            # unde sti ca lipseste fisierul de culori.
            warn = QLabel("NU S-AU INCARCAT CULORILE\n\n"
                          f"{self.load_error}\n\n"
                          f"Cautat in:\n{self.palettes_path}")
            warn.setWordWrap(True)
            warn.setStyleSheet(f"color: {RED.name()}; padding: 12px;")
            warn.setFont(QFont("Consolas", 8))
            root.addWidget(warn)

        self.lbl_status = QLabel("gata")
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setFont(QFont("Consolas", 8))
        root.addWidget(self.lbl_status)
        root.addStretch(1)

    def _refresh_mode_label(self) -> None:
        """Spune clar daca regulile automate ating sau nu luminile."""
        rules_off = getattr(self.router, "dry_run", False)
        if rules_off:
            self.lbl_mode.setText("REGULI OPRITE - spre MagicQ pleaca doar ce apesi aici")
            self.lbl_mode.setStyleSheet(f"color: {GREEN.name()};")
        else:
            self.lbl_mode.setText("ATENTIE: regulile automate sunt PORNITE si "
                                  "schimba luminile singure. Apasa MANUAL in "
                                  "fereastra mare daca vrei doar panoul.")
            self.lbl_mode.setStyleSheet(f"color: {YELLOW.name()};")

    # ------------------------------------------------------------------
    #  Actiuni
    # ------------------------------------------------------------------
    def _exec_action(self, name: str):
        from magicq.actions import Action, ActionType
        return Action(ActionType.PALETTE,
                      {"window": "exec", "name": name, "force": True},
                      source="panou BPM & Culori", priority=1)

    def apply_scheme(self, index: int) -> None:
        """Selecteaza grupul si aplica culoarea, pentru fiecare din cele trei tinte."""
        if not (0 <= index < len(self.schemes)):
            return
        scheme = self.schemes[index]
        mouse = self.router.transports.get("mouse")
        if mouse is None or not mouse.status.connected:
            self._status("mouse-ul nu e activ - ruleaza calibrate_palettes.py exec", RED)
            return
        known = getattr(mouse, "exec_buttons", {})
        # Numele butoanelor de grup vin din configurare, nu din cod: se
        # schimba de la un show la altul (G1/G2/G3 pot fi orice).
        groups = self.cfg.get("magicq.mouse.group_buttons", {}) or {}
        steps = [(groups.get("cap1", "grup_int"), scheme["cap1"]),
                 (groups.get("cap2", "grup_beam"), scheme["cap2"]),
                 (groups.get("par", "grup_par"), scheme["par"])]
        missing = [n for pair in steps for n in pair if n not in known]
        if missing:
            self._status(f"lipsesc din exec_buttons: {', '.join(sorted(set(missing)))}", RED)
            return

        for group, colour in steps:
            self.router.send(self._exec_action(group))
            self.router.send(self._exec_action(colour))

        self.index = index
        for i, btn in enumerate(self.buttons):
            btn.active = (i == index)
            btn.update()
        self._status(f"{scheme['nume']}:  cap1 {scheme['cap1']}  |  "
                     f"cap2 {scheme['cap2']}  |  par {scheme['par']}", ACCENT)

    def _sync_bpm(self) -> None:
        from magicq.actions import Action, ActionType
        snapshot = self.state.snapshot
        if snapshot.bpm <= 20:
            self._status("nu am inca un tempo detectat", RED)
            return
        button = str(self.cfg.get("magicq.tap_button", "tap_tempo"))
        taps = int(self.cfg.get("magicq.tap_count", 8))
        period = 60.0 / snapshot.bpm
        for i in range(taps):
            params = {"window": "exec", "name": button, "force": True}
            if i:
                params["delay"] = round(i * period, 4)
            self.router.send(Action(ActionType.PALETTE, params,
                                    source="panou BPM", priority=0))
        self._status(f"{taps} tap-uri la {snapshot.bpm:.1f} BPM "
                     f"(interval {period:.3f} s)", ACCENT)

    def _status(self, text: str, color: QColor = MUTED) -> None:
        self.lbl_status.setText(text)
        self.lbl_status.setStyleSheet(f"color: {color.name()};")

    # ------------------------------------------------------------------
    def _tick(self) -> None:
        snapshot = self.state.snapshot
        graphics = self.state.graphics()
        # Singurul lucru care se actualizeaza singur este AFISAJUL de BPM.
        # Nicio comanda nu pleaca spre MagicQ fara un click al utilizatorului.
        self.w_bpm.update_values(snapshot.bpm, snapshot.bpm_confidence,
                                 snapshot.beat_in_bar, graphics["beat_flash_t"])
        self._refresh_mode_label()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.timer.stop()
        event.accept()
