"""
core/config.py
==============
Incarcarea, validarea si salvarea configuratiei aplicatiei.

Doua fisiere in config/:
  * settings.json  -> hardware, transporturi, praguri de analiza, UI
  * rules.json     -> regulile IF/THEN (vezi core/rules.py)

Daca fisierele lipsesc, sunt generate automat din DEFAULTS.
Orice cheie lipsa dintr-un fisier existent este completata din DEFAULTS
(merge recursiv), deci fisierele vechi raman compatibile dupa update.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def _project_root() -> Path:
    """Folderul in care stau config/ si logs/.

    Ca script: folderul care contine main.py.
    Ca .exe (PyInstaller): folderul in care sta executabilul - NU folderul
    temporar de extractie. Asa raman config/rules_*.json si settings.json
    editabile langa program, nu ascunse intr-un temp care se sterge.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


ROOT = _project_root()
CONFIG_DIR = ROOT / "config"
SETTINGS_PATH = CONFIG_DIR / "settings.json"
SETTINGS_TEMPLATE = CONFIG_DIR / "settings.example.json"
RULES_PATH = CONFIG_DIR / "rules.json"


# ======================================================================
#  VALORI IMPLICITE
# ======================================================================
DEFAULTS: dict[str, Any] = {
    "audio": {
        "samplerate": 48000,
        # blocksize mic = latenta mica. 256 @48k = 5.33 ms per bloc.
        "block_size": 256,
        # fereastra FFT (2048 @48k = 42.7 ms) si pasul de analiza
        "fft_size": 2048,
        "hop_size": 512,          # -> 93.75 cadre de analiza / secunda
        # cat audio are voie sa se acumuleze in ring buffer inainte sa
        # incepem sa aruncam cadre vechi (protectie anti-drift/lag)
        "max_buffer_ms": 120,
        "sources": {
            "loopback": {
                "enabled": True,
                # null = device-ul de iesire implicit al Windows-ului,
                # sau o parte din numele boxelor ("Speakers", "Realtek", ...)
                "device": None,
                "gain": 1.0,
                # "auto" incearca pe rand: soundcard -> pyaudiowpatch -> sounddevice
                # (poti forta unul singur daca stii ce merge la tine)
                "backend": "auto",
            },
            "microphone": {
                "enabled": False,
                "device": None,     # null = microfonul implicit
                "gain": 1.0,
                # high-pass pe microfon: taie zgomotul de sub 40 Hz
                "highpass_hz": 40.0,
            },
        },
        # pre-amplificare globala aplicata dupa mixaj
        "input_gain": 1.0,
    },

    "analysis": {
        # ---- benzi de frecventa (Hz) ----
        "bands": {
            "sub_bass": [20, 60],
            "bass": [60, 250],
            "low_mid": [250, 500],
            "mid": [500, 2000],
            "high": [2000, 6000],
            "treble": [6000, 20000],
        },
        # normalizare automata (AGC) per banda
        "agc": {
            "enabled": True,
            "attack_ms": 40.0,      # cat de repede urca referinta
            "release_s": 12.0,      # cat de lent coboara referinta
            "floor_db": -70.0,      # nivelul considerat "0%"
            "min_range_db": 18.0,   # domeniu minim ca sa nu explodeze zgomotul
            # anvelopa de afisare/reguli (VU-metru): atac rapid, cadere lenta
            "meter_attack_ms": 20.0,
            "meter_release_ms": 180.0,
            # media lenta expusa regulilor ca bass_avg / mid_avg / ...
            "slow_average_ms": 1200.0,
        },
        # ---- onset ----
        # Detectorul primeste flux NORMALIZAT (flux / media lui mobila),
        # deci pragurile sunt adimensionale si nu depind de volum.
        "onset": {
            "threshold_mult": 1.45,  # x mediana ferestrei
            "threshold_delta": 0.15,
            "min_interval_ms": 55.0,
            "window_s": 1.2,
        },
        # ---- BPM ----
        "bpm": {
            "min": 60.0,
            "max": 200.0,
            "prefer_min": 80.0,      # domeniul in care "pliem" octavele
            "prefer_max": 175.0,
            "window_s": 8.0,         # istoric de novelty pentru autocorelatie
            "update_interval_s": 0.25,
            "prior_center": 128.0,   # tempo tipic pentru muzica de club
            "prior_width": 0.9,      # in octave (log2)
            "smoothing": 7,          # mediana peste ultimele N estimari
            "lock_tolerance": 0.02,  # 2% -> considerat acelasi tempo
            "manual_bpm": None,      # daca e setat, se ignora detectia
        },
        # ---- beat tracking ----
        "beat": {
            "phase_alpha": 0.18,     # cat de agresiv corectam faza din onset
            "max_correction": 0.35,  # fractiune din perioada
            "beats_per_bar": 4,
            "downbeat_decay": 0.92,  # uitarea acumulatorilor de downbeat
            "min_confidence": 0.25,  # sub asta nu emitem beat-uri
        },
        # ---- detectie sectiuni / drop ----
        "structure": {
            "short_s": 0.35,         # EMA "instant"
            "mid_s": 3.0,            # EMA de referinta
            "long_s": 15.0,          # EMA de context
            "history_s": 40.0,
            "warmup_s": 5.0,         # dupa aparitia semnalului
            # LINISTEA trebuie sa tina atat ca sa fie declarata; altfel
            # pauzele scurte dintr-o piesa reseteaza tot show-ul
            "silence_hold_s": 1.5,
            "drop": {
                "bass_jump": 0.28,      # crestere bass normalizat in 300 ms
                "energy_jump": 0.22,    # crestere RMS normalizat
                "flux_mult": 1.8,       # spectral flux fata de medie
                "window_ms": 350.0,
                "cooldown_s": 10.0,
                "require_context_s": 25.0,  # buildup/break recent -> bonus
                "score_threshold": 0.62,
            },
            "buildup": {
                "min_s": 2.5,           # panta calculata pe fereastra asta
                "slope_thr": 0.05,      # crestere energie / secunda (normalizat)
                "hf_slope_thr": 0.02,   # centroid/treble in crestere
                "score_threshold": 0.55,
            },
            "break": {
                "ratio": 0.62,          # short < ratio * mid  -> break
                "bass_max": 0.32,       # bass normalizat mic
                "min_s": 1.2,
            },
            "climax": {
                "ratio": 0.88,          # short > ratio * maxim recent
                "min_s": 3.0,
            },
            "intro_s": 12.0,
            "outro": {
                "decline_s": 20.0,
                "level_max": 0.30,
            },
            "min_section_s": 1.5,       # histerezis global
            # clasificator ML optional (scikit-learn / joblib)
            "ml": {
                "enabled": False,
                "model_path": "config/section_model.joblib",
                "weight": 0.5,          # amestec cu scorul euristic (0..1)
            },
        },
        "silence_db": -58.0,            # sub asta = liniste (nu declansam nimic)
    },

    "magicq": {
        "enabled": True,
        # ordinea de incercare a transporturilor pentru fiecare actiune
        "priority": ["osc", "midi", "keyboard", "mouse"],
        # daca nicio metoda nu suporta actiunea, se incearca maparea de taste
        "auto_keyboard_fallback": True,

        "osc": {
            "enabled": True,
            "host": "127.0.0.1",
            "port": 8000,              # MagicQ: Setup > View Settings > OSC Rx Port
            # ATENTIE: OSC cere MagicQ in "Unlocked Mode" (wing/interfata
            # ChamSys). In Demo Mode nu asculta pe portul OSC deloc.
            #
            # Adresele de mai jos sunt EXACT cele din manualul MagicQ
            # (manual/magicq/manual/OSC.html, "Table 1. OSC Addresses").
            # Doar playback-urile 1-10 si grilele Execute 1-10 sunt suportate.
            "addresses": {
                "pb_go": "/pb/{playback}/go",
                "pb_stop": "/pb/{playback}/pause",     # manualul: /pause, nu /stop
                "pb_release": "/pb/{playback}/release",
                "pb_flash": "/pb/{playback}/flash",    # 0 = 0%, non-zero = 100%
                "pb_level": "/pb/{playback}",          # 0..100 (int) sau 0.0..1.0
                "exec": "/exec/{page}/{item}",
                "rpc": "/rpc",                         # comanda remote ChamSys
                "blackout": "/dbo",
                "swap": "/swap",
                # MagicQ NU are adrese OSC pentru DMX direct sau pentru viteza
                # efectelor. Lasate null => actiunile trec automat pe alt
                # transport (tastatura), conform auto_keyboard_fallback.
                "dmx": None,
                "speed": None,
            },
            # /dbo conform manualului: 0 porneste blackout, non-zero il opreste
            "blackout_value": 0,
            # Daca nimeni nu asculta pe portul OSC (MagicQ in Demo Mode),
            # transportul se declara INACTIV, iar actiunile trec automat pe
            # tastatura. Fara asta, OSC ar "reusi" mereu (UDP nu confirma) si
            # ar inghiti toate comenzile. Pune false doar daca MagicQ e pe alt
            # PC si verificarea locala nu are sens.
            "require_listener": True,
            # MagicQ asteapta 0..100 pentru fadere
            "level_scale": 100.0,
            "level_as_int": True,
            "send_timeout_ms": 30,
        },

        "midi": {
            "enabled": False,
            "port_name": None,          # substring din numele portului MIDI out
            "channel": 1,               # 1..16
            # MagicQ: Setup > View Settings > MIDI, apoi mapari in Playback
            "playback_note_base": 60,   # playback N -> nota base + (N-1)
            "playback_cc_base": 20,     # nivel playback N -> CC base + (N-1)
            "exec_note_base": 36,
            "default_velocity": 127,
        },

        "keyboard": {
            "enabled": True,
            # aduce fereastra MagicQ in prim-plan inainte de a trimite taste
            "focus_window": True,
            # Fereastra MagicQ se cauta INTAI dupa proces (sigur), apoi dupa
            # titlu. Cautarea dupa titlu singura e periculoasa: un Explorer
            # deschis in D:\MAGICQ sau un terminal in D:\PYTHONMAGICQ se
            # potrivesc si tastele ar ajunge acolo.
            "window_process": "mqqt.exe",       # MagicQ PC
            "window_title_regex": "^MagicQ",
            "key_delay_ms": 12,         # pauza intre taste
            "hold_ms": 18,              # cat tinem tasta apasata
            # ------------------------------------------------------------
            #  MAPARILE OFICIALE MagicQ "Playback Shortcuts"
            # ------------------------------------------------------------
            # Din manualul MagicQ (Setup -> View Settings -> MagicQ Keyboard
            # Mode = "Playback shortcuts"):
            #     1..0    selecteaza playback-urile 1..10
            #     Q..P    GO   pe playback-urile 1..10
            #     A..;    STOP pe playback-urile 1..10
            #     \..     toggle test playback la 100%  (folosit ca FLASH)
            #     SPACE   Manual GO       #  Manual STOP
            #     [ / ]   pagina urmatoare / precedenta
            #     -       Release         `  mod Add/Swap
            #
            # ATENTIE: aceasta este SINGURA cale de control fara hardware
            # ChamSys - OSC, MIDI si protocolul remote cer "Unlocked Mode"
            # (wing sau interfata USB). Vezi README §5.
            #
            # O lista = o tasta per playback (index 0 = playback 1).
            # Un sir = sablon, cu {playback_digit} inlocuit (1..9, 0).
            "bindings": {
                "pb_go": ["q", "w", "e", "r", "t", "y", "u", "i", "o", "p"],
                "pb_stop": ["a", "s", "d", "f", "g", "h", "j", "k", "l", ";"],
                # toggle la 100%: prima apasare aprinde, a doua elibereaza
                "pb_flash": ["\\", "z", "x", "c", "v", "b", "n", "m", ",", "."],
                # selecteaza playback-ul, apoi Release
                "pb_release": "{playback_digit} -",
                "exec": None,
                "blackout": None,
                "release_all": None,
                # viteza efectelor NU are scurtatura de tastatura in MagicQ
                # (se face cu S + encoder X) -> actiunile 'speed' vor esua
                "speed_up": None,
                "speed_down": None,
                "tap_tempo": None,
            },
            # Secvente denumite, apelabile cu actiunea "macro" (si folosite
            # ca rezerva automata cand OSC/MIDI nu suporta o actiune).
            # ATENTIE: sunt EXEMPLE - pune aici scurtaturile din show-ul tau.
            "macros": {
                "strobe_on": "shift+8",
                "strobe_off": "alt+8",
                "color_fx": "shift+9",
                "next_cue": "enter",
                "go": "enter",
                "flash": "shift+1",
                "release": "alt+1",
                "blackout": "ctrl+alt+b",
                "release_all": "ctrl+alt+r",
                "speed": "ctrl+plus",
            },
        },

        "mouse": {
            "enabled": False,
            # coordonatele sunt relative la coltul ferestrei MagicQ
            # daca "relative_to_window": true, altfel absolute pe ecran
            "relative_to_window": True,
            # Fereastra MagicQ se cauta INTAI dupa proces (sigur), apoi dupa
            # titlu. Cautarea dupa titlu singura e periculoasa: un Explorer
            # deschis in D:\MAGICQ sau un terminal in D:\PYTHONMAGICQ se
            # potrivesc si tastele ar ajunge acolo.
            "window_process": "mqqt.exe",       # MagicQ PC
            "window_title_regex": "^MagicQ",
            "restore_cursor": True,     # readuce cursorul unde era
            "click_delay_ms": 25,
            # Grilele de palete din pagina curenta MagicQ. Se completeaza
            # automat cu:  py -3.12 tools/calibrate_palettes.py
            # Se retin doar prima si ultima casuta; restul se interpoleaza.
            "grids": {},
            # Butoane din fereastra Execute, cu nume: [rand, coloana], ambele
            # numarate de la 1. Le completezi dupa layout-ul tau; regulile le
            # apeleaza cu {"action":"palette","window":"exec","name":"strobe"}
            "exec_buttons": {},
            # marimea zonei client MagicQ la calibrare; daca difera la rulare,
            # coordonatele se scaleaza (auto_scale) sau se refuza click-ul
            "calibrated_size": None,
            # Citeste de pe ecran daca un buton Execute e aprins (fundal rosu)
            # inainte sa-l apese. Fara asta, aplicatia nu are cum sa stie ce
            # era deja pornit si efectele se suprapun.
            "verify_button_state": True,
            "active_red_margin": 22,
            "auto_scale": True,
            "targets": {
                # "nume_target": [x, y] relativ la coltul ferestrei MagicQ
            },
        },

        # butonul de tap tempo din fereastra Execute, folosit de butonul
        # "BPM -> MagicQ" din interfata si de regulile de resincronizare
        "tap_button": "tap_tempo",
        "tap_count": 8,
        # limita globala de comenzi/secunda (protectie anti-flood MagicQ)
        "rate_limit_per_s": 40,
        # dupa cate secunde fara comenzi consideram transportul "idle"
        "status_idle_s": 5.0,
    },

    "rules": {
        # Ce set de reguli se incarca la pornire, cand nu se da --rules.
        # Implicit e setul pentru fereastra Execute; celelalte rules_*.json
        # sunt exemple pentru alte moduri de control.
        "file": "config/rules_execute.json",
        "enabled": True,
        # multiplicator global de sensibilitate aplicat pragurilor de energie
        "sensitivity": 1.0,
        # cooldown implicit daca regula nu specifica
        "default_cooldown_s": 0.35,
        # in modul "manual" analiza merge dar nu se trimite nimic spre MagicQ
        "auto_mode": True,
    },

    "ui": {
        "enabled": True,
        "fps": 60,
        "spectrogram_seconds": 8.0,
        "waveform_seconds": 3.0,
        "theme": "dark",
        "log_lines": 400,
        "start_analysis_on_launch": True,
        "palettes_file": "config/palettes.json",
    },

    "logging": {
        "level": "INFO",
        "file": "logs/magicq_audio.log",
        "console": True,
    },
}


