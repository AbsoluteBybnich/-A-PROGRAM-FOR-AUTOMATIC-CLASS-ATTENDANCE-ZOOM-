#!/usr/bin/env python3
"""
Zoom Bot control panel (dark theme). The bot starts by itself when the window opens and follows the schedule.

  Auto-attendance ON/OFF   -> the bot keeps running and joining classes, but does / does not answer the roll call
  Exit                     -> closes the window and stops the bot (Zoom stays as it is)
  Exit + close Zoom        -> the same, and Zoom is closed completely
  ☰ (top right)            -> menu: join a class by link, leave the class, today's classes, restart, clear log

Lives next to zoom_attendance_bot.py; shared widgets are in gui_common.py.
The desktop shortcut is created by create_desktop_shortcut.py (run it once).
"""
import json
import os
import queue
import re
import subprocess
import threading
import tkinter as tk
from datetime import date, datetime, timedelta
from tkinter import messagebox

from gui_common import (AMBER, BASE, BG, BLUE, BORDER, BOT, CARD, DIM, ERR, FG, FONT, GHOST, GREEN, IDLE, INFO,
                        MUTED, NOWIN, OK, PANEL, PY, RED, SCHEDULE, TS_RE, WARN, BotProcess, HintEntry, IconButton,
                        LogView, StatTile, Tooltip, close_zoom, dark_titlebar, enable_dpi, fmt_hours, line_tag,
                        looks_like_zoom_link, make_btn, read_stats, setup_style)

# A short wish shown under the status. One is picked per calendar day and changes at midnight.
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

HM_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


