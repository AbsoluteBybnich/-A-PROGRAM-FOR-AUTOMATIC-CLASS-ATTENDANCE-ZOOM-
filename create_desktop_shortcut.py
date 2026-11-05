#!/usr/bin/env python3
"""
Creates a "Zoom Bot" shortcut on the desktop (launches the window without a console).
Run it ONCE from PyCharm or a terminal, using the same Python as the bot (the one from .venv):

    python "attend by claude\\create_desktop_shortcut.py"
"""
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
GUI = os.path.join(BASE, "bot_gui.pyw")

exe = sys.executable
pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
if not os.path.exists(pythonw):
    pythonw = exe

ps = (
    "$d = [Environment]::GetFolderPath('Desktop'); "
    "$s = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'Zoom Bot.lnk')); "
    f"$s.TargetPath = '{pythonw}'; "
    f"$s.Arguments = '\"{GUI}\"'; "
    f"$s.WorkingDirectory = '{BASE}'; "
    "$s.Save(); "
    "Write-Output (Join-Path $d 'Zoom Bot.lnk')"
)

res = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True)
if res.returncode == 0:
    print("Done. Shortcut created:", res.stdout.strip())
else:
    print("Could not create the shortcut:", res.stderr.strip() or res.stdout.strip())
    print("Fallback: right-click bot_gui.pyw -> Send to -> Desktop (create shortcut).")
    print("  The window will find the project's .venv by itself and start the bot from it.")
