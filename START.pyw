#!/usr/bin/env python3
"""
Control window for the Zoom bot (dark theme): buttons, clock and a live log.

  Start     -> launches zoom_attendance_bot.py (waits for classes on schedule and answers the roll call)
  Stop      -> stops the bot only (Zoom stays open)
  Shutdown  -> stops the bot AND fully closes Zoom
  (trash)   -> clears the log
Hover over any button to see what it does.

Lives in the same folder as zoom_attendance_bot.py. The desktop shortcut is created by
create_desktop_shortcut.py (run it once).
"""
import json
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from datetime import date, datetime, timedelta
from tkinter import messagebox, ttk

BASE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.join(BASE, "zoom_attendance_bot.py")
SCHEDULE = os.path.join(BASE, "schedule.json")
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ---- dark palette ----
BG, PANEL, LOG_BG = "#16171a", "#1e2024", "#0f1013"
FG, MUTED = "#e6e8eb", "#8b919a"
OK, INFO, ERR, IDLE = "#3fb950", "#58a6ff", "#f85149", "#8b919a"
BTN_START, BTN_STOP, BTN_SHUT, BTN_GHOST = (
    ("#238636", "#2ea043"), ("#9a6700", "#bb8009"), ("#b62324", "#da3633"), ("#2b2e34", "#3a3e46"))

MAX_LOG_LINES = 600                       # the log never grows beyond this (keeps the window fast for hours)
TS_RE = re.compile(r"^\[\d\d:\d\d:\d\d\]")

# A short wish shown under the status. One is picked per calendar day and changes at midnight.
# Add, remove or rewrite lines freely.
WISHES = [
    "Have a great day. May every class go smoothly.",
    "Wishing you a calm, focused and productive day.",
    "May your day be full of good news and easy answers.",
    "Stay curious today, something interesting is waiting.",
    "Small steps, big progress. Have a good one.",
    "May the Wi-Fi be stable and the lectures short.",
    "Take a breath, drink some water and enjoy the day.",
    "Today is a fresh start. Make it a good one.",
    "Wishing you energy, luck and a clear head.",
    "May everything go exactly as planned today.",
    "Be kind to yourself today. You are doing fine.",
    "Good vibes only. Have a wonderful day.",
    "May your coffee be strong and your day be easy.",
    "Wishing you smooth connections and a stress-free day.",
    "Take it one class at a time. You've got this.",
    "May today bring a little more luck than yesterday.",
    "Wishing you a bright mood and a productive day.",
    "Stay positive, good things are on the way.",
    "May your mind be sharp and your tea be warm.",
    "Let today be a good one.",
    "Smile, it's a new day full of possibilities.",
    "Wishing you patience, focus and lots of free time after classes.",
    "May the day treat you kindly.",
    "Do something you enjoy today, even if it's small.",
    "Wishing you a great day and a restful evening.",
    "Keep going, you're closer than you think.",
    "May luck be on your side today.",
    "A good day starts with a good attitude. Enjoy it!",
    "Wishing you peace, focus and good news.",
    "Stay hydrated, stay relaxed, stay awesome.",
    "Have a day as good as you deserve.",
]


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


class Tooltip:
    """Small dark pop-up that explains a button when the mouse rests on it."""

    def __init__(self, widget, text, delay=350):
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
        tk.Label(tw, text=self.text, bg="#2b2e34", fg=FG, font=("Segoe UI", 9), padx=10, pady=6, justify="left",
                 highlightthickness=1, highlightbackground="#454a53").pack()

    def _leave(self, _e=None):
        self._cancel()
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None


