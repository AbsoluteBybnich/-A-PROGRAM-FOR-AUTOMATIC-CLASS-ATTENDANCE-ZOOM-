"""Shared pieces of the two windows (START.pyw — the control panel, bot_tester.pyw — the test bench):
dark theme, buttons, icons, tooltips, the live log and the bot process (talked to through stdin)."""
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime
from tkinter import ttk

BASE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.join(BASE, "zoom_attendance_bot.py")
SCHEDULE = os.path.join(BASE, "schedule.json")
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ---- palette ----
BG, PANEL, CARD, LOG_BG = "#121316", "#1a1c20", "#202328", "#0d0e10"
BORDER = "#2a2d33"
FG, MUTED, DIM = "#e8eaed", "#9aa1ab", "#5d636d"
OK, INFO, WARN, ERR, IDLE = "#3fb950", "#58a6ff", "#d29922", "#f85149", "#8b919a"
GREEN = ("#238636", "#2ea043")
AMBER = ("#9a6700", "#bb8009")
RED = ("#b62324", "#da3633")
BLUE = ("#1f6feb", "#388bfd")
GHOST = ("#2b2e34", "#3a3e46")
FONT = "Segoe UI"

MAX_LOG_LINES = 800
TS_RE = re.compile(r"^\[\d\d:\d\d:\d\d\]")


def find_python():
    """Python with the installed libraries (the project's venv), without a console window."""
    exe = sys.executable
    if sys.prefix == sys.base_prefix:  # not running inside a venv -> look for .venv nearby/above
        d = BASE
        for _ in range(3):
            cand = os.path.join(d, ".venv", "Scripts", "python.exe")
            if os.path.exists(cand):
                return cand
            d = os.path.dirname(d)
    if os.path.basename(exe).lower() == "pythonw.exe":
        cand = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.exists(cand):
            return cand
    return exe


PY = find_python()


def run_quiet(cmd):
    return subprocess.call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NOWIN)


def close_zoom():
    run_quiet(["taskkill", "/F", "/IM", "Zoom.exe"])


def enable_dpi():
    try:
        import ctypes
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(1)         # crisp text on high-DPI screens
    except Exception:
        pass


def dark_titlebar(root):
    """Dark Windows title bar (Windows 10 2004+ / 11). Silently ignored elsewhere."""
    try:
        import ctypes
        root.update_idletasks()
        hwnd = ctypes.WinDLL("user32").GetParent(root.winfo_id())
        val = ctypes.c_int(1)
        for attr in (20, 19):
            if ctypes.WinDLL("dwmapi").DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)) == 0:
                break
    except Exception:
        pass


def setup_style(root):
    s = ttk.Style(root)
    s.theme_use("clam")
    s.configure("Dark.Vertical.TScrollbar", background="#33363d", troughcolor=LOG_BG, bordercolor=LOG_BG,
                arrowcolor=FG, lightcolor="#33363d", darkcolor="#33363d", relief="flat")
    s.map("Dark.Vertical.TScrollbar", background=[("active", "#4a4f59"), ("pressed", "#5a606b")])


# ---------- widgets ----------
class Tooltip:
    """Small dark pop-up that explains a control when the mouse rests on it."""

    def __init__(self, widget, text, delay=400):
        self.widget, self.text, self.delay = widget, text, delay
        self.tip = None
        self.job = None
        widget.bind("<Enter>", self._enter, add="+")
        widget.bind("<Leave>", self._leave, add="+")
        widget.bind("<ButtonPress>", self._leave, add="+")

    def _enter(self, _e=None):
        self._cancel()
        self.job = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self.job is not None:
            self.widget.after_cancel(self.job)
            self.job = None

    def _show(self):
        self.job = None
        if self.tip is not None:
            return
        x = self.widget.winfo_rootx() + 6
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
        self.tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tw.attributes("-topmost", True)
        tk.Label(tw, text=self.text, bg="#2b2e34", fg=FG, font=(FONT, 9), padx=10, pady=6, justify="left",
                 highlightthickness=1, highlightbackground="#454a53").pack()

    def _leave(self, _e=None):
        self._cancel()
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None


