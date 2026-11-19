#!/usr/bin/env python3
"""
Zoom Bot control panel (dark theme). The bot starts by itself when the window opens and follows the schedule.

  Auto-attendance ON/OFF   -> the bot keeps running and joining classes, but does / does not answer the roll call
  Exit                     -> closes the window and stops the bot (Zoom stays as it is)
  Exit + close Zoom        -> the same, and Zoom is closed completely
  Clear (above the log)    -> clears the log
  ☰ (top right)            -> a panel slides in from the right with sections:
                              Join by link · Today's classes · Stats · Restart bot

Lives next to zoom_attendance_bot.py; shared widgets are in gui_common.py.
The desktop shortcut is created by create_desktop_shortcut.py (run it once).
"""
import queue
import re
import tkinter as tk
from datetime import datetime, timedelta
from tkinter import messagebox

from gui_common import (AMBER, BG, BLUE, BORDER, CARD, DIM, ERR, FG, FONT, GHOST, GREEN, IDLE, INFO, LOG_BG,
                        MUTED, OK, PANEL, RED, TS_RE, WARN, BotProcess, HintEntry, IconButton, LogView, StatTile,
                        Tooltip, close_zoom, dark_titlebar, enable_dpi, fmt_hours, line_tag, looks_like_zoom_link,
                        make_btn, read_stats, schedule_for, setup_style)

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
DRAWER_W = 560            # width of the slide-in panel (at 100 % Windows scaling)
NAV_BG, NAV_SEL = "#1b1d22", "#2b2f37"
SECTIONS = [("link", "Join by link"), ("today", "Today's classes"), ("stats", "Stats"), ("restart", "Restart bot")]


