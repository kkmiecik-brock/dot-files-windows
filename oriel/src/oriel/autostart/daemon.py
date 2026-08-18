"""Launches apps configured in config.json's "autostart" section once,
then exits - unlike the other daemons, there's no ongoing state to watch
for here, so this doesn't run a persistent event loop.

Each entry is {"target": ..., "args": (optional), "cwd": (optional),
"delay_seconds": (optional, waited before launching that entry),
"workspace": (optional, tells tiling to land ONLY this freshly-launched
instance on that workspace instead of whichever is currently active - a
manual relaunch later, with no fresh registration, uses the normal
current-workspace behavior)}. os.startfile (ShellExecute) is used rather
than subprocess.Popen, since it correctly resolves MSIX App Execution
Alias stubs (e.g. ms-teams.exe under
%LOCALAPPDATA%\\Microsoft\\WindowsApps) - a CreateProcess-based launcher
cannot; same gotcha as hotkeyd's own "launch" action.
"""
import logging
import os
import threading
import time

from oriel.config import get_section
from oriel.ipc import send_action
from oriel.logging_setup import configure_logging
from oriel.single_instance import ensure_single_instance

logger = logging.getLogger(__name__)

# tiling might still be mid-startup (DPI setup + a full
# bootstrap_existing_windows() window scan) before its IPC listener comes
# up - send_action's own built-in retry only covers ~100ms, not reliably
# enough to beat that. Retrying the registration itself over a much longer
# window is simpler than coordinating daemon startup order, and harmless
# since re-registering the same pending entry is idempotent. Run on a
# background thread (not a daemon thread - the process should wait for it)
# so it doesn't delay launching whatever app comes next in the list.
REGISTER_RETRY_ATTEMPTS = 8
REGISTER_RETRY_DELAY_SECONDS = 0.5


def _register_workspace(target, workspace):
    process_name = os.path.basename(target).lower()
    for _ in range(REGISTER_RETRY_ATTEMPTS):
        send_action("tiling", "expect_autostart_window", {"process": process_name, "workspace": workspace})
        time.sleep(REGISTER_RETRY_DELAY_SECONDS)


def _launch(app):
    target = app["target"]
    try:
        os.startfile(target, arguments=app.get("args") or "", cwd=app.get("cwd") or None)
        logger.info("launched %s", target)
    except OSError:
        logger.exception("failed to launch %s", target)
        return

    workspace = app.get("workspace")
    if workspace is not None:
        threading.Thread(target=_register_workspace, args=(target, workspace)).start()


def run():
    configure_logging("autostart")
    if not ensure_single_instance("autostart"):
        return
    try:
        _run()
    except Exception:
        logger.exception("autostart daemon crashed")
        raise


def _run():
    for app in get_section("autostart").get("apps", []):
        delay = app.get("delay_seconds", 0)
        if delay:
            time.sleep(delay)
        _launch(app)
