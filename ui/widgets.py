"""
ui/widgets.py
=============
Widget-uri desenate manual (QPainter) pentru dashboard.

Sunt desenate direct in loc sa se foloseasca o biblioteca de grafice
(matplotlib/pyqtgraph) din doua motive: latenta (redesenare in ~1 ms) si
zero dependinte suplimentare. Toate primesc date deja pregatite de UI si
nu ating niciodata firul de analiza.
"""

from __future__ import annotations

import time

import numpy as np
from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (QBrush, QColor, QFont, QImage, QLinearGradient, QPainter,
                         QPen, QPixmap, QPolygonF)
from PyQt6.QtWidgets import QFrame, QLabel, QSizePolicy, QVBoxLayout, QWidget

# ----------------------------------------------------------------------
#  Paleta
# ----------------------------------------------------------------------
BG = QColor(18, 18, 22)
PANEL = QColor(28, 28, 34)
GRID = QColor(48, 48, 58)
TEXT = QColor(228, 228, 235)
MUTED = QColor(140, 140, 155)
ACCENT = QColor(0, 200, 255)
GREEN = QColor(60, 220, 130)
YELLOW = QColor(250, 200, 60)
ORANGE = QColor(255, 140, 40)
RED = QColor(255, 70, 70)
PURPLE = QColor(180, 100, 255)

SECTION_COLORS = {
    "SILENCE": QColor(70, 70, 80),
    "INTRO": QColor(80, 140, 220),
    "GROOVE": QColor(60, 200, 140),
    "BUILDUP": QColor(255, 170, 40),
    "DROP": QColor(255, 60, 90),
    "CLIMAX": QColor(255, 100, 200),
    "BREAK": QColor(120, 130, 255),
    "OUTRO": QColor(120, 120, 140),
    "UNKNOWN": QColor(90, 90, 100),
}

BAND_COLORS = [QColor(255, 60, 90), QColor(255, 130, 50), QColor(250, 210, 60),
               QColor(80, 220, 120), QColor(60, 190, 255), QColor(170, 120, 255)]


def _colormap_lut() -> np.ndarray:
    """LUT 256x3 tip 'inferno' pentru spectrograma."""
    stops = np.array([
        [0.00, 6, 6, 12],
        [0.15, 30, 12, 70],
        [0.35, 110, 25, 110],
        [0.55, 190, 55, 80],
        [0.75, 245, 130, 30],
        [0.90, 252, 210, 80],
        [1.00, 255, 255, 220],
    ], dtype=np.float64)
    x = np.linspace(0.0, 1.0, 256)
    lut = np.empty((256, 3), dtype=np.uint8)
    for c in range(3):
        lut[:, c] = np.interp(x, stops[:, 0], stops[:, c + 1]).astype(np.uint8)
    return lut


LUT = _colormap_lut()


class Panel(QFrame):
    """Cadru cu titlu, folosit ca fundal pentru celelalte widget-uri."""

    def __init__(self, title: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.title = title
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAutoFillBackground(False)

    def paint_background(self, painter: QPainter) -> QRectF:
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(GRID, 1))
        painter.setBrush(QBrush(PANEL))
        painter.drawRoundedRect(rect, 6, 6)
        inner = rect.adjusted(8, 8, -8, -8)
        if self.title:
            painter.setPen(MUTED)
            font = painter.font()
            font.setPointSize(8)
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(QPointF(inner.left(), inner.top() + 10), self.title.upper())
            inner = inner.adjusted(0, 18, 0, 0)
        return inner


