#!/usr/bin/env python3
"""
Zoom roll-call bot (Windows). Free: all speech recognition runs locally on your PC.

What it does:
  1. On schedule (schedule.json) opens the Zoom link and clicks Join.
  2. Listens to the audio coming out of your speakers/headphones (what the teacher says) via loopback.
  3. Recognizes speech locally (faster-whisper; on the NVIDIA GPU when available, otherwise on the CPU)
     and fuzzy-matches your surname/name. A match must be heard twice in a row before the bot answers.
  4. On a match: Zoom unmute (Alt+A) -> plays one of your answer files (picked at random:
     answer.wav, answer2.wav, answer3.wav ...) into the virtual microphone (VB-Cable) -> Zoom mute (Alt+A).
     After the first answer it STOPS listening for that class.

Usage:
  python zoom_attendance_bot.py                          # follow the schedule (two alternating weeks)
  python zoom_attendance_bot.py --today                  # show the current week and today's/tomorrow's classes
  python zoom_attendance_bot.py --test-listen            # only listens and prints, never answers
  python zoom_attendance_bot.py --test-answer            # in 5 s: unmute -> file -> mute
  python zoom_attendance_bot.py --list-audio             # list audio devices (for troubleshooting)
  python zoom_attendance_bot.py --test-file class.wav    # run the detector over a recording: where would it answer?
  python zoom_attendance_bot.py --console --unlimited    # no schedule, answer every time (what the test bench runs)

Commands on stdin (sent by the windows): pause | resume | join LINK [HH:MM] | leave | arm | disarm | answer | preview
  python zoom_attendance_bot.py --join "LINK" --until 10:30   # join now and listen until 10:30

Pause/resume answering: Ctrl+Alt+P
Everything the bot prints is also saved to logs/YYYY-MM-DD.log.
"""
import argparse
import gc
import glob
import json
import os
import queue
from collections import deque
import random
import re
import subprocess
import sys
import threading
import time
import warnings
from datetime import datetime, timedelta
from math import gcd
from urllib.parse import parse_qs, urlparse

import numpy as np
import soundcard as sc
import soundfile as sf
import keyboard
from rapidfuzz import fuzz
from scipy.signal import resample_poly
from faster_whisper import WhisperModel

# harmless: printed by soundcard whenever nothing is playing; it only cluttered the log
warnings.filterwarnings("ignore", message="data discontinuity in recording")

try:                      # preferred way to play the answer (more tolerant than soundcard)
    import sounddevice as sd
except Exception:         # not installed -> soundcard fallback
    sd = None

# =============================== SETTINGS ================================
BASE = os.path.dirname(os.path.abspath(__file__))
SCHEDULE_FILE = os.path.join(BASE, "schedule.json")
ANSWERS_DIR = os.path.join(BASE, "answers")             # optional folder: any audio files inside are used too
JOIN_BUTTON_IMG = os.path.join(BASE, "join_button.png")  # optional: screenshot of the Join button

# Answers: answer.wav, answer2.wav, answer3.wav ... next to this script (wav/ogg/opus/mp3/m4a/flac all work)
# and/or any audio files inside the "answers" folder. One of them is picked at random for every answer.
AVOID_REPEAT = True        # never pick the same file twice in a row (when there are 2 or more)
TRIM_AND_NORMALIZE = True  # cut silence at the start/end of each answer and set a good volume

# Where to play the answer. In Zoom, choose "CABLE Output" as the microphone.
VIRTUAL_MIC_PLAYBACK = "CABLE Input"
# Which output device to listen to. None = the Windows default output device.
# If Zoom plays sound through other headphones, put part of their name here.
LOOPBACK_SPEAKER = None

ZOOM_MUTE_HOTKEY = "alt+a"       # turn on "Enable Global Shortcut" in Zoom
PAUSE_HOTKEY = "ctrl+alt+p"

# Speech recognition
DEVICE = "auto"         # "auto" = NVIDIA GPU when it works, otherwise CPU; or force "cuda" / "cpu"
MODEL_GPU = "large-v3-turbo"   # on the GPU: much more accurate than "small" and still ~0.3 s per pass
MODEL_CPU = "small"            # on the CPU: tiny / base / small (bigger = more accurate but slower)
COMPUTE_GPU = "int8_float16"
COMPUTE_CPU = "int8"
LANGUAGE = "uk"         # "uk" Ukrainian, "ru" Russian, None = auto-detect (worse on short chunks)
# Hint for the model about which words to expect. OFF on purpose: with the hint the model "hears" the surname
# in ordinary speech and even in silence ("Сальніков Сальніков ..." over a lecture) — that was the cause of
# answers at random moments. Without it the surname is still recognized fine. To try it again:
# HOTWORDS = "Сальников Сальніков Богдан"
HOTWORDS = None

# ---- YOUR NAME: change everything in this block (the example below is "Богдан Сальников") ----
# What counts as "I was called" (Cyrillic and Latin both work; matching is fuzzy)
TRIGGERS = [
    "как можна больше вариантов вашего имени, например ==>", "bogdan salnikov", "сальников", "сальнікова", "salnikov", "сальныков",
    "сальникоу", "сальникова", "богдан сальников", "богдан сальніков", "сальнікоу",
]
MATCH_THRESHOLD = 85     # 0..100; lower = catches more but more false positives
NAME_CORE = "lnik"       # a spelling counts straight away only if it contains this part of YOUR surname, written in Latin
                         # ("Сальніков" -> "salnikov" -> "lnik"): Сальников, Сальніков, Салников ... count, but look-alikes
                         # such as "Садников" go to the sound check below. "" = off
NOT_ME = []              # classmates whose surnames sound similar to yours, e.g. ["Петренко", "Петрук"]: written like this
                         # they never trigger, AND every detection of your name is checked by sound against them (RIVAL_GAP)
# The surname is said only once, and the model sometimes WRITES it as a look-alike ("Садников", "Сенников")
# although the teacher said yours. Such words are not trusted by spelling: the bot listens to the same audio
# again and asks the model directly how well your surname fits compared to the word it wrote.
SOUND_CHECK = True
LOOK_ALIKE_THRESHOLD = 75    # how similar a word must be to get the sound check at all
SOUND_CHECK_MARGIN = 3.0     # accept only if your surname is at most this much less likely (log-prob) than the written
                             # word. 0 = only when yours fits best; bigger = more lenient.
                             # Measured: own surname said fast -> -0.5..-3; other surnames -> -6..-13
SURNAME_FORMS = ["Сальников", "Сальніков"]   # YOUR surname as it is written (each language you are called in)
RIVAL_GAP = 1.0              # your surname must fit the audio at least this much better (log-prob) than every NOT_ME
                             # surname — checked on EVERY detection, so a classmate's surname said fast and written as
                             # yours does not make the bot answer for them. 0 = just better; bigger = stricter
CONFIRM_HITS = 2         # the name must be recognized in this many passes ...
CONFIRM_WINDOW_SEC = 8.0 # ... within this many seconds (filters one-off mis-hearings). 1 = answer at the first hit
COOLDOWN_SEC = 60        # after an answer, do not answer again for this many seconds
UNLIMITED_COOLDOWN_SEC = 4   # the same with --unlimited (test app): answer every time, just not twice to one call
STOP_AFTER_ANSWER = True # after the first successful answer, stop listening for the rest of that class
RETRY_AFTER_FAIL_SEC = 8 # if answering failed, retry after this many seconds
DEAF_AFTER_ANSWER_SEC = 3.0  # ignore captured audio while answering and this long after (your answer says the surname too)

