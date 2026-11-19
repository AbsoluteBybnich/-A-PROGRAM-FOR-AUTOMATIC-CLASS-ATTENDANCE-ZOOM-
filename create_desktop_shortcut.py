#!/usr/bin/env python3
"""
Creates two desktop shortcuts (they open the windows without a console):
  "Zoom Bot"       -> START.pyw (or bot_gui.pyw)  the control panel (follows the schedule)
  "Zoom Bot Test"  -> bot_tester.pyw              the test bench

Run it ONCE from PyCharm or a terminal, using the same Python as the bot (the one from .venv):

    python "attend by claude\\create_desktop_shortcut.py"
"""
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
PANEL = "START.pyw" if os.path.exists(os.path.join(BASE, "START.pyw")) else "bot_gui.pyw"
SHORTCUTS = [("Zoom Bot", PANEL), ("Zoom Bot Test", "bot_tester.pyw")]

exe = sys.executable
pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
if not os.path.exists(pythonw):
    pythonw = exe

for name, script in SHORTCUTS:
    gui = os.path.join(BASE, script)
    ps = (
        "$d = [Environment]::GetFolderPath('Desktop'); "
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d '{name}.lnk')); "
        f"$s.TargetPath = '{pythonw}'; "
        f"$s.Arguments = '\"{gui}\"'; "
        f"$s.WorkingDirectory = '{BASE}'; "
        "$s.Save(); "
        f"Write-Output (Join-Path $d '{name}.lnk')"
    )
    res = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True)
    if res.returncode == 0:
        print("Done. Shortcut created:", res.stdout.strip())
    else:
        print(f"Could not create '{name}':", res.stderr.strip() or res.stdout.strip())
        print(f"Fallback: right-click {script} -> Send to -> Desktop (create shortcut).")