# ======================================================================
#  BPM + beat
# ======================================================================
class BPMWidget(Panel):
    def __init__(self, parent=None):
        super().__init__("BPM", parent)
        self.bpm = 0.0
        self.confidence = 0.0
        self.beat_in_bar = 0
        self.beats_per_bar = 4
        self.last_beat = 0.0
        self.last_downbeat = 0.0
        self.locked = False
        self.setMinimumHeight(120)

    def update_values(self, bpm: float, confidence: float, beat_in_bar: int,
                      last_beat: float, last_downbeat: float, locked: bool) -> None:
        self.bpm = bpm
        self.confidence = confidence
        self.beat_in_bar = beat_in_bar
        self.last_beat = last_beat
        self.last_downbeat = last_downbeat
        self.locked = locked
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        inner = self.paint_background(painter)

        # valoarea BPM
        font = QFont("Consolas", 34, QFont.Weight.Bold)
        painter.setFont(font)
        painter.setPen(TEXT if self.confidence > 0.3 else MUTED)
        text = f"{self.bpm:5.1f}" if self.bpm > 0 else "  --"
        painter.drawText(QRectF(inner.left(), inner.top(), inner.width() * 0.62,
                                inner.height() * 0.62),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)

        # bara de incredere
        painter.setFont(QFont("Segoe UI", 8))
        painter.setPen(MUTED)
        conf_rect = QRectF(inner.left(), inner.bottom() - 26, inner.width() * 0.6, 6)
        painter.setBrush(QBrush(QColor(50, 50, 60)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(conf_rect, 3, 3)
        painter.setBrush(QBrush(GREEN if self.confidence > 0.5 else YELLOW))
        painter.drawRoundedRect(QRectF(conf_rect.left(), conf_rect.top(),
                                       conf_rect.width() * float(np.clip(self.confidence, 0, 1)),
                                       conf_rect.height()), 3, 3)
        painter.setPen(MUTED)
        painter.drawText(QPointF(inner.left(), inner.bottom() - 8),
                         f"incredere {self.confidence * 100:3.0f}%  "
                         f"{'LOCK' if self.locked else 'cauta...'}")

        # indicatorii de beat
        now = time.monotonic()
        radius = 11.0
        gap = 10.0
        total = self.beats_per_bar * (2 * radius + gap) - gap
        x0 = inner.right() - total
        cy = inner.top() + inner.height() * 0.42
        for i in range(self.beats_per_bar):
            cx = x0 + i * (2 * radius + gap) + radius
            is_current = (i == self.beat_in_bar)
            age = now - (self.last_downbeat if i == 0 else self.last_beat)
            glow = max(0.0, 1.0 - age / 0.18) if is_current else 0.0
            base = SECTION_COLORS["DROP"] if i == 0 else ACCENT
            color = QColor(base)
            color.setAlpha(int(60 + 195 * glow) if is_current else 55)
            painter.setBrush(QBrush(color))
            painter.setPen(QPen(base if is_current else GRID, 1.5))
            r = radius * (1.0 + 0.25 * glow)
            painter.drawEllipse(QPointF(cx, cy), r, r)
        painter.end()


# ======================================================================
#  Sectiune curenta
# ======================================================================
class SectionWidget(Panel):
    def __init__(self, parent=None):
        super().__init__("SECTIUNE", parent)
        self.section = "SILENCE"
        self.age = 0.0
        self.drop_score = 0.0
        self.buildup_score = 0.0
        self.drop_flash = 0.0
        self.setMinimumHeight(120)

    def update_values(self, section: str, age: float, drop_score: float,
                      buildup_score: float, drop_flash: float) -> None:
        self.section = section
        self.age = age
        self.drop_score = drop_score
        self.buildup_score = buildup_score
        self.drop_flash = drop_flash
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        inner = self.paint_background(painter)

        color = SECTION_COLORS.get(self.section, SECTION_COLORS["UNKNOWN"])
        flash_age = time.monotonic() - self.drop_flash
        if flash_age < 0.35:
            alpha = int(120 * (1.0 - flash_age / 0.35))
            painter.setBrush(QBrush(QColor(color.red(), color.green(), color.blue(), alpha)))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 6, 6)

        painter.setFont(QFont("Segoe UI", 24, QFont.Weight.Bold))
        painter.setPen(color)
        painter.drawText(QRectF(inner.left(), inner.top(), inner.width(), inner.height() * 0.55),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         self.section)

        painter.setFont(QFont("Segoe UI", 9))
        painter.setPen(MUTED)
        painter.drawText(QPointF(inner.left(), inner.bottom() - 34), f"de {self.age:5.1f} s")

        # scoruri drop / buildup
        for i, (label, value, col) in enumerate((
                ("DROP", self.drop_score, RED), ("BUILD", self.buildup_score, ORANGE))):
            y = inner.bottom() - 20 + i * 12
            painter.setPen(MUTED)
            painter.drawText(QPointF(inner.left(), y + 4), label)
            bar = QRectF(inner.left() + 42, y - 4, inner.width() - 52, 7)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor(50, 50, 60)))
            painter.drawRoundedRect(bar, 3, 3)
            painter.setBrush(QBrush(col))
            painter.drawRoundedRect(QRectF(bar.left(), bar.top(),
                                           bar.width() * float(np.clip(value, 0, 1)),
                                           bar.height()), 3, 3)
        painter.end()