class TrashButton(tk.Canvas):
    """Trash-can icon button (drawn on a canvas, so it looks the same on every PC)."""

    def __init__(self, parent, command, size=40):
        k = parent.winfo_fpixels("1i") / 96.0                  # scale with Windows display scaling
        px = int(size * k)
        super().__init__(parent, width=px, height=px, bg=BG, highlightthickness=0, cursor="hand2")
        self.command = command
        self.rect = self.create_rectangle(0, 0, px, px, fill=BTN_GHOST[0], outline="")
        c, s = "#c9ced6", lambda v: v * k * size / 40.0
        self.create_line(s(11), s(14), s(29), s(14), fill=c, width=max(2, int(2 * k)), capstyle="round")      # lid
        self.create_line(s(16), s(14), s(16), s(10), s(24), s(10), s(24), s(14), fill=c,
                         width=max(2, int(2 * k)), joinstyle="round")                                         # handle
        self.create_polygon(s(13), s(17), s(27), s(17), s(25.5), s(31), s(14.5), s(31), outline=c, fill="",
                            width=max(2, int(2 * k)), joinstyle="round")                                      # bin
        for x in (17.5, 20, 22.5):                                                                            # slats
            self.create_line(s(x), s(20), s(x), s(28), fill=c, width=max(1, int(k)))
        self.bind("<Enter>", lambda e: self.itemconfig(self.rect, fill=BTN_GHOST[1]))
        self.bind("<Leave>", lambda e: self.itemconfig(self.rect, fill=BTN_GHOST[0]))
        self.bind("<Button-1>", lambda e: self.command())


