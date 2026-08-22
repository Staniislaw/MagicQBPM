"""
magicq/keyboard.py
==================
Transport prin tastatura - rezerva universala: TOT ce se poate face in
MagicQ de la tastatura poate fi automatizat, inclusiv functiile care nu
au adresa OSC.

Se foloseste `SendInput` din user32 cu SCANCODE-uri (nu coduri virtuale):
asa taste sunt vazute la fel ca cele de la o tastatura fizica, ceea ce
functioneaza si cu MagicQ pe ecran complet. Nu depinde de pywin32 (doar
ctypes), iar pywin32/pynput sunt folosite doar daca sunt disponibile.

Optional aduce fereastra MagicQ in prim-plan inainte de a trimite taste
(`magicq.keyboard.focus_window`), ca sa nu ajunga tastele in alta
aplicatie. Daca fereastra e deja activa, nu se face nimic - zero overhead.

Sintaxa secventelor (in settings.json):
    "ctrl+1"              o combinatie
    "ctrl+1 enter"        doua apasari succesive
    "1,2,enter"           idem (virgula sau spatiu)
    "type:HELLO"          scrie textul
    "wait:0.2"            pauza de 200 ms
    "{playback_digit}"    substituit cu cifra playback-ului (1..9, 0)
"""

from __future__ import annotations

import ctypes
import logging
import re
import time
from ctypes import wintypes
from typing import Any

from magicq.actions import Action, ActionType
from magicq.base import Transport

log = logging.getLogger(__name__)

IS_WINDOWS = hasattr(ctypes, "windll")

# ======================================================================
#  API Windows
# ======================================================================
if IS_WINDOWS:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
else:  # pragma: no cover - doar ca sa se poata importa modulul pe alt OS
    user32 = None
    kernel32 = None

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008
MAPVK_VK_TO_VSC = 0

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUTunion(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUTunion)]


class _RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class _POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


def enable_dpi_awareness() -> str:
    """Face procesul constient de DPI, inainte de orice API de coordonate.

    Fara asta, pe un sistem cu scalare != 100% Windows "virtualizeaza"
    coordonatele si click-urile cad langa tinta. Trebuie apelata identic in
    aplicatie SI in unealta de calibrare, altfel cele doua lucreaza in
    sisteme de coordonate diferite.
    """
    if not IS_WINDOWS:
        return "n/a"
    try:  # Windows 10 1703+: per-monitor v2
        ctx = ctypes.c_void_p(-4)
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctx):
            return "per-monitor-v2"
    except Exception:  # noqa: BLE001
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return "per-monitor"
    except Exception:  # noqa: BLE001
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        return "system"
    except Exception:  # noqa: BLE001
        return "unaware"


def client_area(hwnd: int) -> tuple[int, int, int, int] | None:
    """(origine_x, origine_y, latime, inaltime) a zonei CLIENT.

    Se foloseste zona client, nu GetWindowRect: rect-ul ferestrei include
    bordura si bara de titlu, iar grosimea lor difera intre starea normala
    si maximizata - ceea ce ar deplasa toate coordonatele calibrate.
    """
    if not IS_WINDOWS or not hwnd:
        return None
    rect = _RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    point = _POINT(0, 0)
    if not user32.ClientToScreen(hwnd, ctypes.byref(point)):
        return None
    return int(point.x), int(point.y), int(rect.right), int(rect.bottom)


