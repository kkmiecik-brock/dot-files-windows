"""Central hotkey registrar and dispatcher.

Uses a global WH_KEYBOARD_LL hook rather than RegisterHotKey - RegisterHotKey
is exclusive per-combo system-wide: whichever process registers a given
combo first "owns" it, and every other process (including this one, on a
config with a combo some other running app already grabbed) silently gets
nothing. A low-level keyboard hook intercepts every keystroke before
Windows even gets to that ownership question, and can outright swallow the
keys behind a matched combo (never dispatched to the focused app or any
other listener) by returning non-zero instead of calling CallNextHookEx -
see _low_level_keyboard_proc. Owns the master bindings table in
config.json.

Bindings with target "hotkeyd" are handled locally (launch an app, close
the focused window). Bindings with any other target (e.g. "tiling") are
forwarded over a named pipe via oriel.ipc.send_action() - the target module
is expected to be an independent process listening on its own pipe. If the
target isn't running, the message is silently dropped.
"""
import ctypes
import os
import queue
import logging
from ctypes import wintypes

import win32api
import win32con
import win32gui

from oriel.config import load_config
from oriel.ipc import send_action
from oriel.logging_setup import configure_logging
from oriel.single_instance import ensure_single_instance

logger = logging.getLogger(__name__)

kernel32 = ctypes.windll.kernel32
user32 = ctypes.windll.user32

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104  # fired instead of WM_KEYDOWN whenever Alt is held -
WM_SYSKEYUP = 0x0105    # every binding here requires Alt, so this is the common case

VK_SPACE = 0x20
VK_OEM_3 = 0xC0  # '`~' key on US keyboards - not in win32con, and not its ASCII value
VK_ESCAPE = 0x1B  # win32con.VK_ESCAPE isn't exposed for pywin32's WH_KEYBOARD_LL path

# vkCode -> normalized modifier name, covering both left/right variants -
# WH_KEYBOARD_LL reports the specific L/R vkCode, never the generic one.
MODIFIER_VKS = {
    0xA0: "shift", 0xA1: "shift",     # VK_LSHIFT, VK_RSHIFT
    0xA2: "ctrl", 0xA3: "ctrl",       # VK_LCONTROL, VK_RCONTROL
    0xA4: "alt", 0xA5: "alt",         # VK_LMENU, VK_RMENU
    0x5B: "win", 0x5C: "win",         # VK_LWIN, VK_RWIN
}

# Other independent modules with a named-pipe listener that should refresh
# their own settings whenever "reload_config" fires. taskbar isn't here -
# it already re-reads config.json every poll_interval on its own.
RELOADABLE_TARGETS = ["tiling", "drag"]

# Every other daemon process to tear down for "quit_oriel" - NOT autostart
# (it already ran once and exited, nothing to tear down) and deliberately
# never touches any app autostart itself launched (Teams etc.) - those are
# independent processes with no lifetime tie to oriel at all.
QUIT_TARGETS = ["tiling", "drag", "taskbar"]

# Actions whose hotkey args carry data the target needs (e.g. which
# workspace number) - forwarded as the IPC payload. Every other tiling
# action is a separate hardcoded name instead (resize_grow/resize_shrink
# etc.), so args are intentionally NOT forwarded for those.
ACTIONS_WITH_DATA = {"switch_workspace", "move_to_workspace"}


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

# Without explicit signatures, ctypes assumes 32-bit int returns/args, which
# truncates HWND/HMODULE pointers on 64-bit Windows and corrupts handles.
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HMODULE, wintypes.DWORD]
user32.CallNextHookEx.restype = ctypes.c_long
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]

WM_APP_HOTKEY = 0x8000 + 1  # WM_APP + 1

hook_handle = None
_main_thread_id = None

# Modifier names (see MODIFIER_VKS) currently physically held down, and
# non-modifier vks currently physically held down - both maintained purely
# from raw keydown/keyup events, since WH_KEYBOARD_LL has no concept of
# "chords" the way RegisterHotKey did; oriel has to compute that itself.
_held_modifiers = set()
_down_vks = set()

# vks whose keydown matched a binding and was swallowed - so the matching
# keyup (and any OS auto-repeat keydowns while still held) are swallowed
# too, instead of leaking a lone keyup/repeated dispatches to whichever
# app is focused.
_suppressed_vks = set()

# (frozenset of modifier names, vk) -> binding, rebuilt on load/reload.
_bindings_by_combo = {}

# Matched bindings are handed off here rather than dispatched inline in the
# hook callback - unlike drag's mouse hook (rare, once per gesture), this
# fires on every hotkey press, and dispatch can involve os.startfile or a
# named-pipe write that occasionally retries with a real time.sleep (see
# ipc.send_action); blocking a global keyboard hook that long would stall
# system-wide typing. The hook only decides, queues, and posts a wakeup.
_pending_bindings = queue.Queue()


