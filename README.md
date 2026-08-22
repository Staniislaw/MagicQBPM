# MagicQ Audio Reactive Controller

Aplicatie desktop Windows (Python 3.12) care asculta muzica in timp real si
comanda automat MagicQ — ca un light operator uman, fara timecode si fara DMX
preprogramat.

Analizeaza **sunetul redat de PC** (Spotify, YouTube, Winamp, VirtualDJ — prin
WASAPI loopback) si/sau **microfonul**, detecteaza BPM, beat, downbeat, onset,
energia pe 6 benzi, si structura melodiei (intro / build-up / drop / break /
climax / outro), apoi trimite comenzi catre MagicQ prin **OSC → MIDI → tastatura
→ mouse**, in aceasta ordine de prioritate.

---

## 1. Instalare

```bash
py -3.12 -m pip install -r requirements.txt
```

Verifica ce vede aplicatia:

```bash
py -3.12 main.py --list-devices
```

Verifica captura, cu muzica pornita:

```bash
py -3.12 tools/loopback_check.py 10
```

Verifica lantul de analiza (fara placa de sunet):

```bash
py -3.12 main.py --selftest
```

Verifica legatura cu MagicQ (cu MagicQ pornit):

```bash
py -3.12 tools/magicq_test.py
```

Porneste:

```bash
py -3.12 main.py
```

Alte moduri:

| Comanda | Ce face |
|---|---|
| `py -3.12 main.py --rules config/rules_demo_mode.json` | **reguli pentru MagicQ PC in Demo Mode** (doar tastatura, playback-uri 1-10) |
| `py -3.12 main.py --headless` | fara interfata, doar consola |
| `py -3.12 main.py --simulate 128` | sursa audio sintetica la 128 BPM (test fara boxe) |
| `py -3.12 main.py --no-magicq` | analizeaza, dar nu trimite nimic |
| `py -3.12 main.py --debug` | log detaliat |

---

## 2. Captura sunetului redat de PC (important)

PortAudio-ul livrat cu `sounddevice` (19.7.0-devel) **nu** expune loopback-ul
WASAPI, iar `sd.WasapiSettings` nu are parametru `loopback`. De aceea aplicatia
are trei backend-uri, incercate automat in ordine:

| Backend | Pachet | Observatii |
|---|---|---|
| `soundcard` | `pip install soundcard` | **recomandat**; urmareste device-ul implicit de iesire, livreaza direct 48 kHz |
| `pyaudiowpatch` | `pip install PyAudioWPatch` | fork de PyAudio cu device-uri `[Loopback]`; ruleaza la rata nativa (ex. 44.1 kHz) si se reesantioneaza intern |
| `sounddevice` | inclus | doar daca ai `Stereo Mix`, VB-Cable, VoiceMeeter, sau o versiune de PortAudio cu loopback |

Testat pe acest sistem: toate trei functioneaza, cu latenta de **16–27 ms**.

Alegerea se face in `config/settings.json`:

```json
"audio": {
  "sources": {
    "loopback":   { "enabled": true,  "device": null, "backend": "auto", "gain": 1.0 },
    "microphone": { "enabled": false, "device": null, "gain": 1.0 }
  }
}
```

`device: null` = device-ul implicit; poti pune si o parte din nume
(`"Speakers"`, `"Realtek"`). Cele doua surse se mixeaza automat daca amandoua
sunt active.

### Latenta

| Etapa | Tipic |
|---|---|
| bloc WASAPI (256 esantioane @48 kHz) | 5.3 ms |
| ring buffer | 5–15 ms |
| pas de analiza (hop 512) | 10.7 ms |
| **total masurat** | **16–27 ms** |

Sub pragul de 50 ms cerut. Daca ai pierderi de cadre pe un PC slab, mareste
`audio.block_size` la 512.

Beat-urile sunt **prezise**, nu detectate post-factum (vezi `audio/beat.py`),
deci sincronizarea vizuala nu sufera nici de aceste 20 ms.

---

## 3. Arhitectura