class App:
    def __init__(self, root):
        self.root = root
        self.proc = None
        self.stopping = False
        self.q = queue.Queue()
        self._week_cache = (None, "")
        self._wish_day = None

        root.title("Zoom Bot")
        root.configure(bg=BG)
        root.geometry("860x620")
        root.minsize(640, 440)
        self.build_style()
        self.build_ui()

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.refresh_buttons()
        self.log(f"Python: {PY}", "sys", stamp=False)
        self.log(f"Bot:    {BOT}", "sys", stamp=False)
        self.tick()
        root.after(100, self.poll)

    # ---------- UI ----------
    def build_style(self):
        s = ttk.Style(self.root)
        s.theme_use("clam")
        s.configure("Dark.Vertical.TScrollbar", background="#3a3e46", troughcolor=LOG_BG, bordercolor=LOG_BG,
                    arrowcolor=FG, lightcolor="#3a3e46", darkcolor="#3a3e46", relief="flat")
        s.map("Dark.Vertical.TScrollbar", background=[("active", "#4a4f59"), ("pressed", "#5a606b")])

    def make_btn(self, parent, text, cmd, colors, size=12):
        b = tk.Button(parent, text=text, command=cmd, bg=colors[0], fg="white",
                      activebackground=colors[1], activeforeground="white", disabledforeground="#6b7078",
                      font=("Segoe UI", size, "bold"), relief="flat", bd=0, padx=18, pady=10, cursor="hand2")
        b.colors = colors
        b.bind("<Enter>", lambda e, b=b: b["state"] == "normal" and b.config(bg=b.colors[1]))
        b.bind("<Leave>", lambda e, b=b: b["state"] == "normal" and b.config(bg=b.colors[0]))
        return b

    def build_ui(self):
        root = self.root

        header = tk.Frame(root, bg=PANEL, padx=18, pady=14)
        header.pack(fill="x")
        header.columnconfigure(0, weight=1)

        left = tk.Frame(header, bg=PANEL)
        left.grid(row=0, column=0, sticky="w")
        self.status = tk.Label(left, text="● Stopped", fg=IDLE, bg=PANEL, font=("Segoe UI", 15, "bold"))
        self.status.pack(anchor="w")
        self.wish = tk.Label(left, text="", fg="#9aa4b2", bg=PANEL, font=("Segoe UI", 10, "italic"),
                             wraplength=460, justify="left")
        self.wish.pack(anchor="w", pady=(3, 0))

        right = tk.Frame(header, bg=PANEL)
        right.grid(row=0, column=1, sticky="e")
        self.clock = tk.Label(right, text="--:--:--", fg=FG, bg=PANEL, font=("Segoe UI", 24, "bold"))
        self.clock.pack(anchor="e")
        self.date = tk.Label(right, text="", fg=MUTED, bg=PANEL, font=("Segoe UI", 10))
        self.date.pack(anchor="e")

        row = tk.Frame(root, bg=BG, padx=16, pady=12)
        row.pack(fill="x")
        self.btn_start = self.make_btn(row, "▶  Start", self.start, BTN_START)
        self.btn_start.pack(side="left")
        self.btn_stop = self.make_btn(row, "■  Stop", self.stop, BTN_STOP)
        self.btn_stop.pack(side="left", padx=(10, 0))
        self.btn_shutdown = self.make_btn(row, "✖  Shutdown", self.shutdown, BTN_SHUT)
        self.btn_shutdown.pack(side="left", padx=10)
        btn_today = self.make_btn(row, "Today's classes", self.show_today, BTN_GHOST, 10)
        btn_today.pack(side="left", padx=(10, 0))
        btn_clear = TrashButton(row, self.clear_log)
        btn_clear.pack(side="left", padx=10)

        Tooltip(self.btn_start, "Start the bot: it waits for your classes on schedule,\njoins Zoom and answers the roll call")
        Tooltip(self.btn_stop, "Stop the bot only (Zoom stays open)")
        Tooltip(self.btn_shutdown, "Stop the bot and fully close Zoom")
        Tooltip(btn_today, "Show the current week and today's / tomorrow's classes in the log")
        Tooltip(btn_clear, "Clear the log")

        frame = tk.Frame(root, bg=BG)
        frame.pack(fill="both", expand=True, padx=16, pady=(0, 14))
        self.text = tk.Text(frame, font=("Consolas", 10), state="disabled", wrap="word", bg=LOG_BG, fg="#d0d4da",
                            insertbackground="white", relief="flat", bd=0, padx=10, pady=8,
                            highlightthickness=1, highlightbackground="#2a2d33", highlightcolor="#2a2d33",
                            selectbackground="#2f4a7a")
        sb = ttk.Scrollbar(frame, orient="vertical", style="Dark.Vertical.TScrollbar", command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.text.tag_config("ts", foreground="#5d636d")
        self.text.tag_config("match", background="#4a3d00", foreground="#ffe58a")
        self.text.tag_config("err", foreground="#ff7b72")
        self.text.tag_config("hint", foreground="#e3b341")
        self.text.tag_config("sys", foreground="#7aa2ff")

    def set_enabled(self, btn, enabled):
        if enabled:
            btn.config(state="normal", bg=btn.colors[0], cursor="hand2")
        else:
            btn.config(state="disabled", bg="#2a2d33", cursor="arrow")

    def refresh_buttons(self):
        self.set_enabled(self.btn_start, not self.running())
        self.set_enabled(self.btn_stop, self.running())
        self.set_enabled(self.btn_shutdown, True)

    def set_status(self, text, color):
        self.status.config(text="● " + text, fg=color)

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    # ---------- clock ----------
    def week_label(self, now):
        today = now.date()
        if self._week_cache[0] == today:
            return self._week_cache[1]
        label = ""
        try:
            with open(SCHEDULE, encoding="utf-8") as f:
                anchor = date.fromisoformat(json.load(f)["week1_monday"])
            anchor -= timedelta(days=anchor.weekday())
            monday = today - timedelta(days=today.weekday())
            label = f"Week {((monday - anchor).days // 7) % 2 + 1}"
        except Exception:
            pass
        self._week_cache = (today, label)
        return label

    def tick(self):
        now = datetime.now()
        self.clock.config(text=f"{now:%H:%M:%S}")
        if now.date() != self._wish_day:                       # new day -> new wish
            self._wish_day = now.date()
            self.wish.config(text=WISHES[self._wish_day.toordinal() % len(WISHES)])
        wk = self.week_label(now)
        self.date.config(text=f"{now:%A, %d %B %Y}" + (f"   ·   {wk}" if wk else ""))
        self.root.after(max(50, 1000 - now.microsecond // 1000), self.tick)

    # ---------- log ----------
    def log(self, line, tag=None, stamp=True):
        self.log_many([(line, tag, stamp)])

    def log_many(self, items):
        if not items:
            return
        at_bottom = self.text.yview()[1] >= 0.999
        self.text.config(state="normal")
        for line, tag, stamp in items:
            if stamp:
                self.text.insert("end", f"{datetime.now():%H:%M:%S}  ", "ts")
            self.text.insert("end", line + "\n", tag or ())
        n = int(self.text.index("end-1c").split(".")[0])
        if n > MAX_LOG_LINES:                                  # trim old lines so the widget stays light
            self.text.delete("1.0", f"{n - MAX_LOG_LINES + 1}.0")
        self.text.config(state="disabled")
        if at_bottom:
            self.text.see("end")

    def clear_log(self):
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        self.text.config(state="disabled")

    # ---------- actions ----------
    def start(self):
        if self.running():
            return
        if not os.path.exists(BOT):
            messagebox.showerror("Zoom Bot", f"File not found:\n{BOT}")
            return
        self.stopping = False
        self.log(f"=== starting the bot · {datetime.now():%a %d %b %Y %H:%M:%S} ===", "sys", stamp=False)
        self.set_status("Starting… (loading the model)", INFO)
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        try:
            self.proc = subprocess.Popen(
                [PY, "-u", BOT], cwd=BASE, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=NOWIN,
            )
        except Exception as e:
            self.set_status("Failed to start", ERR)
            self.log(f"Could not start: {e!r}", "err")
            return
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        self.refresh_buttons()

    def _reader(self, proc):
        for line in proc.stdout:
            self.q.put(("line", line.rstrip()))
        proc.wait()
        self.q.put(("exit", proc.returncode))

    def _kill_bot(self):
        self.stopping = True
        was_running = self.running()
        if was_running:
            run_quiet(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)])
        return was_running

    def stop(self):
        """Stop the bot only; Zoom is left as it is."""
        if self._kill_bot():
            self.log("=== bot stopped (Zoom left open) ===", "sys")
        self.set_status("Stopped", IDLE)
        self.refresh_buttons()

    def shutdown(self):
        """Stop the bot and fully close Zoom."""
        self._kill_bot()
        run_quiet(["taskkill", "/F", "/IM", "Zoom.exe"])
        self.log("=== bot stopped, Zoom closed ===", "sys")
        self.set_status("Stopped", IDLE)
        self.refresh_buttons()

    def show_today(self):
        def work():
            try:
                r = subprocess.run([PY, "-u", BOT, "--today"], cwd=BASE, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", creationflags=NOWIN,
                                   env=dict(os.environ, PYTHONIOENCODING="utf-8"))
                for ln in (r.stdout + r.stderr).splitlines():
                    self.q.put(("line", ln))
            except Exception as e:
                self.q.put(("line", f"ERROR: {e!r}"))
        threading.Thread(target=work, daemon=True).start()

    def on_close(self):
        if self.running():
            if not messagebox.askyesno("Zoom Bot", "The bot is running. Shut it down (the bot stops and Zoom closes) and exit?"):
                return
            self.shutdown()
        self.root.destroy()

    # ---------- bot output ----------
    def poll(self):
        items, exit_code, exited = [], None, False
        try:
            for _ in range(300):                               # drain a batch, then draw once
                kind, val = self.q.get_nowait()
                if kind == "line":
                    items.append(self.classify(val))
                else:
                    exited, exit_code = True, val
        except queue.Empty:
            pass
        self.log_many(items)
        if exited:
            if not self.stopping:
                self.log(f"=== bot exited (code {exit_code}) ===", "err" if exit_code else "sys")
                self.set_status("Stopped" if not exit_code else "Stopped (error, see log)", IDLE if not exit_code else ERR)
            self.refresh_buttons()
        self.root.after(100, self.poll)

    def classify(self, line):
        low = line.lower()
        tag = None
        if "<<<" in line or "[answer] answering" in low or "[answer] done" in low:
            tag = "match"
        elif "[hint]" in low:
            tag = "hint"
        elif "traceback" in low or "error" in low:
            tag = "err"

        if "[answer] error" in low:
            self.set_status("In class — answer failed, will retry", ERR)
        elif "[answer] answering" in low:
            self.set_status("Answering…", INFO)
        elif "listening stopped" in low:
            self.set_status("In class — answered, listening stopped", OK)
        elif "[sched]" in low or "class ended" in low:
            self.set_status("Running — waiting for classes", OK)
        elif "joining class" in low:
            self.set_status("In class — listening", INFO)
        return line, tag, not TS_RE.match(line)


def main():
    try:
        import ctypes
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(1)         # crisp text on high-DPI screens
    except Exception:
        pass
    root = tk.Tk()
    root.withdraw()
    App(root)
    dark_titlebar(root)
    root.deiconify()
    root.mainloop()


if __name__ == "__main__":
    main()