# Quality filters for recognized segments (drop guesses the model itself is unsure about)
MAX_NO_SPEECH_PROB = 0.6
MIN_AVG_LOGPROB = -1.0
MAX_COMPRESSION_RATIO = 2.4   # higher = repeated/looping text, a typical hallucination

# Performance (keeps the PC smooth during long use)
CPU_THREADS = None       # None = auto (about half of the cores, at most 4)
LOW_PRIORITY = True      # run the bot at "below normal" priority so other programs stay smooth
LISTEN_LIMIT_MIN = None  # e.g. 20 = listen only during the first 20 minutes of each class; None = whole class
LOG_TRANSCRIPTS = True   # log what was heard (repeated lines are skipped); False = log only matches
SHOW_TRANSCRIPTS = False # False: what was heard goes only to logs/*.log; the window shows the important lines
                         # (joining, answer, errors) and speech only AROUND your name. Test modes show everything
CONTEXT_BEFORE_SEC = 6   # "around your name": how much speech before it is shown in the window ...
CONTEXT_AFTER_SEC = 6    # ... and how long after it
LOG_TO_FILE = True       # also save everything to logs/YYYY-MM-DD.log (to see later why it answered)
LOG_KEEP_DAYS = 30
RECORD_CLASSES = True    # save what the bot heard in each class to logs/recordings/*.ogg (~1 MB per minute).
                         # Only on this PC; used to check and tune recognition on the real teacher's voice
RECORD_KEEP = 6          # keep only the last N class recordings
CLASS_COUNT_AFTER_MIN = 5    # statistics: a class counts as "attended" only after this many minutes in the conference

# Timings
WINDOW_SEC = 6.0         # how many of the latest seconds of audio to analyze
# (new audio before the next pass, idle pause after each pass) — the GPU is fast, so it can check more often
PASS_TIMING = {"cuda": (0.5, 0.1), "cpu": (0.8, 0.4)}
SILENCE_PEAK = 0.01      # if the whole window is quieter than this, skip recognition
PRE_UNMUTE_DELAY = 0.35  # pause after unmute so Zoom has time to enable the microphone
POST_PLAY_DELAY = 0.25   # pause after the file, before mute
TAIL_SILENCE_SEC = 0.35  # silence appended to the answer so its end is not cut off
JOIN_WAIT_SEC = 8        # how long to wait after opening the link before looking for the Join button
AFTER_JOIN_SEC = 6       # wait after Join (audio connecting)
KILL_ZOOM_AT_END = True  # close Zoom at the end of the class (leave the meeting)
OPEN_LINK_DIRECTLY = False  # False = open the link straight in the Zoom app (no browser); True = open the https link as-is
# =========================================================================

CAPTURE_SR = 48000
ANSWER_SR = 48000          # every answer is converted to this sample rate
ASR_SR = 16000
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
PAUSED = threading.Event()

# ---------- normalization and fuzzy name matching ----------
_SRC = "абвгґдеёєжзиіїйклмнопрстуфхцчшщъыьэюя"
_DST = ["a", "b", "v", "g", "g", "d", "e", "e", "e", "j", "z", "i", "i", "i", "i", "k", "l",
        "m", "n", "o", "p", "r", "s", "t", "u", "f", "h", "c", "c", "s", "s", "", "i", "",
        "e", "u", "a"]
assert len(_SRC) == len(_DST)
_TR = dict(zip(_SRC, _DST))


def norm(text: str) -> str:
    """Everything to Latin script: 'Сальніков' / 'сальныков' / 'Salnikov' -> 'salnikov'."""
    t = text.lower().replace("’", "'")
    t = re.sub(r"[^\w\s]", " ", t)
    t = "".join(_TR.get(ch, ch) for ch in t)
    return re.sub(r"\s+", " ", t).strip()


NORM_TRIGGERS = sorted({norm(t) for t in TRIGGERS})
NORM_NOT_ME = sorted({norm(t) for t in NOT_ME})
HOTWORD_FORMS = {w.lower() for w in (HOTWORDS or "").split()}


_WORD_RE = re.compile(r"\w+(?:['’]\w+)*")


def find_name(text: str):
    """Looks for my name in the text.
    -> ("sure", trigger, heard, start) — a real spelling of my surname (passes NAME_CORE),
       ("look-alike", trigger, heard_word, start) — similar word that needs the sound check, or None.
    start = position of the heard word in `text` (the text before it is used by the sound check)."""
    spans = [(m.group(0), m.start()) for m in _WORD_RE.finditer(text)]
    normed = [norm(w) for w, _ in spans]
    look_alike = None
    for n in (2, 1):                                    # full name first, then the surname alone
        for i in range(len(spans) - n + 1):
            cand = " ".join(normed[i:i + n])
            if not cand:
                continue
            best, best_t = 0.0, None
            for t in NORM_TRIGGERS:
                if len(t) - 1 <= len(cand) <= len(t) + 3:   # a cut-off word ("сальник") is not my name
                    r = fuzz.ratio(cand, t)
                    if r > best:
                        best, best_t = r, t
            if any(fuzz.ratio(normed[k], x) >= max(best, 90) for k in range(i, i + n) for x in NORM_NOT_ME):
                continue                                 # someone else's name
            if best >= MATCH_THRESHOLD and (not NAME_CORE or NAME_CORE in cand):
                k = next((k for k in range(i, i + n) if NAME_CORE and NAME_CORE in normed[k]), i + n - 1)
                return "sure", best_t, spans[k][0], spans[k][1]       # the surname word itself
            if n == 1 and look_alike is None and best >= LOOK_ALIKE_THRESHOLD:
                look_alike = ("look-alike", best_t, spans[i][0], spans[i][1])
    return look_alike


def find_trigger(text: str):
    """(trigger, heard_words) if the text surely contains my name (by spelling alone), else None."""
    r = find_name(text)
    return (r[1], r[2]) if r and r[0] == "sure" else None


def is_prompt_echo(text: str) -> bool:
    """The model repeating the HOTWORDS hint instead of real speech ("Сальников Сальніков Сальнікова ...").
    A teacher says the name in one form (maybe twice); 3+ different spellings at once is the hint leaking out."""
    toks = {w for w in re.sub(r"[^\w\s]", " ", text.lower()).split()}
    return len(toks & HOTWORD_FORMS) >= 3


# ---------- system helpers ----------
LOG_DIR = os.path.join(BASE, "logs")
RECORD_DIR = os.path.join(LOG_DIR, "recordings")
STATS_FILE = os.path.join(LOG_DIR, "stats.json")


class Stats:
    """Lifetime counters in logs/stats.json (survive restarts; the control panel shows them).
    Test modes (--console, --test-*) do not count."""

    def __init__(self, path):
        self.path = path
        self.enabled = True
        self.lock = threading.Lock()
        self.data = {"since": datetime.now().strftime("%Y-%m-%d"), "zoom_seconds": 0.0, "classes": 0,
                     "auto_answers": 0, "skipped_calls": 0, "last_class": "", "last_answer": ""}
        try:
            with open(path, encoding="utf-8") as f:
                self.data.update(json.load(f))
        except (OSError, ValueError):
            pass

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)                     # atomic: the panel never reads half a file

    def update(self, **changes):
        """update(zoom_seconds=+30.0, classes=+1, last_class="...") — numbers are added, text is replaced."""
        if not self.enabled:
            return
        with self.lock:
            for k, v in changes.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    self.data[k] = self.data.get(k, 0) + v
                else:
                    self.data[k] = v
            try:
                self._save()
            except OSError as e:
                print(f"[stats] could not save: {e!r}")

    def summary(self):
        s = int(self.data.get("zoom_seconds", 0))
        return (f"in Zoom with the bot: {s // 3600}h {s % 3600 // 60:02d}m · classes: {self.data.get('classes', 0)}"
                f" · auto-answers: {self.data.get('auto_answers', 0)}")