```
                       [ PortAudio / WASAPI callback ]      prioritate maxima
  loopback ─┐                       │
            ├──► RingBuffer ────────┤  fara alocari, fara lock-uri lungi
  microfon ─┘                       │
                                    ▼
                       [ AnalysisEngine ]  fir dedicat, 93.75 cadre/s
                                    │
        ┌──────────┬────────────────┼─────────────┬────────────────┐
        ▼          ▼                ▼             ▼                ▼
   Spectrum    Onset           Tempo (BPM)    BeatTracker    StructureDetector
   6 benzi     spectral flux   autocorelatie   PLL + downbeat  drop/buildup/break
   RMS/centroid                + prior tempo
        └──────────┴────────────────┼─────────────┴────────────────┘
                                    │
                    SharedState (snapshot)      EventBus (evenimente)
                          │                            │
                          ▼                            ▼
                   [ Qt UI, 60 FPS ]           [ RuleEngine ]  fir propriu
                                                       │  IF / THEN
                                                       ▼
                                              [ MagicQRouter ]  fir propriu
                                                       │
                                    OSC → MIDI → TASTATURA → MOUSE
```

Patru fire independente: daca interfata incetineste sau tastatura are pauze de
20 ms, analiza audio nu are de suferit. UI-ul citeste doar snapshot-uri; nu
atinge niciodata modulele DSP.

### Fisiere

```
main.py                 punct de intrare, argumente, pornirea firelor
requirements.txt

audio/
    capture.py          WASAPI loopback (3 backend-uri) + microfon, ring buffer,
                        reesantionare continua, sursa sintetica pentru teste
    spectrum.py         FFT, 6 benzi, AGC, flux, centroid, rolloff, flatness
    onset.py            detectia onset-urilor (prag adaptiv median)
    bpm.py              tempo prin autocorelatie + prior + tap tempo
    beat.py             urmarirea fazei (PLL) + downbeat 4/4
    structure.py        masina de stari: INTRO/BUILDUP/DROP/CLIMAX/BREAK/OUTRO
                        (+ carlig pentru model scikit-learn optional)
    engine.py           firul de analiza care leaga totul

magicq/
    actions.py          vocabularul de actiuni, independent de transport
    shorthand.py        "Flash 4", "Speed=180", "Increase Speed" -> actiuni
    base.py             interfata comuna a transporturilor
    osc.py              OSC (adrese configurabile)
    midi.py             MIDI (mido)
    keyboard.py         SendInput cu scancode-uri + focus fereastra MagicQ
    mouse.py            click-uri pe coordonate relative la fereastra
    router.py           prioritati, rezerva automata, rate limit, auto-release

core/
    config.py           settings.json cu merge peste valorile implicite
    bus.py              bus de evenimente thread-safe
    state.py            snapshot + istoricele pentru grafice
    rules.py            motorul IF/THEN, evaluator de expresii sigur (AST)

ui/
    dashboard.py        fereastra principala
    widgets.py          widget-uri desenate cu QPainter

tools/
    selftest.py         test automat al lantului DSP + reguli
    loopback_check.py   diagnostic de captura, cu muzica ta
    magicq_test.py      test pas cu pas al comenzilor catre MagicQ
    keyboard_test.py    izoleaza unde se rupe controlul prin tastatura
    osc_discover.py     afla ce porturi/adrese OSC foloseste MagicQ-ul tau

config/
    rules.json          set complet (presupune OSC + macro-uri)
    rules_demo_mode.json  doar tastatura, playback-uri 1-10
    rules_4pb.json      set minim: doar PB1-PB4

config/
    settings.json       hardware, transporturi, praguri
```

### De ce OSC nu are voie sa „inghita" comenzile

OSC merge peste UDP, deci `send()` reuseste intotdeauna, chiar daca nu
asculta nimeni. Fiind primul in ordinea de prioritate, ar accepta toate
actiunile si nu s-ar mai ajunge niciodata la tastatura — aplicatia ar
parea ca merge, iar MagicQ n-ar primi nimic.

De aceea, la conectare, transportul OSC verifica daca cineva chiar asculta
pe portul local (`require_listener`, implicit `true`). Daca nu, se declara
INACTIV si comenzile trec automat pe tastatura.

---

## 4. Ce se detecteaza si cum

