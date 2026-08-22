"""
magicq/base.py
==============
Interfata comuna a transporturilor catre MagicQ (OSC / MIDI / tastatura /
mouse). Router-ul lucreaza doar cu aceasta interfata, deci se pot adauga
transporturi noi (Art-Net, sACN, telnet) fara sa se modifice regulile.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from core.state import TransportStatus

if TYPE_CHECKING:  # pragma: no cover
    from magicq.actions import Action, ActionType

log = logging.getLogger(__name__)


class Transport:
    """Clasa de baza. Subclasele suprascriu `supports`, `connect` si `_send`."""

    name: str = "base"

    def __init__(self, cfg, bus=None):
        self.cfg = cfg
        self.bus = bus
        self.enabled = True
        self.status = TransportStatus(name=self.name)
        self.last_error = ""

    # ---------------- capabilitati ----------------
    def supports(self, action_type: "ActionType") -> bool:
        """True daca transportul poate executa acest tip de actiune."""
        raise NotImplementedError

    def can_handle(self, action: "Action") -> bool:
        return (self.enabled and self.status.connected
                and self.supports(action.type))

    # ---------------- ciclu de viata ----------------
    def connect(self) -> bool:
        self.status.connected = True
        return True

    def close(self) -> None:
        self.status.connected = False

    # ---------------- trimitere ----------------
    def send(self, action: "Action") -> bool:
        """Trimite actiunea. Prinde exceptiile si actualizeaza statusul."""
        if not self.status.connected:
            return False
        try:
            ok = self._send(action)
        except Exception as exc:  # noqa: BLE001 - un transport rupt nu opreste aplicatia
            self.status.errors += 1
            self.last_error = str(exc)
            self.status.detail = str(exc)
            log.warning("[%s] eroare la trimitere %s: %s", self.name, action.describe(), exc)
            return False
        if ok:
            self.status.sent += 1
            self.status.last_send = time.monotonic()
        else:
            self.status.errors += 1
        return ok

    def _send(self, action: "Action") -> bool:
        raise NotImplementedError

    # ---------------- informativ ----------------
    def describe(self) -> str:
        return f"{self.name}: {'conectat' if self.status.connected else 'deconectat'}"
