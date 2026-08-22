"""
magicq/mouse.py
===============
Transport prin automatizarea mouse-ului - ultima varianta, folosita doar
pentru butoane care nu au nici OSC, nici MIDI, nici scurtatura de
tastatura (de exemplu un buton anume din fereastra Execute desenata de
tine in MagicQ).

Coordonatele se definesc in settings.json:

    "magicq": {"mouse": {
        "relative_to_window": true,
        "targets": { "strobe_button": [420, 180] }
    }}

Cu `relative_to_window: true` coordonatele sunt fata de coltul din stanga
sus al ferestrei MagicQ, deci raman valide daca muti fereastra.

Cursorul este readus la pozitia initiala dupa click (`restore_cursor`),
ca sa nu incurce operatorul.
"""

from __future__ import annotations

import ctypes
import logging
import time
from ctypes import wintypes

from magicq.actions import Action, ActionType
from magicq.base import Transport
from magicq.keyboard import (INPUT, IS_WINDOWS, MOUSEINPUT, _send_inputs,
                             client_area, find_magicq_window, focus_window, user32)

log = logging.getLogger(__name__)

INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
SM_CXSCREEN = 0
SM_CYSCREEN = 1


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


def _mouse_event(flags: int, dx: int = 0, dy: int = 0) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.union.mi = MOUSEINPUT(dx=dx, dy=dy, mouseData=0, dwFlags=flags,
                              time=0, dwExtraInfo=0)
    return inp


def cursor_pos() -> tuple[int, int]:
    if not IS_WINDOWS:
        return (0, 0)
    pt = POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


def move_cursor(x: int, y: int) -> None:
    if IS_WINDOWS:
        user32.SetCursorPos(int(x), int(y))


def pixel_rgb(x: int, y: int) -> tuple[int, int, int] | None:
    """Culoarea unui pixel de pe ecran (coordonate absolute)."""
    if not IS_WINDOWS:
        return None
    hdc = user32.GetDC(None)
    if not hdc:
        return None
    try:
        value = ctypes.windll.gdi32.GetPixel(hdc, int(x), int(y))
        if value == 0xFFFFFFFF:          # CLR_INVALID
            return None
        return (value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF)
    finally:
        user32.ReleaseDC(None, hdc)


