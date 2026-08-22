"""
core/rules.py
=============
Motorul de reguli IF / THEN - "creierul" light operator-ului.

Doua formate acceptate in config/rules.json:

1) FORMA SCURTA (exact cea din specificatie):

    {
        "Drop": "Flash 4",
        "BuildUp": "Increase Speed",
        "Break": "Release Flash",
        "Bass": "Playback 5",
        "High": "Color FX",
        "BPM>140": "Speed=180"
    }

2) FORMA EXTINSA (control complet):

    {
      "rules": [
        {
          "name": "Drop -> strobe",
          "on": "DROP",                    // eveniment
          "if": "energy > 60",             // conditie suplimentara (optional)
          "cooldown": 4.0,
          "quantize": "beat",              // aliniaza actiunea la beat
          "do": [{"action": "pb_flash", "playback": 4, "duration": 0.8}]
        },
        {
          "name": "Bass puternic -> chase",
          "when": "bass > 80",             // expresie evaluata continuu
          "mode": "edge",                  // edge | level
          "do":   [{"action": "pb_go", "playback": 5}],
          "undo": [{"action": "pb_release", "playback": 5}]
        }
      ]
    }

Variabile disponibile in expresii (vezi core/state.py -> rule_vars):
    bpm, bpm_conf, beat, downbeat, beat_in_bar, bar, phase,
    onset, onset_strength, onset_rate,
    rms (0..1), rms_db, peak, loudness (0..100),
    sub, bass, low_mid, mid, high, treble, highs, lows   (toate 0..100)
    centroid (Hz), brightness (0..100), flatness, flux,
    section ("DROP", "BUILDUP", ...), section_age,
    energy, energy_mid, energy_long (0..100), energy_slope,
    drop_score, buildup_score, drop_age, silence

Expresiile sunt evaluate cu un interpretor AST restrictionat: sunt
permise doar comparatii, operatii aritmetice, and/or/not si functiile
abs/min/max/round/int/float. Nu se poate executa cod arbitrar.
"""

from __future__ import annotations

import ast
import logging
import random
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from core.bus import EventBus, EventType, Subscription
from core.state import SharedState
from magicq.actions import Action, ActionType, parse_action
from magicq.shorthand import parse_shorthand_list

log = logging.getLogger(__name__)


# ======================================================================
#  Evaluator de expresii (sigur)
# ======================================================================
_ALLOWED_NODES = (
    ast.Expression, ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Compare, ast.Name,
    ast.Load, ast.Constant, ast.And, ast.Or, ast.Not, ast.Add, ast.Sub,
    ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow, ast.USub, ast.UAdd,
    ast.Gt, ast.GtE, ast.Lt, ast.LtE, ast.Eq, ast.NotEq, ast.In, ast.NotIn,
    ast.IfExp, ast.Call, ast.Tuple, ast.List, ast.Set,
)
_ALLOWED_FUNCS: dict[str, Any] = {
    "abs": abs, "min": min, "max": max, "round": round,
    "int": int, "float": float, "len": len, "bool": bool,
}

#: sinonime in limbaj natural -> nume de variabila
_ALIASES = [
    ("high frequency", "high"), ("high freq", "high"), ("hi freq", "high"),
    ("sub bass", "sub"), ("subbass", "sub"), ("low mid", "low_mid"),
    ("lowmid", "low_mid"), ("high freqs", "high"), ("treble energy", "treble"),
    ("bass energy", "bass"), ("beat in bar", "beat_in_bar"),
    ("onset rate", "onset_rate"), ("drop score", "drop_score"),
    ("buildup score", "buildup_score"), ("build up", "buildup_score"),
    ("section age", "section_age"), ("energy slope", "energy_slope"),
    ("rms db", "rms_db"), ("bpm conf", "bpm_conf"),
]


class ExpressionError(ValueError):
    pass