class Drawer(tk.Frame):
    """Panel that slides in from the right edge: section list on the left, the chosen section on the right."""

    def __init__(self, app):
        root = app.root
        super().__init__(root, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        self.app = app
        self.k = root.winfo_fpixels("1i") / 96.0
        self.w = int(DRAWER_W * self.k)
        self.is_open = False
        self.x = None
        self.anim = None
        self.current = None

        top = tk.Frame(self, bg=CARD, padx=16, pady=12)
        top.pack(fill="x")
        tk.Label(top, text="Menu", fg=FG, bg=CARD, font=(FONT, 13, "bold")).pack(side="left")
        IconButton(top, "close", self.close, size=30, bg=CARD).pack(side="right")
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        body = tk.Frame(self, bg=CARD)
        body.pack(fill="both", expand=True)
        self.nav = tk.Frame(body, bg=NAV_BG, width=int(170 * self.k))
        self.nav.pack(side="left", fill="y")
        self.nav.pack_propagate(False)
        self.page_host = tk.Frame(body, bg=CARD, padx=18, pady=14)
        self.page_host.pack(side="left", fill="both", expand=True)

        self.items, self.pages = {}, {}
        for key, title in SECTIONS:
            it = tk.Label(self.nav, text="   " + title, anchor="w", fg=MUTED, bg=NAV_BG, font=(FONT, 10),
                          padx=8, pady=11, cursor="hand2")
            it.pack(fill="x")
            it.bind("<Button-1>", lambda e, k=key: self.show(k))
            it.bind("<Enter>", lambda e, k=key: self.items[k]["bg"] != NAV_SEL and self.items[k].config(fg=FG))
            it.bind("<Leave>", lambda e, k=key: self.items[k]["bg"] != NAV_SEL and self.items[k].config(fg=MUTED))
            self.items[key] = it
        self.pages = {"link": self.page_link(), "today": self.page_today(),
                      "stats": self.page_stats(), "restart": self.page_restart()}
        root.bind("<Configure>", self._on_resize, add="+")

    # ----- open / close (slide animation) -----
    def _geom(self):
        top = self.app.header.winfo_height()
        return self.app.root.winfo_width(), self.app.root.winfo_height() - top, top

    def _place(self):
        rw, h, top = self._geom()
        self.place(x=self.x, y=top, width=self.w, height=h)
        self.lift()

    def _on_resize(self, e):
        if e.widget is self.app.root and self.is_open and self.anim is None:
            self.x = self.app.root.winfo_width() - self.w
            self._place()

    def _slide(self, target, then=None):
        if self.anim is not None:
            self.after_cancel(self.anim)
        step = (target - self.x) * 0.35
        if abs(target - self.x) <= 2:
            self.x = target
            self._place()
            self.anim = None
            if then:
                then()
            return
        self.x += int(step) if abs(step) >= 1 else (1 if target > self.x else -1)
        self._place()
        self.anim = self.after(12, lambda: self._slide(target, then))

    def open(self, section=None):
        rw = self.app.root.winfo_width()
        if not self.is_open:
            self.is_open = True
            self.x = rw
            self._place()
            self._slide(rw - self.w)
        self.show(section or self.current or "link")
        self.app.btn_menu.set_active(True)

    def close(self):
        if not self.is_open:
            return
        self.is_open = False
        self.app.btn_menu.set_active(False)
        self._slide(self.app.root.winfo_width(), then=self.place_forget)

    def toggle(self):
        self.close() if self.is_open else self.open()

    def show(self, key):
        self.current = key
        for k, it in self.items.items():
            sel = k == key
            it.config(bg=NAV_SEL if sel else NAV_BG, fg=FG if sel else MUTED,
                      font=(FONT, 10, "bold" if sel else "normal"))
        for k, p in self.pages.items():
            p.pack_forget()
        self.pages[key].pack(fill="both", expand=True)
        if key == "today":
            self.fill_today()
        elif key == "stats":
            self.fill_stats()
        elif key == "link":
            self.app.link.focus_set()

    # ----- sections -----
    def _page(self, title, note=None):
        p = tk.Frame(self.page_host, bg=CARD)
        tk.Label(p, text=title, fg=FG, bg=CARD, font=(FONT, 12, "bold")).pack(anchor="w")
        if note:
            tk.Label(p, text=note, fg=MUTED, bg=CARD, font=(FONT, 9), wraplength=int(340 * self.k),
                     justify="left").pack(anchor="w", pady=(3, 0))
        return p

    def page_link(self):
        app = self.app
        p = self._page("Join by link", "The bot opens the link now, answers the roll call and stays until the end "
                                       "time (empty = 95 minutes).")
        app.link = HintEntry(p, "https://...zoom.us/j/...")
        app.link.pack(fill="x", pady=(14, 8), ipady=5)
        row = tk.Frame(p, bg=CARD)
        row.pack(fill="x")
        tk.Label(row, text="ends at", fg=MUTED, bg=CARD, font=(FONT, 9)).pack(side="left")
        app.until = HintEntry(row, "HH:MM", width=7)
        app.until.pack(side="left", padx=8, ipady=5)
        make_btn(row, "Join now", app.join_link, BLUE, size=9, padx=12, pady=6).pack(side="right")
        app.link.bind("<Return>", lambda e: app.join_link())
        app.until.bind("<Return>", lambda e: app.join_link())
        tk.Frame(p, bg=BORDER, height=1).pack(fill="x", pady=16)
        tk.Label(p, text="Already in a class?", fg=FG, bg=CARD, font=(FONT, 10, "bold")).pack(anchor="w")
        tk.Label(p, text="Leave it now: the bot stops listening and closes Zoom.", fg=MUTED, bg=CARD,
                 font=(FONT, 9)).pack(anchor="w", pady=(2, 8))
        make_btn(p, "Leave current class", app.leave_class, GHOST, size=9, padx=12, pady=6).pack(anchor="w")
        return p

    def page_today(self):
        p = self._page("Today's classes")
        self.today_box = tk.Frame(p, bg=CARD)
        self.today_box.pack(fill="both", expand=True, pady=(10, 0))
        return p

    def fill_today(self):
        for w in self.today_box.winfo_children():
            w.destroy()
        now = datetime.now()
        next_marked = False                                    # only the nearest upcoming class gets "next"
        for offset in (0, 1):
            d = now.date() + timedelta(days=offset)
            try:
                wk, lessons = schedule_for(d)
            except Exception as e:
                tk.Label(self.today_box, text=f"Could not read schedule.json: {e}", fg=ERR, bg=CARD,
                         font=(FONT, 9), wraplength=int(340 * self.k), justify="left").pack(anchor="w")
                return
            head = ("Today" if offset == 0 else "Tomorrow") + f"  ·  {d:%a %d.%m}  ·  week {wk}"
            tk.Label(self.today_box, text=head, fg=INFO, bg=CARD, font=(FONT, 9, "bold")).pack(
                anchor="w", pady=(0 if offset == 0 else 14, 4))
            if not lessons:
                tk.Label(self.today_box, text="no classes", fg=DIM, bg=CARD, font=(FONT, 9)).pack(anchor="w")
            for l in lessons:
                start = datetime.combine(d, datetime.strptime(l["start"], "%H:%M").time())
                end = datetime.combine(d, datetime.strptime(l["end"], "%H:%M").time())
                skip = not l.get("enabled", True)
                tag, color = "", FG
                if skip:
                    tag, color = "skip", DIM
                elif start <= now < end:
                    tag, color = "now", OK
                elif end <= now:
                    color = DIM
                elif not next_marked:
                    tag, color, next_marked = "next", "#79c0ff", True
                row = tk.Frame(self.today_box, bg=LOG_BG, padx=10, pady=6)
                row.pack(fill="x", pady=2)
                tk.Label(row, text=f"{l['start']}–{l['end']}", fg=color, bg=LOG_BG, font=("Consolas", 9),
                         width=11, anchor="w").pack(side="left")
                tk.Label(row, text=l.get("title", ""), fg=color, bg=LOG_BG, font=(FONT, 9), anchor="w",
                         wraplength=int(250 * self.k), justify="left").pack(side="left", fill="x", expand=True)
                if tag:
                    tk.Label(row, text=tag, fg=color, bg=LOG_BG, font=(FONT, 8, "bold")).pack(side="right")

    def page_stats(self):
        p = self._page("Stats", "Counted only in real classes (the test bench does not count).")
        grid = tk.Frame(p, bg=CARD)
        grid.pack(fill="x", pady=(14, 0))
        self.st_hours = StatTile(grid, "hours in Zoom with the bot", "#79c0ff", bg=LOG_BG, size=18)
        self.st_answers = StatTile(grid, "auto-answers", "#ffe58a", bg=LOG_BG, size=18)
        self.st_classes = StatTile(grid, "classes attended", "#56d364", bg=LOG_BG, size=18)
        self.st_hours.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self.st_answers.grid(row=1, column=0, sticky="ew", padx=(0, 4))
        self.st_classes.grid(row=1, column=1, sticky="ew", padx=(4, 0))
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)
        self.st_more = tk.Frame(p, bg=CARD)
        self.st_more.pack(anchor="w", pady=(12, 0))
        return p

    def fill_stats(self):
        s = read_stats()
        self.st_hours.set(fmt_hours(s.get("zoom_seconds", 0)))
        self.st_answers.set(str(s.get("auto_answers", 0)))
        self.st_classes.set(str(s.get("classes", 0)))
        for w in self.st_more.winfo_children():
            w.destroy()
        rows = [(cap, s.get(key)) for cap, key in
                (("counting since", "since"), ("last class", "last_class"), ("last answer", "last_answer")) if s.get(key)]
        if not rows:
            rows = [("", "Nothing counted yet — it starts with the next class.")]
        for i, (cap, val) in enumerate(rows):
            tk.Label(self.st_more, text=cap, fg=DIM, bg=CARD, font=(FONT, 9)).grid(row=i, column=0, sticky="w")
            tk.Label(self.st_more, text=val, fg=MUTED, bg=CARD, font=(FONT, 9)).grid(row=i, column=1, sticky="w",
                                                                                   padx=(12, 0))
        if self.is_open and self.current == "stats":
            self.after(5000, self.fill_stats)

    def page_restart(self):
        p = self._page("Restart bot", "Stops the bot and starts it again: use it after an error, or after you "
                                      "edited the settings in zoom_attendance_bot.py. Takes about 10 seconds "
                                      "(the model is loaded again). Zoom is not touched.")
        self.restart_state = tk.Label(p, text="", fg=MUTED, bg=CARD, font=(FONT, 9))
        make_btn(p, "↻  Restart bot", self.do_restart, AMBER, size=10, padx=14, pady=7).pack(anchor="w", pady=(14, 6))
        self.restart_state.pack(anchor="w")
        return p

    def do_restart(self):
        self.app.restart_bot()
        self.restart_state.config(text=f"restarted at {datetime.now():%H:%M:%S}")


