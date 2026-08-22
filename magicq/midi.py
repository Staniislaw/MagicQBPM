"""
magicq/midi.py
==============
Transport MIDI - a doua optiune dupa OSC. Util cand MagicQ ruleaza pe alt
PC/consola si ai o interfata MIDI, sau cand vrei sa folosesti maparile
MIDI deja existente in show.

CONFIGURARE IN MagicQ:
    Setup -> View Settings -> MIDI/Timecode
      * MIDI In Type : "MIDI"
      * canalul si notele trebuie sa corespunda cu ce trimitem aici
    Maparea implicita din settings.json:
      playback N -> nota (playback_note_base + N - 1)   [GO / FLASH]
      playback N -> CC   (playback_cc_base   + N - 1)   [nivel fader]

Pe Windows ai nevoie de un port MIDI virtual (loopMIDI, LoopBe1) daca
MagicQ ruleaza pe acelasi PC - Windows nu are unul nativ.
"""

from __future__ import annotations

import logging
from typing import Any

from magicq.actions import Action, ActionType
from magicq.base import Transport

log = logging.getLogger(__name__)

try:
    import mido
except Exception as exc:  # pragma: no cover
    mido = None  # type: ignore[assignment]
    _MIDI_ERROR = exc
else:
    _MIDI_ERROR = None


SUPPORTED = {
    ActionType.PB_GO, ActionType.PB_FLASH, ActionType.PB_UNFLASH,
    ActionType.PB_RELEASE, ActionType.PB_LEVEL, ActionType.EXEC,
    ActionType.MIDI_NOTE, ActionType.MIDI_CC,
}


class MIDITransport(Transport):
    name = "midi"

    def __init__(self, cfg, bus=None):
        super().__init__(cfg, bus)
        conf = cfg.get("magicq.midi", {}) or {}
        self.enabled = bool(conf.get("enabled", False))
        self.port_name = conf.get("port_name")
        self.channel = max(0, min(15, int(conf.get("channel", 1)) - 1))
        self.pb_note_base = int(conf.get("playback_note_base", 60))
        self.pb_cc_base = int(conf.get("playback_cc_base", 20))
        self.exec_note_base = int(conf.get("exec_note_base", 36))
        self.velocity = int(conf.get("default_velocity", 127))
        self.port: Any = None
        self.status.supported = tuple(a.value for a in SUPPORTED)

    # ------------------------------------------------------------------
    @staticmethod
    def available_ports() -> list[str]:
        if mido is None:
            return []
        try:
            return list(mido.get_output_names())
        except Exception:  # noqa: BLE001
            return []

    def connect(self) -> bool:
        if not self.enabled:
            self.status.detail = "dezactivat"
            return False
        if mido is None:
            self.status.detail = f"mido lipseste ({_MIDI_ERROR})"
            log.warning("MIDI indisponibil: %s", _MIDI_ERROR)
            return False
        ports = self.available_ports()
        if not ports:
            self.status.detail = "niciun port MIDI de iesire"
            log.warning("MIDI: nu exista porturi de iesire (instaleaza loopMIDI).")
            return False

        target = None
        if self.port_name:
            needle = str(self.port_name).lower()
            for name in ports:
                if needle in name.lower():
                    target = name
                    break
            if target is None:
                self.status.detail = f"portul '{self.port_name}' nu exista"
                log.warning("MIDI: portul '%s' nu a fost gasit. Disponibile: %s",
                            self.port_name, ports)
                return False
        else:
            target = ports[0]

        try:
            self.port = mido.open_output(target)
        except Exception as exc:  # noqa: BLE001
            self.status.detail = str(exc)
            log.error("MIDI: nu am putut deschide portul '%s': %s", target, exc)
            return False

        self.status.available = True
        self.status.connected = True
        self.status.detail = target
        log.info("MIDI pregatit pe portul '%s' (canal %d)", target, self.channel + 1)
        return True

    def close(self) -> None:
        if self.port is not None:
            try:
                self.port.close()
            except Exception:  # noqa: BLE001
                pass
            self.port = None
        self.status.connected = False

    def supports(self, action_type: ActionType) -> bool:
        return action_type in SUPPORTED

    # ------------------------------------------------------------------
    def _send(self, action: Action) -> bool:
        if self.port is None or mido is None:
            return False
        t = action.type
        p = action.params

        if t is ActionType.PB_GO:
            note = self.pb_note_base + action.playback - 1
            self._note(note, self.velocity, True)
            self._note(note, 0, False)
            return True
        if t is ActionType.PB_FLASH:
            self._note(self.pb_note_base + action.playback - 1, self.velocity, True)
            return True
        if t in (ActionType.PB_UNFLASH, ActionType.PB_RELEASE):
            self._note(self.pb_note_base + action.playback - 1, 0, False)
            return True
        if t is ActionType.PB_LEVEL:
            cc = self.pb_cc_base + action.playback - 1
            value = int(round(max(0.0, min(100.0, action.level)) / 100.0 * 127))
            self._cc(cc, value)
            return True
        if t is ActionType.EXEC:
            note = self.exec_note_base + int(p.get("item", 1)) - 1
            self._note(note, self.velocity, True)
            self._note(note, 0, False)
            return True
        if t is ActionType.MIDI_NOTE:
            note = int(p.get("note", 60))
            vel = int(p.get("velocity", self.velocity))
            self._note(note, vel, True)
            if not p.get("hold", False):
                self._note(note, 0, False)
            return True
        if t is ActionType.MIDI_CC:
            self._cc(int(p.get("cc", 1)), int(p.get("value", 127)))
            return True
        return False

    # ------------------------------------------------------------------
    def _note(self, note: int, velocity: int, on: bool) -> None:
        msg = mido.Message("note_on" if on else "note_off",
                           note=max(0, min(127, note)),
                           velocity=max(0, min(127, velocity)),
                           channel=self.channel)
        self.port.send(msg)
        log.debug("MIDI -> %s", msg)

    def _cc(self, control: int, value: int) -> None:
        msg = mido.Message("control_change", control=max(0, min(127, control)),
                           value=max(0, min(127, value)), channel=self.channel)
        self.port.send(msg)
        log.debug("MIDI -> %s", msg)

    def all_notes_off(self) -> None:
        if self.port is None or mido is None:
            return
        try:
            self.port.send(mido.Message("control_change", control=123, value=0,
                                        channel=self.channel))
        except Exception:  # noqa: BLE001
            pass