# ======================================================================
#  Scancode-uri (set 1)
# ======================================================================
SCANCODES: dict[str, int] = {
    "esc": 0x01, "escape": 0x01,
    "1": 0x02, "2": 0x03, "3": 0x04, "4": 0x05, "5": 0x06,
    "6": 0x07, "7": 0x08, "8": 0x09, "9": 0x0A, "0": 0x0B,
    "-": 0x0C, "=": 0x0D, "backspace": 0x0E, "tab": 0x0F,
    "q": 0x10, "w": 0x11, "e": 0x12, "r": 0x13, "t": 0x14, "y": 0x15,
    "u": 0x16, "i": 0x17, "o": 0x18, "p": 0x19, "[": 0x1A, "]": 0x1B,
    "enter": 0x1C, "return": 0x1C,
    "ctrl": 0x1D, "lctrl": 0x1D, "control": 0x1D,
    "a": 0x1E, "s": 0x1F, "d": 0x20, "f": 0x21, "g": 0x22, "h": 0x23,
    "j": 0x24, "k": 0x25, "l": 0x26, ";": 0x27, "'": 0x28, "`": 0x29,
    "shift": 0x2A, "lshift": 0x2A, "\\": 0x2B,
    "z": 0x2C, "x": 0x2D, "c": 0x2E, "v": 0x2F, "b": 0x30, "n": 0x31,
    "m": 0x32, ",": 0x33, ".": 0x34, "/": 0x35, "rshift": 0x36,
    "alt": 0x38, "lalt": 0x38, "space": 0x39, "capslock": 0x3A,
    "f1": 0x3B, "f2": 0x3C, "f3": 0x3D, "f4": 0x3E, "f5": 0x3F, "f6": 0x40,
    "f7": 0x41, "f8": 0x42, "f9": 0x43, "f10": 0x44, "f11": 0x57, "f12": 0x58,
    "numlock": 0x45, "scrolllock": 0x46,
    "num7": 0x47, "num8": 0x48, "num9": 0x49, "minus": 0x4A,
    "num4": 0x4B, "num5": 0x4C, "num6": 0x4D, "plus": 0x4E,
    "num1": 0x4F, "num2": 0x50, "num3": 0x51, "num0": 0x52, "numdot": 0x53,
}

# taste extinse (prefix 0xE0)
EXTENDED: dict[str, int] = {
    "rctrl": 0x1D, "ralt": 0x38, "altgr": 0x38,
    "insert": 0x52, "delete": 0x53, "del": 0x53,
    "home": 0x47, "end": 0x4F, "pageup": 0x49, "pgup": 0x49,
    "pagedown": 0x51, "pgdn": 0x51,
    "up": 0x48, "down": 0x50, "left": 0x4B, "right": 0x4D,
    "numenter": 0x1C, "numdiv": 0x35, "nummul": 0x37,
    "win": 0x5B, "lwin": 0x5B, "rwin": 0x5C,
}

MODIFIERS = {"ctrl", "control", "lctrl", "rctrl", "shift", "lshift", "rshift",
             "alt", "lalt", "ralt", "altgr", "win", "lwin", "rwin"}


def _resolve_key(name: str) -> tuple[int, bool] | None:
    """(scancode, extins) pentru un nume de tasta, sau None daca nu se poate."""
    key = name.strip().lower()
    if key in EXTENDED:
        return EXTENDED[key], True
    if key in SCANCODES:
        return SCANCODES[key], False
    if len(key) == 1 and IS_WINDOWS:
        # caractere care depind de layout-ul de tastatura
        vk = user32.VkKeyScanW(ctypes.c_wchar(key))
        if vk != -1:
            scan = user32.MapVirtualKeyW(vk & 0xFF, MAPVK_VK_TO_VSC)
            if scan:
                return scan, False
    return None


# ======================================================================
#  SendInput
# ======================================================================
def _key_event(scan: int, extended: bool, keyup: bool) -> INPUT:
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_EXTENDEDKEY if extended else 0)
    if keyup:
        flags |= KEYEVENTF_KEYUP
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.union.ki = KEYBDINPUT(wVk=0, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    return inp


def _unicode_event(char: str, keyup: bool) -> INPUT:
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if keyup else 0)
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.union.ki = KEYBDINPUT(wVk=0, wScan=ord(char), dwFlags=flags, time=0, dwExtraInfo=0)
    return inp


def _send_inputs(inputs: list[INPUT]) -> bool:
    if not IS_WINDOWS or not inputs:
        return False
    array = (INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), ctypes.byref(array), ctypes.sizeof(INPUT))
    if sent != len(inputs):
        err = ctypes.get_last_error()
        log.warning("SendInput a trimis %d/%d evenimente (eroare %d)", sent, len(inputs), err)
        return False
    return True


# ======================================================================
#  Focus fereastra MagicQ
# ======================================================================
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM) \
    if IS_WINDOWS else None