class App:
    def __init__(self, root):
        self.root = root
        self.bot = BotProcess(queue.Queue())
        self.auto_on = True
        self._week_cache = (None, "")
        self._wish_day = None

        root.title("Zoom Bot")
        root.configure(bg=BG)
        root.geometry("920x660")
        root.minsize(760, 520)
        setup_style(root)
        self.build_ui()
        self.drawer = Drawer(self)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind("<Escape>", lambda e: self.drawer.close())

        self.tick()
        root.after(100, self.poll)
        root.after(300, self.start_bot)

    # ---------- UI ----------
    def build_ui(self):
        root = self.root

        # header: status + wish | clock | ☰
        self.header = header = tk.Frame(root, bg=PANEL, padx=20, pady=16)
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
        right.grid(row=0, column=1, sticky="se", padx=(16, 16))   # bottom-aligned: date on the wish line
        self.clock = tk.Label(right, text="--:--:--", fg=FG, bg=PANEL, font=(FONT, 24, "bold"))
        self.clock.pack(anchor="e")
        self.date = tk.Label(right, text="", fg=MUTED, bg=PANEL, font=(FONT, 10))
        self.date.pack(anchor="e")

        self.btn_menu = IconButton(header, "menu", lambda: self.drawer.toggle(), bg=PANEL)
        self.btn_menu.grid(row=0, column=2, sticky="e")             # vertically centred
        Tooltip(self.btn_menu, "Menu: join by link, today's classes, stats, restart the bot")

        # main actions
        actions = tk.Frame(root, bg=BG, padx=20, pady=12)
        actions.pack(fill="x")
        self.btn_auto = make_btn(actions, "", self.toggle_auto, GREEN, size=10, padx=14, pady=7)
        self.btn_auto.pack(side="left")
        btn_exit_zoom = make_btn(actions, "✖  Exit + close Zoom", self.exit_with_zoom, RED, size=10, padx=12, pady=7)
        btn_exit_zoom.pack(side="right")
        btn_exit = make_btn(actions, "Exit", self.exit_app, GHOST, size=10, padx=12, pady=7)
        btn_exit.pack(side="right", padx=8)
        Tooltip(self.btn_auto, "On: the bot answers the roll call by itself.\n"
                               "Off: it still joins classes but stays silent. Hotkey: Ctrl+Alt+P")
        Tooltip(btn_exit, "Stop the bot and close this window. Zoom stays as it is")
        Tooltip(btn_exit_zoom, "Stop the bot, close this window and close Zoom completely")
        self.show_auto()

        # log with its own small header (Clear is always visible here)
        bar = tk.Frame(root, bg=BG, padx=20)
        bar.pack(fill="x", pady=(0, 6))
        tk.Label(bar, text="LOG", fg=DIM, bg=BG, font=(FONT, 9, "bold")).pack(side="left", anchor="s")
        btn_clear = make_btn(bar, "🗑  Clear", lambda: self.logv.clear(), GHOST, size=9, padx=10, pady=3, bold=False)
        btn_clear.pack(side="right")
        Tooltip(btn_clear, "Clear the log in this window (the log files in logs/ stay)")
        self.logv = LogView(root)
        self.logv.pack(fill="both", expand=True, padx=20, pady=(0, 16))

    def show_auto(self):
        if self.auto_on:
            self.btn_auto.config(text="●  Auto-attendance: ON   —   turn off")
            self.btn_auto.recolor(GREEN)
        else:
            self.btn_auto.config(text="○  Auto-attendance: OFF   —   turn on")
            self.btn_auto.recolor(AMBER)

    def set_status(self, text, color):
        self.status.config(text="● " + text, fg=color)

    # ---------- clock ----------
    def week_label(self, now):
        today = now.date()
        if self._week_cache[0] != today:
            try:
                label = f"Week {schedule_for(today)[0]}"
            except Exception:
                label = ""
            self._week_cache = (today, label)
        return self._week_cache[1]

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
            self.logv.log("The bot is not running — ☰ → Restart bot", "err")
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
            self.logv.log("The bot is not running — ☰ → Restart bot", "err")
            return
        self.bot.send(f"join {link} {until}".strip())
        self.link.clear()
        self.until.clear()
        self.drawer.close()

    def leave_class(self):
        if self.bot.running():
            self.bot.send("leave")
        else:
            close_zoom()
            self.logv.log("Zoom closed", "sys")
        self.drawer.close()

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