class App:
    def __init__(self, root):
        self.root = root
        self.bot = BotProcess(queue.Queue())
        self.auto_on = True
        self.menu_open = False
        self._week_cache = (None, "")
        self._wish_day = None

        root.title("Zoom Bot")
        root.configure(bg=BG)
        root.geometry("920x680")
        root.minsize(700, 500)
        setup_style(root)
        self.build_ui()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind("<Escape>", lambda e: self.menu_open and self.toggle_menu())

        self.tick()
        root.after(100, self.poll)
        root.after(300, self.start_bot)

    # ---------- UI ----------
    def build_ui(self):
        root = self.root

        # header: status + wish | clock | ☰
        header = tk.Frame(root, bg=PANEL, padx=20, pady=16)
        header.pack(fill="x")
        header.columnconfigure(0, weight=1)

        left = tk.Frame(header, bg=PANEL)
        left.grid(row=0, column=0, sticky="w")
        tk.Label(left, text="ZOOM BOT", fg=DIM, bg=PANEL, font=(FONT, 9, "bold")).pack(anchor="w")
        self.status = tk.Label(left, text="● Starting…", fg=INFO, bg=PANEL, font=(FONT, 16, "bold"))
        self.status.pack(anchor="w", pady=(2, 0))
        self.wish = tk.Label(left, text="", fg="#9aa4b2", bg=PANEL, font=(FONT, 10, "italic"),
                             wraplength=480, justify="left")
        self.wish.pack(anchor="w", pady=(4, 0))

        right = tk.Frame(header, bg=PANEL)
        right.grid(row=0, column=1, sticky="ne", padx=(16, 14))
        self.clock = tk.Label(right, text="--:--:--", fg=FG, bg=PANEL, font=(FONT, 24, "bold"))
        self.clock.pack(anchor="e")
        self.date = tk.Label(right, text="", fg=MUTED, bg=PANEL, font=(FONT, 10))
        self.date.pack(anchor="e")

        self.btn_menu = IconButton(header, "menu", self.toggle_menu, bg=PANEL)
        self.btn_menu.grid(row=0, column=2, sticky="ne")
        Tooltip(self.btn_menu, "Menu: join a class by link, leave the class,\ntoday's classes, restart the bot, clear the log")

        # ☰ menu: hidden card under the header
        self.menu = tk.Frame(root, bg=CARD, padx=20, pady=14, highlightthickness=1, highlightbackground=BORDER)
        tk.Label(self.menu, text="Join a class by link", fg=FG, bg=CARD, font=(FONT, 11, "bold")).pack(anchor="w")
        tk.Label(self.menu, text="The bot opens the link now and listens until the end time "
                                 "(empty = 95 minutes). A scheduled class in progress is left for it.",
                 fg=MUTED, bg=CARD, font=(FONT, 9), wraplength=820, justify="left").pack(anchor="w", pady=(2, 8))
        row = tk.Frame(self.menu, bg=CARD)
        row.pack(fill="x")
        self.link = HintEntry(row, "https://...zoom.us/j/...")
        self.link.pack(side="left", fill="x", expand=True, ipady=5)
        self.until = HintEntry(row, "ends at HH:MM", width=13)
        self.until.pack(side="left", padx=8, ipady=5)
        self.btn_join = make_btn(row, "Join now", self.join_link, BLUE, size=9, padx=12, pady=6)
        self.btn_join.pack(side="left")
        self.link.bind("<Return>", lambda e: self.join_link())
        self.until.bind("<Return>", lambda e: self.join_link())

        tools = tk.Frame(self.menu, bg=CARD)
        tools.pack(fill="x", pady=(12, 0))
        b_leave = make_btn(tools, "Leave current class", self.leave_class, GHOST, size=9, pady=6, padx=12)
        b_leave.pack(side="left")
        b_today = make_btn(tools, "Today's classes", self.show_today, GHOST, size=9, pady=6, padx=12)
        b_today.pack(side="left", padx=8)
        b_restart = make_btn(tools, "Restart bot", self.restart_bot, GHOST, size=9, pady=6, padx=12)
        b_restart.pack(side="left")
        b_clear = make_btn(tools, "Clear log", lambda: self.logv.clear(), GHOST, size=9, pady=6, padx=12)
        b_clear.pack(side="left", padx=8)
        Tooltip(b_leave, "Leave the class the bot is in now (Zoom is closed)")
        Tooltip(b_today, "Show the current week and today's / tomorrow's classes in the log")
        Tooltip(b_restart, "Stop and start the bot again (e.g. after an error)")

        # main actions
        self.actions = tk.Frame(root, bg=BG, padx=20, pady=12)
        self.actions.pack(fill="x")
        self.btn_auto = make_btn(self.actions, "", self.toggle_auto, GREEN, size=10, padx=14, pady=7)
        self.btn_auto.pack(side="left")
        self.btn_exit_zoom = make_btn(self.actions, "✖  Exit + close Zoom", self.exit_with_zoom, RED,
                                      size=10, padx=12, pady=7)
        self.btn_exit_zoom.pack(side="right")
        self.btn_exit = make_btn(self.actions, "Exit", self.exit_app, GHOST, size=10, padx=12, pady=7)
        self.btn_exit.pack(side="right", padx=8)
        Tooltip(self.btn_auto, "On: the bot answers the roll call by itself.\n"
                               "Off: it still joins classes but stays silent. Hotkey: Ctrl+Alt+P")
        Tooltip(self.btn_exit, "Stop the bot and close this window. Zoom stays as it is")
        Tooltip(self.btn_exit_zoom, "Stop the bot, close this window and close Zoom completely")
        self.show_auto()

        # statistics (logs/stats.json, written by the bot)
        stats = tk.Frame(root, bg=BG, padx=20)
        stats.pack(fill="x", pady=(0, 12))
        self.st_hours = StatTile(stats, "in Zoom with the bot", "#79c0ff")
        self.st_answers = StatTile(stats, "auto-answers", "#ffe58a")
        self.st_classes = StatTile(stats, "classes attended", "#56d364")
        self.st_skipped = StatTile(stats, "calls skipped (auto-attendance off)", MUTED)
        for i, t in enumerate((self.st_hours, self.st_answers, self.st_classes, self.st_skipped)):
            t.pack(side="left", padx=(0 if i == 0 else 8, 0))
        self.st_since = tk.Label(stats, text="", fg=DIM, bg=BG, font=(FONT, 9), justify="right")
        self.st_since.pack(side="right", anchor="s")
        Tooltip(self.st_hours, "Time from opening a class link to the end of the class.\n"
                               "The test bench does not count")
        Tooltip(self.st_answers, "How many times the bot answered the roll call by itself")
        self.refresh_stats()

        # log
        self.logv = LogView(root)
        self.logv.pack(fill="both", expand=True, padx=20, pady=(0, 16))

    def show_auto(self):
        if self.auto_on:
            self.btn_auto.config(text="●  Auto-attendance: ON   —   turn off")
            self.btn_auto.recolor(GREEN)
        else:
            self.btn_auto.config(text="○  Auto-attendance: OFF   —   turn on")
            self.btn_auto.recolor(AMBER)

    def refresh_stats(self):
        s = read_stats()
        self.st_hours.set(fmt_hours(s.get("zoom_seconds", 0)))
        self.st_answers.set(str(s.get("auto_answers", 0)))
        self.st_classes.set(str(s.get("classes", 0)))
        self.st_skipped.set(str(s.get("skipped_calls", 0)))
        lines = []
        if s.get("since"):
            lines.append(f"counting since {s['since']}")
        if s.get("last_answer"):
            lines.append(f"last answer {s['last_answer']}")
        self.st_since.config(text="\n".join(lines) or "no classes counted yet")
        self.root.after(5000, self.refresh_stats)

    def toggle_menu(self):
        self.menu_open = not self.menu_open
        if self.menu_open:
            self.menu.pack(fill="x", padx=20, pady=(14, 0), before=self.actions)
            self.link.focus_set()
        else:
            self.menu.pack_forget()
        self.btn_menu.set_active(self.menu_open)

    def set_status(self, text, color):
        self.status.config(text="● " + text, fg=color)

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

    # ---------- actions ----------
    def start_bot(self):
        self.logv.log(f"=== starting the bot · {datetime.now():%a %d %b %Y %H:%M:%S} ===", "sys", stamp=False)
        self.set_status("Starting… (loading the model)", INFO)
        self.auto_on = True                                    # a new bot process starts with answers on
        self.show_auto()
        if not self.bot.start():
            self.set_status("Failed to start — see log", ERR)

    def restart_bot(self):
        if self.bot.kill():
            self.logv.log("=== bot stopped for restart ===", "sys")
        self.root.after(800, self.start_bot)

    def toggle_auto(self):
        if not self.bot.running():
            self.logv.log("The bot is not running — use ☰ → Restart bot", "err")
            return
        self.auto_on = not self.auto_on                        # optimistic; the bot confirms with a log line
        self.show_auto()
        self.bot.send("resume" if self.auto_on else "pause")

    def join_link(self):
        link, until = self.link.value(), self.until.value()
        if not looks_like_zoom_link(link):
            self.logv.log("Paste a Zoom link first (…zoom.us/j/…)", "err")
            self.link.focus_set()
            return
        if until and not HM_RE.match(until):
            self.logv.log("End time must look like 10:30 (or leave it empty)", "err")
            self.until.focus_set()
            return
        if not self.bot.running():
            self.logv.log("The bot is not running — use ☰ → Restart bot", "err")
            return
        self.bot.send(f"join {link} {until}".strip())
        self.link.clear()
        self.until.clear()
        self.toggle_menu()

    def leave_class(self):
        if self.bot.running():
            self.bot.send("leave")
        else:
            close_zoom()
            self.logv.log("Zoom closed", "sys")

    def show_today(self):
        def work():
            try:
                r = subprocess.run([PY, "-u", BOT, "--today"], cwd=BASE, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", creationflags=NOWIN,
                                   env=dict(os.environ, PYTHONIOENCODING="utf-8"))
                for ln in (r.stdout + r.stderr).splitlines():
                    self.bot.q.put(("line", ln))
            except Exception as e:
                self.bot.q.put(("line", f"ERROR: {e!r}"))
        threading.Thread(target=work, daemon=True).start()

    def exit_app(self):
        self.bot.kill()
        self.root.destroy()

    def exit_with_zoom(self):
        self.bot.kill()
        close_zoom()
        self.root.destroy()

    def on_close(self):
        if self.bot.running() and not messagebox.askyesno(
                "Zoom Bot", "Close the window? The bot will stop and will not join or answer classes.\n"
                            "(Zoom stays as it is.)"):
            return
        self.exit_app()

    # ---------- bot output ----------
    def poll(self):
        items = []
        for kind, val in self.bot.drain():
            if kind == "line":
                self.react(val)
                items.append((val, line_tag(val), not TS_RE.match(val)))
            elif not self.bot.stopping:
                items.append((f"=== bot exited (code {val}) — ☰ → Restart bot ===", "err" if val else "sys", False))
                self.set_status("Bot stopped (see log)" if val else "Bot stopped", ERR if val else IDLE)
        self.logv.log_many(items)
        self.root.after(100, self.poll)

    def react(self, line):
        low = line.lower()
        if "[pause] answers paused" in low:
            self.auto_on = False
            self.show_auto()
        elif "[pause] answers enabled" in low:
            self.auto_on = True
            self.show_auto()
        if "[answer] error" in low:
            self.set_status("In class — answer failed, will retry", ERR)
        elif "[answer] answering" in low:
            self.set_status("Answering…", INFO)
        elif "listening stopped" in low:
            self.set_status("In class — answered, listening stopped", OK)
        elif "[sched]" in low or "class ended" in low:
            self.set_status("Waiting for classes", OK)
        elif "joining class" in low:
            self.set_status("In class — listening", INFO)
        elif "[pause] answers paused" in low:
            self.set_status("Auto-attendance is off", WARN)


def main():
    enable_dpi()
    root = tk.Tk()
    root.withdraw()
    App(root)
    dark_titlebar(root)
    root.deiconify()
    root.mainloop()


if __name__ == "__main__":
    main()