def normalize_expression(text: str) -> str:
    """Curata o expresie scrisa 'omeneste' si o face valida in Python.

    Sirurile intre ghilimele NU sunt atinse (ca sa functioneze comparatii
    de tipul  section == "DROP"), restul este trecut la litere mici, cu
    sinonimele inlocuite si '=' transformat in '=='.
    """
    parts = re.split(r"(\"[^\"]*\"|'[^']*')", str(text).strip())
    out: list[str] = []
    for i, part in enumerate(parts):
        if i % 2 == 1:          # segment intre ghilimele - se pastreaza
            out.append(part)
            continue
        s = part.lower()
        for phrase, var in _ALIASES:
            s = s.replace(phrase, var)
        # '%' are doua intelesuri: sufix de procent ("bass > 80%") si modulo
        # ("bar % 4 == 0", util pentru "la fiecare 4 masuri"). Modulo se
        # scrie cu spatii in jur, deci il protejam inainte sa stergem
        # sufixele de procent.
        s = s.replace(" % ", "\x00MOD\x00")
        s = s.replace("%", "")
        s = s.replace("\x00MOD\x00", " % ")
        s = re.sub(r"(?<![<>=!])=(?!=)", "==", s)   # '=' singur -> '=='
        out.append(s)
    return "".join(out).strip()