def make_btn(parent, text, cmd, colors, size=11, padx=16, pady=9, bold=True):
    """Flat colored button with a hover color. btn.recolor(colors) changes its colors later."""
    b = tk.Button(parent, text=text, command=cmd, bg=colors[0], fg="white",
                  activebackground=colors[1], activeforeground="white", disabledforeground="#6b7078",
                  font=(FONT, size, "bold" if bold else "normal"), relief="flat", bd=0, padx=padx, pady=pady,
                  cursor="hand2", highlightthickness=0)
    b.colors = colors

    def recolor(new):
        b.colors = new
        if str(b["state"]) == "normal":
            b.config(bg=new[0], activebackground=new[1])

    def enable(on=True):
        if on:
            b.config(state="normal", bg=b.colors[0], cursor="hand2")
        else:
            b.config(state="disabled", bg="#24272c", cursor="arrow")

    b.recolor, b.enable = recolor, enable
    b.bind("<Enter>", lambda e: str(b["state"]) == "normal" and b.config(bg=b.colors[1]))
    b.bind("<Leave>", lambda e: str(b["state"]) == "normal" and b.config(bg=b.colors[0]))
    return b


class IconButton(tk.Canvas):
    """Square icon button drawn on a canvas (looks the same on every PC). kind: 'menu' | 'trash' | 'close'."""

    def __init__(self, parent, kind, command, size=38, bg=None, colors=GHOST):
        k = parent.winfo_fpixels("1i") / 96.0                  # scale with Windows display scaling
        px = int(size * k)
        super().__init__(parent, width=px, height=px, bg=bg or parent["bg"], highlightthickness=0, cursor="hand2")
        self.command, self.colors = command, colors
        self.rect = self.create_rectangle(0, 0, px, px, fill=colors[0], outline="")
        c = "#c9ced6"
        s = lambda v: v * px / 40.0
        w = max(2, int(2 * k))
        if kind == "menu":                                     # three bars
            for y in (13, 20, 27):
                self.create_line(s(11), s(y), s(29), s(y), fill=c, width=w, capstyle="round")
        elif kind == "trash":
            self.create_line(s(11), s(14), s(29), s(14), fill=c, width=w, capstyle="round")
            self.create_line(s(16), s(14), s(16), s(10), s(24), s(10), s(24), s(14), fill=c, width=w, joinstyle="round")
            self.create_polygon(s(13), s(17), s(27), s(17), s(25.5), s(31), s(14.5), s(31), outline=c, fill="",
                                width=w, joinstyle="round")
            for x in (17.5, 20, 22.5):
                self.create_line(s(x), s(20), s(x), s(28), fill=c, width=max(1, int(k)))
        elif kind == "close":
            self.create_line(s(13), s(13), s(27), s(27), fill=c, width=w, capstyle="round")
            self.create_line(s(27), s(13), s(13), s(27), fill=c, width=w, capstyle="round")
        self.bind("<Enter>", lambda e: self.itemconfig(self.rect, fill=self.colors[1]))
        self.bind("<Leave>", lambda e: self.itemconfig(self.rect, fill=self.colors[0]))
        self.bind("<Button-1>", lambda e: self.command())

    def set_active(self, on):
        self.colors = BLUE if on else GHOST
        self.itemconfig(self.rect, fill=self.colors[0])


class HintEntry(tk.Entry):
    """Dark text field with a grey hint text while it is empty."""

    def __init__(self, parent, hint, width=40, **kw):
        super().__init__(parent, width=width, bg=LOG_BG, fg=FG, insertbackground=FG, relief="flat", bd=0,
                         font=(FONT, 11), highlightthickness=1, highlightbackground=BORDER, highlightcolor=INFO, **kw)
        self.hint = hint
        self._hinting = False
        self.bind("<FocusIn>", self._clear_hint)
        self.bind("<FocusOut>", self._show_hint)
        self._show_hint()

    def _show_hint(self, _e=None):
        if not super().get():
            self._hinting = True
            self.config(fg=DIM)
            self.insert(0, self.hint)

    def _clear_hint(self, _e=None):
        if self._hinting:
            self.delete(0, "end")
            self.config(fg=FG)
            self._hinting = False

    def value(self):
        return "" if self._hinting else super().get().strip()

    def clear(self):
        self._clear_hint()
        self.delete(0, "end")
        if self.focus_get() is not self:
            self._show_hint()


