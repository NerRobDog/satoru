#!/usr/bin/env python3
"""Run the installed bundle entry, retaining output and exit status for diagnosis."""
import datetime
import os
from pathlib import Path
import subprocess

home = Path.home()
entry = home / "Applications/satoru/Magicka.app/Contents/MacOS/launch"
logs = home / "Library/Logs/satoru/magicka"
logs.mkdir(parents=True, exist_ok=True)
stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
log = logs / ("play-" + stamp + ".log")
print("Log: " + str(log), flush=True)
with log.open("w", buffering=1) as stream:
    stream.write("Started: " + datetime.datetime.now().isoformat() + "\n")
    proc = subprocess.Popen([str(entry)], cwd="/", stdout=stream,
                            stderr=subprocess.STDOUT, env=os.environ.copy())
    print("PID: " + str(proc.pid), flush=True)
    code = proc.wait()
    stream.write("\nExit code: %d\n" % code)
print("Exit code: %d; log: %s" % (code, log), flush=True)