class MouseTransport(Transport):
    name = "mouse"

    def __init__(self, cfg, bus=None):
        super().__init__(cfg, bus)
        conf = cfg.get("magicq.mouse", {}) or {}
        self.enabled = bool(conf.get("enabled", False))
        self.relative = bool(conf.get("relative_to_window", True))
        self.process_name = str(conf.get("window_process", "mqqt.exe"))
        self.title_regex = str(conf.get("window_title_regex", "^MagicQ"))
        self.restore = bool(conf.get("restore_cursor", True))
        self.delay = float(conf.get("click_delay_ms", 25)) / 1000.0
        self.targets: dict[str, list[int]] = dict(conf.get("targets", {}))
        # grile de palete: {"colour": {"first":[x,y], "last":[x,y], "cols":5, "rows":5}}
        self.grids: dict[str, dict] = dict(conf.get("grids", {}))
        # butoane Execute cu nume: {"strobe": [rand, coloana], ...}
        self.exec_buttons: dict[str, list[int]] = dict(conf.get("exec_buttons", {}))
        # marimea zonei client la momentul calibrarii, ca sa detectam cand
        # fereastra MagicQ a fost redimensionata si coordonatele nu mai sunt valide
        # citirea starii butoanelor de pe ecran (activ = fundal rosu)
        self.verify_state = bool(conf.get("verify_button_state", True))
        self.active_red_margin = int(conf.get("active_red_margin", 22))
        size = conf.get("calibrated_size")
        self.calibrated_size = tuple(size) if size else None
        self.auto_scale = bool(conf.get("auto_scale", True))
        self._scale_warned = False
        self._restore_attempt = 0.0
        self.hwnd: int | None = None
        self.status.supported = (ActionType.CLICK.value, ActionType.PALETTE.value,
                                 ActionType.SELECT_GROUP.value, ActionType.CLEAR.value)

    # ------------------------------------------------------------------
    def connect(self) -> bool:
        if not self.enabled:
            self.status.detail = "dezactivat"
            return False
        if not IS_WINDOWS:
            self.status.detail = "disponibil doar pe Windows"
            return False
        self.status.available = True
        self.status.connected = True
        self.hwnd = find_magicq_window(self.process_name, self.title_regex) if self.relative else None
        grids = ", ".join(sorted(self.grids)) or "niciuna"
        self.status.detail = f"{len(self.targets)} tinte, grile: {grids}"
        return True

    def supports(self, action_type: ActionType) -> bool:
        if action_type is ActionType.CLICK:
            return True
        if action_type in (ActionType.PALETTE, ActionType.SELECT_GROUP):
            return bool(self.grids)
        if action_type is ActionType.CLEAR:
            return bool(self.targets.get("clear"))
        return False

    # ------------------------------------------------------------------
    #  Grile de palete
    # ------------------------------------------------------------------
    def grid_position(self, window: str, item: int) -> tuple[int, int] | None:
        """Pozitia casutei `item` (1-based) dintr-o fereastra de palete.

        Nu se salveaza 100 de coordonate: se retin doar centrul primei
        casute si al ultimei, plus numarul de coloane/randuri. Restul se
        interpoleaza - casutele MagicQ sunt perfect uniforme.
        """
        grid = self.grids.get(str(window).lower())
        if not grid:
            log.warning("Mouse: grila '%s' nu e calibrata "
                        "(ruleaza tools/calibrate_palettes.py)", window)
            return None
        cols = max(1, int(grid.get("cols", 5)))
        rows = max(1, int(grid.get("rows", 5)))
        index = int(item) - 1
        if index < 0 or index >= cols * rows:
            log.warning("Mouse: casuta %s%d este in afara grilei (%dx%d)",
                        window, item, cols, rows)
            return None
        first = grid.get("first")
        last = grid.get("last")
        if not first or not last:
            return None
        col, row = index % cols, index // cols
        dx = (last[0] - first[0]) / (cols - 1) if cols > 1 else 0.0
        dy = (last[1] - first[1]) / (rows - 1) if rows > 1 else 0.0
        return self.grid_position_rc(window, row + 1, col + 1)

    def grid_position_rc(self, window: str, row: int, col: int) -> tuple[int, int] | None:
        """Pozitia casutei de pe randul `row` si coloana `col` (ambele 1-based).

        Pentru fereastra Execute e mai natural decat un numar de item:
        te uiti in MagicQ si numeri randul si coloana.
        """
        grid = self.grids.get(str(window).lower())
        if not grid:
            log.warning("Mouse: grila '%s' nu e calibrata "
                        "(ruleaza tools/calibrate_palettes.py)", window)
            return None
        cols = max(1, int(grid.get("cols", 5)))
        rows = max(1, int(grid.get("rows", 5)))
        if not (1 <= col <= cols and 1 <= row <= rows):
            log.warning("Mouse: %s randul %d coloana %d este in afara grilei (%dx%d)",
                        window, row, col, rows, cols)
            return None
        first, last = grid.get("first"), grid.get("last")
        if not first or not last:
            return None
        dx = (last[0] - first[0]) / (cols - 1) if cols > 1 else 0.0
        dy = (last[1] - first[1]) / (rows - 1) if rows > 1 else 0.0
        return (int(round(first[0] + (col - 1) * dx)),
                int(round(first[1] + (row - 1) * dy)))

    def grid_pitch(self, window: str) -> tuple[float, float] | None:
        """Distanta dintre centrele a doua casute vecine."""
        grid = self.grids.get(str(window).lower())
        if not grid or not grid.get("first") or not grid.get("last"):
            return None
        cols = max(1, int(grid.get("cols", 5)))
        rows = max(1, int(grid.get("rows", 5)))
        dx = (grid["last"][0] - grid["first"][0]) / (cols - 1) if cols > 1 else 0.0
        dy = (grid["last"][1] - grid["first"][1]) / (rows - 1) if rows > 1 else 0.0
        return dx, dy

    def button_is_active(self, window: str, row: int, col: int) -> bool | None:
        """True/False daca butonul e aprins, None daca nu se poate determina.

        MagicQ deseneaza butoanele active cu fundal ROSU inchis si pe cele
        inactive cu gri foarte inchis. Se citesc cativa pixeli din partea de
        JOS a casutei (unde e doar fundal - in mijloc pot fi pastile de
        culoare la paletele de COLOUR, care ar da fals pozitiv).

        Asta rezolva problema de fond: aplicatia nu are cum sa stie ce ai
        apasat tu manual sau ce era aprins inainte sa porneasca.
        """
        if not self.verify_state:
            return None
        pos = self.grid_position_rc(window, row, col)
        pitch = self.grid_pitch(window)
        area = self._client_area()
        if pos is None or pitch is None or area is None:
            return None
        ox, oy, width, height = area
        dx, dy = pitch
        samples: list[tuple[int, int, int]] = []
        for fx in (-0.32, 0.0, 0.32):
            px = int(round(pos[0] + fx * dx))
            py = int(round(pos[1] + 0.30 * dy))
            if not (0 <= px < width and 0 <= py < height):
                continue
            rgb = pixel_rgb(ox + px, oy + py)
            if rgb:
                samples.append(rgb)
        if not samples:
            return None
        r = sum(c[0] for c in samples) / len(samples)
        g = sum(c[1] for c in samples) / len(samples)
        b = sum(c[2] for c in samples) / len(samples)
        # activ = dominanta clara de rosu fata de verde si albastru
        return (r - g) >= self.active_red_margin and (r - b) >= self.active_red_margin

    def resolve_exec(self, params: dict) -> tuple[int, int] | None:
        """Rezolva un buton Execute din nume, sau din rand/coloana."""
        name = params.get("name")
        if name:
            rc = self.exec_buttons.get(str(name))
            if not rc:
                log.warning("Mouse: butonul Execute '%s' nu e definit in "
                            "magicq.mouse.exec_buttons", name)
                return None
            return int(rc[0]), int(rc[1])
        if "row" in params and "col" in params:
            return int(params["row"]), int(params["col"])
        return None

    # ------------------------------------------------------------------
    def _send(self, action: Action) -> bool:
        p = action.params

        # ---- palete / grupuri / clear ----
        if action.type in (ActionType.PALETTE, ActionType.SELECT_GROUP):
            window = ("group" if action.type is ActionType.SELECT_GROUP
                      else str(p.get("window", "colour")).lower())
            # Paleta se aplica pe capetele SELECTATE. Daca regula spune si
            # grupul, il selectam intai (un click in fereastra GROUP).
            group = p.get("group")
            if group and action.type is ActionType.PALETTE:
                pos = self.grid_position("group", int(group))
                if pos is None:
                    return False
                if not self._click_relative(*pos):
                    return False
                time.sleep(self.delay * 2)
            if window == "exec":
                rc = self.resolve_exec(p)
                if rc is None:
                    return False
                # "ensure": butoanele Execute sunt toggle. Citim starea reala
                # de pe ecran si apasam DOAR daca trebuie schimbata. Fara
                # asta, o apasare "de stingere" pe un buton deja stins l-ar
                # aprinde, si s-ar suprapune doua efecte.
                ensure = str(p.get("ensure", "")).lower()
                if ensure in ("on", "off"):
                    active = self.button_is_active("exec", rc[0], rc[1])
                    if active is not None:
                        want = (ensure == "on")
                        if active == want:
                            log.debug("Buton '%s' deja %s - nu apas",
                                      p.get("name"), ensure)
                            return True
                pos = self.grid_position_rc("exec", rc[0], rc[1])
            else:
                pos = self.grid_position(window, int(p.get("item", 1)))
            if pos is None:
                return False
            return self._click_relative(*pos)

        if action.type is ActionType.CLEAR:
            coords = self.targets.get("clear")
            if not coords:
                log.warning("Mouse: butonul CLEAR nu e calibrat "
                            "(magicq.mouse.targets.clear)")
                return False
            return self._click_relative(int(coords[0]), int(coords[1]))

        target = p.get("target")
        if target:
            coords = self.targets.get(str(target))
            if not coords:
                self.status.detail = f"tinta '{target}' nedefinita"
                log.warning("Mouse: tinta '%s' nu exista in magicq.mouse.targets", target)
                return False
            x, y = int(coords[0]), int(coords[1])
        else:
            if "x" not in p or "y" not in p:
                return False
            x, y = int(p["x"]), int(p["y"])

        if self.relative:
            origin = self._window_origin()
            if origin is None:
                log.warning("Mouse: fereastra MagicQ nu a fost gasita pentru "
                            "coordonate relative.")
                return False
            x += origin[0]
            y += origin[1]

        return self.click(x, y, button=str(p.get("button", "left")),
                          double=bool(p.get("double", False)))

    def _click_relative(self, x: int, y: int) -> bool:
        """Click la coordonate relative la zona client a ferestrei MagicQ.

        Trei protectii inainte de a misca mouse-ul:
          1. se foloseste zona CLIENT (fara bordura/bara de titlu)
          2. daca fereastra a fost redimensionata fata de calibrare,
             coordonatele se scaleaza proportional (sau se refuza)
          3. un punct in afara ferestrei NU se apasa niciodata - altfel
             click-ul ar cadea pe desktop sau in alta aplicatie
        """
        if not self.relative:
            return self.click(x, y)

        area = self._client_area()
        if area is None:
            log.warning("Mouse: fereastra MagicQ nu a fost gasita.")
            return False
        ox, oy, width, height = area

        if self.calibrated_size and tuple(self.calibrated_size) != (width, height):
            cw, ch = self.calibrated_size
            if not self.auto_scale:
                log.error("Mouse: MagicQ a fost redimensionat (%dx%d, calibrat %dx%d). "
                          "Recalibreaza sau pune auto_scale=true.", width, height, cw, ch)
                return False
            if not self._scale_warned:
                log.warning("Mouse: MagicQ e %dx%d dar calibrarea e pentru %dx%d - "
                            "coordonatele se scaleaza. Pentru precizie, recalibreaza.",
                            width, height, cw, ch)
                self._scale_warned = True
            x = int(round(x * width / max(cw, 1)))
            y = int(round(y * height / max(ch, 1)))

        if not (0 <= x < width and 0 <= y < height):
            log.error("Mouse: punctul (%d,%d) este IN AFARA ferestrei MagicQ "
                      "(%dx%d) - click anulat. Recalibreaza paletele.",
                      x, y, width, height)
            return False

        return self.click(ox + x, oy + y)

    def _client_area(self) -> tuple[int, int, int, int] | None:
        """Zona client a ferestrei MagicQ, restaurand-o daca e minimizata.

        O fereastra minimizata raporteaza zona client 0x0, iar toate
        coordonatele ar cadea 'in afara'. Se incearca restaurarea o data
        (ShowWindow SW_RESTORE, prin focus_window) inainte de a renunta.
        """
        if not self.hwnd or not user32.IsWindow(self.hwnd):
            self.hwnd = find_magicq_window(self.process_name, self.title_regex)
        if not self.hwnd:
            return None
        area = client_area(self.hwnd)
        if area and area[2] > 0 and area[3] > 0:
            return area

        now = time.monotonic()
        if now - self._restore_attempt > 2.0:
            self._restore_attempt = now
            log.warning("MagicQ pare minimizat - incerc sa-l restaurez.")
            focus_window(self.hwnd)
            time.sleep(0.15)
            area = client_area(self.hwnd)
            if area and area[2] > 0 and area[3] > 0:
                return area
            log.error("MagicQ este minimizat sau ascuns - comenzile de mouse "
                      "nu pot fi trimise. Lasa fereastra MagicQ vizibila.")
        return None

    # ------------------------------------------------------------------
    def click(self, x: int, y: int, button: str = "left", double: bool = False) -> bool:
        if not IS_WINDOWS:
            log.info("[simulare mouse] click %d,%d", x, y)
            return True
        old = cursor_pos() if self.restore else None
        if self.hwnd:
            focus_window(self.hwnd)
        move_cursor(x, y)
        time.sleep(self.delay)

        down = MOUSEEVENTF_LEFTDOWN if button == "left" else MOUSEEVENTF_RIGHTDOWN
        up = MOUSEEVENTF_LEFTUP if button == "left" else MOUSEEVENTF_RIGHTUP
        events = [_mouse_event(down), _mouse_event(up)]
        if double:
            events += [_mouse_event(down), _mouse_event(up)]
        ok = _send_inputs(events)

        if old is not None:
            time.sleep(self.delay)
            move_cursor(*old)
        return ok

    def _window_origin(self) -> tuple[int, int] | None:
        area = self._client_area()
        return (area[0], area[1]) if area else None
