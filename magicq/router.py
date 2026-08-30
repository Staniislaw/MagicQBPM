"""
magicq/router.py
================
Dispecerul de actiuni catre MagicQ.

Regulile produc `Action`-uri; router-ul decide PRIN CE CANAL pleaca, in
ordinea de prioritate din settings.json:

        OSC  ->  MIDI  ->  TASTATURA  ->  MOUSE

Reguli de rutare:
  * se alege primul transport conectat care suporta acel tip de actiune;
    "suporta" inseamna si "are maparea configurata" (ex: tastatura are o
    combinatie definita pentru pb_go)
  * daca trimiterea esueaza, se incearca automat urmatorul transport
  * daca nicio metoda dedicata nu suporta actiunea si
    `auto_keyboard_fallback` e activ, se incearca tastatura - exact
    cerinta "daca MagicQ nu suporta OSC pentru functia dorita, foloseste
    automat tastatura"
  * o actiune poate forta un transport anume: {"action": "...", "via": "midi"}

Totul se executa intr-un FIR SEPARAT: tastatura si mouse-ul au pauze de
milisecunde, iar firul de analiza nu are voie sa astepte niciodata.
Actiunile de tip FLASH cu `duration` primesc automat o eliberare
programata (heap de timere) - nu raman lumini blocate aprinse.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import queue
import threading
import time

from core.bus import EventBus, EventType
from magicq.actions import Action, ActionType
from magicq.base import Transport
from magicq.keyboard import KeyboardTransport
from magicq.midi import MIDITransport
from magicq.mouse import MouseTransport
from magicq.osc import OSCTransport

log = logging.getLogger(__name__)


class MagicQRouter(threading.Thread):
    def __init__(self, cfg, bus: EventBus):
        super().__init__(name="MagicQRouter", daemon=True)
        self.cfg = cfg
        self.bus = bus
        self.enabled = bool(cfg.get("magicq.enabled", True))
        self.auto_fallback = bool(cfg.get("magicq.auto_keyboard_fallback", True))
        self.rate_limit = int(cfg.get("magicq.rate_limit_per_s", 40))

        self.transports: dict[str, Transport] = {
            "osc": OSCTransport(cfg, bus),
            "midi": MIDITransport(cfg, bus),
            "keyboard": KeyboardTransport(cfg, bus),
            "mouse": MouseTransport(cfg, bus),
        }
        self.priority: list[str] = [
            name for name in (cfg.get("magicq.priority", []) or [])
            if name in self.transports
        ] or ["osc", "midi", "keyboard", "mouse"]

        self._queue: queue.PriorityQueue = queue.PriorityQueue(maxsize=512)
        self._counter = itertools.count()
        self._timers: list[tuple[float, int, Action]] = []
        self._timers_lock = threading.Lock()
        self._stop = threading.Event()

        # stare pentru rate limiting
        self._window_start = time.monotonic()
        self._window_count = 0
        self.dropped = 0
        self.sent = 0
        self.failed = 0
        self.last_action: str = ""
        self.last_route: str = ""
        # flash-uri active: playback -> deadline
        self.active_flashes: dict[int, float] = {}
        self.dry_run = False       # modul "manual": nu se trimite nimic real

    # ==================================================================
    #  Conectare
    # ==================================================================
    def connect(self) -> dict[str, bool]:
        results: dict[str, bool] = {}
        for name in self.priority:
            transport = self.transports[name]
            try:
                ok = transport.connect()
            except Exception as exc:  # noqa: BLE001
                log.error("Transportul %s a esuat la conectare: %s", name, exc)
                transport.status.detail = str(exc)
                ok = False
            results[name] = ok
            self.bus.emit(EventType.TRANSPORT_STATUS, transport=name, connected=ok,
                          detail=transport.status.detail)
        active = [n for n, ok in results.items() if ok]
        if active:
            log.info("Transporturi active catre MagicQ: %s", ", ".join(active))
        else:
            log.warning("Niciun transport catre MagicQ nu este activ! "
                        "Aplicatia va analiza audio, dar nu va trimite comenzi.")
        return results

    def close(self) -> None:
        for transport in self.transports.values():
            try:
                transport.close()
            except Exception:  # noqa: BLE001
                pass

    # ==================================================================
    #  Trimitere
    # ==================================================================
    def send(self, action: Action) -> bool:
        """Pune actiunea in coada (nu blocheaza niciodata firul apelant)."""
        if not self.enabled:
            return False
        # "Release Flash" fara numar = elibereaza tot ce e aprins acum
        if action.type is ActionType.PB_UNFLASH and action.params.get("all"):
            targets = list(self.active_flashes.keys())
            if not targets:
                return True
            ok = True
            for playback in targets:
                ok = self.send(Action(ActionType.PB_UNFLASH, {"playback": playback},
                                      source=action.source, priority=action.priority,
                                      transport=action.transport)) and ok
            return ok
        # Actiune programata in viitor (ex: flash-uri in ritmul BPM-ului):
        # intra in heap-ul de timere, nu in coada imediata.
        if action.delay > 0:
            deadline = time.monotonic() + action.delay
            with self._timers_lock:
                heapq.heappush(self._timers, (deadline, next(self._counter), action))
            return True
        try:
            self._queue.put_nowait((action.priority, next(self._counter), action))
            return True
        except queue.Full:
            self.dropped += 1
            log.warning("Coada de actiuni plina - actiune ignorata: %s", action.describe())
            return False

    def send_many(self, actions: list[Action]) -> int:
        return sum(1 for a in actions if self.send(a))

    # ==================================================================
    #  Firul de executie
    # ==================================================================
    def run(self) -> None:
        log.info("Router MagicQ pornit (prioritate: %s)", " > ".join(self.priority))
        while not self._stop.is_set():
            self._service_timers()
            try:
                _, _, action = self._queue.get(timeout=0.005)
            except queue.Empty:
                continue
            if not self._rate_ok():
                self.dropped += 1
                continue
            self._dispatch(action)
        # la oprire: eliberam tot ce a ramas aprins
        self._release_all_flashes()
        self.close()
        log.info("Router MagicQ oprit (%d trimise, %d esuate, %d ignorate).",
                 self.sent, self.failed, self.dropped)

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    def _rate_ok(self) -> bool:
        now = time.monotonic()
        if now - self._window_start >= 1.0:
            self._window_start = now
            self._window_count = 0
        if self._window_count >= self.rate_limit:
            return False
        self._window_count += 1
        return True

    def _dispatch(self, action: Action) -> None:
        # Pe tastatura, FLASH este un TOGGLE (MagicQ: "toggle test playback on
        # at 100%"). Daca playback-ul e deja aprins, a doua apasare l-ar STINGE.
        # Deci nu retrimitem, ci doar prelungim durata.
        if action.duration > 0 and action.release_action() is not None:
            key = action.hold_key()
            current = self.active_flashes.get(key)
            if current is not None and (current == float("inf") or current > time.monotonic()):
                self._extend_flash(action)
                return

        route = self._route(action)
        if not route:
            self.failed += 1
            self.bus.emit(EventType.ACTION_FAILED, action=action.describe(),
                          rule=action.source, reason="niciun transport disponibil")
            log.warning("Nicio metoda nu poate executa: %s (regula '%s')",
                        action.describe(), action.source)
            return

        # Modul MANUAL opreste automatismele, nu si comenzile date explicit
        # de utilizator din interfata (marcate cu "force").
        if self.dry_run and not action.params.get("force"):
            self.last_action = action.describe()
            self.last_route = "MANUAL"
            self.bus.emit(EventType.ACTION_SENT, action=action.describe(),
                          transport="manual", rule=action.source)
            return

        for transport in route:
            if transport.send(action):
                self.sent += 1
                self.last_action = action.describe()
                self.last_route = transport.name
                self.bus.emit(EventType.ACTION_SENT, action=action.describe(),
                              transport=transport.name, rule=action.source)
                log.info("-> %-28s [%s]  regula: %s", action.describe(),
                         transport.name, action.source)
                self._schedule_release(action)
                return
            log.debug("Transportul %s nu a reusit %s; se incearca urmatorul.",
                      transport.name, action.describe())

        self.failed += 1
        self.bus.emit(EventType.ACTION_FAILED, action=action.describe(),
                      rule=action.source, reason="toate transporturile au esuat")

    def _route(self, action: Action) -> list[Transport]:
        """Lista ordonata de transporturi care pot incerca aceasta actiune."""
        if action.transport:
            t = self.transports.get(action.transport)
            if t and t.status.connected and t.supports(action.type):
                return [t]
            if t and t.status.connected:
                log.debug("Transportul cerut '%s' nu suporta %s; se cauta alternativa.",
                          action.transport, action.type.value)

        route = [self.transports[name] for name in self.priority
                 if self.transports[name].can_handle(action)]

        # rezerva: tastatura, chiar daca actiunea nu are mapare directa
        if not route and self.auto_fallback:
            kb = self.transports.get("keyboard")
            if kb and kb.status.connected:
                converted = self._to_keyboard(action)
                if converted is not None and kb.supports(converted.type):
                    action.params.update(converted.params)
                    action.type = converted.type
                    route = [kb]
        return route

    def _to_keyboard(self, action: Action) -> Action | None:
        """Traduce o actiune fara suport intr-una de tastatura (macro)."""
        kb = self.transports["keyboard"]
        assert isinstance(kb, KeyboardTransport)
        name_map = {
            ActionType.PB_GO: "go",
            ActionType.PB_FLASH: "flash",
            ActionType.PB_RELEASE: "release",
            ActionType.BLACKOUT: "blackout",
            ActionType.RELEASE_ALL: "release_all",
            ActionType.SPEED: "speed",
            ActionType.EXEC: "exec",
        }
        macro_name = name_map.get(action.type)
        if macro_name and macro_name in kb.macros:
            return Action(ActionType.MACRO, {"name": macro_name},
                          source=action.source, priority=action.priority)
        return None

    # ------------------------------------------------------------------
    #  Flash cu durata: eliberare programata
    # ------------------------------------------------------------------
    def _schedule_release(self, action: Action) -> None:
        if action.type is ActionType.PB_UNFLASH:
            self.active_flashes.pop(action.hold_key(), None)
            return
        release = action.release_action()
        if release is None:
            return
        key = action.hold_key()
        duration = action.duration
        if duration <= 0:
            if action.type is ActionType.PB_FLASH:
                # flash fara durata = tinut pana la un release explicit
                self.active_flashes[key] = float("inf")
            return
        deadline = time.monotonic() + duration
        self.active_flashes[key] = deadline
        with self._timers_lock:
            heapq.heappush(self._timers, (deadline, next(self._counter), release))

    def _extend_flash(self, action: Action) -> None:
        """Comanda cu durata ceruta din nou cat timp e activa: doar prelungim."""
        duration = action.duration
        if duration <= 0:
            self.active_flashes[action.hold_key()] = float("inf")
            return
        key = action.hold_key()
        deadline = time.monotonic() + duration
        if deadline <= self.active_flashes.get(key, 0.0):
            return                       # deadline-ul existent e deja mai lung
        release = action.release_action()
        if release is None:
            return
        self.active_flashes[key] = deadline
        with self._timers_lock:
            heapq.heappush(self._timers, (deadline, next(self._counter), release))
        log.debug("Flash PB%d prelungit pana la +%.2fs", action.playback, duration)

    def _service_timers(self) -> None:
        now = time.monotonic()
        due: list[tuple[float, Action]] = []
        with self._timers_lock:
            while self._timers and self._timers[0][0] <= now:
                deadline, _, action = heapq.heappop(self._timers)
                due.append((deadline, action))
        for deadline, action in due:
            if not action.params.get("_release"):
                # actiune normala, doar amanata -> trece prin dispecerul complet
                # (ca sa functioneze si `duration` pe ea)
                self._dispatch(action)
                continue
            # eliberare: daca intre timp durata a fost prelungita, timerul e vechi
            key = action.hold_key()
            current = self.active_flashes.get(key)
            if current is not None and current > deadline:
                continue
            self.active_flashes.pop(key, None)
            for transport in self._route(action):
                if transport.send(action):
                    break

    def _release_all_flashes(self) -> None:
        for key in list(self.active_flashes.keys()):
            if not key.startswith("pb"):
                continue                 # butoanele Execute se sting singure
            try:
                playback = int(key[2:])
            except ValueError:
                continue
            action = Action(ActionType.PB_UNFLASH, {"playback": playback}, source="shutdown")
            for transport in self._route(action):
                if transport.send(action):
                    break
        self.active_flashes.clear()

    # ==================================================================
    #  Comenzi speciale
    # ==================================================================
    def panic(self) -> None:
        """Oprire de urgenta: goleste coada, elibereaza flash-urile,
        trimite RELEASE ALL si BLACKOUT."""
        log.warning("PANIC: se elibereaza toate playback-urile.")
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        with self._timers_lock:
            self._timers.clear()
        self._release_all_flashes()
        for atype in (ActionType.RELEASE_ALL, ActionType.BLACKOUT):
            action = Action(atype, {}, source="PANIC", priority=0)
            for transport in self._route(action):
                if transport.send(action):
                    break
        midi = self.transports.get("midi")
        if isinstance(midi, MIDITransport):
            midi.all_notes_off()

    def tap_burst(self, bpm: float, buttons: list[str], taps: int = 8) -> str:
        """Sincronizeaza unul sau mai multe Speed Master-e cu BPM-ul dat.

        Trimite `taps` apasari pe fiecare buton, la intervalul exact al
        beat-ului. Cand sunt mai multe butoane (Tap SP2 si Tap SP3), la
        fiecare beat se apasa pe rand toate - fiecare Speed Master isi
        masoara propriile intervale, iar decalajul de ~50 ms dintre ele nu
        conteaza, doar distanta dintre doua apasari pe ACELASI buton.

        Returneaza un text pentru jurnal / bara de status.
        """
        if bpm <= 20:
            return "tempo nedetectat"
        names = [b for b in buttons if b]
        if not names:
            return "niciun buton de tap configurat"

        mouse = self.transports.get("mouse")
        known = getattr(mouse, "exec_buttons", {}) if mouse else {}
        missing = [n for n in names if n not in known]
        if missing:
            return f"lipsesc din exec_buttons: {', '.join(missing)}"

        period = 60.0 / bpm
        for i in range(taps):
            for name in names:
                params = {"window": "exec", "name": name, "force": True}
                if i:
                    params["delay"] = round(i * period, 4)
                self.send(Action(ActionType.PALETTE, params,
                                 source="sincronizare BPM", priority=0))
        return (f"{taps} tap-uri x {len(names)} ({', '.join(names)}) "
                f"la {bpm:.1f} BPM, interval {period:.3f} s")

    def tap_buttons(self) -> list[str]:
        """Butoanele de tap din configurare (lista noua sau cel vechi)."""
        names = self.cfg.get("magicq.tap_buttons")
        if isinstance(names, list) and names:
            return [str(n) for n in names]
        single = self.cfg.get("magicq.tap_button")
        return [str(single)] if single else []

    def set_manual_mode(self, manual: bool) -> None:
        """In modul manual actiunile sunt doar afisate, nu trimise."""
        self.dry_run = manual

    def status_map(self) -> dict[str, dict]:
        out = {}
        for name, transport in self.transports.items():
            s = transport.status
            out[name] = {
                "connected": s.connected,
                "available": s.available,
                "sent": s.sent,
                "errors": s.errors,
                "detail": s.detail,
                "last_send": s.last_send,
                "priority": self.priority.index(name) if name in self.priority else 99,
            }
        return out
