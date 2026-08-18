"""Per-module single-instance guard.

main.py's own mutex only protects against running main.py itself twice -
running a module directly (e.g. `python -m oriel.tiling`, exactly how
these daemons get manually restarted during development) bypasses that
entirely. Two instances of the same daemon double-hook the same WinEvents/
global input hooks and double-process everything - confirmed historically:
two hotkeyd instances both calling RegisterHotKey for the same combo
crashed with "Hot key is already registered" (see hotkeyd's own history;
the newer WH_KEYBOARD_LL version wouldn't even crash, it would just
silently double-fire every hotkey instead, which is worse).
"""
import logging

import win32api
import win32event
import winerror

logger = logging.getLogger(__name__)

MUTEX_PREFIX = "Global\\Oriel"

# Kept referenced for the process's lifetime - a mutex handle only holds
# the lock while at least one reference to it is alive; letting it get
# garbage-collected would silently release it.
_held_mutex = None


def ensure_single_instance(name):
    """Returns True if this process is the only one running `name` right
    now. Returns False (caller should exit immediately without starting
    anything) if another instance already holds the lock."""
    global _held_mutex
    mutex = win32event.CreateMutex(None, False, MUTEX_PREFIX + name.capitalize() + "Daemon")
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        logger.error("another %s instance is already running - exiting", name)
        return False
    _held_mutex = mutex
    return True
