"""
magicq/actions.py
=================
Vocabularul comun de actiuni catre MagicQ.

Regulile din rules.json produc obiecte `Action` independente de transport.
Router-ul (magicq/router.py) decide DUPA aceea prin ce canal se trimit:
OSC, MIDI, tastatura sau mouse. Astfel poti schimba metoda de control
fara sa atingi regulile.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ActionType(str, Enum):
    # --- playback-uri / cue stack-uri ---
    PB_GO = "pb_go"                 # GO pe playback-ul N
    PB_STOP = "pb_stop"             # STOP / PAUSE
    PB_RELEASE = "pb_release"       # RELEASE
    PB_FLASH = "pb_flash"           # FLASH (cu auto-release dupa `duration`)
    PB_LEVEL = "pb_level"           # nivel fader 0..100
    PB_UNFLASH = "pb_unflash"       # eliberarea unui flash tinut

    # --- fereastra Execute ---
    EXEC = "exec"                   # buton din Execute Window (page/item)

    # --- diverse MagicQ ---
    DMX = "dmx"                     # canal DMX direct
    RPC = "rpc"                     # linie de comanda MagicQ (remote)
    SPEED = "speed"                 # viteza efectelor (procent)

    # --- palete / programator (prin mouse, pe pagina curenta) ---
    PALETTE = "palette"             # click pe o casuta de paleta
    SELECT_GROUP = "select_group"   # click pe o casuta de grup
    CLEAR = "clear"                 # goleste programatorul
    EXCLUSIVE_OFF = "exclusive_off"  # stinge butonul activ dintr-un grup exclusiv

    # --- transport brut ---
    KEY = "key"                     # secventa de taste
    MACRO = "macro"                 # macro denumit din settings.json
    CLICK = "click"                 # click de mouse pe o coordonata
    MIDI_NOTE = "midi_note"
    MIDI_CC = "midi_cc"

    # --- globale ---
    BLACKOUT = "blackout"
    RELEASE_ALL = "release_all"


#: actiunile care au nevoie de o "eliberare" ulterioara (flash tinut)
HOLD_ACTIONS = {ActionType.PB_FLASH}


@dataclass
class Action:
    """O comanda concreta catre MagicQ."""

    type: ActionType
    params: dict[str, Any] = field(default_factory=dict)
    source: str = ""                 # numele regulii care a generat-o
    priority: int = 5                # 0 = cel mai urgent
    created: float = field(default_factory=time.monotonic)
    # daca e setat, actiunea nu se trimite prin transportul preferat, ci
    # prin cel cerut explicit ("osc" / "midi" / "keyboard" / "mouse")
    transport: str | None = None

    # ---- acces comod la parametri ----
    @property
    def playback(self) -> int:
        return int(self.params.get("playback", self.params.get("pb", 1)))

    @property
    def level(self) -> float:
        return float(self.params.get("level", 100.0))

    @property
    def duration(self) -> float:
        return float(self.params.get("duration", 0.0))

    def describe(self) -> str:
        p = self.params
        t = self.type
        if t is ActionType.PB_UNFLASH and p.get("all"):
            return "UNFLASH toate"
        if t in (ActionType.PB_GO, ActionType.PB_STOP, ActionType.PB_RELEASE,
                 ActionType.PB_UNFLASH):
            return f"{t.value} PB{self.playback}"
        if t is ActionType.PB_FLASH:
            d = f" {p['duration']:.2f}s" if p.get("duration") else ""
            return f"FLASH PB{self.playback}{d}"
        if t is ActionType.PB_LEVEL:
            return f"PB{self.playback} = {self.level:.0f}%"
        if t is ActionType.EXEC:
            return f"EXEC {p.get('page', 1)}/{p.get('item', 1)}"
        if t is ActionType.SPEED:
            target = f"PB{p['playback']}" if "playback" in p else "global"
            if "delta" in p:
                return f"SPEED {target} {float(p['delta']):+.0f}%"
            return f"SPEED {target} = {float(p.get('percent', 100)):.0f}%"
        if t is ActionType.KEY:
            return f"KEY '{p.get('keys', '')}'"
        if t is ActionType.MACRO:
            return f"MACRO '{p.get('name', '')}'"
        if t is ActionType.CLICK:
            return f"CLICK {p.get('target') or (p.get('x'), p.get('y'))}"
        if t is ActionType.PALETTE:
            window = str(p.get("window", "?")).lower()
            label = p.get("label")
            if window == "exec":
                if p.get("name"):
                    target = str(p["name"])
                elif "row" in p and "col" in p:
                    target = f"r{p['row']}c{p['col']}"
                else:
                    target = "?"
                return f"EXEC '{target}'" + (f" ({label})" if label else "")
            grp = f" G{p['group']}->" if p.get("group") else ""
            return (f"PALETA{grp} {window.upper()[:3]}{p.get('item', '?')}"
                    + (f" ({label})" if label else ""))
        if t is ActionType.SELECT_GROUP:
            return f"SELECT G{p.get('item', '?')}"
        if t is ActionType.CLEAR:
            return "CLEAR programator"
        if t is ActionType.EXCLUSIVE_OFF:
            return f"OFF grup '{p.get('group', '?')}'"
        if t is ActionType.DMX:
            return f"DMX {p.get('channel')} = {p.get('value')}"
        if t is ActionType.RPC:
            return f"RPC '{p.get('command', '')}'"
        if t is ActionType.MIDI_NOTE:
            return f"NOTE {p.get('note')} vel {p.get('velocity', 127)}"
        if t is ActionType.MIDI_CC:
            return f"CC {p.get('cc')} = {p.get('value')}"
        return t.value

    def release_action(self) -> "Action | None":
        """Actiunea complementara pentru comenzile tinute.

        - PB_FLASH -> PB_UNFLASH
        - buton Execute cu `duration` -> aceeasi apasare inca o data
          (butoanele Execute sunt cue stack-uri, deci TOGGLE: a doua
          apasare il stinge)
        """
        if self.type is ActionType.PB_FLASH:
            params = dict(self.params)
            params["_release"] = True
            return Action(ActionType.PB_UNFLASH, params, source=self.source,
                          priority=self.priority, transport=self.transport)
        if self.type is ActionType.PALETTE and self.duration > 0:
            params = {k: v for k, v in self.params.items()
                      if k not in ("duration", "delay")}
            params["_release"] = True
            return Action(ActionType.PALETTE, params, source=self.source,
                          priority=self.priority, transport=self.transport)
        return None

    @property
    def delay(self) -> float:
        """Cu cate secunde in viitor trebuie executata (0 = acum)."""
        return float(self.params.get("delay", 0.0))

    def hold_key(self) -> str:
        """Identitate stabila pentru comenzile cu durata (auto-release)."""
        p = self.params
        if self.type in (ActionType.PB_FLASH, ActionType.PB_UNFLASH):
            return f"pb{self.playback}"
        if self.type is ActionType.PALETTE:
            target = p.get("name") or f"{p.get('row')}:{p.get('col')}:{p.get('item')}"
            return f"{p.get('window', '?')}:{target}"
        return self.describe()


# ======================================================================
#  Parsare din JSON
# ======================================================================
_ALIASES = {
    "go": ActionType.PB_GO,
    "playback": ActionType.PB_GO,
    "flash": ActionType.PB_FLASH,
    "release": ActionType.PB_RELEASE,
    "stop": ActionType.PB_STOP,
    "pause": ActionType.PB_STOP,
    "level": ActionType.PB_LEVEL,
    "fader": ActionType.PB_LEVEL,
    "execute": ActionType.EXEC,
    "keys": ActionType.KEY,
    "keyboard": ActionType.KEY,
    "command": ActionType.RPC,
    "palette": ActionType.PALETTE,
    "paleta": ActionType.PALETTE,
    "group": ActionType.SELECT_GROUP,
    "note": ActionType.MIDI_NOTE,
    "cc": ActionType.MIDI_CC,
}


def parse_action(spec: dict[str, Any] | str, source: str = "") -> Action:
    """Construieste un Action dintr-o intrare din rules.json.

    Forme acceptate:
        {"action": "pb_flash", "playback": 4, "duration": 0.8}
        {"flash": 4}                       # forma scurta
        "Flash 4"                          # forma text (vezi rules.py)
    """
    if isinstance(spec, str):
        from magicq.shorthand import parse_shorthand  # import local (ciclu)
        return parse_shorthand(spec, source)

    data = dict(spec)
    raw_type = data.pop("action", None) or data.pop("type", None)

    if raw_type is None:
        # forma scurta: prima cheie cunoscuta este tipul
        for key in list(data.keys()):
            if key.lower() in _ALIASES:
                raw_type = key
                value = data.pop(key)
                if isinstance(value, dict):
                    data.update(value)
                else:
                    data.setdefault("playback", value)
                break

    if raw_type is None:
        raise ValueError(f"Actiune fara tip: {spec!r}")

    key = str(raw_type).lower().strip()
    if key in _ALIASES:
        atype = _ALIASES[key]
    else:
        try:
            atype = ActionType(key)
        except ValueError as exc:
            valid = ", ".join(a.value for a in ActionType)
            raise ValueError(f"Tip de actiune necunoscut: '{raw_type}'. Valide: {valid}") from exc

    transport = data.pop("transport", None) or data.pop("via", None)
    priority = int(data.pop("priority", 5))
    return Action(atype, data, source=source, priority=priority, transport=transport)