# ======================================================================
#  Bara de nivel (benzi + RMS)
# ======================================================================
class MetersWidget(Panel):
    def __init__(self, labels: list[str], parent=None):
        super().__init__("BENZI DE FRECVENTA", parent)
        self.labels = labels
        self.values = np.zeros(len(labels))
        self.peaks = np.zeros(len(labels))
        self.peak_time = np.zeros(len(labels))
        self.setMinimumHeight(170)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def update_values(self, values) -> None:
        now = time.monotonic()
        self.values = np.asarray(values, dtype=np.float64)
        for i, v in enumerate(self.values):
            if v >= self.peaks[i]:
                self.peaks[i] = v
                self.peak_time[i] = now
            elif now - self.peak_time[i] > 0.6:
                self.peaks[i] = max(0.0, self.peaks[i] - 0.02)
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        inner = self.paint_background(painter)

        n = len(self.labels)
        gap = 8.0
        width = (inner.width() - gap * (n - 1)) / n
        bar_top = inner.top()
        bar_bottom = inner.bottom() - 16

        painter.setFont(QFont("Segoe UI", 8))
        for i, label in enumerate(self.labels):
            x = inner.left() + i * (width + gap)
            rect = QRectF(x, bar_top, width, bar_bottom - bar_top)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor(38, 38, 46)))
            painter.drawRoundedRect(rect, 3, 3)

            value = float(np.clip(self.values[i] if i < len(self.values) else 0, 0, 1))
            h = rect.height() * value
            color = BAND_COLORS[i % len(BAND_COLORS)]
            grad = QLinearGradient(0, rect.bottom(), 0, rect.top())
            grad.setColorAt(0.0, color.darker(150))
            grad.setColorAt(1.0, color)
            painter.setBrush(QBrush(grad))
            painter.drawRoundedRect(QRectF(rect.left(), rect.bottom() - h, rect.width(), h), 3, 3)

            # peak hold
            peak_y = rect.bottom() - rect.height() * float(np.clip(self.peaks[i], 0, 1))
            painter.setPen(QPen(color.lighter(140), 2))
            painter.drawLine(QPointF(rect.left(), peak_y), QPointF(rect.right(), peak_y))

            painter.setPen(MUTED)
            painter.drawText(QRectF(x, bar_bottom + 2, width, 14),
                             Qt.AlignmentFlag.AlignCenter, label)
            painter.setPen(TEXT)
            painter.drawText(QRectF(x, rect.top() + 2, width, 12),
                             Qt.AlignmentFlag.AlignCenter, f"{value * 100:.0f}")
        painter.end()


class LevelBar(Panel):
    """Bara orizontala simpla (RMS / loudness)."""

    def __init__(self, title: str, color: QColor = GREEN, parent=None):
        super().__init__(title, parent)
        self.value = 0.0
        self.text = ""
        self.color = color
        self.setMinimumHeight(58)
        self.setMaximumHeight(70)

    def update_value(self, value: float, text: str = "") -> None:
        self.value = float(np.clip(value, 0.0, 1.0))
        self.text = text
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        inner = self.paint_background(painter)
        bar = QRectF(inner.left(), inner.top() + 2, inner.width(), 12)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(38, 38, 46)))
        painter.drawRoundedRect(bar, 3, 3)
        col = self.color if self.value < 0.9 else RED
        painter.setBrush(QBrush(col))
        painter.drawRoundedRect(QRectF(bar.left(), bar.top(), bar.width() * self.value,
                                       bar.height()), 3, 3)
        if self.text:
            painter.setPen(MUTED)
            painter.setFont(QFont("Consolas", 8))
            painter.drawText(QPointF(inner.left(), bar.bottom() + 14), self.text)
        painter.end()


# ======================================================================
#  Spectrograma
# ======================================================================
class SpectrogramWidget(Panel):
    def __init__(self, parent=None):
        super().__init__("SPECTROGRAMA", parent)
        self._pixmap: QPixmap | None = None
        self._buffer: np.ndarray | None = None
        self.setMinimumHeight(150)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def update_data(self, data: np.ndarray) -> None:
        """`data`: matrice (bins x coloane) cu valori 0..1, bins de jos = grave."""
        if data.size == 0:
            return
        idx = np.clip((data * 255.0), 0, 255).astype(np.uint8)
        rgb = LUT[idx]                       # (bins, cols, 3)
        rgb = np.flipud(rgb)                 # gravele jos
        rgb = np.ascontiguousarray(rgb)
        self._buffer = rgb                   # pastram referinta (QImage nu copiaza)
        h, w, _ = rgb.shape
        image = QImage(rgb.data, w, h, w * 3, QImage.Format.Format_RGB888)
        self._pixmap = QPixmap.fromImage(image)
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        inner = self.paint_background(painter)
        if self._pixmap is not None:
            painter.drawPixmap(inner.toRect(), self._pixmap)
        painter.setPen(QPen(GRID, 1))
        painter.drawRect(inner)
        painter.end()