| Caracteristica | Metoda |
|---|---|
| **BPM** | anvelopa de novelty (spectral flux) pe 8 s → autocorelatie prin FFT → suma armonica `acf(T)+0.5·acf(2T)+0.25·acf(3T)` (rezolva ambiguitatea de octava) → prior log-normal centrat pe 128 BPM → interpolare parabolica → mediana + histerezis. Actualizare la 250 ms. |
| **Beat** | PLL: perioada de la estimatorul de tempo, faza corectata din onset-uri. Corectia se acumuleaza intre beat-uri si se aplica o singura data, limitata la 15% din perioada (altfel rolele de snare din build-up trag faza si se pierd beat-uri). |
| **Downbeat** | 4 acumulatoare (unul per pozitie in masura) in care se aduna energia de bass la fiecare beat, cu uitare exponentiala; pozitia cu cel mai mult kick devine „1". |
| **Onset** | spectral flux cu prag adaptiv median, declansare pe flancul crescator + perioada refractara de 55 ms. |
| **Benzi** | Sub 20–60 · Bass 60–250 · LowMid 250–500 · Mid 500–2k · High 2k–6k · Treble 6k–20k Hz. Fiecare cu AGC propriu (plafon cu cadere lenta + prag de zgomot) → valori 0–100% independente de volumul sistemului si de masterizarea piesei. |
| **RMS / loudness** | RMS pe cadru + versiune normalizata prin AGC. |
| **Spectral centroid** | „agresivitate": expus si ca `brightness` 0–100% pe scala logaritmica 100 Hz – 8 kHz. |
| **Build-up** | regresie liniara a energiei pe ultimele 2.5 s + centroid in crestere + densitate de onset-uri in crestere + lipsa bass-ului. |
| **Drop** | saltul mediei de 300 ms fata de media dintre acum 2.5 s si acum 0.5 s (bass **si** energie), + spectral flux, + bonus daca a fost build-up sau break recent. Filtru absolut: un drop e obligatoriu tare si cu bass, altfel un riser ar fi luat drept drop. Cooldown 6 s. |
| **Break** | energie mult sub referinta (maximul dintre mediile pe 3 s si pe 15 s) + bass absent, minim 1.2 s. |
| **Climax** | energie sustinuta aproape de maximul ultimelor 30 s, dupa un drop. |
| **Intro / Outro** | primele secunde dupa aparitia semnalului / declin lung si nivel mic. |

Toate deciziile trec printr-o masina de stari cu histerezis (`min_section_s`)
si o perioada de incalzire de 5 s dupa aparitia semnalului, ca sa nu se declare
„drop" chiar la pornirea aplicatiei.

### Rezultatul testului automat

Pe piesa sintetica din `tools/selftest.py` (structura cunoscuta):

```
BPM detectat        : 127.85   (real 128.00)
Beat-uri            : 47       (asteptat 55, dupa lock)
Sectiuni: INTRO 0.0s | BUILDUP 10.6s | DROP 16.4s | BREAK 28.8s
          (real:       build-up 8-16s |  drop 16s |  break 27s)
CPU: 0.2 ms per cadru din 10.7 ms disponibile (52x mai rapid decat realtime)
```

### Machine learning (optional)

Euristica de mai sus functioneaza bine pe muzica electronica. Daca vrei un
model antrenat, `audio/structure.py` are un carlig gata pregatit:

```json
"analysis": { "structure": { "ml": {
    "enabled": true,
    "model_path": "config/section_model.joblib",
    "weight": 0.5
}}}
```

Modelul (scikit-learn, salvat cu joblib) primeste vectorul
`[energy_short, energy_mid, energy_long, slope, bass, hf, centroid, flux,
onset_rate, bass_jump, energy_jump]` si intoarce probabilitati pentru clasele
`DROP / BUILDUP / BREAK / ...`; scorul lui se amesteca cu cel euristic in
proportia `weight`. Fara model, aplicatia foloseste doar euristica.

---

## 5. Integrarea cu MagicQ