def find_window(title_regex: str) -> int | None:
    """Primul handle de fereastra vizibila al carei titlu se potriveste."""
    if not IS_WINDOWS:
        return None
    pattern = re.compile(title_regex, re.I)
    found: list[int] = []

    def callback(hwnd, _lparam):  # noqa: ANN001
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if pattern.search(buf.value):
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return found[0] if found else None


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _process_name_of_window(hwnd: int) -> str:
    """Numele executabilului care detine fereastra (ex: 'mqqt.exe')."""
    if not IS_WINDOWS:
        return ""
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1]
    finally:
        kernel32.CloseHandle(handle)
    return ""


#: ferestre secundare ale MagicQ care NU trebuie folosite ca referinta
SECONDARY_WINDOW_HINTS = ("visualiser", "visualizer", "magicvis", "magichd",
                          "media player", "remote")


def list_process_windows(exe_name: str) -> list[tuple[int, str, tuple[int, int, int, int]]]:
    """Toate ferestrele vizibile cu titlu ale unui proces: (hwnd, titlu, rect)."""
    if not IS_WINDOWS or not exe_name:
        return []
    needle = exe_name.lower()
    found: list[tuple[int, str, tuple[int, int, int, int]]] = []

    def callback(hwnd, _lparam):  # noqa: ANN001
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        if _process_name_of_window(hwnd).lower() != needle:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        rect = _RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        found.append((hwnd, buf.value,
                      (rect.left, rect.top, rect.right - rect.left,
                       rect.bottom - rect.top)))
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return found


def find_window_by_process(exe_name: str) -> int | None:
    """Fereastra vizibila a unui proces dat.

    Mai sigur decat cautarea dupa titlu: un Explorer deschis in D:\\MAGICQ sau
    un terminal in D:\\PYTHONMAGICQ se potrivesc cu aproape orice regex pe
    titlu, iar tastele ar ajunge in fereastra gresita.
    """
    if not IS_WINDOWS or not exe_name:
        return None
    needle = exe_name.lower()
    found: list[int] = []

    def callback(hwnd, _lparam):  # noqa: ANN001
        if not user32.IsWindowVisible(hwnd):
            return True
        if user32.GetWindowTextLengthW(hwnd) <= 0:
            return True          # ferestre ascunse / fara titlu
        if _process_name_of_window(hwnd).lower() == needle:
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return found[0] if found else None


def find_magicq_window(process_name: str, title_regex: str = "^MagicQ") -> int | None:
    """Fereastra PRINCIPALA MagicQ.

    MagicQ deschide mai multe ferestre de nivel superior: fereastra
    principala si, optional, "MagicQ Visualiser" / MagicHD. Nu se poate lua
    "prima gasita": EnumWindows le da in ordinea z-order, deci referinta
    s-ar schimba de la o rulare la alta, in functie de care fereastra e
    deasupra. Coordonatele de mouse calibrate ar deveni aiurea, iar tastele
    ar ajunge in Visualiser.

    Se alege dupa: titlu care NU pare fereastra secundara, apoi suprafata
    cea mai mare (fereastra principala e cea mare).
    """
    windows = list_process_windows(process_name)
    if not windows:
        return find_window(title_regex) if title_regex else None

    def score(item: tuple[int, str, tuple[int, int, int, int]]):
        _, title, (_, _, width, height) = item
        low = title.lower()
        primary = 0 if any(h in low for h in SECONDARY_WINDOW_HINTS) else 1
        return (primary, width * height)

    best = max(windows, key=score)
    if len(windows) > 1:
        others = ", ".join(f"{t!r}" for h, t, _ in windows if h != best[0])
        log.debug("MagicQ are %d ferestre; aleasa: %r (%dx%d). Ignorate: %s",
                  len(windows), best[1], best[2][2], best[2][3], others)
    return best[0]


def describe_magicq_windows(process_name: str) -> list[str]:
    """Text pentru diagnostic: ce ferestre are MagicQ si care e aleasa."""
    windows = list_process_windows(process_name)
    chosen = find_magicq_window(process_name)
    out = []
    for hwnd, title, (x, y, w, h) in windows:
        mark = " <== FOLOSITA" if hwnd == chosen else ""
        out.append(f"hwnd {hwnd:>9}  ({x:>5},{y:>4})  {w:>5}x{h:<5}  {title!r}{mark}")
    return out