STATS = Stats(STATS_FILE)


class _Tee:
    """print() goes to the console/GUI as before AND to logs/YYYY-MM-DD.log (a new file every day)."""

    def __init__(self, stream):
        self.stream = stream
        self.day = None
        self.f = None
        self.lock = threading.Lock()

    def _file(self):
        day = datetime.now().strftime("%Y-%m-%d")
        if day != self.day:
            if self.f:
                self.f.close()
            os.makedirs(LOG_DIR, exist_ok=True)
            self.f = open(os.path.join(LOG_DIR, f"{day}.log"), "a", encoding="utf-8", buffering=1)
            self.day = day
        return self.f

    def write(self, s):
        try:
            self.stream.write(s)
        except Exception:
            pass
        with self.lock:
            try:
                self._file().write(s)
            except Exception:
                pass
        return len(s)

    def flush(self):
        try:
            self.stream.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self.stream, name)


def setup_file_log():
    if not LOG_TO_FILE:
        return
    sys.stdout = _Tee(sys.stdout)
    sys.stderr = _Tee(sys.stderr)          # tracebacks end up in the log file too
    cutoff = time.time() - LOG_KEEP_DAYS * 86400
    for p in glob.glob(os.path.join(LOG_DIR, "*.log")):
        try:
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
        except OSError:
            pass


def ts():
    return f"[{datetime.now():%H:%M:%S}]"


def window_only(line):
    """Shows a line in the console/window without writing it to the log file again."""
    out = sys.stdout
    try:
        (out.stream if isinstance(out, _Tee) else out).write(line + "\n")
    except Exception:
        pass


def file_only(line):
    """Writes a line to logs/YYYY-MM-DD.log without showing it in the console/window."""
    out = sys.stdout
    if isinstance(out, _Tee):
        with out.lock:
            try:
                out._file().write(line + "\n")
            except Exception:
                pass


