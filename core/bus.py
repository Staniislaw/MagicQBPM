"""
core/bus.py
===========
Bus de evenimente thread-safe intre firele de executie.

Firul de analiza publica evenimente (BEAT, DROP, BUILDUP...), iar
consumatorii (motorul de reguli, UI-ul, logger-ul) le primesc fara sa
blocheze producatorul: fiecare abonat are propria coada marginita, iar
daca un abonat lent ramane in urma, se arunca cele mai vechi evenimente
ale lui - niciodata nu se blocheaza firul audio.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

log = logging.getLogger(__name__)


class EventType(str, Enum):
    """Tipurile de evenimente care circula prin aplicatie."""

    # --- ritm ---
    BEAT = "BEAT"
    DOWNBEAT = "DOWNBEAT"
    ONSET = "ONSET"
    BPM_CHANGE = "BPM_CHANGE"

    # --- structura melodiei ---
    SECTION_CHANGE = "SECTION_CHANGE"
    INTRO = "INTRO"
    BUILDUP = "BUILDUP"
    DROP = "DROP"
    BREAK = "BREAK"
    CLIMAX = "CLIMAX"
    OUTRO = "OUTRO"
    GROOVE = "GROOVE"

    # --- semnal ---
    SILENCE = "SILENCE"
    SIGNAL = "SIGNAL"

    # --- sistem ---
    ACTION_SENT = "ACTION_SENT"
    ACTION_FAILED = "ACTION_FAILED"
    RULE_FIRED = "RULE_FIRED"
    TRANSPORT_STATUS = "TRANSPORT_STATUS"
    AUDIO_ERROR = "AUDIO_ERROR"
    LOG = "LOG"


@dataclass
class Event:
    type: EventType
    t: float = field(default_factory=time.monotonic)
    data: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover - doar pentru log
        if not self.data:
            return self.type.value
        items = ", ".join(f"{k}={v}" for k, v in self.data.items())
        return f"{self.type.value}({items})"


class Subscription:
    """Coada personala a unui abonat."""

    def __init__(self, bus: "EventBus", types: set[EventType] | None, maxsize: int):
        self.bus = bus
        self.types = types
        self.queue: queue.Queue[Event] = queue.Queue(maxsize=maxsize)
        self.dropped = 0

    def offer(self, event: Event) -> None:
        if self.types is not None and event.type not in self.types:
            return
        try:
            self.queue.put_nowait(event)
        except queue.Full:
            # abonat lent: aruncam cel mai vechi eveniment al lui
            try:
                self.queue.get_nowait()
                self.queue.put_nowait(event)
            except (queue.Empty, queue.Full):
                pass
            self.dropped += 1

    def get(self, timeout: float | None = 0.1) -> Event | None:
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self, limit: int = 256) -> list[Event]:
        """Scoate tot ce s-a acumulat (folosit de UI la fiecare cadru)."""
        out: list[Event] = []
        for _ in range(limit):
            try:
                out.append(self.queue.get_nowait())
            except queue.Empty:
                break
        return out

    def close(self) -> None:
        self.bus.unsubscribe(self)


class EventBus:
    """Publish/subscribe simplu, fara dependinte externe."""

    def __init__(self) -> None:
        self._subs: list[Subscription] = []
        self._callbacks: list[tuple[set[EventType] | None, Callable[[Event], None]]] = []
        self._lock = threading.Lock()
        self.published = 0

    # ---------------- abonare ----------------
    def subscribe(self, types: list[EventType] | None = None, maxsize: int = 512) -> Subscription:
        sub = Subscription(self, set(types) if types else None, maxsize)
        with self._lock:
            self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)

    def subscribe_callback(self, callback: Callable[[Event], None],
                           types: list[EventType] | None = None) -> None:
        """Callback apelat SINCRON in firul care publica. A se folosi doar
        pentru operatii foarte scurte (nu blocati firul de analiza!)."""
        with self._lock:
            self._callbacks.append((set(types) if types else None, callback))

    # ---------------- publicare ----------------
    def publish(self, event: Event) -> None:
        with self._lock:
            subs = list(self._subs)
            callbacks = list(self._callbacks)
        self.published += 1
        for sub in subs:
            sub.offer(event)
        for types, cb in callbacks:
            if types is None or event.type in types:
                try:
                    cb(event)
                except Exception:  # noqa: BLE001 - un abonat rupt nu opreste audio
                    log.exception("Eroare in callback pentru %s", event.type)

    def emit(self, type_: EventType, **data: Any) -> Event:
        ev = Event(type_, time.monotonic(), data)
        self.publish(ev)
        return ev