def focus_window(hwnd: int) -> bool:
    """Aduce fereastra in prim-plan (cu trucul AttachThreadInput)."""
    if not IS_WINDOWS or not hwnd:
        return False
    if user32.GetForegroundWindow() == hwnd:
        return True
    fg = user32.GetForegroundWindow()
    cur_thread = kernel32.GetCurrentThreadId()
    fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = False
    if fg_thread and fg_thread != cur_thread:
        attached = bool(user32.AttachThreadInput(fg_thread, cur_thread, True))
    user32.ShowWindow(hwnd, 9)          # SW_RESTORE
    ok = bool(user32.SetForegroundWindow(hwnd))
    if attached:
        user32.AttachThreadInput(fg_thread, cur_thread, False)
    return ok


# ======================================================================
#  Transport
# ======================================================================
SUPPORTED_ALWAYS = {ActionType.KEY, ActionType.MACRO}
BINDABLE = {
    ActionType.PB_GO: "pb_go",
    ActionType.PB_STOP: "pb_stop",
    ActionType.PB_RELEASE: "pb_release",
    ActionType.PB_FLASH: "pb_flash",
    # In MagicQ tasta de flash este un TOGGLE ("toggle test playback on at
    # 100%"), deci eliberarea se face apasand aceeasi tasta inca o data.
    ActionType.PB_UNFLASH: "pb_flash",
    ActionType.EXEC: "exec",
    ActionType.BLACKOUT: "blackout",
    ActionType.RELEASE_ALL: "release_all",
}