class LogView(tk.Frame):
    """Live log with colored lines. Lines from the bot that already carry [HH:MM:SS] are not stamped again."""

    def __init__(self, parent):
        super().__init__(parent, bg=parent["bg"])
        self.text = tk.Text(self, font=("Consolas", 10), state="disabled", wrap="word", bg=LOG_BG, fg="#d0d4da",
                            insertbackground="white", relief="flat", bd=0, padx=12, pady=10,
                            highlightthickness=1, highlightbackground=BORDER, highlightcolor=BORDER,
                            selectbackground="#2f4a7a")
        sb = ttk.Scrollbar(self, orient="vertical", style="Dark.Vertical.TScrollbar", command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        t = self.text
        t.tag_config("ts", foreground=DIM)
        t.tag_config("match", background="#3d3300", foreground="#ffe58a")
        t.tag_config("err", foreground="#ff7b72")
        t.tag_config("hint", foreground="#e3b341")
        t.tag_config("sys", foreground="#7aa2ff")
        t.tag_config("ok", foreground="#56d364")

    def log(self, line, tag=None, stamp=True):
        self.log_many([(line, tag, stamp)])

    def log_many(self, items):
        if not items:
            return
        t = self.text
        at_bottom = t.yview()[1] >= 0.999
        t.config(state="normal")
        for line, tag, stamp in items:
            if stamp:
                t.insert("end", f"{datetime.now():%H:%M:%S}  ", "ts")
            t.insert("end", line + "\n", tag or ())
        n = int(t.index("end-1c").split(".")[0])
        if n > MAX_LOG_LINES:                                  # trim old lines so the widget stays light
            t.delete("1.0", f"{n - MAX_LOG_LINES + 1}.0")
        t.config(state="disabled")
        if at_bottom:
            t.see("end")

    def clear(self):
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        self.text.config(state="disabled")


def line_tag(line):
    low = line.lower()
    if "<<<" in line or "[answer] answering" in low or "[answer] done" in low:
        return "match"
    if "[hint]" in low:
        return "hint"
    if "traceback" in low or "error" in low:
        return "err"
    if "[control]" in low or "[pause]" in low:
        return "sys"
    return None


# ---------- the bot process ----------
class BotProcess:
    """zoom_attendance_bot.py as a child process: its output lines go to `q` as ("line", text),
    its exit as ("exit", code); commands are sent as lines on its stdin."""

    def __init__(self, q):
        self.q = q
        self.proc = None
        self.stopping = False

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, args=()):
        if self.running():
            return True
        self.stopping = False
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        try:
            self.proc = subprocess.Popen(
                [PY, "-u", BOT, *args], cwd=BASE, env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=NOWIN)
        except Exception as e:
            self.q.put(("line", f"Could not start the bot: {e!r}"))
            return False
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        return True

    def _reader(self, proc):
        for line in proc.stdout:
            self.q.put(("line", line.rstrip()))
        proc.wait()
        self.q.put(("exit", proc.returncode))

    def send(self, cmd):
        if not self.running():
            return False
        try:
            self.proc.stdin.write(cmd.strip() + "\n")
            self.proc.stdin.flush()
            return True
        except Exception:
            return False

    def kill(self):
        self.stopping = True
        if self.running():
            run_quiet(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)])
            return True
        return False

    def drain(self, limit=300):
        """[(kind, value)] waiting in the queue (call from the Tk thread)."""
        out = []
        try:
            for _ in range(limit):
                out.append(self.q.get_nowait())
        except queue.Empty:
            pass
        return out


def looks_like_zoom_link(s):
    return bool(re.search(r"(zoom\.us|zoommtg:)", s or "", re.I))


# ---------- statistics written by the bot (logs/stats.json) ----------
STATS_FILE = os.path.join(BASE, "logs", "stats.json")


def read_stats():
    """{} when there are no statistics yet."""
    import json
    try:
        with open(STATS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def fmt_hours(seconds):
    s = int(seconds or 0)
    h, m = s // 3600, s % 3600 // 60
    return f"{h}h {m:02d}m" if h else f"{m}m"


class StatTile(tk.Frame):
    """Small tile: big value + caption."""

    def __init__(self, parent, caption, accent=FG):
        super().__init__(parent, bg=CARD, padx=14, pady=7, highlightthickness=1, highlightbackground=BORDER)
        self.value = tk.Label(self, text="—", fg=accent, bg=CARD, font=(FONT, 13, "bold"))
        self.value.pack(anchor="w")
        tk.Label(self, text=caption, fg=MUTED, bg=CARD, font=(FONT, 8)).pack(anchor="w")

    def set(self, text):
        if self.value["text"] != text:
            self.value.config(text=text)
