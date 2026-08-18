"""Taskbar auto-hide, config-driven on/off - or manually toggled via a
hotkey (see config.json's "toggle" binding targeting "taskbar").

Polls config.json's taskbar.enabled flag so it can be toggled live by
editing the config file - no restart needed. When switched off, restores
the taskbar's visibility instead of just stopping further hides. Whenever
the taskbar's actual visibility changes (either way), tells the tiling
daemon to reflow, since that changes how much work area it has to fill.
"""
import threading
import time
import logging

import win32con
import win32gui

from oriel.config import get_section
from oriel.ipc import send_action, serve_actions
from oriel.logging_setup import configure_logging
from oriel.single_instance import ensure_single_instance

logger = logging.getLogger(__name__)

TASKBAR_CLASSES = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd"}

DEFAULTS = {"enabled": True, "poll_interval": 1.0}

# None = follow config.json's "enabled"; True/False = hotkey-forced
# override that takes precedence until toggled again. _applied_hidden is
# the last state actually applied, shared between the poll loop and the
# IPC thread's toggle() so neither undoes the other's most recent change.
_override = None
_applied_hidden = None


def _load_settings():
    return {**DEFAULTS, **get_section("taskbar")}


def _get_taskbar_windows():
    handles = []

    def callback(hwnd, _):
        try:
            class_name = win32gui.GetClassName(hwnd)
        except win32gui.error:
            return True
        if class_name in TASKBAR_CLASSES:
            handles.append(hwnd)
        return True

    win32gui.EnumWindows(callback, None)
    return handles


def _set_taskbar_visible(visible):
    show_state = win32con.SW_SHOW if visible else win32con.SW_HIDE
    for hwnd in _get_taskbar_windows():
        if win32gui.IsWindow(hwnd):
            win32gui.ShowWindow(hwnd, show_state)


def _apply(should_hide):
    global _applied_hidden
    if should_hide == _applied_hidden:
        return
    _set_taskbar_visible(not should_hide)
    _applied_hidden = should_hide
    send_action("tiling", "reflow")


def toggle(_data=None):
    """IPC action (bound to a hotkey) - takes effect immediately rather
    than waiting for the next poll tick, so it feels responsive."""
    global _override
    current = _override if _override is not None else _load_settings()["enabled"]
    _override = not current
    _apply(_override)


_stop_event = threading.Event()


def _quit(_data=None):
    """IPC "quit" action - restores the taskbar first (a hidden taskbar
    with the daemon that hides/shows it gone would be stuck hidden until
    oriel restarts), then signals the poll loop to stop via _stop_event
    rather than a blind time.sleep, so this doesn't wait out a full
    poll_interval to actually exit."""
    _set_taskbar_visible(True)
    _stop_event.set()


ACTIONS = {"toggle": toggle, "quit": _quit}


def run():
    configure_logging("taskbar")
    if not ensure_single_instance("taskbar"):
        return
    threading.Thread(target=serve_actions, args=("taskbar", ACTIONS), daemon=True).start()
    poll_interval = DEFAULTS["poll_interval"]
    while not _stop_event.is_set():
        try:
            settings = _load_settings()
            poll_interval = settings["poll_interval"]
            should_hide = _override if _override is not None else settings["enabled"]
            _apply(should_hide)
        except Exception:
            logger.exception("taskbar poll loop failed")
        if _stop_event.wait(timeout=poll_interval):
            break