def compile_expression(text: str):
    """Compileaza o expresie, validand fiecare nod AST."""
    normalized = normalize_expression(text)
    try:
        tree = ast.parse(normalized, mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"Expresie invalida '{text}': {exc}") from exc
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ExpressionError(
                f"Constructie interzisa in '{text}': {type(node).__name__}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCS:
                raise ExpressionError(f"Functie interzisa in '{text}'")
    return compile(tree, "<rule>", "eval")


def eval_expression(code, variables: dict[str, Any]) -> bool:
    env = dict(_ALLOWED_FUNCS)
    env.update(variables)
    try:
        return bool(eval(code, {"__builtins__": {}}, env))  # noqa: S307 - AST validat
    except Exception as exc:  # noqa: BLE001
        log.debug("Evaluare esuata: %s", exc)
        return False


# ======================================================================
#  Regula
# ======================================================================
@dataclass
class Rule:
    name: str
    actions: list[Action] = field(default_factory=list)
    undo_actions: list[Action] = field(default_factory=list)
    event: EventType | None = None
    expr_text: str = ""
    expr_code: Any = None
    guard_text: str = ""
    guard_code: Any = None
    mode: str = "edge"                  # edge | level
    cooldown: float = 0.35
    # Timpul minim in care o regula nu-si poate schimba din nou starea.
    # Fara el, o valoare care oscileaza in jurul pragului (bass 79/81%)
    # produce zeci de perechi do/undo pe secunda catre MagicQ.
    min_hold: float = 0.35
    quantize: str = "off"               # off | beat | downbeat
    enabled: bool = True
    priority: int = 5
    description: str = ""

    # --- runtime ---
    last_fired: float = 0.0
    fired_count: int = 0
    active: bool = False                # starea anterioara a expresiei
    last_state_change: float = 0.0
    did_fire: bool = False              # 'do' chiar a plecat (nu blocat de cooldown)
    last_error: str = ""

    def condition_text(self) -> str:
        if self.event is not None:
            base = f"ON {self.event.value}"
            if self.guard_text:
                base += f" IF {self.guard_text}"
            return base
        return f"IF {self.expr_text}" + (f" [{self.mode}]" if self.mode != "edge" else "")

    def actions_text(self) -> str:
        return " + ".join(a.describe() for a in self.actions) or "-"


# ======================================================================
#  Incarcarea regulilor
# ======================================================================
_EVENT_KEYS = {
    "drop": EventType.DROP,
    "buildup": EventType.BUILDUP,
    "build_up": EventType.BUILDUP,
    "build-up": EventType.BUILDUP,
    "break": EventType.BREAK,
    "climax": EventType.CLIMAX,
    "intro": EventType.INTRO,
    "outro": EventType.OUTRO,
    "groove": EventType.GROOVE,
    "beat": EventType.BEAT,
    "downbeat": EventType.DOWNBEAT,
    "onset": EventType.ONSET,
    "bpm_change": EventType.BPM_CHANGE,
    "section_change": EventType.SECTION_CHANGE,
    "silence": EventType.SILENCE,
    "signal": EventType.SIGNAL,
}

#: cheile din forma scurta care sunt de fapt praguri pe o banda
_SHORT_LEVEL_DEFAULTS = {
    "bass": "bass > 75",
    "sub": "sub > 75",
    "sub_bass": "sub > 75",
    "mid": "mid > 70",
    "low_mid": "low_mid > 70",
    "high": "high > 70",
    "treble": "treble > 70",
    "rms": "rms > 0.85",
    "loudness": "loudness > 85",
    "energy": "energy > 80",
}


def load_rules(data: Any, defaults: dict | None = None) -> list[Rule]:
    """Construieste lista de reguli din continutul rules.json."""
    defaults = defaults or {}
    default_cooldown = float(defaults.get("default_cooldown_s", 0.35))
    rules: list[Rule] = []

    if isinstance(data, dict) and "rules" in data:
        raw_rules = data.get("rules") or []
        for idx, spec in enumerate(raw_rules):
            try:
                rules.append(_build_rule(spec, idx, default_cooldown))
            except Exception as exc:  # noqa: BLE001
                log.error("Regula #%d ignorata (%s): %s", idx, exc, spec)
        # forma extinsa poate contine si un bloc scurt
        short = data.get("simple") or {}
        rules.extend(_load_short(short, default_cooldown))
        return rules

    if isinstance(data, dict):
        return _load_short(data, default_cooldown)

    if isinstance(data, list):
        for idx, spec in enumerate(data):
            try:
                rules.append(_build_rule(spec, idx, default_cooldown))
            except Exception as exc:  # noqa: BLE001
                log.error("Regula #%d ignorata (%s)", idx, exc)
        return rules

    log.error("Format rules.json necunoscut (%s)", type(data).__name__)
    return rules


def _load_short(mapping: dict, default_cooldown: float) -> list[Rule]:
    rules: list[Rule] = []
    for key, value in (mapping or {}).items():
        if key.startswith("_"):        # chei de comentariu
            continue
        try:
            rules.append(_build_short_rule(str(key), value, default_cooldown))
        except Exception as exc:  # noqa: BLE001
            log.error("Regula scurta '%s' ignorata: %s", key, exc)
    return rules


def _build_short_rule(key: str, value: Any, default_cooldown: float) -> Rule:
    actions = (parse_shorthand_list(value, key) if isinstance(value, str)
               else [parse_action(v, key) for v in (value if isinstance(value, list) else [value])])
    clean = key.strip()
    low = clean.lower().replace(" ", "_")

    # 1. eveniment cunoscut ("Drop", "BuildUp", "Break", ...)
    if low in _EVENT_KEYS:
        return Rule(name=clean, actions=actions, event=_EVENT_KEYS[low],
                    cooldown=max(default_cooldown, 1.0 if low in
                                 ("drop", "buildup", "break", "climax") else default_cooldown),
                    description="regula scurta")

    # 2. prag implicit pe o banda ("Bass", "High", "RMS")
    if low in _SHORT_LEVEL_DEFAULTS:
        expr = _SHORT_LEVEL_DEFAULTS[low]
        return Rule(name=clean, actions=actions, expr_text=expr,
                    expr_code=compile_expression(expr), mode="edge",
                    cooldown=max(default_cooldown, 0.5), description="prag implicit")

    # 3. expresie explicita ("BPM>140", "Bass > 80")
    expr = clean
    return Rule(name=clean, actions=actions, expr_text=expr,
                expr_code=compile_expression(expr), mode="edge",
                cooldown=default_cooldown, description="regula scurta")


def _build_rule(spec: dict, index: int, default_cooldown: float) -> Rule:
    if not isinstance(spec, dict):
        raise ValueError("regula trebuie sa fie un obiect JSON")

    name = str(spec.get("name") or spec.get("id") or f"rule_{index + 1}")
    raw_actions = spec.get("do") or spec.get("then") or spec.get("actions") or []
    if isinstance(raw_actions, (str, dict)):
        raw_actions = [raw_actions]
    actions = [parse_action(a, name) if isinstance(a, dict) else parse_shorthand_list(a, name)
               for a in raw_actions]
    flat: list[Action] = []
    for item in actions:
        flat.extend(item if isinstance(item, list) else [item])

    raw_undo = spec.get("undo") or spec.get("else") or []
    if isinstance(raw_undo, (str, dict)):
        raw_undo = [raw_undo]
    undo: list[Action] = []
    for a in raw_undo:
        parsed = parse_action(a, name) if isinstance(a, dict) else parse_shorthand_list(a, name)
        undo.extend(parsed if isinstance(parsed, list) else [parsed])

    event = None
    on = spec.get("on") or spec.get("event")
    if on:
        key = str(on).strip().lower().replace(" ", "_")
        if key in _EVENT_KEYS:
            event = _EVENT_KEYS[key]
        else:
            try:
                event = EventType(str(on).upper())
            except ValueError as exc:
                raise ValueError(f"eveniment necunoscut: '{on}'") from exc

    expr_text = str(spec.get("when") or spec.get("if_expr") or "").strip()
    expr_code = compile_expression(expr_text) if expr_text else None
    guard_text = str(spec.get("if") or "").strip() if event is not None else ""
    guard_code = compile_expression(guard_text) if guard_text else None

    if event is None and expr_code is None:
        raise ValueError("regula fara 'on' si fara 'when'")

    return Rule(
        name=name, actions=flat, undo_actions=undo, event=event,
        expr_text=expr_text, expr_code=expr_code,
        guard_text=guard_text, guard_code=guard_code,
        mode=str(spec.get("mode", "edge")).lower(),
        cooldown=float(spec.get("cooldown", default_cooldown)),
        min_hold=float(spec.get("hold", spec.get("min_hold", 0.35))),
        quantize=str(spec.get("quantize", "off")).lower(),
        enabled=bool(spec.get("enabled", True)),
        priority=int(spec.get("priority", 5)),
        description=str(spec.get("description", "")),
    )


# ======================================================================
#  Motorul
# ======================================================================
class RuleEngine(threading.Thread):
    """Fir separat: asculta evenimentele, evalueaza expresiile la 100 Hz
    si trimite actiunile in router."""

    POLL_HZ = 100.0

    def __init__(self, cfg, state: SharedState, bus: EventBus, router, rules: list[Rule]):
        super().__init__(name="RuleEngine", daemon=True)
        self.cfg = cfg
        self.state = state
        self.bus = bus
        self.router = router
        self.rules = rules
        self.enabled = bool(cfg.get("rules.enabled", True))
        self.auto_mode = bool(cfg.get("rules.auto_mode", True))
        self._stop = threading.Event()
        self._sub: Subscription | None = None
        self._pending_quantized: list[tuple[str, Rule, list[Action]]] = []
        self._cycle_state: dict[int, int] = {}   # rotatia valorilor ("cycle")
        # grupuri exclusive: ce buton e aprins acum in fiecare grup
        self._exclusive_state: dict[str, tuple[Any, Action]] = {}
        self._exclusive_since: dict[str, float] = {}   # cand s-a schimbat ultima oara
        self._lock = threading.Lock()
        self.fired_total = 0
        self.last_fired_rule = ""
        self.last_fired_t = 0.0

    # ------------------------------------------------------------------
    def run(self) -> None:
        self._sub = self.bus.subscribe(maxsize=1024)
        period = 1.0 / self.POLL_HZ
        next_poll = time.monotonic()
        log.info("Motor de reguli pornit cu %d reguli.", len(self.rules))

        while not self._stop.is_set():
            # 1. evenimente (asteptam scurt ca sa nu ardem CPU)
            event = self._sub.get(timeout=0.005)
            while event is not None:
                self._handle_event(event)
                event = self._sub.get(timeout=0.0)

            # 2. expresii, la rata fixa
            now = time.monotonic()
            if now >= next_poll:
                next_poll = now + period
                self._evaluate_expressions(now)

        if self._sub:
            self._sub.close()
        log.info("Motor de reguli oprit (%d declansari).", self.fired_total)

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    def _handle_event(self, event) -> None:
        # actiunile amanate pana la urmatorul beat / downbeat
        if event.type is EventType.BEAT:
            self._flush_quantized("beat")
        elif event.type is EventType.DOWNBEAT:
            self._flush_quantized("beat")
            self._flush_quantized("downbeat")

        if not self.enabled:
            return
        variables = None
        for rule in self.rules:
            if not rule.enabled or rule.event is not event.type:
                continue
            if rule.guard_code is not None:
                if variables is None:
                    # Datele EVENIMENTULUI au prioritate peste snapshot-ul
                    # curent: un DROP transporta drop_score-ul din momentul
                    # declansarii, iar pana cand ajunge regula sa fie
                    # evaluata (cateva ms mai tarziu) valoarea din snapshot
                    # a scazut deja. Fara asta, garzile pe scoruri nu se
                    # potrivesc niciodata.
                    variables = self._variables()
                    variables.update(event.data)
                if not eval_expression(rule.guard_code, variables):
                    continue
            self._fire(rule, rule.actions, event.t)

    def _evaluate_expressions(self, now: float) -> None:
        if not self.enabled:
            return
        active_rules = [r for r in self.rules if r.enabled and r.expr_code is not None]
        if not active_rules:
            return
        variables = self._variables()
        for rule in active_rules:
            value = eval_expression(rule.expr_code, variables)

            if rule.mode == "level":
                # atat timp cat e adevarat: re-declansare limitata de cooldown
                if value:
                    if self._fire(rule, rule.actions, now):
                        rule.did_fire = True
                elif rule.active and rule.undo_actions and rule.did_fire:
                    self._fire(rule, rule.undo_actions, now, ignore_cooldown=True)
                    rule.did_fire = False
                rule.active = value
                continue

            # mode == "edge": doar la SCHIMBAREA starii, cu anti-chatter
            if value == rule.active:
                continue
            if (now - rule.last_state_change) < rule.min_hold:
                continue                      # inca in perioada de stabilizare
            rule.last_state_change = now
            rule.active = value
            if value:
                rule.did_fire = self._fire(rule, rule.actions, now)
            elif rule.undo_actions and rule.did_fire:
                # 'undo' se trimite doar daca 'do' chiar a plecat spre MagicQ
                self._fire(rule, rule.undo_actions, now, ignore_cooldown=True)
                rule.did_fire = False

    def _variables(self) -> dict[str, Any]:
        return self.state.snapshot.rule_vars()

    # ------------------------------------------------------------------
    def _fire(self, rule: Rule, actions: list[Action], now: float,
              ignore_cooldown: bool = False) -> bool:
        """True daca actiunile chiar au fost trimise (nu blocate de cooldown)."""
        if not actions:
            return False
        if not ignore_cooldown and (now - rule.last_fired) < rule.cooldown:
            return False
        rule.last_fired = now
        rule.fired_count += 1
        self.fired_total += 1
        self.last_fired_rule = rule.name
        self.last_fired_t = now

        if rule.quantize in ("beat", "downbeat"):
            with self._lock:
                self._pending_quantized.append((rule.quantize, rule, list(actions)))
            return True

        self._dispatch(rule, actions)
        return True

    def _dispatch(self, rule: Rule, actions: list[Action]) -> None:
        actions = [self._resolve_cycle(a) for a in actions]
        actions = self._apply_exclusive(actions)
        actions = self._expand_repeats(actions)
        if not actions:
            return
        self.bus.emit(EventType.RULE_FIRED, rule=rule.name,
                      actions=", ".join(a.describe() for a in actions))
        if not self.auto_mode:
            return
        for action in actions:
            self.router.send(action)

    # ------------------------------------------------------------------
    def _apply_exclusive(self, actions: list[Action]) -> list[Action]:
        """Grupuri exclusive pentru butoane de tip TOGGLE.

        Butoanele din fereastra Execute sunt cue stack-uri: o apasare le
        aprinde, alta le stinge. Ca sa ai mereu o singura miscare activa,
        marchezi actiunile cu {"exclusive": "miscare"}. Motorul tine minte
        ce buton e aprins in fiecare grup si, inainte sa aprinda altul, il
        apasa pe cel vechi ca sa-l stinga.

        Daca se cere exact butonul deja aprins, nu se face nimic (altfel
        l-ar stinge).
        """
        out: list[Action] = []
        for action in actions:
            if action.type is ActionType.EXCLUSIVE_OFF:
                group = str(action.params.get("group", ""))
                previous = self._exclusive_state.pop(group, None)
                self._exclusive_since.pop(group, None)
                if previous:
                    out.append(previous[1])
                continue

            group = action.params.get("exclusive")
            if not group:
                out.append(action)
                continue

            key = action.params.get("name") or (action.params.get("row"),
                                                action.params.get("col"),
                                                action.params.get("item"))
            previous = self._exclusive_state.get(group)
            if previous and previous[0] == key:
                continue                       # deja aprins - nu-l stingem

            # Timp minim intre doua schimbari in acelasi grup. Sectiunile
            # muzicale se pot schimba la cateva secunde; fara asta, capurile
            # ar sari intre stiluri de miscare tot timpul. Cu
            # "exclusive_hold": 25 se schimba cel mult o data la 25 s.
            hold = float(action.params.get("exclusive_hold", 0.0))
            if previous and hold > 0:
                elapsed = time.monotonic() - self._exclusive_since.get(group, 0.0)
                if elapsed < hold:
                    log.debug("Grup '%s': schimbare amanata (%.1fs din %.1fs)",
                              group, elapsed, hold)
                    continue

            # Stingem TOTI ceilalti membri ai grupului, nu doar pe cel pe
            # care il tinem noi minte. Transportul verifica pe ecran daca
            # butonul chiar e aprins, deci o comanda "off" pe unul deja stins
            # nu face nimic. Asa se rezolva si cazul in care ceva era pornit
            # inainte sa porneasca aplicatia, sau ai apasat tu manual.
            members = action.params.get("members") or []
            for member in members:
                if member == key:
                    continue
                out.append(Action(action.type,
                                  {**self._base_params(action), "name": member,
                                   "ensure": "off"},
                                  source=action.source, priority=action.priority,
                                  transport=action.transport))
            if previous and previous[0] != key and not members:
                out.append(previous[1])        # fara lista de membri: cel vechi

            self._exclusive_since[group] = time.monotonic()
            params = {k: v for k, v in action.params.items()
                      if k not in ("exclusive", "exclusive_hold", "members")}
            params["ensure"] = "on"
            fresh = Action(action.type, params, source=action.source,
                           priority=action.priority, transport=action.transport)
            out.append(fresh)
            off = Action(action.type,
                         {**self._base_params(action), "name": key, "ensure": "off"},
                         source=action.source, priority=action.priority,
                         transport=action.transport)
            self._exclusive_state[group] = (key, off)
        return out

    def _expand_repeats(self, actions: list[Action]) -> list[Action]:
        """{"repeat": 8, "interval": "beat"} -> 8 actiuni pe beat-uri succesive.

        Asa se poate face "blackout, apoi flash in ritmul muzicii" sau o
        rafala de tap tempo: intervalul se ia din BPM-ul detectat in acel
        moment, iar router-ul le programeaza in viitor.
        """
        out: list[Action] = []
        for action in actions:
            count = int(action.params.get("repeat", 1))
            if count <= 1:
                out.append(action)
                continue
            interval = action.params.get("interval", "beat")
            if isinstance(interval, str) and interval.lower().startswith("beat"):
                bpm = self.state.snapshot.bpm
                step = 60.0 / bpm if bpm > 20 else 0.5
                # "interval": "beat2" = la fiecare al doilea beat
                suffix = interval.lower().replace("beat", "").strip()
                if suffix.isdigit():
                    step *= int(suffix)
            else:
                step = float(interval)
            for i in range(count):
                params = {k: v for k, v in action.params.items()
                          if k not in ("repeat", "interval")}
                if i:
                    params["delay"] = round(i * step, 4)
                out.append(Action(action.type, params, source=action.source,
                                  priority=action.priority, transport=action.transport))
        return out

    @staticmethod
    def _base_params(action: Action) -> dict:
        """Parametrii comuni (fereastra, transport) fara tinta si fara grup."""
        skip = ("exclusive", "exclusive_hold", "members", "name", "row", "col",
                "item", "cycle", "random", "labels", "label", "duration", "ensure")
        return {k: v for k, v in action.params.items() if k not in skip}

    # ------------------------------------------------------------------
    def _resolve_cycle(self, action: Action) -> Action:
        """Rotatia automata a valorilor: {"cycle": [1, 5, 7, 10]}.

        La fiecare declansare se ia urmatoarea valoare din lista (sau una
        aleatoare cu "random": true). Asa se schimba singure culorile /
        gobo-urile, fara sa scrii o regula pentru fiecare.

        Indexul e tinut aici, in firul de reguli (unic), deci nu e nevoie
        de sincronizare. Se intoarce o COPIE, ca actiunea din regula sa
        ramana neschimbata pentru data viitoare.
        """
        cycle = action.params.get("cycle")
        if not cycle:
            return action
        key = id(action)
        if action.params.get("random"):
            index = random.randrange(len(cycle))
        else:
            index = self._cycle_state.get(key, -1) + 1
        self._cycle_state[key] = index
        value = cycle[index % len(cycle)]

        params = {k: v for k, v in action.params.items() if k not in ("cycle", "random")}
        if isinstance(value, dict):
            params.update(value)
        else:
            params["item"] = value
        labels = action.params.get("labels")
        if labels and not isinstance(value, dict):
            try:
                params["label"] = labels[index % len(labels)]
            except (TypeError, IndexError):
                pass
        return Action(action.type, params, source=action.source,
                      priority=action.priority, transport=action.transport)

    def _flush_quantized(self, kind: str) -> None:
        with self._lock:
            due = [item for item in self._pending_quantized if item[0] == kind]
            if not due:
                return
            self._pending_quantized = [item for item in self._pending_quantized
                                       if item[0] != kind]
        for _, rule, actions in due:
            self._dispatch(rule, actions)

    # ------------------------------------------------------------------
    #  Control din UI
    # ------------------------------------------------------------------
    def set_auto_mode(self, auto: bool) -> None:
        self.auto_mode = auto
        log.info("Mod %s", "AUTOMAT" if auto else "MANUAL (nu se trimite nimic)")

    def set_rule_enabled(self, name: str, enabled: bool) -> None:
        for rule in self.rules:
            if rule.name == name:
                rule.enabled = enabled
                log.info("Regula '%s' %s", name, "activata" if enabled else "dezactivata")

    def trigger_rule(self, name: str) -> bool:
        """Declansare manuala (buton TEST din UI)."""
        for rule in self.rules:
            if rule.name == name:
                self._dispatch(rule, rule.actions)
                rule.fired_count += 1
                rule.last_fired = time.monotonic()
                return True
        return False

    def replace_rules(self, rules: list[Rule]) -> None:
        """Reincarcare la cald a fisierului rules.json."""
        with self._lock:
            self.rules = rules
            self._pending_quantized.clear()
        log.info("Reguli reincarcate: %d", len(rules))