# ======================================================================
#  UTILITARE
# ======================================================================
def deep_merge(base: dict, override: dict) -> dict:
    """Merge recursiv: `override` are prioritate, dar cheile lipsa vin din `base`."""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


class Config:
    """Wrapper peste dict-ul de configurare, cu acces prin cale ('audio.hop_size')."""

    def __init__(self, data: dict[str, Any], path: Path | None = None):
        self.data = data
        self.path = path

    # ---- acces ----
    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, path: str, value: Any) -> None:
        parts = path.split(".")
        node = self.data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def section(self, name: str) -> dict:
        return self.get(name, {}) or {}

    # ---- persistenta ----
    def save(self, path: Path | None = None) -> Path:
        target = Path(path or self.path or SETTINGS_PATH)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=4, ensure_ascii=False)
        os.replace(tmp, target)
        log.info("Configuratie salvata in %s", target)
        return target


def load_settings(path: str | Path | None = None) -> Config:
    """Incarca settings.json (il creeaza daca lipseste) si completeaza cheile lipsa."""
    target = Path(path) if path else SETTINGS_PATH
    if target.exists():
        try:
            with open(target, "r", encoding="utf-8") as fh:
                user_data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            log.error("settings.json invalid (%s). Se folosesc valorile implicite.", exc)
            user_data = {}
    elif SETTINGS_TEMPLATE.exists():
        # Prima pornire pe un PC nou: pornim de la sablon, ca sa nu se piarda
        # maparea butoanelor si pragurile reglate. Calibrarea de mouse NU e
        # in sablon - aia e specifica fiecarei masini.
        try:
            with open(SETTINGS_TEMPLATE, "r", encoding="utf-8") as fh:
                user_data = json.load(fh)
            user_data.pop("_README", None)
            log.info("settings.json lipseste - se creeaza din %s",
                     SETTINGS_TEMPLATE.name)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("settings.example.json invalid (%s) - se folosesc "
                        "valorile implicite.", exc)
            user_data = {}
    else:
        user_data = {}
        log.info("settings.json lipseste - se genereaza %s", target)

    merged = deep_merge(DEFAULTS, user_data)
    cfg = Config(merged, target)
    if not target.exists():
        cfg.save()
    return cfg


def load_rules_file(path: str | Path | None = None) -> Any:
    """Incarca rules.json brut (dict simplu sau format extins). Vezi core/rules.py."""
    target = Path(path) if path else RULES_PATH
    if not target.exists():
        log.warning("rules.json lipseste la %s", target)
        return {"rules": []}
    with open(target, "r", encoding="utf-8") as fh:
        return json.load(fh)


def setup_logging(cfg: Config) -> None:
    """Configureaza logging pe consola + fisier rotativ simplu."""
    level = getattr(logging, str(cfg.get("logging.level", "INFO")).upper(), logging.INFO)
    handlers: list[logging.Handler] = []

    if cfg.get("logging.console", True):
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
                                              datefmt="%H:%M:%S"))
        handlers.append(stream)

    logfile = cfg.get("logging.file")
    if logfile:
        p = ROOT / logfile
        p.parent.mkdir(parents=True, exist_ok=True)
        fileh = logging.FileHandler(p, encoding="utf-8")
        fileh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s"))
        handlers.append(fileh)

    logging.basicConfig(level=level, handlers=handlers, force=True)
    # bibliotecile terte sunt zgomotoase pe DEBUG
    for noisy in ("PyQt6", "matplotlib", "numba"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