Router-ul incearca transporturile in ordinea din `magicq.priority` si trece la
urmatorul daca unul nu suporta actiunea **sau** daca trimiterea esueaza. Daca
nicio metoda dedicata nu poate executa actiunea, se foloseste automat
**tastatura** (`auto_keyboard_fallback: true`) — exact cerinta „daca MagicQ nu
suporta OSC pentru functia dorita, foloseste tastatura".

### ⚠ MagicQ PC Demo Mode: OSC si MIDI NU sunt disponibile

Din manualul MagicQ (`manual/magicq/manual/magicq_pc.html`, sectiunea
*MagicQ PC / Mac Restrictions*), functiile sunt impartite pe trei niveluri:

| Nivel | Cum se obtine | Ce contine (relevant aici) |
|---|---|---|
| **DEMO MODE** | fara hardware ChamSys | programare + playback, Art-Net/sACN in/out, MagicDMX, RDM |
| **CORE MODE** | Dual DMX, Audio Interface, SnakeSys B4/T2/R4/R8, GeNetix GN2/GN4IP | + **Automations**, aplicatie remote iOS/Android, Web Server |
| **UNLOCKED** | Mini/Compact/Stadium Connect, Compact Wing, Rack Mount Dongle, GeNetix GN5/GN10, wing-uri legacy | + **OSC**, **MIDI**, **ChamSys Remote Protocol**, timecode, control DJ |

Citate din manual:

> „OSC is supported on MagicQ consoles (except MQ40 and MQ40N) and PCs **when
> fully unlocked (Unlocked Mode)**."
>
> „Note that the use of the ChamSys Remote Ethernet Protocol on MagicQ PC/Mac
> is only enabled when it is connected to a MagicQ Wing or Interface (not
> MagicDMX)."
>
> „Automation is only supported on MagicQ PC/Mac when connected to a MagicQ USB
> Wing, MagicQ USB Interface or SnakeSys product."

**MagicDMX (Basic sau Full) NU deblocheaza** aceste functii — manualul o spune
explicit.

**Concluzie:** in Demo Mode raman doar **tastatura** si **mouse-ul** — aplicatia
tasteaza in interfata MagicQ, ceea ce nu este o functie „remote" si deci nu este
restrictionata. Vezi §5.2.

### 5.2 Control prin tastatura (functioneaza in Demo Mode)

In MagicQ: `Setup` → `View Settings` → **MagicQ Keyboard Mode** =
**„Playback shortcuts"**. Atunci MagicQ mapeaza (conform manualului):