# ======================================================================
#  Waveform
# ======================================================================
class WaveformWidget(Panel):
    def __init__(self, parent=None):
        super().__init__("WAVEFORM", parent)
        self.samples = np.zeros(0, dtype=np.float32)
        self.beat_flash = 0.0
        self.setMinimumHeight(90)

    def update_data(self, samples: np.ndarray, beat_flash: float = 0.0) -> None:
        self.samples = samples
        self.beat_flash = beat_flash
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        inner = self.paint_background(painter)
        painter.setPen(QPen(GRID, 1, Qt.PenStyle.DotLine))
        mid_y = inner.center().y()
        painter.drawLine(QPointF(inner.left(), mid_y), QPointF(inner.right(), mid_y))

        n = self.samples.shape[0]
        if n > 4:
            width = int(inner.width())
            width = max(width, 2)
            # min/max pe fiecare coloana de pixeli (envelope)
            step = max(1, n // width)
            usable = (n // step) * step
            chunks = self.samples[:usable].reshape(-1, step)
            mins = chunks.min(axis=1)
            maxs = chunks.max(axis=1)
            xs = np.linspace(inner.left(), inner.right(), mins.shape[0])
            scale = inner.height() * 0.45
            poly = QPolygonF()
            for x, v in zip(xs, maxs):
                poly.append(QPointF(float(x), float(mid_y - v * scale)))
            for x, v in zip(xs[::-1], mins[::-1]):
                poly.append(QPointF(float(x), float(mid_y - v * scale)))
            flash = max(0.0, 1.0 - (time.monotonic() - self.beat_flash) / 0.15)
            color = QColor(ACCENT)
            color.setAlpha(int(150 + 105 * flash))
            painter.setPen(QPen(color, 1))
            painter.setBrush(QBrush(QColor(color.red(), color.green(), color.blue(), 70)))
            painter.drawPolygon(poly)
        painter.end()


# ======================================================================
#  Indicator de status (LED + text)
# ======================================================================
class StatusLed(QLabel):
    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        self.name = name
        self.state = "off"     # off | ok | warn | error | active
        self.detail = ""
        self.setMinimumWidth(120)
        self.setFont(QFont("Segoe UI", 8))
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(22)

    def set_state(self, state: str, detail: str = "") -> None:
        if state != self.state or detail != self.detail:
            self.state = state
            self.detail = detail
            self.setToolTip(f"{self.name}: {detail}")
            self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        colors = {"off": QColor(70, 70, 80), "ok": GREEN, "warn": YELLOW,
                  "error": RED, "active": ACCENT}
        color = colors.get(self.state, MUTED)
        painter.setBrush(QBrush(color))
        painter.setPen(QPen(color.darker(160), 1))
        painter.drawEllipse(QPointF(9, self.height() / 2), 5, 5)
        painter.setPen(TEXT if self.state != "off" else MUTED)
        painter.drawText(QRectF(20, 0, self.width() - 22, self.height()),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                         self.name)
        painter.end()


class StatWidget(Panel):
    """Cifre de diagnostic: FPS, latenta, CPU, actiuni."""

    def __init__(self, parent=None):
        super().__init__("DIAGNOSTIC", parent)
        self.rows: list[tuple[str, str, QColor]] = []
        self.setMinimumHeight(120)

    def update_rows(self, rows: list[tuple[str, str, QColor]]) -> None:
        self.rows = rows
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        inner = self.paint_background(painter)
        painter.setFont(QFont("Consolas", 9))
        y = inner.top() + 12
        for label, value, color in self.rows:
            painter.setPen(MUTED)
            painter.drawText(QPointF(inner.left(), y), label)
            painter.setPen(color)
            painter.drawText(QRectF(inner.left(), y - 11, inner.width(), 14),
                             Qt.AlignmentFlag.AlignRight, value)
            y += 15
        painter.end()
