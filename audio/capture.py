"""
audio/capture.py
================
Captura audio de latenta mica pe Windows.

Doua surse independente, mixate intr-un singur semnal mono:

  1. LOOPBACK (WASAPI)  - exact ce se aude in boxe: Spotify, YouTube,
     Winamp, VirtualDJ, orice.
  2. MICROFON - pentru sunetul din sala (util cand DJ-ul are mixer
     propriu si PC-ul nu reda nimic).

IMPORTANT despre loopback: PortAudio-ul livrat cu `sounddevice` (19.7.0-devel)
NU expune loopback-ul WASAPI, iar `sd.WasapiSettings` nu are parametru
`loopback`. De aceea captura desktop are mai multe backend-uri, incercate
in ordine (`audio.sources.loopback.backend: "auto"`):

    1. soundcard      - WASAPI loopback nativ, recomandat (pip install soundcard)
    2. pyaudiowpatch  - fork de PyAudio cu device-uri "[Loopback]"
    3. sounddevice    - daca versiunea ta chiar suporta loopback, sau daca
                        exista un device de intrare "Stereo Mix" / VB-Cable

Backend-urile 1 si 2 citesc blocant, deci ruleaza in fire proprii;
backend-ul 3 foloseste callback-ul PortAudio. Toate scriu in acelasi tip
de ring buffer, deci restul aplicatiei nu stie si nu-i pasa care e activ.

Fiecare sursa scrie in propriul ring buffer din callback-ul PortAudio
(fir de prioritate ridicata - fara alocari, fara log-uri, fara lock-uri
lungi). Firul de analiza citeste cate `hop` esantioane din fiecare si le
mixeaza. Daca o sursa ramane in urma (drift de ceas intre placi), datele
vechi sunt aruncate automat ca latenta sa ramana marginita.

Latenta tipica: block_size 256 @ 48 kHz = 5.3 ms + un hop de 512 (10.7 ms)
=> ~16-25 ms pana la prima reactie. Sub pragul de 50 ms cerut.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

try:
    import sounddevice as sd
except Exception as exc:  # pragma: no cover
    sd = None  # type: ignore[assignment]
    _SD_IMPORT_ERROR = exc
else:
    _SD_IMPORT_ERROR = None

log = logging.getLogger(__name__)


# ======================================================================
#  RING BUFFER
# ======================================================================
class RingBuffer:
    """Buffer circular mono, thread-safe, cu aruncarea datelor vechi.

    Scriitorul este callback-ul audio, cititorul este firul de analiza.
    Lock-ul este tinut doar cateva microsecunde (copiere numpy).
    """

    def __init__(self, capacity: int):
        self.capacity = int(capacity)
        self._buf = np.zeros(self.capacity, dtype=np.float32)
        self._write = 0
        self._read = 0
        self._count = 0
        self._lock = threading.Lock()
        self.overflows = 0
        self.total_written = 0

    def write(self, data: np.ndarray) -> None:
        n = data.shape[0]
        if n == 0:
            return
        if n > self.capacity:
            data = data[-self.capacity:]
            n = self.capacity
        with self._lock:
            end = self._write + n
            if end <= self.capacity:
                self._buf[self._write:end] = data
            else:
                first = self.capacity - self._write
                self._buf[self._write:] = data[:first]
                self._buf[:n - first] = data[first:]
            self._write = end % self.capacity
            self._count += n
            self.total_written += n
            if self._count > self.capacity:
                # am suprascris date necitite -> mutam cititorul
                overrun = self._count - self.capacity
                self._read = (self._read + overrun) % self.capacity
                self._count = self.capacity
                self.overflows += 1

    def available(self) -> int:
        with self._lock:
            return self._count

    def read(self, n: int) -> np.ndarray | None:
        """Scoate exact n esantioane sau None daca nu sunt suficiente."""
        with self._lock:
            if self._count < n:
                return None
            end = self._read + n
            if end <= self.capacity:
                out = self._buf[self._read:end].copy()
            else:
                first = self.capacity - self._read
                out = np.concatenate((self._buf[self._read:], self._buf[:n - first]))
            self._read = end % self.capacity
            self._count -= n
            return out

    def trim(self, max_frames: int) -> int:
        """Pastreaza cel mult `max_frames` esantioane (arunca cele vechi).
        Returneaza cate esantioane au fost aruncate."""
        with self._lock:
            excess = self._count - max_frames
            if excess <= 0:
                return 0
            self._read = (self._read + excess) % self.capacity
            self._count = max_frames
            return excess

    def clear(self) -> None:
        with self._lock:
            self._read = self._write
            self._count = 0


# ======================================================================
#  DESCOPERIRE DEVICE-URI
# ======================================================================
@dataclass
class DeviceInfo:
    index: int
    name: str
    hostapi: str
    max_input: int
    max_output: int
    default_samplerate: float
    is_default_input: bool = False
    is_default_output: bool = False

    def __str__(self) -> str:
        io = f"in:{self.max_input} out:{self.max_output}"
        flags = []
        if self.is_default_input:
            flags.append("DEFAULT-IN")
        if self.is_default_output:
            flags.append("DEFAULT-OUT")
        tag = (" [" + ",".join(flags) + "]") if flags else ""
        return f"{self.index:3d}  {self.name}  ({self.hostapi}, {io}){tag}"


def _require_sd() -> None:
    if sd is None:
        raise RuntimeError(
            f"sounddevice nu este disponibil ({_SD_IMPORT_ERROR}). "
            "Instaleaza: py -3.12 -m pip install sounddevice>=0.5.0"
        )


def list_devices() -> list[DeviceInfo]:
    """Toate device-urile audio vazute de PortAudio."""
    _require_sd()
    hostapis = sd.query_hostapis()
    try:
        default_in, default_out = sd.default.device
    except Exception:  # pragma: no cover
        default_in = default_out = -1
    out: list[DeviceInfo] = []
    for idx, dev in enumerate(sd.query_devices()):
        out.append(DeviceInfo(
            index=idx,
            name=dev["name"],
            hostapi=hostapis[dev["hostapi"]]["name"],
            max_input=dev["max_input_channels"],
            max_output=dev["max_output_channels"],
            default_samplerate=dev["default_samplerate"],
            is_default_input=(idx == default_in),
            is_default_output=(idx == default_out),
        ))
    return out


def wasapi_hostapi_index() -> int | None:
    _require_sd()
    for idx, api in enumerate(sd.query_hostapis()):
        if "wasapi" in api["name"].lower():
            return idx
    return None


def find_loopback_device(hint: Any = None) -> tuple[int, int, bool]:
    """Gaseste device-ul pentru captura sunetului redat de PC.

    Returneaza (index_device, canale, foloseste_flag_loopback).

    Strategie:
      1. daca `hint` e int -> se foloseste direct
      2. daca `hint` e string -> se cauta dupa nume in device-urile WASAPI
      3. altfel -> device-ul de iesire implicit WASAPI, in modul loopback
      4. rezerva -> un device de INTRARE cu nume tip "Stereo Mix"/"loopback"
    """
    _require_sd()
    devices = list_devices()
    wasapi_idx = wasapi_hostapi_index()

    # 1 / 2 - indicatie explicita din config
    if isinstance(hint, int):
        dev = devices[hint]
        if dev.max_output > 0:
            return hint, min(2, dev.max_output), True
        return hint, min(2, max(1, dev.max_input)), False
    if isinstance(hint, str) and hint.strip():
        needle = hint.lower()
        for dev in devices:
            if needle in dev.name.lower():
                if dev.max_output > 0 and "wasapi" in dev.hostapi.lower():
                    return dev.index, min(2, dev.max_output), True
                if dev.max_input > 0:
                    return dev.index, min(2, dev.max_input), False
        log.warning("Device-ul loopback '%s' nu a fost gasit; se incearca automat.", hint)

    # 3 - iesirea implicita, cu loopback WASAPI
    if wasapi_idx is not None:
        api = sd.query_hostapis(wasapi_idx)
        default_out = api.get("default_output_device", -1)
        if default_out is not None and default_out >= 0:
            dev = devices[default_out]
            return dev.index, min(2, max(1, dev.max_output)), True

    # 4 - Stereo Mix / What U Hear / driver virtual (VB-Cable, VoiceMeeter)
    keywords = ("stereo mix", "mixaj stereo", "what u hear", "loopback",
                "cable output", "voicemeeter out")
    for dev in devices:
        low = dev.name.lower()
        if dev.max_input > 0 and any(k in low for k in keywords):
            return dev.index, min(2, dev.max_input), False

    raise RuntimeError(
        "Nu am gasit niciun device de loopback. Verifica: sounddevice >= 0.5.0, "
        "sau seteaza manual 'audio.sources.loopback.device' in config/settings.json "
        "(ruleaza 'py -3.12 main.py --list-devices')."
    )


def find_input_device(hint: Any = None) -> tuple[int, int]:
    """Gaseste microfonul (index, canale)."""
    _require_sd()
    devices = list_devices()
    if isinstance(hint, int):
        return hint, min(2, max(1, devices[hint].max_input))
    if isinstance(hint, str) and hint.strip():
        needle = hint.lower()
        for dev in devices:
            if needle in dev.name.lower() and dev.max_input > 0:
                return dev.index, min(2, dev.max_input)
        log.warning("Microfonul '%s' nu a fost gasit; se foloseste cel implicit.", hint)
    for dev in devices:
        if dev.is_default_input and dev.max_input > 0:
            return dev.index, min(2, dev.max_input)
    for dev in devices:
        if dev.max_input > 0:
            return dev.index, min(2, dev.max_input)
    raise RuntimeError("Nu exista niciun device de intrare (microfon).")


def _wasapi_loopback_settings() -> Any:
    """WasapiSettings(loopback=True) daca versiunea de sounddevice o suporta.

    In sounddevice 0.5.x parametrul nu exista inca -> returneaza None si se
    foloseste alt backend (soundcard / pyaudiowpatch).
    """
    _require_sd()
    if not hasattr(sd, "WasapiSettings"):
        return None
    try:
        return sd.WasapiSettings(loopback=True)
    except TypeError:
        return None


def sounddevice_supports_loopback() -> bool:
    """True daca sounddevice poate captura direct sunetul redat de PC."""
    if sd is None:
        return False
    if _wasapi_loopback_settings() is not None:
        return True
    # PortAudio nou expune device-uri de intrare numite "... [Loopback]"
    try:
        return any(d.max_input > 0 and "[loopback]" in d.name.lower()
                   for d in list_devices())
    except Exception:  # noqa: BLE001
        return False


def find_sounddevice_loopback_input() -> tuple[int, int] | None:
    """Device de INTRARE care duplica iesirea: '[Loopback]', 'Stereo Mix',
    VB-Cable, VoiceMeeter."""
    keywords = ("[loopback]", "stereo mix", "mixaj stereo", "what u hear",
                "cable output", "voicemeeter out")
    try:
        devices = list_devices()
    except Exception:  # noqa: BLE001
        return None
    for keyword in keywords:                     # ordinea conteaza
        for dev in devices:
            if dev.max_input > 0 and keyword in dev.name.lower():
                return dev.index, min(2, dev.max_input)
    return None


# ======================================================================
#  SURSA AUDIO
# ======================================================================
@dataclass
class SourceStats:
    frames: int = 0
    callbacks: int = 0
    overflows: int = 0
    xruns: int = 0
    last_callback: float = 0.0
    stream_latency_ms: float = 0.0
    rms: float = 0.0
    active: bool = False
    error: str = ""


class AudioSource:
    """Un singur stream PortAudio + ring buffer-ul lui."""

    def __init__(self, name: str, device: int, channels: int, samplerate: int,
                 block_size: int, gain: float, extra_settings: Any = None,
                 max_frames: int = 8192, on_error: Callable[[str, str], None] | None = None,
                 highpass_hz: float = 0.0):
        self.name = name
        self.device = device
        self.channels = max(1, channels)
        self.samplerate = samplerate
        self.block_size = block_size
        self.gain = float(gain)
        self.extra_settings = extra_settings
        self.max_frames = max_frames
        self.on_error = on_error
        self.ring = RingBuffer(capacity=max(max_frames * 4, samplerate))
        self.stats = SourceStats()
        self.stream: Any = None
        # filtru trece-sus de ordinul 1 pentru microfon (taie DC si rumble):
        #   y[n] = a*y[n-1] + a*(x[n] - x[n-1])
        # implementat cu lfilter (C) + stare persistenta intre callback-uri
        self._hp_b: np.ndarray | None = None
        self._hp_a: np.ndarray | None = None
        self._hp_zi: np.ndarray | None = None
        if highpass_hz and highpass_hz > 0:
            rc = 1.0 / (2 * np.pi * highpass_hz)
            dt = 1.0 / samplerate
            a = rc / (rc + dt)
            self._hp_b = np.array([a, -a], dtype=np.float64)
            self._hp_a = np.array([1.0, -a], dtype=np.float64)
            self._hp_zi = np.zeros(1, dtype=np.float64)

    # ---------------- callback PortAudio ----------------
    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ANN001
        if status:
            self.stats.xruns += 1
        try:
            if indata.ndim > 1 and indata.shape[1] > 1:
                mono = indata.mean(axis=1)
            else:
                mono = indata.reshape(-1)
            mono = np.asarray(mono, dtype=np.float32)
            if self.gain != 1.0:
                mono = mono * self.gain
            if self._hp_b is not None:
                mono = self._highpass(mono)
            self.ring.write(mono)
            self.ring.trim(self.max_frames)
            self.stats.frames += frames
            self.stats.callbacks += 1
            self.stats.last_callback = time.monotonic()
        except Exception as exc:  # noqa: BLE001 - nu avem voie sa aruncam din callback
            self.stats.error = str(exc)

    def _highpass(self, x: np.ndarray) -> np.ndarray:
        from scipy.signal import lfilter  # import local: nu apare in hot path la import
        y, self._hp_zi = lfilter(self._hp_b, self._hp_a, x, zi=self._hp_zi)
        return y.astype(np.float32, copy=False)

    # ---------------- ciclu de viata ----------------
    def start(self) -> None:
        _require_sd()
        kwargs: dict[str, Any] = dict(
            device=self.device,
            channels=self.channels,
            samplerate=self.samplerate,
            blocksize=self.block_size,
            dtype="float32",
            latency="low",
            callback=self._callback,
        )
        if self.extra_settings is not None:
            kwargs["extra_settings"] = self.extra_settings
        self.stream = sd.InputStream(**kwargs)
        self.stream.start()
        self.stats.active = True
        self.stats.stream_latency_ms = float(getattr(self.stream, "latency", 0.0) or 0.0) * 1000.0
        log.info("Sursa '%s' pornita: device=%s canale=%d sr=%d block=%d latenta=%.1f ms",
                 self.name, self.device, self.channels, self.samplerate,
                 self.block_size, self.stats.stream_latency_ms)

    def stop(self) -> None:
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:  # noqa: BLE001
                log.debug("Eroare la inchiderea sursei %s", self.name, exc_info=True)
            self.stream = None
        self.stats.active = False
        self.ring.clear()


# ======================================================================
#  SURSA LOOPBACK PE FIR PROPRIU (soundcard / pyaudiowpatch)
# ======================================================================
def _com_initialize() -> bool:
    """Initializeaza COM pentru firul curent (apartament multi-threaded).

    WASAPI se acceseaza prin COM, iar COM se initializeaza PER FIR. Fara
    asta, deschiderea stream-ului intr-un fir de lucru esueaza cu
    0x800401f0 (CO_E_NOTINITIALIZED).
    """
    try:
        import ctypes
        hresult = ctypes.windll.ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED
        return hresult in (0, 1)          # S_OK / S_FALSE (deja initializat)
    except Exception:  # noqa: BLE001 - alt OS sau COM indisponibil
        return False


def _com_uninitialize() -> None:
    try:
        import ctypes
        ctypes.windll.ole32.CoUninitialize()
    except Exception:  # noqa: BLE001
        pass



class LoopbackThreadSource:
    """Captura sunetului redat de PC, cu citire blocanta intr-un fir dedicat.

    Interfata este identica cu AudioSource (name / ring / stats / start /
    stop), deci mixerul nu face nicio diferenta intre ele.
    """

    def __init__(self, name: str, backend: str, samplerate: int, block_size: int,
                 gain: float, max_frames: int, device_hint: Any = None,
                 on_error: Callable[[str, str], None] | None = None):
        self.name = name
        self.backend = backend
        self.samplerate = samplerate
        self.block_size = block_size
        self.gain = float(gain)
        self.max_frames = max_frames
        self.device_hint = device_hint
        self.on_error = on_error
        self.ring = RingBuffer(capacity=max(max_frames * 4, samplerate))
        self.stats = SourceStats()
        self.device_name = ""
        self.native_rate = samplerate
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ---------------- ciclu de viata ----------------
    def start(self) -> None:
        opener = {"soundcard": self._open_soundcard,
                  "pyaudiowpatch": self._open_pyaudiowpatch}[self.backend]
        recorder_factory = opener()          # ridica exceptie daca nu merge
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(recorder_factory,),
                                        name=f"Loopback-{self.backend}", daemon=True)
        self._thread.start()
        # asteptam confirmarea ca stream-ul chiar s-a deschis
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if self.stats.active:
                break
            if self.stats.error:
                raise RuntimeError(self.stats.error)
            time.sleep(0.02)
        if not self.stats.active:
            raise RuntimeError(f"backend-ul '{self.backend}' nu a pornit in 3 s")
        log.info("Loopback pornit prin '%s': %s (%d Hz)",
                 self.backend, self.device_name, self.native_rate)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None
        self.stats.active = False
        self.ring.clear()

    # ---------------- backend: soundcard ----------------
    def _open_soundcard(self):
        import soundcard as sc  # dependinta optionala
        # WASAPI se acceseaza prin COM, iar COM se initializeaza PER FIR.
        # Fara initializarea din _run() apare 0x800401f0 (CO_E_NOTINITIALIZED).

        speaker = None
        if isinstance(self.device_hint, str) and self.device_hint.strip():
            needle = self.device_hint.lower()
            for spk in sc.all_speakers():
                if needle in spk.name.lower():
                    speaker = spk
                    break
            if speaker is None:
                log.warning("Boxele '%s' nu au fost gasite; se foloseste iesirea "
                            "implicita.", self.device_hint)
        if speaker is None:
            speaker = sc.default_speaker()
        self.device_name = speaker.name
        self.native_rate = self.samplerate
        mic = sc.get_microphone(str(speaker.name), include_loopback=True)

        def factory():
            return mic.recorder(samplerate=self.samplerate, channels=2,
                                blocksize=self.block_size)
        return factory

    # ---------------- backend: pyaudiowpatch ----------------
    def _open_pyaudiowpatch(self):
        import pyaudiowpatch as pa  # dependinta optionala

        # Descoperirea device-ului se face cu o instanta temporara; instanta
        # reala se creeaza IN FIRUL de citire (PortAudio+WASAPI vrea COM
        # initializat in acelasi fir in care se deschide stream-ul).
        audio = pa.PyAudio()
        try:
            info = None
            if isinstance(self.device_hint, str) and self.device_hint.strip():
                needle = self.device_hint.lower()
                for dev in audio.get_loopback_device_info_generator():
                    if needle in dev["name"].lower():
                        info = dev
                        break
            if info is None:
                wasapi = audio.get_host_api_info_by_type(pa.paWASAPI)
                default_out = audio.get_device_info_by_index(wasapi["defaultOutputDevice"])
                if default_out.get("isLoopbackDevice"):
                    info = default_out
                else:
                    for dev in audio.get_loopback_device_info_generator():
                        if default_out["name"] in dev["name"]:
                            info = dev
                            break
            if info is None:
                raise RuntimeError("niciun device [Loopback] WASAPI disponibil")
            info = dict(info)
        finally:
            audio.terminate()

        self.device_name = info["name"]
        self.native_rate = int(info["defaultSampleRate"])
        # Loopback-ul WASAPI accepta DOAR numarul nativ de canale al
        # device-ului (8 la un headset 7.1). Cerand 2 canale se primeste
        # "[Errno -9996] Invalid device". Se deschide nativ si se mixeaza
        # in mono la citire.
        channels = max(1, int(info["maxInputChannels"]))

        def factory():
            return _PyAudioRecorder(pa, info, channels, self.native_rate,
                                    self.block_size)
        return factory

    # ---------------- firul de citire ----------------
    def _run(self, recorder_factory) -> None:
        resampler = (StreamResampler(self.native_rate, self.samplerate)
                     if self.native_rate != self.samplerate else None)
        com = _com_initialize()
        try:
            with recorder_factory() as recorder:
                self.stats.active = True
                self.stats.stream_latency_ms = self.block_size / self.samplerate * 1000.0
                while not self._stop.is_set():
                    data = recorder.record(numframes=self.block_size)
                    if data is None or len(data) == 0:
                        continue
                    arr = np.asarray(data, dtype=np.float32)
                    mono = arr.mean(axis=1) if arr.ndim > 1 and arr.shape[1] > 1 \
                        else arr.reshape(-1)
                    if resampler is not None:
                        mono = resampler.process(mono)
                        if mono.size == 0:
                            continue
                    if self.gain != 1.0:
                        mono = mono * self.gain
                    self.ring.write(mono)
                    self.ring.trim(self.max_frames)
                    self.stats.frames += mono.shape[0]
                    self.stats.callbacks += 1
                    self.stats.last_callback = time.monotonic()
        except Exception as exc:  # noqa: BLE001
            self.stats.error = f"{type(exc).__name__}: {exc}"
            log.error("Firul de loopback (%s) s-a oprit: %s", self.backend, exc)
            if self.on_error:
                self.on_error(self.name, str(exc))
        finally:
            self.stats.active = False
            if com:
                _com_uninitialize()


class _PyAudioRecorder:
    """Adaptor peste un stream PyAudioWPatch, cu aceeasi interfata ca
    recorder-ul din `soundcard` (context manager + record(numframes))."""

    def __init__(self, pa_module, info, channels, rate, block_size):
        self.pa = pa_module
        self.info = info
        self.channels = channels
        self.rate = rate
        self.block_size = block_size
        self.audio = None
        self.stream = None

    def __enter__(self):
        self.audio = self.pa.PyAudio()      # creat in firul de citire
        self.stream = self.audio.open(
            format=self.pa.paFloat32, channels=self.channels, rate=self.rate,
            frames_per_buffer=self.block_size, input=True,
            input_device_index=int(self.info["index"]))
        return self

    def __exit__(self, *_exc):
        try:
            if self.stream is not None:
                self.stream.stop_stream()
                self.stream.close()
        finally:
            if self.audio is not None:
                self.audio.terminate()
        return False

    def record(self, numframes: int) -> np.ndarray:
        raw = self.stream.read(numframes, exception_on_overflow=False)
        arr = np.frombuffer(raw, dtype=np.float32)
        if self.channels > 1:
            arr = arr.reshape(-1, self.channels)
        return arr


class StreamResampler:
    """Reesantionare continua bloc-cu-bloc (ex: device 44.1 kHz -> analiza 48 kHz).

    NU se poate folosi `resample_poly` pe fiecare bloc: filtrul polifazic are
    mii de coeficienti (prea lent pentru blocuri de 256 esantioane la 187
    blocuri/s) si ar introduce discontinuitati la marginile blocurilor.

    Aici: interpolare liniara cu faza si ultimul esantion pastrate intre
    blocuri (deci fara discontinuitati), plus un filtru anti-alias
    Butterworth cu stare doar cand se COBOARA rata (96 -> 48 kHz).
    Pentru analiza de ritm si energie precizia este mai mult decat suficienta.
    """

    def __init__(self, src_rate: int, dst_rate: int):
        self.src_rate = int(src_rate)
        self.dst_rate = int(dst_rate)
        self.step = self.src_rate / self.dst_rate
        self.phase = 0.0
        self._prev = np.zeros(1, dtype=np.float32)
        self._sos = None
        self._zi = None
        if self.dst_rate < self.src_rate:
            from scipy.signal import butter, sosfilt_zi
            cutoff = 0.45 * self.dst_rate / (0.5 * self.src_rate)
            self._sos = butter(4, min(cutoff, 0.99), btype="low", output="sos")
            self._zi = sosfilt_zi(self._sos) * 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        if x.size == 0:
            return x
        if self._sos is not None:
            from scipy.signal import sosfilt
            x, self._zi = sosfilt(self._sos, x, zi=self._zi)
            x = x.astype(np.float32, copy=False)

        buf = np.concatenate((self._prev, x))
        last = buf.shape[0] - 1
        if last <= 0 or self.phase > last:
            self._prev = buf[-1:]
            self.phase = max(0.0, self.phase - last)
            return np.zeros(0, dtype=np.float32)

        n_out = int(np.floor((last - self.phase) / self.step)) + 1
        idx = self.phase + np.arange(n_out) * self.step
        out = np.interp(idx, np.arange(buf.shape[0]), buf).astype(np.float32)
        # faza urmatorului esantion, raportata la ultimul esantion pastrat
        self.phase = float(idx[-1] + self.step - last)
        self._prev = buf[-1:]
        return out


def available_loopback_backends() -> list[str]:
    """Backend-urile de loopback instalate, in ordinea de preferinta."""
    found: list[str] = []
    try:
        import soundcard  # noqa: F401
        found.append("soundcard")
    except Exception:  # noqa: BLE001
        pass
    try:
        import pyaudiowpatch  # noqa: F401
        found.append("pyaudiowpatch")
    except Exception:  # noqa: BLE001
        pass
    if sounddevice_supports_loopback() or find_sounddevice_loopback_input():
        found.append("sounddevice")
    return found


# ======================================================================
#  CAPTURA (mixer de surse)
# ======================================================================
class AudioCapture:
    """Deschide sursele configurate si livreaza cadre mixate de `hop` esantioane."""

    def __init__(self, cfg, on_error: Callable[[str, str], None] | None = None):
        self.cfg = cfg
        self.on_error = on_error
        self.samplerate = int(cfg.get("audio.samplerate", 48000))
        self.block_size = int(cfg.get("audio.block_size", 256))
        self.hop = int(cfg.get("audio.hop_size", 512))
        self.input_gain = float(cfg.get("audio.input_gain", 1.0))
        max_ms = float(cfg.get("audio.max_buffer_ms", 120))
        self.max_frames = max(self.hop * 2, int(self.samplerate * max_ms / 1000.0))
        self.sources: list[Any] = []          # AudioSource | LoopbackThreadSource
        self.running = False
        self._mix = np.zeros(self.hop, dtype=np.float32)
        self.warnings: list[str] = []
        self.loopback_backend = ""

    # ---------------- pornire ----------------
    def start(self) -> None:
        if self.running:
            return
        _require_sd()
        self.warnings.clear()
        sources_cfg = self.cfg.get("audio.sources", {}) or {}

        loop_cfg = sources_cfg.get("loopback", {})
        if loop_cfg.get("enabled", True):
            try:
                self._start_loopback(loop_cfg)
            except Exception as exc:  # noqa: BLE001
                msg = f"Loopback indisponibil: {exc}"
                log.error(msg)
                self.warnings.append(msg)
                if self.on_error:
                    self.on_error("loopback", str(exc))

        mic_cfg = sources_cfg.get("microphone", {})
        if mic_cfg.get("enabled", False):
            try:
                self._start_microphone(mic_cfg)
            except Exception as exc:  # noqa: BLE001
                msg = f"Microfon indisponibil: {exc}"
                log.error(msg)
                self.warnings.append(msg)
                if self.on_error:
                    self.on_error("microphone", str(exc))

        if not self.sources:
            raise RuntimeError(
                "Nicio sursa audio nu a putut fi deschisa. "
                + (" | ".join(self.warnings) if self.warnings else "")
            )
        self.running = True

    def _start_loopback(self, conf: dict) -> None:
        """Porneste captura desktop, incercand backend-urile pe rand."""
        requested = str(conf.get("backend", "auto")).lower()
        candidates = (available_loopback_backends() if requested in ("auto", "", "none")
                      else [requested])
        if not candidates:
            raise RuntimeError(
                "Niciun backend de loopback disponibil. Instaleaza unul:\n"
                "  py -3.12 -m pip install soundcard\n"
                "  py -3.12 -m pip install PyAudioWPatch\n"
                "sau activeaza 'Stereo Mix' in Windows / instaleaza VB-Cable."
            )

        errors: list[str] = []
        for backend in candidates:
            try:
                if backend == "sounddevice":
                    self._start_loopback_sounddevice(conf)
                else:
                    src = LoopbackThreadSource(
                        name="loopback", backend=backend,
                        samplerate=self.samplerate, block_size=self.block_size,
                        gain=float(conf.get("gain", 1.0)), max_frames=self.max_frames,
                        device_hint=conf.get("device"), on_error=self.on_error,
                    )
                    src.start()
                    self.sources.append(src)
                self.loopback_backend = backend
                return
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{backend}: {exc}")
                log.warning("Backend-ul de loopback '%s' a esuat: %s", backend, exc)
        raise RuntimeError("toate backend-urile au esuat -> " + " | ".join(errors))

    def _start_loopback_sounddevice(self, conf: dict) -> None:
        """Varianta clasica: WASAPI loopback nativ sau un device tip Stereo Mix."""
        extra = _wasapi_loopback_settings()
        if extra is not None:
            device, channels, _ = find_loopback_device(conf.get("device"))
        else:
            found = find_sounddevice_loopback_input()
            if found is None:
                raise RuntimeError(
                    "sounddevice nu poate captura sunetul redat de PC "
                    "(fara suport WASAPI loopback si fara device 'Stereo Mix')")
            device, channels = found
        src = AudioSource(
            name="loopback", device=device, channels=channels,
            samplerate=self.samplerate, block_size=self.block_size,
            gain=float(conf.get("gain", 1.0)), extra_settings=extra,
            max_frames=self.max_frames, on_error=self.on_error,
        )
        src.start()
        self.sources.append(src)

    def _start_microphone(self, conf: dict) -> None:
        device, channels = find_input_device(conf.get("device"))
        src = AudioSource(
            name="microphone", device=device, channels=channels,
            samplerate=self.samplerate, block_size=self.block_size,
            gain=float(conf.get("gain", 1.0)), extra_settings=None,
            max_frames=self.max_frames, on_error=self.on_error,
            highpass_hz=float(conf.get("highpass_hz", 0.0)),
        )
        src.start()
        self.sources.append(src)

    # ---------------- citire ----------------
    def read(self) -> np.ndarray | None:
        """Un cadru mixat de `hop` esantioane, sau None daca nu e gata.

        Nu blocheaza. Firul de analiza apeleaza in bucla cu o pauza scurta.
        """
        if not self.sources:
            return None
        ready = [s for s in self.sources if s.ring.available() >= self.hop]
        if not ready:
            return None
        self._mix[:] = 0.0
        for src in ready:
            chunk = src.ring.read(self.hop)
            if chunk is not None:
                self._mix += chunk
        if self.input_gain != 1.0:
            self._mix *= self.input_gain
        np.clip(self._mix, -4.0, 4.0, out=self._mix)
        return self._mix.copy()

    def backlog_frames(self) -> int:
        """Cate esantioane asteapta in cea mai plina sursa (indicator de latenta)."""
        if not self.sources:
            return 0
        return max(s.ring.available() for s in self.sources)

    def latency_ms(self) -> float:
        """Latenta estimata: latenta stream-ului + ce sta in ring buffer."""
        if not self.sources:
            return 0.0
        stream_lat = max((s.stats.stream_latency_ms for s in self.sources), default=0.0)
        buffered = self.backlog_frames() / self.samplerate * 1000.0
        hop_lat = self.hop / self.samplerate * 1000.0
        return stream_lat + buffered + hop_lat

    def stats(self) -> dict[str, SourceStats]:
        return {s.name: s.stats for s in self.sources}

    def total_overflows(self) -> int:
        return sum(s.ring.overflows for s in self.sources)

    # ---------------- oprire ----------------
    def stop(self) -> None:
        for src in self.sources:
            src.stop()
        self.sources.clear()
        self.running = False


# ======================================================================
#  SURSA DE TEST (fara hardware) - pentru dezvoltare si teste automate
# ======================================================================
class SyntheticCapture:
    """Inlocuitor pentru AudioCapture care genereaza un click-track + bass.

    Folosit de tools/selftest.py si de `main.py --simulate` ca sa poti
    testa lantul complet (analiza -> reguli -> MagicQ) fara boxe/microfon.
    """

    def __init__(self, cfg, bpm: float = 128.0, realtime: bool = True):
        self.samplerate = int(cfg.get("audio.samplerate", 48000))
        self.hop = int(cfg.get("audio.hop_size", 512))
        self.bpm = bpm
        self.realtime = realtime
        self.running = False
        self.n = 0
        self.warnings: list[str] = []
        self._t0 = 0.0

    def start(self) -> None:
        self.running = True
        self._t0 = time.monotonic()

    def read(self) -> np.ndarray | None:
        if not self.running:
            return None
        if self.realtime:
            target = self._t0 + (self.n + self.hop) / self.samplerate
            delay = target - time.monotonic()
            if delay > 0:
                time.sleep(min(delay, 0.05))
        t = (np.arange(self.hop) + self.n) / self.samplerate
        period = 60.0 / self.bpm
        phase = np.mod(t, period)
        # kick: sinus 55 Hz cu anvelopa exponentiala scurta
        kick = np.sin(2 * np.pi * 55 * phase) * np.exp(-phase * 28.0)
        # hi-hat: zgomot filtrat pe optimi
        eighth = np.mod(t, period / 2)
        hat = (np.random.default_rng(self.n).standard_normal(self.hop).astype(np.float32)
               * np.exp(-eighth * 120.0) * 0.12)
        pad = 0.06 * np.sin(2 * np.pi * 220 * t)
        self.n += self.hop
        return (kick * 0.8 + hat + pad).astype(np.float32)

    def backlog_frames(self) -> int:
        return 0

    def latency_ms(self) -> float:
        return self.hop / self.samplerate * 1000.0

    def stats(self) -> dict:
        return {}

    def total_overflows(self) -> int:
        return 0

    def stop(self) -> None:
        self.running = False
