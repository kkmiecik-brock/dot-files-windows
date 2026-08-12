"""Taskbar auto-hide, config-driven on/off.

Polls config.json's taskbar.enabled flag so it can be toggled live by
editing the config file - no restart needed. When switched off, restores
the taskbar's visibility instead of just stopping further hides.
"""
import time

import win32con
import win32gui

from oriel.config import get_section

TASKBAR_CLASSES = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd"}

DEFAULTS = {"enabled": True, "poll_interval": 1.0}


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


def run():
    was_enabled = None
    while True:
        settings = _load_settings()
        enabled = settings["enabled"]

        if enabled:
            _set_taskbar_visible(False)
        elif was_enabled:
            # Just switched off - restore visibility instead of leaving it hidden.
            _set_taskbar_visible(True)

        was_enabled = enabled
        time.sleep(settings["poll_interval"])
