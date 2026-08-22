"""
magicq/osc.py
=============
Transport OSC catre MagicQ - metoda preferata (rapida, fara focus pe
fereastra, nu fura tastatura).

CONFIGURARE IN MagicQ (o singura data):
    Setup -> View Settings -> Network
      * OSC Mode      : "Rx OSC"  (sau "Tx and Rx OSC")
      * OSC Rx Port   : 8000            <- acelasi port ca in settings.json
      * OSC Tx IP/Port: doar daca vrei feedback catre aplicatie
    (in unele versiuni optiunile sunt sub Setup -> View Settings -> Ports)

!! OSC NECESITA MagicQ IN "UNLOCKED MODE" !!
Din manualul MagicQ: "OSC is supported on MagicQ consoles (except MQ40 and
MQ40N) and PCs when fully unlocked (Unlocked Mode)". Adica un wing sau o
interfata USB ChamSys (MagicDMX NU deblocheaza). In Demo Mode, MagicQ nici
macar nu deschide portul OSC - vezi tools/osc_discover.py ports.

Adresele de mai jos sunt cele oficiale din manual (OSC.html, Table 1),
dar raman sabloane in config/settings.json ca sa le poti ajusta:

    /pb/<N>            0..100 sau 0.0..1.0   nivel fader playback
    /pb/<N>/go                                GO
    /pb/<N>/pause                             PAUSE
    /pb/<N>/release                           RELEASE
    /pb/<N>/flash      0 = 0%, non-zero = 100%
    /pb/<N>/<cue>                             sari la cue
    /exec/<pag>/<item> <valoare>              buton/fader Execute Window
    /dbo               0 = blackout pornit    blackout
    /swap              0 = add, non-zero = swap
    /rpc               "<comanda>"            comanda remote ChamSys
    /midi              <bytes>                mesaj MIDI

Doar playback-urile 1-10 si grilele Execute 1-10 sunt suportate nativ.
NU exista adrese pentru DMX direct sau pentru viteza efectelor.

OSC merge peste UDP: nu exista confirmare de la MagicQ. Statusul afisat
in UI inseamna "socket deschis + pachete trimise fara eroare".
"""

from __future__ import annotations

import logging
import socket
from typing import Any

from magicq.actions import Action, ActionType
from magicq.base import Transport

log = logging.getLogger(__name__)

try:
    from pythonosc.udp_client import SimpleUDPClient
except Exception as exc:  # pragma: no cover
    SimpleUDPClient = None  # type: ignore[assignment]
    _OSC_ERROR = exc
else:
    _OSC_ERROR = None


SUPPORTED = {
    ActionType.PB_GO, ActionType.PB_STOP, ActionType.PB_RELEASE,
    ActionType.PB_FLASH, ActionType.PB_UNFLASH, ActionType.PB_LEVEL,
    ActionType.EXEC, ActionType.DMX, ActionType.RPC, ActionType.SPEED,
    ActionType.BLACKOUT, ActionType.RELEASE_ALL,
}


