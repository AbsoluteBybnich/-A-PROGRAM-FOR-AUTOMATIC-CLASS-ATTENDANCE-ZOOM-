#!/usr/bin/env python3
"""
Zoom roll-call bot (Windows). Free: all speech recognition runs locally on your PC.

What it does:
  1. On schedule (schedule.json) opens the Zoom link and clicks Join.
  2. Listens to the audio coming out of your speakers/headphones (what the teacher says) via loopback.
  3. Recognizes speech locally (faster-whisper) and fuzzy-matches your surname/name.
  4. On a match: Zoom unmute (Alt+A) -> plays one of your answer files (picked at random:
     answer.wav, answer2.wav, answer3.wav ...) into the virtual microphone (VB-Cable) -> Zoom mute (Alt+A).
     After the first answer it STOPS listening for that class.

Usage:
  python zoom_attendance_bot.py                          # follow the schedule (two alternating weeks)
  python zoom_attendance_bot.py --today                  # show the current week and today's/tomorrow's classes
  python zoom_attendance_bot.py --test-listen            # only listens and prints, never answers
  python zoom_attendance_bot.py --test-answer            # in 5 s: unmute -> file -> mute
  python zoom_attendance_bot.py --list-audio             # list audio devices (for troubleshooting)
  python zoom_attendance_bot.py --join "LINK" --until 10:30   # join now and listen until 10:30

Pause/resume answering: Ctrl+Alt+P
"""
import argparse
import gc
import glob
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
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
MODEL_SIZE = "small"    # tiny / base / small / medium / large-v3-turbo (bigger = more accurate but slower)
DEVICE = "cpu"          # "cuda" if you have an NVIDIA GPU (needs the CUDA libraries, see the faster-whisper docs)
COMPUTE_TYPE = "int8"   # cpu: "int8", cuda: "float16"
LANGUAGE = "uk"         # "uk" Ukrainian, "ru" Russian, None = auto-detect (worse on short chunks)
# Hint for the model about which words to expect (helps a lot with the surname). None = off.
HOTWORDS = "ваша фамилия, имя"

# What counts as "I was called"
TRIGGERS = [
    "как можна больше вариантов вашего имени, например ==>", "bogdan salnikov", "сальников", "сальнікова", "salnikov", "сальныков",
    "сальникоу", "сальникова", "богдан сальников", "богдан сальніков", "сальнікоу",
]
MATCH_THRESHOLD = 82     # 0..100; lower = catches more but more false positives
COOLDOWN_SEC = 60        # after an answer, do not answer again for this many seconds
STOP_AFTER_ANSWER = True # after the first successful answer, stop listening for the rest of that class
RETRY_AFTER_FAIL_SEC = 8 # if answering failed, retry after this many seconds

# Performance (keeps the PC smooth during long use)
CPU_THREADS = None       # None = auto (about half of the cores, at most 4)
LOW_PRIORITY = True      # run the bot at "below normal" priority so other programs stay smooth
PASS_GAP_SEC = 0.4       # idle pause after every recognition pass (lower CPU load, tiny extra delay)
LISTEN_LIMIT_MIN = None  # e.g. 20 = listen only during the first 20 minutes of each class; None = whole class
LOG_TRANSCRIPTS = True   # print what was heard (repeated lines are skipped); False = print only matches

# Timings
WINDOW_SEC = 6.0         # how many of the latest seconds of audio to analyze
MIN_NEW_SEC = 0.8        # run recognition once this much new audio has accumulated
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


def find_trigger(text: str):
    words = norm(text).split()
    for n in (1, 2):
        for i in range(len(words) - n + 1):
            cand = " ".join(words[i:i + n])
            for t in NORM_TRIGGERS:
                if abs(len(cand) - len(t)) <= 3 and fuzz.ratio(cand, t) >= MATCH_THRESHOLD:
                    return t, cand
    return None


# ---------- system helpers ----------
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


