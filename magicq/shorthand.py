"""
magicq/shorthand.py
===================
Traducerea formei scurte, in limbaj natural, din rules.json.

Exact formatul cerut in specificatie:

    {
        "Drop": "Flash 4",
        "BuildUp": "Increase Speed",
        "Break": "Release Flash",
        "Bass": "Playback 5",
        "High": "Color FX",
        "BPM>140": "Speed=180"
    }

Sirul din dreapta este tradus aici intr-un `Action`. Se accepta si
inlantuiri: "Flash 4 + Speed=180" sau "Flash 4; Strobe".

Orice text nerecunoscut devine un MACRO cu acel nume: il definesti in
config/settings.json la magicq.keyboard.macros si merge automat.
"""

from __future__ import annotations

import re

from magicq.actions import Action, ActionType

# expresii, in ordinea de incercare
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^(?:release\s+all|release\s+everything|clear\s+all)$", re.I), "release_all"),
    (re.compile(r"^(?:release\s+flash|unflash|flash\s+off)(?:\s+(\d+))?$", re.I), "unflash"),
    (re.compile(r"^(?:flash|bump)\s*(\d+)(?:\s+for\s+([\d.]+)\s*s)?$", re.I), "flash"),
    (re.compile(r"^(?:go|playback|pb|activate\s+playback|chase|cue\s*stack)\s*(\d+)$", re.I), "go"),
    (re.compile(r"^(?:stop|pause)\s*(\d+)$", re.I), "stop"),
    (re.compile(r"^release\s*(\d+)$", re.I), "release"),
    (re.compile(r"^(?:pb|playback|fader)\s*(\d+)\s*=\s*([\d.]+)%?$", re.I), "level"),
    (re.compile(r"^(?:exec|execute)\s*(\d+)[\s/,:-]+(\d+)$", re.I), "exec"),
    (re.compile(r"^speed\s*=?\s*([\d.]+)%?$", re.I), "speed_abs"),
    (re.compile(r"^(?:increase\s+speed|speed\s*\+\+?|faster|speed\s+up)(?:\s+([\d.]+)%?)?$", re.I),
     "speed_up"),
    (re.compile(r"^(?:decrease\s+speed|speed\s*--?|slower|speed\s+down|reduce\s+speed)"
                r"(?:\s+([\d.]+)%?)?$", re.I), "speed_down"),
    (re.compile(r"^dmx\s*(\d+)\s*=\s*(\d+)$", re.I), "dmx"),
    (re.compile(r"^(?:rpc|cmd|command)\s+(.+)$", re.I), "rpc"),
    (re.compile(r"^(?:key|keys|press)\s+(.+)$", re.I), "key"),
    (re.compile(r"^(?:macro)\s+(.+)$", re.I), "macro"),
    (re.compile(r"^(?:click)\s+(-?\d+)[\s,]+(-?\d+)$", re.I), "click"),
    (re.compile(r"^(?:blackout|bo)$", re.I), "blackout"),
    (re.compile(r"^note\s*(\d+)(?:\s+(\d+))?$", re.I), "note"),
    (re.compile(r"^cc\s*(\d+)\s*=\s*(\d+)$", re.I), "cc"),
]


def parse_shorthand(text: str, source: str = "") -> Action:
    """Traduce un singur sir intr-un Action."""
    s = str(text).strip()
    if not s:
        raise ValueError("Actiune goala")

    for pattern, kind in _PATTERNS:
        m = pattern.match(s)
        if not m:
            continue
        g = m.groups()

        if kind == "flash":
            params = {"playback": int(g[0])}
            if g[1]:
                params["duration"] = float(g[1])
            return Action(ActionType.PB_FLASH, params, source)
        if kind == "unflash":
            params = {"playback": int(g[0])} if g[0] else {"all": True}
            return Action(ActionType.PB_UNFLASH, params, source)
        if kind == "go":
            return Action(ActionType.PB_GO, {"playback": int(g[0])}, source)
        if kind == "stop":
            return Action(ActionType.PB_STOP, {"playback": int(g[0])}, source)
        if kind == "release":
            return Action(ActionType.PB_RELEASE, {"playback": int(g[0])}, source)
        if kind == "level":
            return Action(ActionType.PB_LEVEL,
                          {"playback": int(g[0]), "level": float(g[1])}, source)
        if kind == "exec":
            return Action(ActionType.EXEC, {"page": int(g[0]), "item": int(g[1])}, source)
        if kind == "speed_abs":
            return Action(ActionType.SPEED, {"percent": float(g[0])}, source)
        if kind == "speed_up":
            step = float(g[0]) if g[0] else 25.0
            return Action(ActionType.SPEED, {"delta": step}, source)
        if kind == "speed_down":
            step = float(g[0]) if g[0] else 25.0
            return Action(ActionType.SPEED, {"delta": -step}, source)
        if kind == "dmx":
            return Action(ActionType.DMX, {"channel": int(g[0]), "value": int(g[1])}, source)
        if kind == "rpc":
            return Action(ActionType.RPC, {"command": g[0].strip()}, source)
        if kind == "key":
            return Action(ActionType.KEY, {"keys": g[0].strip()}, source)
        if kind == "macro":
            return Action(ActionType.MACRO, {"name": g[0].strip()}, source)
        if kind == "click":
            return Action(ActionType.CLICK, {"x": int(g[0]), "y": int(g[1])}, source)
        if kind == "blackout":
            return Action(ActionType.BLACKOUT, {}, source)
        if kind == "release_all":
            return Action(ActionType.RELEASE_ALL, {}, source)
        if kind == "note":
            params = {"note": int(g[0])}
            if g[1]:
                params["velocity"] = int(g[1])
            return Action(ActionType.MIDI_NOTE, params, source)
        if kind == "cc":
            return Action(ActionType.MIDI_CC, {"cc": int(g[0]), "value": int(g[1])}, source)

    # necunoscut -> macro cu acest nume (ex: "Color FX", "Strobe")
    return Action(ActionType.MACRO, {"name": _slug(s), "label": s}, source)


def parse_shorthand_list(text: str, source: str = "") -> list[Action]:
    """Traduce "Flash 4 + Speed=180" sau "Flash 4; Strobe" intr-o lista.

    Separatorul '+' este recunoscut doar cu spatii in jur, ca sa nu rupa
    combinatiile de taste: "Key ctrl+f4" ramane o singura actiune.
    """
    parts = [p.strip() for p in re.split(r"\s*;\s*|\s+\+\s+|\s+then\s+", str(text)) if p.strip()]
    return [parse_shorthand(p, source) for p in parts]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")
