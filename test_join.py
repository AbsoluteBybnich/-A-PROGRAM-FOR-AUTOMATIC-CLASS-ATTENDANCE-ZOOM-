#!/usr/bin/env python3
"""
Test launcher WITHOUT a schedule: joins one conference right away
and works like the main bot (listens -> hears your surname -> answers), except that it keeps
listening and answers EVERY time your name is called (it does not stop after the first answer).

Put it next to zoom_attendance_bot.py and answer.wav.

  python test_join.py                    # join the first link from schedule.json + listen + answer the roll call
  python test_join.py "ZOOM_LINK"        # same, but with the link you pass
  python test_join.py --join-only        # only join and click Join (checks the join step)

Hotkeys:
  Ctrl+Alt+P — pause/resume answering
  Ctrl+Alt+T — manual test: unmute -> play the file -> mute
               (fires after ~1 second so you have time to release the keys)
"""
import sys
import threading
import time
from datetime import datetime, timedelta

import zoom_attendance_bot as bot

LISTEN_MINUTES = 90          # how long to listen after joining
UNLIMITED_ANSWERS = True     # True = answer every time your name is called; False = stop after the first answer (like the main bot)
COOLDOWN_SEC = 20            # minimum pause between two answers (stops a double answer to one call)


def pick_link():
    """The link from the command line, otherwise the first Zoom link in schedule.json."""
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if args:
        return args[0]
    try:
        for les in bot.load_schedule()["lessons"]:
            if str(les.get("link", "")).startswith("http"):
                return les["link"]
    except Exception:
        pass
    raise SystemExit('No link: run  python test_join.py "https://...zoom.us/j/..."  or fill in schedule.json')


def main():
    join_only = "--join-only" in sys.argv
    model = responder = listener = None

    if bot.LOW_PRIORITY:
        bot.lower_priority()

    if not join_only:
        model = bot.load_model()          # load in advance so listening starts right after joining
        try:
            responder = bot.Responder()
        except Exception as e:
            print(f"[!] could not open answer.wav / the virtual microphone: {e!r}")
            print("    I will only listen and print, without answering (check VB-Cable and answer.wav).")

    bot.register_hotkey(bot.PAUSE_HOTKEY, bot.toggle_pause)

    def manual_answer():
        time.sleep(1.0)                   # give the user time to release Ctrl+Alt
        print("[test] manual answer")
        try:
            responder.answer()
        except Exception as e:
            print(f"[answer] ERROR: {e!r}")
            hint = bot.audio_error_hint(e)
            if hint:
                print(hint)

    if responder:
        bot.register_hotkey("ctrl+alt+t", lambda: threading.Thread(target=manual_answer, daemon=True).start())

    link = pick_link().strip().rstrip(".,;)")
    uri = None if bot.OPEN_LINK_DIRECTLY else bot.to_zoom_uri(link)
    print(f"[join] opening: {uri or link}")
    bot.open_target(uri or link)
    time.sleep(bot.JOIN_WAIT_SEC)
    bot.click_join()
    time.sleep(bot.AFTER_JOIN_SEC)

    if join_only:
        print("[join] done. Check the Zoom window: are you in, and is the microphone muted?")
        return

    bot.STOP_AFTER_ANSWER = not UNLIMITED_ANSWERS      # keep listening after an answer
    bot.COOLDOWN_SEC = COOLDOWN_SEC
    listener = bot.Listener(model, responder)
    listener.start()
    listener.arm()
    end = datetime.now() + timedelta(minutes=LISTEN_MINUTES)
    mode = f"answers: unlimited, {COOLDOWN_SEC} s between answers" if UNLIMITED_ANSWERS else "answers: once"
    print(f"[run] listening until {end:%H:%M} ({mode}). Ctrl+C — quit, Ctrl+Alt+P — pause, Ctrl+Alt+T — test answer.")
    try:
        while datetime.now() < end:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nexit")
    finally:
        listener.close()


if __name__ == "__main__":
    main()