# ---------- listener ----------
class Listener:
    """One persistent listener for the whole run (no threads piling up from class to class).
    arm() = start listening for a class, disarm() = stop (idle, no CPU)."""

    def __init__(self, model, responder=None):
        self.model = model
        self.responder = responder
        self.active = threading.Event()
        self.shutdown = threading.Event()
        self.lock = threading.Lock()
        self.buf = np.zeros(0, dtype=np.float32)
        self.new_samples = 0
        self.last_answer = 0.0
        self.last_text = ""
        self.last_gc = time.time()

    def start(self):
        threading.Thread(target=self._capture, daemon=True).start()
        threading.Thread(target=self._recognize, daemon=True).start()

    def arm(self):
        with self.lock:
            self.buf = self.buf[:0]
            self.new_samples = 0
        self.last_answer = 0.0
        self.last_text = ""
        self.active.set()

    def disarm(self):
        self.active.clear()

    def close(self):
        self.shutdown.set()
        self.active.set()

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
                        with self.lock:
                            self.buf = np.concatenate([self.buf, mono])[-keep:]
                            self.new_samples += len(mono)
            except Exception as e:
                print(f"[listen] AUDIO CAPTURE ERROR: {e!r} — retrying in 5 s")
                self.shutdown.wait(5)

    def _transcribe(self, audio):
        segs, _ = self.model.transcribe(
            audio, language=LANGUAGE, beam_size=1, temperature=0.0,
            vad_filter=True, vad_parameters={"min_silence_duration_ms": 300},
            condition_on_previous_text=False, without_timestamps=True,
            hotwords=HOTWORDS or None,
        )
        return " ".join(s.text.strip() for s in segs if s.no_speech_prob < 0.6).strip()

    def _on_hit(self):
        if self.responder is None:
            self.last_answer = time.time()
            print("[test] name detected (not answering: test mode)")
            return
        if PAUSED.is_set():
            print("[pause] paused — not answering")
            return
        self.last_answer = time.time()
        print("[answer] answering...")
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
            if STOP_AFTER_ANSWER:
                self.active.clear()
                print("[listen] answered — listening stopped for this class")
        with self.lock:
            self.buf = self.buf[:0]
            self.new_samples = 0

    def _recognize(self):
        while not self.shutdown.is_set():
            if not self.active.wait(timeout=1.0):
                continue
            with self.lock:
                if self.new_samples < MIN_NEW_SEC * ASR_SR:
                    audio = None
                else:
                    audio = self.buf.copy()
                    self.new_samples = 0
            if audio is None or len(audio) < ASR_SR or np.abs(audio).max() < SILENCE_PEAK:
                time.sleep(0.1)
                continue

            t0 = time.time()
            try:
                text = self._transcribe(audio)
            except Exception as e:
                print(f"[asr] error: {e!r}")
                time.sleep(1)
                continue
            dt = time.time() - t0

            if text and self.active.is_set():
                hit = find_trigger(text)
                key = norm(text)
                if hit or (LOG_TRANSCRIPTS and key != self.last_text):
                    shown = text if len(text) <= 160 else "…" + text[-159:]
                    print(f"[{datetime.now():%H:%M:%S}] ({dt:.1f}s) {shown}" + (f"    <<< MATCH {hit}" if hit else ""))
                self.last_text = key
                if hit and time.time() - self.last_answer > COOLDOWN_SEC:
                    self._on_hit()

            if time.time() - self.last_gc > 600:            # tidy up memory every 10 minutes
                gc.collect()
                self.last_gc = time.time()
            time.sleep(PASS_GAP_SEC)


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
    print(f"[{datetime.now():%H:%M:%S}] joining class '{title}', ends at {end_dt:%H:%M}")
    open_target(link if OPEN_LINK_DIRECTLY else (to_zoom_uri(link) or link))
    time.sleep(JOIN_WAIT_SEC)
    click_join()
    time.sleep(AFTER_JOIN_SEC)

    listener.arm()
    limit_min = LISTEN_LIMIT_MIN or 0
    listen_until = datetime.now() + timedelta(minutes=limit_min) if limit_min else end_dt
    try:
        while datetime.now() < end_dt:
            if listener.active.is_set() and datetime.now() >= listen_until:
                listener.disarm()
                print("[listen] time limit reached — listening stopped for this class")
            time.sleep(2)
    finally:
        listener.disarm()
    print(f"[{datetime.now():%H:%M:%S}] class ended")
    if KILL_ZOOM_AT_END:
        kill_zoom()


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
    before = timedelta(minutes=cfg.get("join_minutes_before", 1))
    done = set()
    print(f"[sched] today is week #{week_number(cfg, datetime.now().date())}. Waiting for classes on schedule. Ctrl+C to quit.")
    while True:
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
        time.sleep(5)


def cpu_threads():
    if CPU_THREADS:
        return CPU_THREADS
    return max(2, min(4, (os.cpu_count() or 4) // 2))


def load_model():
    n = cpu_threads()
    print(f"[asr] loading model {MODEL_SIZE} ({DEVICE}/{COMPUTE_TYPE}, {n} threads) — the first run downloads it from the internet...")
    return WhisperModel(MODEL_SIZE, device=DEVICE, compute_type=COMPUTE_TYPE, cpu_threads=n, num_workers=1)


def toggle_pause():
    if PAUSED.is_set():
        PAUSED.clear()
        print("[pause] answers ENABLED")
    else:
        PAUSED.set()
        print("[pause] answers PAUSED")


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
    ap.add_argument("--join")
    ap.add_argument("--until", help="HH:MM, listen until this time (used with --join)")
    args = ap.parse_args()

    if args.today:
        print_today()
        return
    if args.list_audio:
        list_audio()
        return

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

    model = load_model()

    if args.test_listen:
        lst = Listener(model, None)
        lst.start()
        lst.arm()
        print("Listening (not answering). Ctrl+C to quit.")
        while True:
            time.sleep(1)

    responder = Responder()
    listener = Listener(model, responder)
    listener.start()

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