def _vk_for(key_name):
    if key_name.lower() == "space":
        return VK_SPACE
    if key_name.lower() == "escape":
        return VK_ESCAPE
    if key_name == "`":
        return VK_OEM_3
    return ord(key_name.upper())


def _close_focused_window():
    hwnd = win32gui.GetForegroundWindow()
    if hwnd:
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)


def _launch(args):
    # ShellExecute (via os.startfile) correctly resolves MSIX App Execution
    # Alias stubs (e.g. wt.exe under WindowsApps); CreateProcess-based
    # launchers (subprocess.Popen) cannot.
    os.startfile(args["target"], arguments=args.get("args") or "", cwd=args.get("cwd") or None)


def _quit_oriel():
    """Tears down every other oriel daemon, then this one - runs on the
    message-loop thread already (see _handle_binding), so PostQuitMessage
    posts WM_QUIT to the right queue directly, no PostThreadMessageW/thread
    id needed the way the other daemons' own "quit" handlers do."""
    for target in QUIT_TARGETS:
        send_action(target, "quit")
    user32.PostQuitMessage(0)


LOCAL_ACTIONS = {
    "launch": lambda args: _launch(args),
    "close_focused": lambda args: _close_focused_window(),
    "quit_oriel": lambda _args=None: _quit_oriel(),
}


def _rebuild_lookup(bindings):
    global _bindings_by_combo
    table = {}
    for binding in bindings:
        mods = frozenset(name.lower() for name in binding["modifiers"])
        vk = _vk_for(binding["key"])
        table[(mods, vk)] = binding
    _bindings_by_combo = table


def _reload_config():
    for target in RELOADABLE_TARGETS:
        send_action(target, "reload")
    _rebuild_lookup(load_config()["bindings"])


def _low_level_keyboard_proc(nCode, wParam, lParam):
    try:
        if nCode == 0:  # HC_ACTION
            info = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            vk = info.vkCode
            is_keydown = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)
            is_keyup = wParam in (WM_KEYUP, WM_SYSKEYUP)

            mod_name = MODIFIER_VKS.get(vk)
            if mod_name is not None:
                if is_keydown:
                    _held_modifiers.add(mod_name)
                elif is_keyup:
                    _held_modifiers.discard(mod_name)
            elif is_keydown:
                if vk not in _down_vks:
                    _down_vks.add(vk)
                    binding = _bindings_by_combo.get((frozenset(_held_modifiers), vk))
                    if binding is not None:
                        _suppressed_vks.add(vk)
                        _pending_bindings.put(binding)
                        if _main_thread_id is not None:
                            user32.PostThreadMessageW(_main_thread_id, WM_APP_HOTKEY, 0, 0)
                        return 1
                elif vk in _suppressed_vks:
                    return 1  # swallow OS auto-repeat of an already-matched combo
            elif is_keyup:
                _down_vks.discard(vk)
                if vk in _suppressed_vks:
                    _suppressed_vks.discard(vk)
                    return 1
    except Exception:
        # MUST still fall through to CallNextHookEx below - a low-level
        # hook that never calls it can stall/break system-wide input.
        logger.exception("keyboard hook proc failed")

    return user32.CallNextHookEx(hook_handle, nCode, wParam, lParam)


def _handle_binding(binding):
    try:
        target = binding.get("target", "hotkeyd")
        action = binding["action"]
        if action == "reload_config":
            _reload_config()
        elif target == "hotkeyd":
            LOCAL_ACTIONS[action](binding.get("args", {}))
        elif action in ACTIONS_WITH_DATA:
            send_action(target, action, data=binding.get("args"))
        else:
            send_action(target, action)
    except Exception:
        logger.exception("hotkey dispatch failed for binding %s", binding)


def run():
    configure_logging("hotkeyd")
    if not ensure_single_instance("hotkeyd"):
        return
    try:
        _run()
    except Exception:
        logger.exception("hotkeyd daemon crashed")
        raise


def _run():
    global hook_handle, _main_thread_id
    _main_thread_id = win32api.GetCurrentThreadId()
    _rebuild_lookup(load_config()["bindings"])

    pointer = HOOKPROC(_low_level_keyboard_proc)
    hook_handle = user32.SetWindowsHookExW(WH_KEYBOARD_LL, pointer, kernel32.GetModuleHandleW(None), 0)
    if not hook_handle:
        raise ctypes.WinError()

    try:
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == WM_APP_HOTKEY:
                while True:
                    try:
                        binding = _pending_bindings.get_nowait()
                    except queue.Empty:
                        break
                    _handle_binding(binding)
                continue
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    finally:
        user32.UnhookWindowsHookEx(hook_handle)

