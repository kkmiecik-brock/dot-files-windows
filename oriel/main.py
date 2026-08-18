"""oriel - convenience launcher for the desktop-management daemons.

Each daemon (hotkeyd, tiling, drag, taskbar) is an independent process
launched as `pythonw -m oriel.<name>`, with PYTHONPATH pointing at this
package's src/ directory so the modules are importable without installing
the package. Launch order doesn't matter - hotkeyd forwards actions to the
others over named pipD
Acquires a single-instance lock so running this twice (e.g. once from the
Startup folder, once manually) doesn't double-launch everything.

oriel.autostart isn't a persistent daemon like the others - it launches
config.json's "autostart" apps once and exits immediately.
"""
import os
import subprocess
import sys

import win32api
import win32event
import winerror

SINGLE_INSTANCE_MUTEX_NAME = "Global\\OrielDesktopDaemon"
PACKAGE_ROOT = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(PACKAGE_ROOT, "src")
MODULES = ["oriel.hotkeyd", "oriel.tiling", "oriel.drag", "oriel.taskbar", "oriel.autostart"]


def _acquire_single_instance_lock():
    mutex = win32event.CreateMutex(None, False, SINGLE_INSTANCE_MUTEX_NAME)
    
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        return None
    return mutex


def main():
    mutex = _acquire_single_instance_lock()
    if mutex is None:
        print("oriel is already running - exiting.")
        sys.exit(1)

    pythonw = sys.executable.replace("python.exe", "pythonw.exe")
    env = {**os.environ, "PYTHONPATH": SRC_DIR}
    for module in MODULES:
        subprocess.Popen(
            [pythonw, "-m", module], cwd=SRC_DIR, env=env,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )


if __name__ == "__main__":
    main()
