#!/usr/bin/env python3
"""
Zoom Bot — TEST BENCH. A separate window only for testing; it does not follow the schedule.

The bot runs here in test mode (--console --unlimited): it answers EVERY time it hears the name
(the real panel answers only once per class), nothing is recorded.

  Link field + Join      -> open a Zoom link now (Leave closes it)
  Start / Stop listening -> listen to the speakers and answer on the name, without any class
  Answers ON / OFF       -> keep listening but do not answer
  Play answer to Zoom    -> unmute -> play an answer file into the virtual mic -> mute (like a real answer)
  Play on speakers       -> play an answer file so YOU hear it; while listening, the bot hears it too
                            and should answer — a quick end-to-end check without a teacher
"""
import queue
import re
import tkinter as tk
from datetime import datetime

from gui_common import (AMBER, BG, BLUE, BORDER, CARD, DIM, ERR, FG, FONT, GHOST, GREEN, IDLE, INFO, MUTED, OK,
                        PANEL, RED, TS_RE, WARN, BotProcess, HintEntry, IconButton, LogView, Tooltip, close_zoom,
                        dark_titlebar, enable_dpi, line_tag, looks_like_zoom_link, make_btn, setup_style)

PURPLE = ("#6e40c9", "#8957e5")
HM_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


class Tester:
    def __init__(self, root):
        self.root = root
        self.bot = BotProcess(queue.Queue())
        self.ready = False
        self.listening = False
        self.answers_on = True
        self.n_heard = 0
        self.n_answers = 0

        root.title("Zoom Bot — Test bench")
        root.configure(bg=BG)
        root.geometry("960x880")
        root.minsize(760, 720)
        setup_style(root)
        self.build_ui()
        root.protocol("WM_DELETE_WINDOW", self.quit)
        self.refresh()
        root.after(100, self.poll)
        root.after(300, self.start_bot)

    # ---------- UI ----------
    def card(self, title, note=None):
        f = tk.Frame(self.root, bg=CARD, padx=18, pady=12, highlightthickness=1, highlightbackground=BORDER)
        f.pack(fill="x", padx=20, pady=(12, 0))
        tk.Label(f, text=title, fg=FG, bg=CARD, font=(FONT, 10, "bold")).pack(anchor="w")
        if note:
            tk.Label(f, text=note, fg=MUTED, bg=CARD, font=(FONT, 9), wraplength=860, justify="left").pack(anchor="w")
        body = tk.Frame(f, bg=CARD)
        body.pack(fill="x", pady=(8, 0))
        return body

    def build_ui(self):
        root = self.root
        header = tk.Frame(root, bg=PANEL, padx=20, pady=14)
        header.pack(fill="x")
        header.columnconfigure(0, weight=1)
        left = tk.Frame(header, bg=PANEL)
        left.grid(row=0, column=0, sticky="w")
        tk.Label(left, text="TEST BENCH  ·  answers on every call, no schedule, no recordings",
                 fg=WARN, bg=PANEL, font=(FONT, 9, "bold")).pack(anchor="w")
        self.status = tk.Label(left, text="● Starting…", fg=INFO, bg=PANEL, font=(FONT, 16, "bold"))
        self.status.pack(anchor="w", pady=(2, 0))
        stats = tk.Frame(header, bg=PANEL)
        stats.grid(row=0, column=1, sticky="e")
        self.lbl_heard = self.stat(stats, "heard the name", 0)
        self.lbl_answers = self.stat(stats, "answers played", 1)

        # Zoom link
        body = self.card("Zoom", "Opens the link now, like the real bot does (the class lasts until the end time, "
                                 "empty = 95 min). Listening starts by itself after joining.")
        self.link = HintEntry(body, "https://...zoom.us/j/...")
        self.link.pack(side="left", fill="x", expand=True, ipady=7)
        self.until = HintEntry(body, "ends at HH:MM", width=13)
        self.until.pack(side="left", padx=8, ipady=7)
        self.btn_join = make_btn(body, "Join", self.join, BLUE, size=10, pady=7)
        self.btn_join.pack(side="left")
        self.btn_leave = make_btn(body, "Leave / close Zoom", self.leave, GHOST, size=10, pady=7)
        self.btn_leave.pack(side="left", padx=(8, 0))
        self.link.bind("<Return>", lambda e: self.join())

        # listening + answers
        body = self.card("Recognition", "While listening, every time the name is heard the bot answers "
                                        "(unmute → answer file → mute). Unlimited: it does not stop after the first answer.")
        self.btn_listen = make_btn(body, "", self.toggle_listen, GREEN, size=11)
        self.btn_listen.pack(side="left")
        self.btn_answers = make_btn(body, "", self.toggle_answers, GHOST, size=11)
        self.btn_answers.pack(side="left", padx=10)
        Tooltip(self.btn_listen, "Listen to what plays on the speakers (Zoom, a video, a recording)")
        Tooltip(self.btn_answers, "Off: still listens and shows matches, but does not answer")

        # sound
        body = self.card("Sound")
        self.btn_play = make_btn(body, "▶  Play answer to Zoom", self.play_zoom, PURPLE, size=11)
        self.btn_play.pack(side="left")
        self.btn_preview = make_btn(body, "🔊  Play on speakers", self.play_speakers, GHOST, size=11)
        self.btn_preview.pack(side="left", padx=10)
        Tooltip(self.btn_play, "Exactly what happens on a real answer: Alt+A (unmute) → answer file\n"
                               "into the virtual microphone → Alt+A (mute). Switch to Zoom to see it")
        Tooltip(self.btn_preview, "Plays an answer file on your speakers so you can hear it.\n"
                                  "If listening is on, the bot hears it too and should answer —\n"
                                  "a quick end-to-end test without a teacher")

        # controls
        bar = tk.Frame(root, bg=BG, padx=20, pady=12)
        bar.pack(fill="x")
        self.btn_restart = make_btn(bar, "Restart bot", self.restart, GHOST, size=10, pady=7)
        self.btn_restart.pack(side="left")
        IconButton(bar, "trash", lambda: self.logv.clear(), bg=BG).pack(side="left", padx=10)
        make_btn(bar, "Exit + close Zoom", self.quit_with_zoom, RED, size=10, pady=7).pack(side="right")
        make_btn(bar, "Exit", self.quit, GHOST, size=10, pady=7).pack(side="right", padx=10)

        self.logv = LogView(root)
        self.logv.pack(fill="both", expand=True, padx=20, pady=(0, 16))

    def stat(self, parent, label, col):
        f = tk.Frame(parent, bg=PANEL, padx=14)
        f.grid(row=0, column=col)
        v = tk.Label(f, text="0", fg=FG, bg=PANEL, font=(FONT, 22, "bold"))
        v.pack()
        tk.Label(f, text=label, fg=MUTED, bg=PANEL, font=(FONT, 9)).pack()
        return v

    def refresh(self):
        up = self.bot.running() and self.ready
        for b in (self.btn_join, self.btn_leave, self.btn_listen, self.btn_answers, self.btn_play, self.btn_preview):
            b.enable(up)
        if self.listening:
            self.btn_listen.config(text="■  Stop listening")
            self.btn_listen.recolor(AMBER)
        else:
            self.btn_listen.config(text="●  Start listening")
            self.btn_listen.recolor(GREEN)
        self.btn_answers.config(text="Answers: ON" if self.answers_on else "Answers: OFF")
        self.btn_answers.recolor(GHOST if self.answers_on else AMBER)
        self.lbl_heard.config(text=str(self.n_heard))
        self.lbl_answers.config(text=str(self.n_answers))
        if up:
            if self.listening:
                self.set_status("Listening" + ("" if self.answers_on else " — answers off"),
                                INFO if self.answers_on else WARN)
            else:
                self.set_status("Ready — not listening", OK)

    def set_status(self, text, color):
        self.status.config(text="● " + text, fg=color)

    # ---------- actions ----------
    def start_bot(self):
        self.ready, self.listening, self.answers_on = False, False, True
        self.logv.log(f"=== starting the test bot · {datetime.now():%H:%M:%S} ===", "sys", stamp=False)
        self.set_status("Starting… (loading the model)", INFO)
        if not self.bot.start(["--console", "--unlimited"]):
            self.set_status("Failed to start — see log", ERR)
        self.refresh()

    def restart(self):
        self.bot.kill()
        self.ready = False
        self.refresh()
        self.root.after(800, self.start_bot)

    def join(self):
        link, until = self.link.value(), self.until.value()
        if not looks_like_zoom_link(link):
            self.logv.log("Paste a Zoom link first (…zoom.us/j/…)", "err")
            return
        if until and not HM_RE.match(until):
            self.logv.log("End time must look like 10:30 (or leave it empty)", "err")
            return
        self.bot.send(f"join {link} {until}".strip())

    def leave(self):
        self.bot.send("leave")

    def toggle_listen(self):
        self.bot.send("disarm" if self.listening else "arm")

    def toggle_answers(self):
        self.bot.send("pause" if self.answers_on else "resume")

    def play_zoom(self):
        self.bot.send("answer")

    def play_speakers(self):
        self.bot.send("preview")

    def quit(self):
        self.bot.kill()
        self.root.destroy()

    def quit_with_zoom(self):
        self.bot.kill()
        close_zoom()
        self.root.destroy()

    # ---------- bot output ----------
    def poll(self):
        items = []
        for kind, val in self.bot.drain():
            if kind == "line":
                self.react(val)
                items.append((val, line_tag(val), not TS_RE.match(val)))
            elif not self.bot.stopping:
                self.ready = False
                items.append((f"=== test bot exited (code {val}) — press Restart bot ===", "err", False))
                self.set_status("Bot stopped (see log)", ERR)
        self.logv.log_many(items)
        if items:
            self.refresh()
        self.root.after(100, self.poll)

    def react(self, line):
        low = line.lower()
        if "[console] ready" in low:
            self.ready = True
        elif "[control] listening on" in low or "joining class" in low:
            self.listening = True
        elif "[control] listening off" in low or "class ended" in low:
            self.listening = False
        elif "[pause] answers paused" in low:
            self.answers_on = False
        elif "[pause] answers enabled" in low:
            self.answers_on = True
        if ("[answer] answering" in low and "(manual)" not in low) or "[pause] paused — not answering" in low:
            self.n_heard += 1                            # one per detected call (answered or skipped)
        if "[answer] done" in low:
            self.n_answers += 1


def main():
    enable_dpi()
    root = tk.Tk()
    root.withdraw()
    Tester(root)
    dark_titlebar(root)
    root.deiconify()
    root.mainloop()


if __name__ == "__main__":
    main()