def lower_priority():
    """Run at 'below normal' priority so the rest of Windows stays responsive."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        k = ctypes.WinDLL("kernel32")
        k.GetCurrentProcess.restype = ctypes.c_void_p
        k.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k.SetPriorityClass(k.GetCurrentProcess(), 0x00004000)   # BELOW_NORMAL_PRIORITY_CLASS
    except Exception:
        pass


_HK_ID = [0]


def register_hotkey(combo: str, callback):
    """Cheap OS-level hotkey (RegisterHotKey). Unlike keyboard.add_hotkey it does NOT install a global
    keyboard hook that every keystroke in Windows has to pass through (a known cause of typing lag)."""
    if not sys.platform.startswith("win"):
        return False
    import ctypes
    from ctypes import wintypes
    parts = combo.lower().split("+")
    mods = 0x4000                                              # MOD_NOREPEAT
    for p in parts[:-1]:
        mods |= {"alt": 1, "ctrl": 2, "shift": 4, "win": 8}[p]
    vk = ord(parts[-1].upper())
    _HK_ID[0] += 1
    hid = _HK_ID[0]

    def loop():
        user32 = ctypes.WinDLL("user32")
        if not user32.RegisterHotKey(None, hid, mods, vk):
            print(f"[hotkey] could not register {combo} (already used by another program?)")
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == 0x0312 and msg.wParam == hid:     # WM_HOTKEY
                try:
                    callback()
                except Exception as e:
                    print(f"[hotkey] error: {e!r}")

    threading.Thread(target=loop, daemon=True).start()
    return True


def audio_error_hint(e: Exception):
    if "8889000a" in str(e).lower():
        return ("[hint] 0x8889000a = the audio device is busy / taken in exclusive mode. Fix: Windows Sound settings -> "
                "Playback -> CABLE Input -> Properties -> Advanced -> untick 'Allow applications to take exclusive "
                "control' (same for Recording -> CABLE Output). Also make sure Zoom's *Speaker* is NOT 'CABLE Input' "
                "(only the Microphone should be 'CABLE Output'). Then restart the bot.")
    return None


# ---------- answer files ----------
AUDIO_EXTS = {".wav", ".ogg", ".oga", ".opus", ".mp3", ".m4a", ".aac", ".flac", ".mp4"}


def find_answer_files():
    """answer.wav, answer2.wav, answer3.ogg ... next to the script, plus anything inside the 'answers' folder."""
    found = []
    candidates = sorted(glob.glob(os.path.join(BASE, "answer*"))) + sorted(glob.glob(os.path.join(ANSWERS_DIR, "*")))
    for p in candidates:
        if os.path.isfile(p) and os.path.splitext(p)[1].lower() in AUDIO_EXTS and p not in found:
            found.append(p)
    return found


def decode_with_av(path):
    """Fallback decoder (PyAV, installed together with faster-whisper): handles Telegram voice notes (Opus), mp3, m4a..."""
    import av
    resampler = av.AudioResampler(format="flt", layout="mono", rate=ANSWER_SR)
    chunks = []

    def collect(out):
        if out is None:
            return
        for f in (out if isinstance(out, list) else [out]):
            chunks.append(f.to_ndarray())

    with av.open(path) as container:
        stream = container.streams.audio[0]
        for frame in container.decode(stream):
            collect(resampler.resample(frame))
    try:
        collect(resampler.resample(None))
    except Exception:
        pass
    if not chunks:
        raise ValueError("no audio decoded")
    return np.concatenate(chunks, axis=1)[0].astype(np.float32), ANSWER_SR


def read_audio(path):
    """Any audio file -> mono float32 at ANSWER_SR (the file extension does not matter, the content does)."""
    try:
        data, sr = sf.read(path, dtype="float32", always_2d=True)
        mono = data.mean(axis=1)
    except Exception:
        mono, sr = decode_with_av(path)
    if sr != ANSWER_SR:
        g = gcd(int(sr), ANSWER_SR)
        mono = resample_poly(mono, ANSWER_SR // g, int(sr) // g).astype(np.float32)
    return mono.astype(np.float32)


def prepare_clip(x):
    """Cut leading/trailing silence (less delay before the voice starts) and normalize the volume."""
    peak = float(np.abs(x).max()) if len(x) else 0.0
    if peak < 1e-4:
        raise ValueError("the file is silent")
    idx = np.nonzero(np.abs(x) > max(0.002, 0.05 * peak))[0]
    a = max(0, int(idx[0]) - int(0.10 * ANSWER_SR))
    b = min(len(x), int(idx[-1]) + int(0.15 * ANSWER_SR))
    seg = x[a:b]
    return np.clip(seg * (0.89 / float(np.abs(seg).max())), -1.0, 1.0).astype(np.float32)


# ---------- answer: unmute -> file -> mute ----------
class Responder:
    """Plays one of the answer files (random) into the virtual microphone. The output stream is opened
    once at start-up and kept open, so the answer starts instantly and 'device busy' problems show up immediately."""

    def __init__(self):
        paths = find_answer_files()
        if not paths:
            raise FileNotFoundError("no answer audio found: put answer.wav (and answer2.wav, answer3.wav ...) next to the script")
        self.clips = []                                   # [(file name, mono float32 at ANSWER_SR)]
        for p in paths:
            name = os.path.basename(p)
            try:
                x = read_audio(p)
                if TRIM_AND_NORMALIZE:
                    x = prepare_clip(x)
                self.clips.append((name, x))
                print(f"[audio] answer: {name} ({len(x) / ANSWER_SR:.1f} s)")
            except Exception as e:
                print(f"[audio] could not load {name}: {e!r}")
        if not self.clips:
            raise RuntimeError("none of the answer files could be loaded")
        print(f"[audio] {len(self.clips)} answer(s) loaded — one is picked at random each time")
        self.lock = threading.Lock()
        self.stream = None
        self.ch = 2
        self.backend = None
        self.last = None
        self._fit_cache = {}
        self._prepare()

    def _pick(self):
        idxs = list(range(len(self.clips)))
        if AVOID_REPEAT and self.last is not None and len(idxs) > 1:
            idxs.remove(self.last)
        self.last = random.choice(idxs)
        return self.last

    def _fit(self, i, ch):
        key = (i, ch)
        if key not in self._fit_cache:
            d = np.repeat(self.clips[i][1][:, None], ch, axis=1)
            tail = np.zeros((int(TAIL_SILENCE_SEC * ANSWER_SR), ch), dtype=np.float32)
            self._fit_cache[key] = np.ascontiguousarray(np.concatenate([d, tail]).astype(np.float32))
        return self._fit_cache[key]

    def _open_sd(self):
        order = {"MME": 0, "Windows WASAPI": 1, "Windows DirectSound": 2}
        apis = sd.query_hostapis()
        cands = []
        for idx, d in enumerate(sd.query_devices()):
            if d["max_output_channels"] > 0 and VIRTUAL_MIC_PLAYBACK.lower() in d["name"].lower():
                api = apis[d["hostapi"]]["name"]
                cands.append((order.get(api, 9), idx, api, d))
        if not cands:
            raise RuntimeError(f"no output device matching '{VIRTUAL_MIC_PLAYBACK}' (is VB-Cable installed?)")
        last = None
        for _, idx, api, d in sorted(cands, key=lambda c: c[0]):
            ch = min(2, d["max_output_channels"])
            extra = sd.WasapiSettings(auto_convert=True) if "WASAPI" in api else None
            try:
                st = sd.OutputStream(samplerate=ANSWER_SR, device=idx, channels=ch, dtype="float32",
                                     latency="low", extra_settings=extra)
                st.start()
            except Exception as e:
                last = e
                print(f"[audio] {api}: could not open '{d['name']}': {e}")
                continue
            self.stream, self.ch, self.backend = st, ch, f"sounddevice/{api}"
            print(f"[audio] output ready: '{d['name']}' via {api}")
            return
        raise last

    def _close_stream(self):
        st, self.stream = self.stream, None
        if st is not None:
            try:
                st.stop()
                st.close()
            except Exception:
                pass

    def _prepare(self):
        if self.stream is not None:
            return
        if sd is not None:
            try:
                self._open_sd()
                return
            except Exception as e:
                print(f"[audio] sounddevice output failed: {e!r}")
                hint = audio_error_hint(e)
                if hint:
                    print(hint)
        else:
            print("[audio] 'sounddevice' is not installed (python -m pip install sounddevice) — using soundcard")
        self.backend = "soundcard"

    def _play(self, i):
        if self.stream is not None:
            try:
                self.stream.write(self._fit(i, self.ch))
                return
            except Exception as e:
                print(f"[audio] stream write failed ({e!r}) — reopening")
                self._close_stream()
                self._open_sd()
                self.stream.write(self._fit(i, self.ch))
                return
        spk = sc.get_speaker(VIRTUAL_MIC_PLAYBACK)        # fallback path (created right here, in this thread)
        spk.play(self._fit(i, spk.channels), samplerate=ANSWER_SR)

    def preview(self):
        """Plays an answer on the normal speakers (to hear it yourself; the listening bot will hear it too)."""
        i = self._pick()
        name, x = self.clips[i]
        print(f"{ts()} [preview] playing '{name}' on the speakers")
        try:
            if sd is not None:
                sd.play(x, ANSWER_SR, blocking=True)
            else:
                sc.default_speaker().play(x, samplerate=ANSWER_SR)
        except Exception as e:
            print(f"[preview] ERROR: {e!r}")

    def answer(self):
        with self.lock:
            self._prepare()
            i = self._pick()
            print(f"[answer] playing '{self.clips[i][0]}' ({i + 1} of {len(self.clips)})")
            keyboard.send(ZOOM_MUTE_HOTKEY)               # unmute
            try:
                time.sleep(PRE_UNMUTE_DELAY)
                self._play(i)
                time.sleep(POST_PLAY_DELAY)
            finally:
                keyboard.send(ZOOM_MUTE_HOTKEY)           # mute (always)


# ---------- sound check for look-alike words ----------
class SoundCheck:
    """Asks the Whisper decoder how likely each spelling is at this place in THIS audio:
    log P(word | audio, the text before it). Used when the transcript has a look-alike ("Санников")
    instead of my surname: was it really someone else, or just "Сальников" said fast?"""

    def __init__(self, model):
        import ctranslate2
        from faster_whisper.tokenizer import Tokenizer
        self.ct2 = ctranslate2
        self.m = model
        self.tok = Tokenizer(model.hf_tokenizer, model.model.is_multilingual, task="transcribe",
                             language=LANGUAGE or "uk")
        self.head = list(self.tok.sot_sequence) + [self.tok.no_timestamps]

    def _encode(self, audio):
        n = 3000                                                     # 30 s of mel frames, what Whisper expects
        feats = self.m.feature_extractor(audio)[:, :n]
        if feats.shape[1] < n:
            feats = np.pad(feats, ((0, 0), (0, n - feats.shape[1])))
        sv = self.ct2.StorageView.from_array(np.ascontiguousarray(feats[None].astype(np.float32)))
        return np.array(self.m.model.encode(sv, to_cpu=True))        # (1, 1500, d)

    def scores(self, audio, prefix_text, words):
        """Sum of token log-probs of each word, forced after `prefix_text`. ctranslate2 returns the logits of
        forced prompt tokens as well: step i = distribution of the i-th token after <|notimestamps|>."""
        enc = self._encode(audio)
        pre = self.tok.encode(" " + prefix_text.strip()) if prefix_text.strip() else []
        seqs = [pre + self.tok.encode(" " + w) for w in words]
        prompts = [self.head + s for s in seqs]
        feats = self.ct2.StorageView.from_array(np.ascontiguousarray(np.repeat(enc, len(prompts), axis=0)))
        res = self.m.model.generate(feats, prompts, beam_size=1, max_length=min(448, 2 * max(map(len, prompts)) + 8),
                                    return_logits_vocab=True, suppress_blank=False, suppress_tokens=[])
        out = []
        for s, r in zip(seqs, res):
            steps = r.logits[0]
            if len(steps) < len(s):
                raise RuntimeError(f"decoder returned {len(steps)} steps for {len(s)} tokens")
            total = 0.0
            for j in range(len(pre), len(s)):
                lg = np.array(steps[j].to(self.ct2.DataType.float32).to_device(self.ct2.Device.cpu)).ravel()
                lg = lg - lg.max()
                total += float(lg[s[j]] - np.log(np.exp(lg).sum()))
            out.append(total)
        return out

    def check(self, audio, text, heard, start):
        """(ok, explanation). ok = "Сальников" fits this audio
        1) almost as well as the word the model wrote (SOUND_CHECK_MARGIN), and
        2) clearly better than every classmate's surname in NOT_ME (RIVAL_GAP)."""
        prefix = text[:start]
        if len(prefix) > 300:                                        # only the recent context matters
            prefix = prefix[-300:]
        mine_forms = list(SURNAME_FORMS)
        rivals = [w for w in NOT_ME if w.lower() != heard.lower()]
        s = self.scores(audio, prefix, mine_forms + rivals + [heard])
        k = len(mine_forms)
        mine = max(s[:k])
        form = mine_forms[int(np.argmax(s[:k]))]
        other = s[-1]
        parts = [f"{form} {mine:.1f}"]
        ok = True
        if heard.lower() not in (f.lower() for f in mine_forms):
            parts.append(f"{heard} {other:.1f}")
            ok = mine - other >= -SOUND_CHECK_MARGIN
        if rivals:
            r = int(np.argmax(s[k:k + len(rivals)]))
            rv, rival = s[k + r], rivals[r]
            parts.append(f"{rival} {rv:.1f}")
            ok = ok and mine - rv >= RIVAL_GAP
        return ok, " vs ".join(parts) + f" -> {'me' if ok else 'not me'}"


