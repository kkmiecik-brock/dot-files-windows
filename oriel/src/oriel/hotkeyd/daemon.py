"""Central hotkey registrar and dispatcher.

The only module that calls RegisterHotKey - this avoids the class of
"hotkey already registered" conflicts that come from multiple processes
each registering their own global hotkeys. Owns the master bindings table
in config.json.

Bindings with target "hotkeyd" are handled locally (launch an app, close
the focused window). Bindings with any other target (e.g. "tiling") are
forwarded over a named pipe via oriel.ipc.send_action() - the target module
is expected to be an independent process listening on its own pipe. If the
target isn't running, the message is silently dropped.
"""
import os

import win32con
import win32gui

from oriel.config import load_config
from oriel.ipc import send_action

MOD_MAP = {
    "alt": win32con.MOD_ALT,
    "ctrl": win32con.MOD_CONTROL,
    "shift": win32con.MOD_SHIFT,
    "win": win32con.MOD_WIN,
}
MOD_NOREPEAT = 0x4000  # not defined in win32con - suppresses repeat-fire while held
VK_SPACE = 0x20

# Other independent modules with a named-pipe listener that should refresh
# their own settings whenever "reload_config" fires. taskbar isn't here -
# it already re-reads config.json every poll_interval on its own.
RELOADABLE_TARGETS = ["tiling", "drag"]


def _vk_for(key_name):
    return VK_SPACE if key_name.lower() == "space" else ord(key_name.upper())


def _close_focused_window():
    hwnd = win32gui.GetForegroundWindow()
    if hwnd:
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)


def _launch(args):
    # ShellExecute (via os.startfile) correctly resolves MSIX App Execution
    # Alias stubs (e.g. wt.exe under WindowsApps); CreateProcess-based
    # launchers (subprocess.Popen) cannot.
    os.startfile(args["target"], arguments=args.get("args") or "", cwd=args.get("cwd") or None)


LOCAL_ACTIONS = {
    "launch": lambda args: _launch(args),
    "close_focused": lambda args: _close_focused_window(),
}


def _register_all(bindings):
    id_to_binding = {}
    for hotkey_id, binding in enumerate(bindings, start=1):
        modifiers = MOD_NOREPEAT
        for name in binding["modifiers"]:
            modifiers |= MOD_MAP[name.lower()]
        vk = _vk_for(binding["key"])
        win32gui.RegisterHotKey(0, hotkey_id, modifiers, vk)
        id_to_binding[hotkey_id] = binding
    return id_to_binding


def _unregister_all(id_to_binding):
    for hotkey_id in id_to_binding:
        win32gui.UnregisterHotKey(0, hotkey_id)


def _reload_config():
    for target in RELOADABLE_TARGETS:
        send_action(target, "reload")
    return _register_all(load_config()["bindings"])


def run():
    id_to_binding = _register_all(load_config()["bindings"])

    try:
        while True:
            # MSG struct fields: hwnd, message, wParam, lParam, time, pt
            result, (_hwnd, message, wparam, lparam, _time, _pt) = win32gui.GetMessage(0, 0, 0)
            if result == 0:
                break
            if message != win32con.WM_HOTKEY:
                continue

            binding = id_to_binding.get(wparam)
            if binding is None:
                continue

            target = binding.get("target", "hotkeyd")
            action = binding["action"]
            if action == "reload_config":
                _unregister_all(id_to_binding)
                id_to_binding = _reload_config()
            elif target == "hotkeyd":
                LOCAL_ACTIONS[action](binding.get("args", {}))
            else:
                send_action(target, action)
    finally:
        _unregister_all(id_to_binding)