| Taste | Functie |
|---|---|
| `1` … `0` | selecteaza playback-urile 1–10 |
| `Q` … `P` | **GO** pe playback-urile 1–10 |
| `A` … `;` | **STOP** pe playback-urile 1–10 |
| `\` `Z` `X` `C` `V` `B` `N` `M` `,` `.` | **toggle** playback la 100% (folosit ca FLASH) |
| `SPACE` / `#` | Manual GO / Manual STOP |
| `[` / `]` | pagina urmatoare / precedenta |
| `-` | Release |
| `` ` `` | mod Add / Swap |

Acestea sunt deja maparile implicite din `settings.json`, deci nu ai de
configurat nimic. Flash-ul e un toggle: aplicatia apasa o data ca sa aprinda si
inca o data la expirarea `duration`, ca sa elibereze.

Ce **nu** merge prin tastatura: viteza efectelor (in MagicQ se face cu
`S` + encoder X, nu are scurtatura), `exec`, `blackout` si `release_all` — acele
actiuni vor fi raportate ca esuate in jurnal. Regulile care le folosesc trebuie
inlocuite cu actiuni pe playback.

**Atentie:** cu `focus_window: true` aplicatia aduce MagicQ in prim-plan inainte
de fiecare comanda — deci iti fura focusul de pe alte ferestre. Asta e inerent
metodei; foloseste MagicQ pe un al doilea monitor.

### Ordinea corecta de pornire

1. **MagicQ** (proces separat, pornit de tine) cu show-ul tau incarcat.
2. **Playback-urile trebuie sa fie deja programate** — aplicatia nu creeaza
   lumini, doar apasa butoanele existente. Vezi §5.1.
3. `py -3.12 tools/magicq_test.py` — confirma ca ajung comenzile.
4. `py -3.12 main.py` — porneste analiza. Lasa-l pe **MANUAL** la inceput.

### 5.1 Ce trebuie sa existe in MagicQ inainte

Regulile implicite din `config/rules.json` presupun urmatoarele playback-uri.
Programeaza-le (sau schimba numerele in `rules.json`):

| Playback | Continut sugerat | Folosit de |
|---|---|---|
| PB 1 | look de baza, fader-abil | INTRO, OUTRO, CLIMAX |
| PB 2 | bump / blinder scurt | downbeat |
| PB 3 | chase rapid | BUILDUP |
| PB 4 | strobe / flash puternic | **DROP** |
| PB 5 | chase pe bass | `bass > 80` |
| PB 6 | look „totul pornit" | CLIMAX |
| PB 7 | flash foarte scurt | kick pe beat |

Plus macro-urile de tastatura `strobe_on`, `strobe_off`, `color_fx` din
`settings.json` — pune acolo scurtaturile reale din show-ul tau.

### OSC (prima optiune)

In MagicQ: `Setup → View Settings → Network` (in unele versiuni `→ Ports`):

* **OSC Mode**: `Rx OSC` sau `Tx and Rx OSC`
* **OSC Rx Port**: `8000` (acelasi ca in `settings.json`)

**OSC merge peste UDP: trimiterea „reuseste" mereu, chiar daca nimeni nu
asculta.** De aceea „0 comenzi esuate" NU inseamna ca MagicQ a primit ceva.
Singura verificare valida este sa te uiti pe ce porturi asculta MagicQ:

```bash
py -3.12 tools/osc_discover.py ports
```

Daca vezi doar `6454` (Art-Net) si `6553` (retea ChamSys), **OSC Rx este oprit**
— degeaba schimbi adresele, mai intai porneste-l in MagicQ.

### Cand adresele OSC nu se potrivesc

Adresele difera intre versiuni. In loc sa ghicesti, afla-le empiric — ce
FORMAT trimite MagicQ este acelasi pe care il si asculta:

```bash
py -3.12 tools/osc_discover.py listen 9000
```

In MagicQ pui `OSC Mode = Tx and Rx OSC`, `OSC Tx IP = 127.0.0.1`,
`OSC Tx Port = 9000`, apoi misti un fader. Scriptul iti arata exact adresa
(ex. `/pb/1  [75]`), pe care o copiezi in `settings.json` inlocuind numarul
cu `{playback}`.

Daca versiunea ta nu are Tx OSC, scaneaza variantele si uita-te la faderul PB1:

```bash
py -3.12 tools/osc_discover.py scan 8000
```

Adresele sunt **sabloane in configurare**, nu in cod, pentru ca difera intre
versiunile de MagicQ:

```json
"osc": {
  "host": "127.0.0.1", "port": 8000,
  "addresses": {
    "pb_go":      "/pb/{playback}/go",
    "pb_stop":    "/pb/{playback}/stop",
    "pb_release": "/pb/{playback}/release",
    "pb_flash":   "/pb/{playback}/flash",
    "pb_level":   "/pb/{playback}",
    "exec":       "/exec/{page}/{item}",
    "dmx":        "/dmx/{channel}",
    "rpc":        "/rpc",
    "speed":      "/pb/{playback}/speed"
  }
}
```

> Verifica adresele cu versiunea ta de MagicQ (Setup → View Settings → OSC arata
> ce primeste consola). Daca una nu exista, sterge-o din JSON: actiunea va trece
> automat pe MIDI sau pe tastatura.

OSC este UDP, deci nu exista confirmare de la MagicQ; LED-ul din interfata arata
„socket deschis + pachete trimise fara eroare".

### MIDI (a doua optiune)

`Setup → View Settings → MIDI`. Pe acelasi PC ai nevoie de un port MIDI virtual
(loopMIDI / LoopBe1). Maparea implicita: playback N → nota `60 + N - 1` (GO /
FLASH), nivel → CC `20 + N - 1`.

### Tastatura (a treia optiune, universala)

`SendInput` cu scancode-uri (ca de la o tastatura fizica, merge si cu MagicQ pe
ecran complet). Optional aduce fereastra MagicQ in prim-plan inainte de a
trimite.

```json
"keyboard": {
  "focus_window": true,
  "window_title_regex": "(?<![A-Za-z0-9])MagicQ(?![A-Za-z0-9])",
  "bindings": {
    "pb_go":      "ctrl+{playback_digit}",
    "pb_flash":   "shift+{playback_digit}",
    "pb_release": "alt+{playback_digit}",
    "speed_up":   "ctrl+plus",
    "speed_down": "ctrl+minus"
  },
  "macros": { "strobe_on": "shift+8", "strobe_off": "alt+8", "color_fx": "shift+9" }
}
```

> **Maparile de mai sus sunt EXEMPLE.** Pune scurtaturile din show-ul tau.
> Regex-ul e cu delimitare de cuvant intentionat: un simplu `"MagicQ"` se
> potriveste si cu un terminal deschis in `D:\PYTHONMAGICQ` si tastele ar
> ajunge acolo.

Sintaxa secventelor: `"ctrl+1"`, `"ctrl+1 enter"`, `"1,2,enter"`,
`"type:HELLO"`, `"wait:0.2"`.

### Mouse: paletele de pe pagina (COLOUR / BEAM / POSITION / GROUP)

Coordonate relative la fereastra MagicQ, cu revenirea cursorului la pozitia
initiala dupa click.

**De ce mouse si nu tastatura:** selectarea grupurilor si aplicarea paletelor
se fac din linia de comanda MagicQ (`2**`), care cere `MagicQ Keyboard Mode =
Normal`. Dar playback-urile PB1-10 cer `Playback shortcuts`. Nu poti fi in
ambele moduri simultan. Mouse-ul functioneaza in **orice** mod, deci se
combina: tastatura pentru playback-uri + mouse pentru palete.

Calibrare (o singura data, ~2 minute):

```bash
py -3.12 tools/calibrate_palettes.py
```

Nu introduci 100 de coordonate: casutele MagicQ sunt uniforme, deci se retin
doar **prima si ultima** casuta din fiecare fereastra, plus coloane × randuri.
Restul se interpoleaza. Verificare, fara click:

```bash
py -3.12 tools/calibrate_palettes.py --test beam 4
```

Actiuni noi:

```json
{ "action": "palette", "window": "beam", "group": 2, "item": 4 }
{ "action": "palette", "window": "colour", "group": 2,
  "cycle": [2, 5, 7, 10], "labels": ["Red", "Green", "Blue", "Magenta"] }
{ "action": "palette", "window": "colour", "cycle": [2, 10], "random": true }
{ "action": "select_group", "item": 2 }
{ "action": "clear" }
```

`window`: `group` | `colour` | `beam` | `position`. `group` selecteaza intai
capetele, apoi aplica paleta. `cycle` ia urmatoarea valoare la fiecare
declansare (rotatie automata a culorilor); `random: true` alege aleator.

### ⚠ Paletele scriu in PROGRAMATOR

Valorile din programator au prioritate peste **toate** playback-urile pana la
`CLEAR`. Adica dupa un „Shut fast" pe capuri, ramane asa chiar daca
playback-urile se schimba. De aceea seturile cu palete au reguli de `clear`
la `SILENCE` si `OUTRO`, iar calibrarea cere si pozitia butonului CLEAR.

Calibrarea depinde de layout: daca muti/redimensionezi ferestrele MagicQ sau
schimbi pagina de palete, recalibrezi.

### Modulo in expresii

`%` inseamna procent ca sufix (`bass > 80%`) si modulo cand are spatii in jur
(`bar % 4 == 0` = la fiecare 4 masuri).

---

## 6. Reguli (config/rules.json)

### Forma scurta — exact cea ceruta

```json
{
    "Drop": "Flash 4",
    "BuildUp": "Increase Speed",
    "Break": "Release Flash",
    "Bass": "Playback 5",
    "High": "Color FX",
    "BPM>140": "Speed=180"
}
```

Cheia poate fi un eveniment (`Drop`, `BuildUp`, `Break`, `Climax`, `Intro`,
`Outro`, `Beat`, `Downbeat`, `Onset`, `Silence`), un prag implicit pe o banda
(`Bass`, `High`, `RMS`) sau o expresie (`BPM>140`, `Bass > 80`).
Valoarea se poate inlantui: `"Flash 4 + Speed=180"`.

Text nerecunoscut devine un **macro** cu acel nume (`"Color FX"` → macro
`color_fx`), pe care il definesti in `settings.json`.

### Forma extinsa — control complet

```json
{
  "rules": [
    {
      "name": "DROP -> strobe + flash",
      "on": "DROP",
      "cooldown": 4.0,
      "do": [
        { "action": "pb_flash", "playback": 4, "duration": 1.2 },
        { "action": "macro", "name": "strobe_on" }
      ]
    },
    {
      "name": "Bass puternic -> chase",
      "when": "bass > 80",
      "mode": "edge",
      "hold": 0.35,
      "cooldown": 1.5,
      "do":   [ { "action": "pb_go", "playback": 5 } ],
      "undo": [ { "action": "pb_release", "playback": 5 } ]
    },
    {
      "name": "Kick pe beat",
      "on": "BEAT",
      "if": "bass > 70 and is_drop",
      "quantize": "beat",
      "do": [ { "action": "pb_flash", "playback": 7, "duration": 0.09 } ]
    }
  ]
}
```

| Camp | Rol |
|---|---|
| `on` | eveniment: `DROP`, `BUILDUP`, `BREAK`, `CLIMAX`, `INTRO`, `OUTRO`, `GROOVE`, `BEAT`, `DOWNBEAT`, `ONSET`, `SILENCE`, `SIGNAL`, `BPM_CHANGE` |
| `if` | conditie suplimentara pentru evenimente |
| `when` | expresie evaluata continuu (100 Hz), in locul lui `on` |
| `mode` | `edge` (la trecerea in adevarat) sau `level` (cat timp e adevarat) |
| `cooldown` | secunde minime intre doua declansari |
| `hold` | timp minim intre schimbari de stare — **anti-chatter** (implicit 0.35 s) |
| `quantize` | `off` / `beat` / `downbeat` — amana actiunea pana la urmatorul beat |
| `do` / `undo` | actiuni; `undo` se trimite cand conditia dispare (doar daca `do` chiar a plecat) |

### Variabile disponibile in expresii

| Variabila | Domeniu |
|---|---|
| `bpm`, `bpm_conf` | BPM, 0–1 |
| `bpm_age` | secunde de la ultima schimbare de tempo (pentru rafale de tap tempo) |
| `beat`, `downbeat`, `beat_in_bar` (1–4), `bar`, `phase` | ritm |
| `onset`, `onset_strength`, `onset_rate` | onset-uri |
| `rms` (0–1), `rms_db`, `peak`, `loudness` (0–100) | nivel |
| `sub`, `bass`, `low_mid`, `mid`, `high`, `treble`, `lows`, `highs` | **0–100**, instantaneu (anvelopa VU) |
| `sub_avg`, `bass_avg`, `mid_avg`, `high_avg`, `treble_avg`, `lows_avg`, `highs_avg` | **0–100**, mediat pe ~1.2 s |
| `centroid` (Hz), `brightness` (0–100), `flatness`, `flux` | spectru |
| `section`, `section_age`, `is_drop`, `is_buildup`, `is_break`, `is_climax`, `is_intro`, `is_outro`, `is_groove`, `is_silence` | structura |
| `energy`, `energy_mid`, `energy_long` (0–100), `energy_slope` | energie |
| `drop_score`, `buildup_score` (0–1), `drop_age` (s) | scoruri |

> **Foloseste `bass_avg`, nu `bass`, in pragurile de tip „IF bass > 80".**
> Valoarea instantanee cade aproape la zero intre doua kick-uri, deci
> traverseaza pragul de doua ori pe beat si playback-ul ar comuta continuu.
> Cea mediata raspunde la „cat de bass-oasa e sectiunea asta". Masurat pe
> 40 s de test: `bass > 82` -> 13 comutari; `bass_avg > 70` -> 2.
> Valorile instantanee sunt bune pentru evenimente (`on: BEAT`, `if: bass > 75`).

Expresiile se evalueaza cu un interpretor **AST restrictionat** (doar
comparatii, aritmetica, `and/or/not` si `abs/min/max/round/int/float`) — nu se
poate executa cod arbitrar dintr-un fisier de configurare.

Sinonime acceptate: `High Frequency` → `high`, `Sub Bass` → `sub`,
`=` → `==`, `%` se ignora. Deci `"High Frequency > 70%"` este valid.

### Actiuni

`pb_go`, `pb_stop`, `pb_release`, `pb_flash` (cu `duration` → auto-release),
`pb_unflash`, `pb_level`, `exec`, `dmx`, `rpc`, `speed`, `key`, `macro`,
`click`, `midi_note`, `midi_cc`, `blackout`, `release_all`.

Orice actiune poate forta un transport: `{"action": "pb_go", "playback": 3, "via": "midi"}`.

---

## 7. Interfata

* **BPM** mare + bara de incredere + 4 indicatoare de beat (primul = downbeat)
* **Sectiunea curenta** cu culoare proprie, varsta ei si scorurile drop/build-up
* **6 metere** de banda cu peak-hold + **RMS**, **energie (AGC)**, **brightness**
* **Spectrograma** (log-frecventa, 8 s) si **waveform** (3 s, pulseaza pe beat)
* **Diagnostic**: FPS analiza, FPS interfata, latenta, CPU, onset/s, comenzi trimise/esuate
* **LED-uri** pentru AUDIO / OSC / MIDI / KEYBOARD / MOUSE (verde = conectat, albastru = trimite acum)
* **Tabel de reguli**: activare individuala, contor de declansari, buton TEST
* **Jurnal** cu fiecare regula declansata si fiecare comanda trimisa

Butoane: `PAUZA ANALIZA`, `AUTO / MANUAL` (in MANUAL se afiseaza dar nu se
trimite nimic — ideal pentru reglaj in timpul unui set), `PANIC` (elibereaza
tot; si global cu **CTRL+ALT+P**), `TAP`, `SYNC` (forteaza downbeat-ul acum),
`RELOAD REGULI` (la cald, fara restart), slider de **sensibilitate**
(scaleaza pragurile de drop/build-up), si `SIMULEAZA` o sectiune pentru a testa
regulile fara muzica.

---

## 8. Reglaj rapid

| Problema | Ce modifici |
|---|---|
| Drop-uri ratate | slider-ul de sensibilitate spre dreapta, sau `analysis.structure.drop.score_threshold` mai mic (0.5) |
| Drop-uri false | `score_threshold` mai mare (0.7), sau `drop.cooldown_s` mai mare |
| BPM instabil | `analysis.bpm.window_s` la 10–12, `smoothing` la 9 |
| BPM dublat/injumatatit | `prefer_min` / `prefer_max` (ex. 90–180 pentru drum&bass) |
| Prea multe comenzi | `magicq.rate_limit_per_s` mai mic, `cooldown` mai mare pe reguli |
| Lumini care palpaie | `hold` mai mare pe regula (0.5–1.0) |
| Pierderi de cadre | `audio.block_size` la 512 |
| Tastele ajung aiurea | `keyboard.focus_window: true` si verifica `window_title_regex` |

---

## 9. Limitari cunoscute

* **Adresele OSC** implicite urmeaza schema standard MagicQ, dar difera intre
  versiuni — verifica-le la tine si ajusteaza `settings.json` (nu codul).
* **Maparile de tastatura si macro-urile** din configurarea implicita sunt
  exemple; trebuie inlocuite cu scurtaturile show-ului tau.
* `aubio` nu are wheel pentru Python 3.12, iar `librosa` este prea lent pentru
  bucla realtime — de aceea BPM/beat/onset sunt implementate direct in
  numpy/scipy. Nu lipseste nimic functional.
* Detectia de structura este calibrata pe muzica electronica cu bataie
  constanta. Pe muzica live, cu tempo liber, BPM-ul si downbeat-ul sunt mai
  putin stabile (foloseste `TAP` si `SYNC`).
* Downbeat-ul presupune masura de 4/4.