# ---------- listener ----------
class Listener:
    """One persistent listener for the whole run (no threads piling up from class to class).
    arm() = start listening for a class, disarm() = stop (idle, no CPU)."""

    def __init__(self, model, device, responder=None):
        self.model = model
        self.responder = responder
        self.min_new_sec, self.pass_gap = PASS_TIMING.get(device, PASS_TIMING["cpu"])
        self.active = threading.Event()
        self.shutdown = threading.Event()
        self.lock = threading.Lock()
        self.buf = np.zeros(0, dtype=np.float32)
        self.new_samples = 0
        self.last_answer = 0.0
        self.last_text = ""
        self.hits = []                 # times of recent unconfirmed name detections
        self.deaf_until = 0.0          # captured audio is dropped until this moment
        self.last_gc = time.time()
        self.clock = time.time         # replaced by the position in the recording in --test-file
        self.stamp = ts
        self.context = deque(maxlen=40)  # recent speech lines kept for "around your name"
        self.show_until = 0.0
        self.test_answers = []
        self.sound_check = None
        if SOUND_CHECK:
            try:
                self.sound_check = SoundCheck(model)
            except Exception as e:
                print(f"[asr] sound check unavailable ({e!r}) — look-alike words will be ignored")
        self.rec = None                # open recording of the current class (RECORD_CLASSES)
        self.rec_lock = threading.Lock()

    def start(self):
        threading.Thread(target=self._capture, daemon=True).start()
        threading.Thread(target=self._recognize, daemon=True).start()

    def _clear(self):
        with self.lock:
            self.buf = self.buf[:0]
            self.new_samples = 0
        self.hits = []

    def arm(self, title=""):
        self._clear()
        self.last_answer = 0.0
        self.last_text = ""
        self._start_recording(title)
        self.active.set()

    def disarm(self):
        self.active.clear()
        self._stop_recording()

    def close(self):
        self.shutdown.set()
        self.active.set()
        self._stop_recording()

    # --- class recordings: real data to check/tune recognition later (python zoom_attendance_bot.py --test-file ...)
    def _start_recording(self, title):
        if not RECORD_CLASSES:
            return
        try:
            os.makedirs(RECORD_DIR, exist_ok=True)
            safe = re.sub(r"[^\w\- ]+", "", title)[:40].strip().replace(" ", "_") or "class"
            path = os.path.join(RECORD_DIR, f"{datetime.now():%Y-%m-%d_%H-%M}_{safe}.ogg")
            with self.rec_lock:
                self.rec = sf.SoundFile(path, "w", samplerate=ASR_SR, channels=1, format="OGG", subtype="VORBIS")
            print(f"[rec] recording the class to {os.path.relpath(path, BASE)}")
            files = sorted(glob.glob(os.path.join(RECORD_DIR, "*.ogg")))
            for old in files[:-RECORD_KEEP]:
                os.remove(old)
        except Exception as e:
            print(f"[rec] could not start recording: {e!r}")
            self.rec = None

    def _stop_recording(self):
        with self.rec_lock:
            rec, self.rec = self.rec, None
        if rec is not None:
            try:
                rec.close()
            except Exception:
                pass

    def _record(self, mono):
        with self.rec_lock:
            if self.rec is not None:
                try:
                    self.rec.write(mono)
                except Exception as e:
                    print(f"[rec] write failed, recording stopped: {e!r}")
                    self.rec = None

    def _capture(self):
        keep = int(WINDOW_SEC * ASR_SR)
        while not self.shutdown.is_set():
            try:
                spk = sc.get_speaker(LOOPBACK_SPEAKER) if LOOPBACK_SPEAKER else sc.default_speaker()
                mic = sc.get_microphone(id=str(spk.name), include_loopback=True)
                print(f"[listen] capturing output: {spk.name}")
                with mic.recorder(samplerate=CAPTURE_SR) as rec:
                    while not self.shutdown.is_set():
                        block = rec.record(numframes=int(CAPTURE_SR * 0.25))
                        if not self.active.is_set():
                            continue                       # idle: read and drop, no processing
                        mono = block.mean(axis=1) if block.ndim > 1 else block
                        mono = resample_poly(mono, 1, CAPTURE_SR // ASR_SR).astype(np.float32)
                        self._record(mono)
                        if time.time() < self.deaf_until:
                            continue                       # just answered: do not analyze
                        with self.lock:
                            self.buf = np.concatenate([self.buf, mono])[-keep:]
                            self.new_samples += len(mono)
            except Exception as e:
                print(f"[listen] AUDIO CAPTURE ERROR: {e!r} — retrying in 5 s")
                self.shutdown.wait(5)

    def _transcribe(self, audio):
        """Recognized text, without segments the model is unsure about and without echoes of the HOTWORDS hint."""
        segs, _ = self.model.transcribe(
            audio, language=LANGUAGE, beam_size=1, temperature=0.0,
            vad_filter=True, vad_parameters={"min_silence_duration_ms": 300},
            condition_on_previous_text=False, without_timestamps=True,
            hotwords=HOTWORDS or None,
        )
        kept, dropped = [], []
        for s in segs:
            t = s.text.strip()
            if not t:
                continue
            if (s.no_speech_prob > MAX_NO_SPEECH_PROB or s.avg_logprob < MIN_AVG_LOGPROB
                    or s.compression_ratio > MAX_COMPRESSION_RATIO or is_prompt_echo(t)):
                dropped.append(t)
            else:
                kept.append(t)
        return " ".join(kept).strip(), " ".join(dropped).strip()

    def _on_hit(self):
        self.hits = []
        if self.responder is None:
            self.last_answer = self.clock()
            self.test_answers.append(self.clock())
            print(f"{self.stamp()} [test] name detected — would answer now (test mode, not answering)")
            return
        if PAUSED.is_set():
            self.last_answer = self.clock()             # count this call once, do not repeat the message every pass
            print(f"{self.stamp()} [pause] paused — not answering")
            STATS.update(skipped_calls=1)
            return
        self.last_answer = time.time()
        print("[answer] answering...")
        self.deaf_until = float("inf")                     # do not hear my own "Сальников тут"
        try:
            self.responder.answer()
        except Exception as e:
            print(f"[answer] ERROR: {e!r}")
            hint = audio_error_hint(e)
            if hint:
                print(hint)
            self.last_answer = time.time() - COOLDOWN_SEC + RETRY_AFTER_FAIL_SEC   # allow a retry soon
        else:
            print("[answer] done")
            STATS.update(auto_answers=1, last_answer=datetime.now().strftime("%Y-%m-%d %H:%M"))
            if STOP_AFTER_ANSWER:
                self.active.clear()
                print("[listen] answered — listening stopped for this class")
        finally:
            self.deaf_until = time.time() + DEAF_AFTER_ANSWER_SEC
            self._clear()

    def _recognize(self):
        while not self.shutdown.is_set():
            if not self.active.wait(timeout=1.0):
                continue
            with self.lock:
                if self.new_samples < self.min_new_sec * ASR_SR:
                    audio = None
                else:
                    audio = self.buf.copy()
                    self.new_samples = 0
            if audio is None or len(audio) < ASR_SR or np.abs(audio).max() < SILENCE_PEAK:
                time.sleep(0.1)
                continue

            t0 = time.time()
            try:
                text, dropped = self._transcribe(audio)
            except Exception as e:
                print(f"[asr] error: {e!r}")
                time.sleep(1)
                continue
            dt = time.time() - t0

            if self.active.is_set():
                if dropped and find_trigger(dropped):
                    print(f"{ts()} ({dt:.1f}s) ignored an unsure guess: {dropped[-120:]}")
                if text:
                    self._check(text, dt, audio)

            if time.time() - self.last_gc > 600:            # tidy up memory every 10 minutes
                gc.collect()
                self.last_gc = time.time()
            time.sleep(self.pass_gap)

    def _show_context(self, now):
        """My name (or a look-alike) was heard: show in the window what was said just before it
        (those lines are already in the log file) and keep showing speech for CONTEXT_AFTER_SEC."""
        if not SHOW_TRANSCRIPTS:
            before = [l for t, l in self.context if now - t <= CONTEXT_BEFORE_SEC]
            if before or now >= self.show_until:
                window_only(f"{'─' * 12} around your name {'─' * 12}")
            for l in before:
                window_only(l)
        self.context.clear()
        self.show_until = now + CONTEXT_AFTER_SEC

    def _check(self, text, dt, audio):
        found = find_name(text)
        hit, note = None, ""
        if found and found[0] == "sure" and (self.sound_check is None or not NOT_ME):
            hit = (found[1], found[2])
        elif found and self.sound_check is not None:
            # look-alike, or my spelling while a classmate has a similar surname: listen to the same audio again
            kind, trig, heard, start = found
            try:
                ok, why = self.sound_check.check(audio, text, heard, start)
            except Exception as e:
                ok, why = False, f"sound check error {e!r}"
            note = f"    [sound check: {why}]"
            if ok:
                hit = (trig, heard)
        key = norm(text)
        now = self.clock()
        if hit:
            self.hits = [t for t in self.hits if now - t <= CONFIRM_WINDOW_SEC] + [now]
        confirmed = bool(hit) and len(self.hits) >= CONFIRM_HITS
        if hit or (LOG_TRANSCRIPTS and key != self.last_text):
            shown = text if len(text) <= 160 else "…" + text[-159:]
            if confirmed:
                tag = f"    <<< MATCH {hit}"
            elif hit:
                tag = f"    (name? {hit[1]!r} — waiting for confirmation {len(self.hits)}/{CONFIRM_HITS})"
            else:
                tag = ""
            line = f"{self.stamp()} ({dt:.1f}s) {shown}{tag}{note}"
            if hit or note:
                self._show_context(now)
                print(line)
            elif SHOW_TRANSCRIPTS or now < self.show_until:
                print(line)                              # test mode, or just after my name: show it
            else:
                file_only(line)                          # plain speech: only into the log file ...
                self.context.append((now, line))         # ... but kept in case my name comes next
        elif note:
            self._show_context(now)
            print(f"{self.stamp()} ({dt:.1f}s) {text[-160:]}{note}")
        self.last_text = key
        if confirmed and now - self.last_answer > COOLDOWN_SEC:
            self._on_hit()


# ---------- joining Zoom ----------
def to_zoom_uri(link: str):
    """https://us02web.zoom.us/j/123?pwd=abc -> zoommtg://us02web.zoom.us/join?action=join&confno=123&pwd=abc
    No guest name is added, so Zoom joins with the account that is signed in on this PC."""
    u = urlparse(link)
    m = re.search(r"/j/(\d+)", u.path)
    if not m or "zoom" not in u.netloc:
        return None
    uri = f"zoommtg://{u.netloc}/join?action=join&confno={m.group(1)}"
    pwd = parse_qs(u.query).get("pwd")
    if pwd:
        uri += f"&pwd={pwd[0]}"
    return uri


def open_target(target: str):
    if sys.platform.startswith("win"):
        os.startfile(target)  # noqa
    elif sys.platform == "darwin":
        subprocess.Popen(["open", target])
    else:
        subprocess.Popen(["xdg-open", target])


JOIN_LABELS = {"join", "join meeting", "приєднатися", "приєднатись", "присоединиться", "войти"}


def click_join(timeout=40):
    """Tries to click Join: 1) via UI Automation, 2) by button screenshot, 3) Enter in the Zoom window."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            from pywinauto import Desktop
            for w in Desktop(backend="uia").windows():
                if "zoom" not in (w.window_text() or "").lower():
                    continue
                for b in w.descendants(control_type="Button"):
                    if (b.window_text() or "").strip().lower() in JOIN_LABELS:
                        b.click_input()
                        print("[join] clicked Join (UI Automation)")
                        return True
        except Exception:
            pass
        if os.path.exists(JOIN_BUTTON_IMG):
            try:
                import pyautogui
                pos = pyautogui.locateCenterOnScreen(JOIN_BUTTON_IMG, minSearchTime=0)
                if pos:
                    pyautogui.click(pos)
                    print("[join] clicked Join (by image)")
                    return True
            except Exception:
                pass
        time.sleep(1.5)

    # last resort: focus the Zoom window + Enter
    try:
        from pywinauto import Desktop
        for w in Desktop(backend="uia").windows():
            if "zoom" in (w.window_text() or "").lower():
                w.set_focus()
                time.sleep(0.3)
                keyboard.send("enter")
                print("[join] Join button not found, sent Enter to the Zoom window")
                return False
    except Exception:
        pass
    print("[join] Zoom window/Join button not found — maybe already inside the meeting")
    return False


def kill_zoom():
    if sys.platform.startswith("win"):
        subprocess.call(["taskkill", "/F", "/IM", "Zoom.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def attend(link, end_dt, listener, title=""):
    link = link.strip().rstrip(".,;)")
    LEAVE_REQ.clear()
    print(f"[{datetime.now():%H:%M:%S}] joining class '{title}', ends at {end_dt:%H:%M}")
    counted = time.time()                               # time in Zoom is added every 30 s (safe if the bot is killed)
    class_counted = False                               # "classes attended" +1 only after CLASS_COUNT_AFTER_MIN in it
    open_target(link if OPEN_LINK_DIRECTLY else (to_zoom_uri(link) or link))
    time.sleep(JOIN_WAIT_SEC)
    click_join()
    time.sleep(AFTER_JOIN_SEC)

    listener.arm(title)
    joined = time.time()                                # presence in the conference counts from here
    limit_min = LISTEN_LIMIT_MIN or 0
    listen_until = datetime.now() + timedelta(minutes=limit_min) if limit_min else end_dt
    try:
        while datetime.now() < end_dt:
            if listener.active.is_set() and datetime.now() >= listen_until:
                listener.disarm()
                print("[listen] time limit reached — listening stopped for this class")
            if LEAVE_REQ.wait(1.0):                      # "leave" from the control window
                LEAVE_REQ.clear()
                print(f"{ts()} [control] leaving the class")
                break
            if not JOIN_REQS.empty():                     # a new link was sent: switch to it
                print(f"{ts()} [control] switching to the new link")
                break
            if time.time() - counted >= 30:
                STATS.update(zoom_seconds=time.time() - counted)
                counted = time.time()
            if not class_counted and time.time() - joined >= CLASS_COUNT_AFTER_MIN * 60:
                class_counted = True
                STATS.update(classes=1, last_class=f"{datetime.now():%Y-%m-%d %H:%M} {title}".strip())
                if STATS.enabled:
                    print(f"{ts()} [stats] {CLASS_COUNT_AFTER_MIN} min in the class — counted as attended")
    finally:
        listener.disarm()
        STATS.update(zoom_seconds=time.time() - counted)
    print(f"[{datetime.now():%H:%M:%S}] class ended")
    if STATS.enabled:
        print(f"[stats] {STATS.summary()}")
    if KILL_ZOOM_AT_END:
        kill_zoom()


# ---------- commands from the control window (one per line on stdin) ----------
JOIN_REQS = queue.Queue()        # (link, "HH:MM" or None)
LEAVE_REQ = threading.Event()
MANUAL_CLASS_MIN = 95            # a class joined by link without an end time lasts this long


def class_end(until):
    """'HH:MM' -> datetime today (tomorrow if that time has already passed); None -> now + MANUAL_CLASS_MIN."""
    now = datetime.now()
    if not until:
        return now + timedelta(minutes=MANUAL_CLASS_MIN)
    end = datetime.combine(now.date(), parse_hm(until))
    return end if end > now else end + timedelta(days=1)


def set_paused(flag):
    if flag:
        PAUSED.set()
        print(f"{ts()} [pause] answers PAUSED — auto-attendance is off")
    else:
        PAUSED.clear()
        print(f"{ts()} [pause] answers ENABLED — auto-attendance is on")


def control_loop(listener, responder):
    """pause | resume | join LINK [HH:MM] | leave | arm | disarm | answer | preview"""
    for raw in sys.stdin:
        cmd, _, rest = raw.strip().partition(" ")
        cmd, rest = cmd.lower(), rest.strip()
        try:
            if not cmd:
                continue
            if cmd == "pause":
                set_paused(True)
            elif cmd == "resume":
                set_paused(False)
            elif cmd == "join":
                parts = rest.split()
                if not parts:
                    print("[control] join: no link given")
                    continue
                until = parts[1] if len(parts) > 1 else None
                if until:
                    parse_hm(until)                      # validate early: "25:99" -> error below
                JOIN_REQS.put((parts[0], until))
                print(f"{ts()} [control] join requested: {parts[0]}" + (f" until {until}" if until else ""))
            elif cmd == "leave":
                LEAVE_REQ.set()
                print(f"{ts()} [control] leave requested")
            elif cmd == "arm":
                listener.arm("test")
                print(f"{ts()} [control] listening ON")
            elif cmd == "disarm":
                listener.disarm()
                print(f"{ts()} [control] listening OFF")
            elif cmd == "answer":
                threading.Thread(target=manual_answer, args=(responder,), daemon=True).start()
            elif cmd == "preview":
                threading.Thread(target=responder.preview, daemon=True).start()
            else:
                print(f"[control] unknown command: {raw.strip()}")
        except Exception as e:
            print(f"[control] error in '{raw.strip()}': {e!r}")


def manual_answer(responder):
    print(f"{ts()} [answer] answering... (manual)")
    try:
        responder.answer()
        print(f"{ts()} [answer] done")
    except Exception as e:
        print(f"{ts()} [answer] ERROR: {e!r}")
        hint = audio_error_hint(e)
        if hint:
            print(hint)


def start_control(listener, responder):
    if sys.stdin is not None:
        threading.Thread(target=control_loop, args=(listener, responder), daemon=True).start()


def run_console(listener):
    """No schedule: just waits for commands (used by the test app)."""
    print(f"{ts()} [console] ready — waiting for commands (join, arm, answer, ...)")
    while True:
        try:
            link, until = JOIN_REQS.get(timeout=1.0)
        except queue.Empty:
            if LEAVE_REQ.is_set():                       # "leave" while not in a class -> just close Zoom
                LEAVE_REQ.clear()
                kill_zoom()
                print(f"{ts()} [control] Zoom closed")
            continue
        attend(link, class_end(until), listener, "by link")


# ---------- schedule ----------
def parse_hm(s: str):
    return datetime.strptime(s, "%H:%M").time()


def load_schedule():
    with open(SCHEDULE_FILE, encoding="utf-8") as f:
        cfg = json.load(f)
    anchor = datetime.strptime(cfg["week1_monday"], "%Y-%m-%d").date()
    cfg["_anchor"] = anchor - timedelta(days=anchor.weekday())   # Monday of week 1
    return cfg


def week_number(cfg, d):
    """1 or 2: which week it is (weeks alternate)."""
    monday = d - timedelta(days=d.weekday())
    return ((monday - cfg["_anchor"]).days // 7) % 2 + 1


def lessons_for(cfg, d):
    wk, day = week_number(cfg, d), DAYS[d.weekday()]
    res = []
    for les in cfg["lessons"]:
        if les.get("week") not in (None, wk):
            continue
        if day not in [x.lower() for x in les["days"]]:
            continue
        res.append(les)
    return sorted(res, key=lambda l: l["start"])


def print_today():
    cfg = load_schedule()
    for offset in (0, 1):
        d = datetime.now().date() + timedelta(days=offset)
        print(f"\n{d:%a %d.%m.%Y} — week #{week_number(cfg, d)}")
        items = lessons_for(cfg, d)
        if not items:
            print("  (no classes)")
        for l in items:
            mark = "JOIN" if l.get("enabled", True) else "skip"
            print(f"  {l['start']}-{l['end']}  [{mark}]  {l.get('title', '')}")


def run_scheduler(listener):
    cfg = load_schedule()
    mtime = os.path.getmtime(SCHEDULE_FILE)
    done = set()
    print(f"[sched] today is week #{week_number(cfg, datetime.now().date())}. Waiting for classes on schedule. Ctrl+C to quit.")
    while True:
        try:                                             # schedule.json edited while running -> pick it up
            m = os.path.getmtime(SCHEDULE_FILE)
            if m != mtime:
                mtime = m
                cfg = load_schedule()
                print(f"{ts()} [sched] schedule.json changed — reloaded")
        except Exception as e:
            print(f"{ts()} [sched] could not reload schedule.json ({e!r}) — keeping the previous one")
        before = timedelta(minutes=cfg.get("join_minutes_before", 1))
        now = datetime.now()
        for les in lessons_for(cfg, now.date()):
            if not les.get("enabled", True):
                continue
            start = datetime.combine(now.date(), parse_hm(les["start"]))
            end = datetime.combine(now.date(), parse_hm(les["end"]))
            key = (now.date(), les["start"], les["link"])
            if start - before <= now < end and key not in done:
                done.add(key)
                attend(les["link"], end, listener, les.get("title", ""))
        try:                                             # a link sent from the control window
            link, until = JOIN_REQS.get(timeout=5)
        except queue.Empty:
            continue
        attend(link, class_end(until), listener, "by link")


def cpu_threads():
    if CPU_THREADS:
        return CPU_THREADS
    return max(2, min(4, (os.cpu_count() or 4) // 2))


def add_cuda_dll_dirs():
    """CUDA libraries installed with pip (nvidia-cublas-cu12, nvidia-cudnn-cu12) live in site-packages/nvidia/*/bin."""
    import importlib.util
    spec = importlib.util.find_spec("nvidia")
    if not spec or not spec.submodule_search_locations:
        return
    for root in spec.submodule_search_locations:
        for b in glob.glob(os.path.join(root, "*", "bin")):
            try:
                os.add_dll_directory(b)
            except (OSError, AttributeError):
                pass
            os.environ["PATH"] = b + os.pathsep + os.environ.get("PATH", "")


def load_model():
    """(model, "cuda" | "cpu"). With DEVICE="auto" the GPU is tried first and the CPU is the fallback."""
    if DEVICE in ("auto", "cuda"):
        try:
            add_cuda_dll_dirs()
            print(f"[asr] loading model {MODEL_GPU} on the GPU ({COMPUTE_GPU}) — the first run downloads it...")
            m = WhisperModel(MODEL_GPU, device="cuda", compute_type=COMPUTE_GPU)
            segs, _ = m.transcribe(np.zeros(ASR_SR, np.float32), language=LANGUAGE)
            list(segs)                                   # a real pass: catches missing CUDA libraries right now
            print("[asr] GPU ready")
            return m, "cuda"
        except Exception as e:
            if DEVICE == "cuda":
                raise
            print(f"[asr] GPU not available ({str(e)[:150]}) — using the CPU")
            gc.collect()
    n = cpu_threads()
    print(f"[asr] loading model {MODEL_CPU} on the CPU ({COMPUTE_CPU}, {n} threads) — the first run downloads it...")
    return WhisperModel(MODEL_CPU, device="cpu", compute_type=COMPUTE_CPU, cpu_threads=n, num_workers=1), "cpu"


def test_file(model, device, path):
    """Feeds a recording through the same detector as in a live class (sliding window, filters, confirmation)
    and prints where in the recording the bot would answer."""
    x = read_audio(path)                                        # mono, ANSWER_SR
    x = resample_poly(x, 1, ANSWER_SR // ASR_SR).astype(np.float32)
    lst = Listener(model, device, None)
    lst.active.set()
    lst.last_answer = -1e9                                      # no cooldown at the very start of the recording
    pos = [0.0]
    lst.clock = lambda: pos[0]
    lst.stamp = lambda: f"[{int(pos[0] // 60):02d}:{pos[0] % 60:04.1f}]"
    step, keep = int(lst.min_new_sec * ASR_SR), int(WINDOW_SEC * ASR_SR)
    print(f"[test-file] {os.path.basename(path)}: {len(x) / ASR_SR:.1f} s, a pass every {lst.min_new_sec} s of audio")
    t_start = time.time()
    for end in range(step, len(x) + step, step):
        end = min(end, len(x))
        pos[0] = end / ASR_SR
        win = x[max(0, end - keep):end]
        if len(win) < ASR_SR or np.abs(win).max() < SILENCE_PEAK:
            continue
        t0 = time.time()
        text, dropped = lst._transcribe(win)
        dt = time.time() - t0
        if dropped and find_trigger(dropped):
            print(f"{lst.stamp()} ({dt:.1f}s) ignored an unsure guess: {dropped[-120:]}")
        if text:
            lst._check(text, dt, win)
    took = time.time() - t_start
    where = ", ".join(f"{int(t // 60):02d}:{t % 60:04.1f}" for t in lst.test_answers) or "nowhere"
    print(f"[test-file] would answer at: {where}   (processed in {took:.1f} s)")
    return lst.test_answers


def toggle_pause():
    set_paused(not PAUSED.is_set())


def list_audio():
    print("answer files found:")
    for p in find_answer_files():
        print("  ", os.path.relpath(p, BASE))
    if sd is not None:
        print("sounddevice output devices:")
        apis = sd.query_hostapis()
        for i, d in enumerate(sd.query_devices()):
            if d["max_output_channels"] > 0:
                print(f"  [{i}] {d['name']}  ({apis[d['hostapi']]['name']}, {d['max_output_channels']} ch)")
    else:
        print("sounddevice is not installed")
    print("soundcard speakers:")
    for s in sc.all_speakers():
        print("  ", s.name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-listen", action="store_true")
    ap.add_argument("--test-answer", action="store_true")
    ap.add_argument("--list-audio", action="store_true", help="list audio output devices (for troubleshooting)")
    ap.add_argument("--today", action="store_true", help="show the week number and today's/tomorrow's classes")
    ap.add_argument("--test-file", metavar="AUDIO",
                    help="run the name detector over a recording (e.g. a recorded class) and show where it would answer")
    ap.add_argument("--join")
    ap.add_argument("--until", help="HH:MM, listen until this time (used with --join)")
    ap.add_argument("--console", action="store_true",
                    help="no schedule: wait for commands on stdin (join/arm/answer/...); used by the test app")
    ap.add_argument("--unlimited", action="store_true",
                    help="answer EVERY time the name is heard (no stop after the first answer); for testing")
    args = ap.parse_args()

    global STOP_AFTER_ANSWER, COOLDOWN_SEC, RECORD_CLASSES, SHOW_TRANSCRIPTS
    if args.console or args.test_listen or args.test_file:
        SHOW_TRANSCRIPTS = True                      # test modes: show everything that was heard
    if args.unlimited:
        STOP_AFTER_ANSWER = False
        COOLDOWN_SEC = UNLIMITED_COOLDOWN_SEC
    if args.console:
        RECORD_CLASSES = False                       # test sessions are not recorded ...
        STATS.enabled = False                        # ... and do not count in the statistics

    if args.today:
        print_today()
        return
    if args.list_audio:
        list_audio()
        return

    setup_file_log()
    print(f"===== {datetime.now():%Y-%m-%d %H:%M:%S} bot started {' '.join(sys.argv[1:])} =====")
    if LOW_PRIORITY:
        lower_priority()
    register_hotkey(PAUSE_HOTKEY, toggle_pause)

    if args.test_answer:
        r = Responder()
        print("In 5 seconds: unmute -> file -> mute. Switch to the Zoom window.")
        time.sleep(5)
        try:
            r.answer()
        except Exception as e:
            print(f"[answer] ERROR: {e!r}")
            hint = audio_error_hint(e)
            if hint:
                print(hint)
        return

    model, device = load_model()

    if args.test_file:
        test_file(model, device, args.test_file)
        return

    if args.test_listen:
        lst = Listener(model, device, None)
        lst.start()
        lst.arm()
        print("Listening (not answering). Ctrl+C to quit.")
        while True:
            time.sleep(1)

    responder = Responder()
    listener = Listener(model, device, responder)
    listener.start()
    start_control(listener, responder)
    if args.unlimited:
        print("[mode] UNLIMITED: answers every time the name is heard")

    if args.console:
        run_console(listener)
        return

    if args.join:
        until = datetime.combine(datetime.now().date(), parse_hm(args.until)) if args.until \
            else datetime.now() + timedelta(hours=1)
        attend(args.join, until, listener)
        return

    run_scheduler(listener)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nexit")