class OSCTransport(Transport):
    name = "osc"

    def __init__(self, cfg, bus=None):
        super().__init__(cfg, bus)
        conf = cfg.get("magicq.osc", {}) or {}
        self.enabled = bool(conf.get("enabled", True))
        self.host = str(conf.get("host", "127.0.0.1"))
        self.port = int(conf.get("port", 8000))
        self.addresses: dict[str, str] = dict(conf.get("addresses", {}))
        self.level_scale = float(conf.get("level_scale", 100.0))
        self.blackout_value = int(conf.get("blackout_value", 0))
        # daca nimeni nu asculta pe portul OSC, transportul se declara INACTIV
        # ca actiunile sa treaca pe tastatura (vezi _listener_present)
        self.require_listener = bool(conf.get("require_listener", True))
        self.level_as_int = bool(conf.get("level_as_int", True))
        self.client: Any = None
        self.status.supported = tuple(a.value for a in SUPPORTED)

    # ------------------------------------------------------------------
    def connect(self) -> bool:
        if not self.enabled:
            self.status.detail = "dezactivat in settings.json"
            return False
        if SimpleUDPClient is None:
            self.status.detail = f"python-osc lipseste ({_OSC_ERROR})"
            self.status.available = False
            log.warning("OSC indisponibil: %s", _OSC_ERROR)
            return False
        try:
            # UDP nu are handshake; verificam doar ca gazda se poate rezolva
            socket.getaddrinfo(self.host, self.port, type=socket.SOCK_DGRAM)
            self.client = SimpleUDPClient(self.host, self.port)
        except Exception as exc:  # noqa: BLE001
            self.status.detail = str(exc)
            self.status.available = False
            log.error("OSC: nu am putut crea clientul pentru %s:%d - %s",
                      self.host, self.port, exc)
            return False

        # ---- verificarea esentiala ----
        # UDP nu esueaza niciodata la trimitere. Daca ne-am declara
        # "conectati" fara sa asculte nimeni, OSC ar inghiti TOATE actiunile
        # (fiind primul in ordinea de prioritate) si nu s-ar mai ajunge
        # niciodata la tastatura - aplicatia ar parea ca merge, dar MagicQ
        # nu ar primi nimic. Exact cazul MagicQ PC in Demo Mode.
        if self.require_listener and not self._listener_present():
            self.status.available = True
            self.status.connected = False
            self.status.detail = f"nimeni nu asculta pe {self.host}:{self.port}"
            log.warning(
                "OSC: nimic nu asculta pe %s:%d -> transportul ramane INACTIV, "
                "actiunile trec pe tastatura. (MagicQ PC in Demo Mode nu suporta "
                "OSC; e nevoie de Unlocked Mode.)", self.host, self.port)
            return False

        self.status.available = True
        self.status.connected = True
        self.status.detail = f"{self.host}:{self.port}"
        log.info("OSC pregatit: %s:%d", self.host, self.port)
        return True

    def _listener_present(self) -> bool:
        """True daca cineva asculta pe portul OSC.

        Se poate verifica doar local: daca reusim sa ocupam noi portul,
        inseamna ca era liber. Pentru o gazda din retea nu avem cum sa
        stim, deci presupunem ca asculta.
        """
        if self.host not in ("127.0.0.1", "localhost", "::1"):
            return True
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.bind(("127.0.0.1", self.port))
            return False           # am putut lega portul => nimeni nu asculta
        except OSError:
            return True            # ocupat => cineva asculta acolo
        finally:
            probe.close()

    def close(self) -> None:
        self.client = None
        self.status.connected = False

    def supports(self, action_type: ActionType) -> bool:
        if action_type not in SUPPORTED:
            return False
        # o actiune este suportata doar daca are un sablon de adresa definit
        key = _address_key(action_type)
        return bool(self.addresses.get(key))

    # ------------------------------------------------------------------
    def _send(self, action: Action) -> bool:
        if self.client is None:
            return False
        t = action.type
        p = action.params

        if t is ActionType.PB_LEVEL:
            addr = self._addr("pb_level", playback=action.playback)
            value = action.level / 100.0 * self.level_scale
            return self._emit(addr, int(round(value)) if self.level_as_int else float(value))

        if t is ActionType.PB_GO:
            return self._emit(self._addr("pb_go", playback=action.playback), 1)
        if t is ActionType.PB_STOP:
            return self._emit(self._addr("pb_stop", playback=action.playback), 1)
        if t is ActionType.PB_RELEASE:
            return self._emit(self._addr("pb_release", playback=action.playback), 1)
        if t is ActionType.PB_FLASH:
            return self._emit(self._addr("pb_flash", playback=action.playback), 1)
        if t is ActionType.PB_UNFLASH:
            return self._emit(self._addr("pb_flash", playback=action.playback), 0)

        if t is ActionType.EXEC:
            addr = self._addr("exec", page=int(p.get("page", 1)), item=int(p.get("item", 1)))
            return self._emit(addr, int(p.get("value", 1)))

        if t is ActionType.DMX:
            addr = self._addr("dmx", channel=int(p.get("channel", 1)))
            return self._emit(addr, int(p.get("value", 255)))

        if t is ActionType.SPEED:
            addr = self._addr("speed", playback=int(p.get("playback", 1)))
            return self._emit(addr, float(p.get("percent", 100.0)))

        if t is ActionType.RPC:
            return self._emit(self._addr("rpc"), str(p.get("command", "")))

        if t is ActionType.BLACKOUT:
            # manual: /dbo  -> 0 porneste blackout, non-zero il opreste
            value = int(p.get("value", self.blackout_value))
            return self._emit(self._addr("blackout"), value)
        if t is ActionType.RELEASE_ALL:
            return self._emit(self._addr("rpc"), "RELEASE ALL")

        return False

    # ------------------------------------------------------------------
    def _addr(self, key: str, **fields: Any) -> str:
        template = self.addresses.get(key)
        if not template:
            raise ValueError(f"Adresa OSC '{key}' nu este configurata")
        return template.format(**fields)

    def _emit(self, address: str, value: Any) -> bool:
        self.client.send_message(address, value)
        log.debug("OSC -> %s %r", address, value)
        return True

    # ------------------------------------------------------------------
    def send_raw(self, address: str, value: Any) -> bool:
        """Trimitere manuala (buton de test din UI)."""
        if self.client is None:
            return False
        try:
            self._emit(address, value)
            self.status.sent += 1
            return True
        except Exception as exc:  # noqa: BLE001
            self.status.errors += 1
            self.last_error = str(exc)
            return False


def _address_key(action_type: ActionType) -> str:
    if action_type in (ActionType.PB_FLASH, ActionType.PB_UNFLASH):
        return "pb_flash"
    if action_type is ActionType.BLACKOUT:
        return "blackout"
    if action_type is ActionType.RELEASE_ALL:
        return "rpc"
    return action_type.value