class KeyboardTransport(Transport):
    name = "keyboard"

    def __init__(self, cfg, bus=None):
        super().__init__(cfg, bus)
        conf = cfg.get("magicq.keyboard", {}) or {}
        self.enabled = bool(conf.get("enabled", True))
        self.focus = bool(conf.get("focus_window", True))
        self.process_name = str(conf.get("window_process", "mqqt.exe"))
        self.title_regex = str(conf.get("window_title_regex", "^MagicQ"))
        self.key_delay = float(conf.get("key_delay_ms", 12)) / 1000.0
        self.hold = float(conf.get("hold_ms", 18)) / 1000.0
        self.bindings: dict[str, Any] = dict(conf.get("bindings", {}))
        self.macros: dict[str, str] = dict(conf.get("macros", {}))
        self.hwnd: int | None = None
        self._speed = 100.0
        supported = [a.value for a in SUPPORTED_ALWAYS]
        supported += [a.value for a, key in BINDABLE.items() if self.bindings.get(key)]
        if self.bindings.get("speed_up") and self.bindings.get("speed_down"):
            supported.append(ActionType.SPEED.value)
        self.status.supported = tuple(supported)

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
        self.hwnd = find_magicq_window(self.process_name, self.title_regex)
        self.status.detail = (f"MagicQ gasit ({self.process_name})" if self.hwnd
                              else "MagicQ nu ruleaza (se cauta din nou la trimitere)")
        log.info("Tastatura pregatita (%s)", self.status.detail)
        return True

    def supports(self, action_type: ActionType) -> bool:
        if action_type in SUPPORTED_ALWAYS:
            return True
        if action_type is ActionType.SPEED:
            return bool(self.bindings.get("speed_up") and self.bindings.get("speed_down"))
        key = BINDABLE.get(action_type)
        return bool(key and self.bindings.get(key))

    # ------------------------------------------------------------------
    def _send(self, action: Action) -> bool:
        t = action.type
        p = action.params

        if t is ActionType.KEY:
            return self.send_sequence(str(p.get("keys", "")))

        if t is ActionType.MACRO:
            name = str(p.get("name", ""))
            seq = self.macros.get(name) or self.macros.get(name.lower())
            if not seq:
                self.status.detail = f"macro '{name}' nedefinit"
                log.warning("Macro '%s' nu exista in magicq.keyboard.macros "
                            "(actiune ignorata)", name)
                return False
            return self.send_sequence(seq)

        if t is ActionType.SPEED:
            return self._send_speed(p)

        key = BINDABLE.get(t)
        binding = self.bindings.get(key) if key else None
        sequence = self._resolve_binding(binding, action)
        if not sequence:
            return False
        return self.send_sequence(sequence)

    def _resolve_binding(self, binding: Any, action: Action) -> str:
        """Maparea poate fi o lista (o tasta per playback) sau un sablon."""
        if not binding:
            return ""
        if isinstance(binding, (list, tuple)):
            index = action.playback - 1
            if 0 <= index < len(binding):
                return str(binding[index])
            log.warning("MagicQ PC controleaza doar playback-urile 1-10; "
                        "PB%d nu are tasta alocata.", action.playback)
            return ""
        return self._format(str(binding), action)

    def _format(self, template: str, action: Action) -> str:
        pb = action.playback
        return template.format(
            playback=pb,
            playback_digit=str(pb % 10),
            page=action.params.get("page", 1),
            item=action.params.get("item", 1),
            level=int(action.params.get("level", 100)),
        )

    def _send_speed(self, params: dict) -> bool:
        """Viteza prin apasari repetate (tastatura nu are valoare absoluta)."""
        up = self.bindings.get("speed_up")
        down = self.bindings.get("speed_down")
        if not up or not down:
            return False
        if "delta" in params:
            delta = float(params["delta"])
        else:
            target = float(params.get("percent", 100.0))
            delta = target - self._speed
        steps = int(min(8, max(1, round(abs(delta) / 25.0))))
        seq = up if delta >= 0 else down
        ok = True
        for _ in range(steps):
            ok = self.send_sequence(seq) and ok
        self._speed = max(10.0, min(400.0, self._speed + delta))
        return ok

    # ------------------------------------------------------------------
    def send_sequence(self, sequence: str) -> bool:
        """Trimite o secventa completa de taste."""
        if not sequence:
            return False
        if not IS_WINDOWS:
            log.info("[simulare tastatura] %s", sequence)
            return True
        if self.focus and not self._ensure_focus():
            log.debug("Fereastra MagicQ nu a putut fi activata; se trimite oricum.")

        ok = True
        for step in _split_steps(sequence):
            if step.startswith("wait:"):
                try:
                    time.sleep(float(step[5:]))
                except ValueError:
                    pass
                continue
            if step.startswith("type:"):
                ok = self._type_text(step[5:]) and ok
                continue
            ok = self._press_combo(step) and ok
            if self.key_delay:
                time.sleep(self.key_delay)
        return ok

    def _press_combo(self, combo: str) -> bool:
        parts = [x for x in combo.split("+") if x]
        if not parts:
            return False
        resolved: list[tuple[int, bool]] = []
        for part in parts:
            r = _resolve_key(part)
            if r is None:
                log.warning("Tasta necunoscuta: '%s' (in '%s')", part, combo)
                return False
            resolved.append(r)

        down = [_key_event(scan, ext, False) for scan, ext in resolved]
        up = [_key_event(scan, ext, True) for scan, ext in reversed(resolved)]
        if not _send_inputs(down):
            return False
        time.sleep(self.hold)
        return _send_inputs(up)

    def _type_text(self, text: str) -> bool:
        inputs: list[INPUT] = []
        for ch in text:
            inputs.append(_unicode_event(ch, False))
            inputs.append(_unicode_event(ch, True))
        return _send_inputs(inputs)

    def _ensure_focus(self) -> bool:
        if self.hwnd and user32.IsWindow(self.hwnd):
            if user32.GetForegroundWindow() == self.hwnd:
                return True
            return focus_window(self.hwnd)
        self.hwnd = find_magicq_window(self.process_name, self.title_regex)
        if self.hwnd:
            return focus_window(self.hwnd)
        return False


def _split_steps(sequence: str) -> list[str]:
    """'ctrl+1 enter, wait:0.1' -> ['ctrl+1', 'enter', 'wait:0.1']

    Nu se face lowercase: `type:` trebuie sa pastreze majusculele.
    Numele tastelor sunt normalizate oricum in _resolve_key().
    """
    raw = re.split(r"[,\s]+", sequence.strip())
    return [x.strip() for x in raw if x.strip()]
