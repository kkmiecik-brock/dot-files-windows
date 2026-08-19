"""Alt+drag move/resize for any window.

Alt+Left-drag moves the window under the cursor; Alt+Right-drag resizes it.
Fires the native interactive-move/resize-START accessibility event via
NotifyWinEvent so any listening window manager knows a real gesture is in
progress and shouldn't fight it - the same technique AltSnap uses. The end
of the gesture is reported directly over IPC (record_drag_kind) instead of
a matching synthetic END event, since this module already knows the
authoritative outcome and a synthetic END would just race that same report.

Movement is driven by a polling loop (GetCursorPos + SetWindowPos on a tight
sleep interval), mirroring the AHK implementation this replaces. Both a
direct WM_MOUSEMOVE hook reaction and an AltSnap-style worker-thread/message
hybrid were tried and both reintroduced "fighting", so polling is the
proven-working approach.
"""
import ctypes
import threading
import time
import logging
from ctypes import wintypes

import win32api
import win32con
import win32gui
import win32process

from oriel.config import get_section
from oriel.ipc import send_action, serve_actions
from oriel.logging_setup import configure_logging
from oriel.single_instance import ensure_single_instance

logger = logging.getLogger(__name__)

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WH_MOUSE_LL = 14
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
GA_ROOT = 2

EVENT_SYSTEM_MOVESIZESTART = 0x000A

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_ASYNCWINDOWPOS = 0x4000

# VK codes to check via GetAsyncKeyState for each configurable modifier name.
# win checks both L/R since GetAsyncKeyState has no combined VK for it.
MODIFIER_VKS = {
    "alt": [0x12],    # VK_MENU
    "ctrl": [0x11],   # VK_CONTROL
    "shift": [0x10],  # VK_SHIFT
    "win": [0x5B, 0x5C],  # VK_LWIN, VK_RWIN
}
BUTTON_DOWN_MSGS = {"left": WM_LBUTTONDOWN, "right": WM_RBUTTONDOWN}

DEFAULTS = {
    "modifier": "alt",
    "move_button": "left",
    "resize_button": "right",
    "min_size": 100,
}


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", POINT),
        ("mouseData", wintypes.DWORD),
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
user32.GetAncestor.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
user32.NotifyWinEvent.argtypes = [wintypes.DWORD, wintypes.HWND, ctypes.c_long, ctypes.c_long]

hook_handle = None
dragging = False
drag_button = None
hwnd = None
start_x = start_y = 0
win_x = win_y = win_w = win_h = 0
resize_edges = frozenset()
settings = DEFAULTS


def _load_settings():
    return {**DEFAULTS, **get_section("drag")}


def _modifier_down():
    vks = MODIFIER_VKS[settings["modifier"]]
    return any((user32.GetAsyncKeyState(vk) & 0x8000) != 0 for vk in vks)


def _force_foreground(target_hwnd):
    # SetForegroundWindow can silently no-op when called from a background
    # process due to Windows' foreground-lock restriction. Temporarily
    # attaching our input queue to both the current foreground window's
    # thread and the target's thread bypasses that restriction reliably.
    # CRITICAL: every AttachThreadInput(..., True) must be matched by a
    # (..., False) no matter what happens in between - a stuck attachment
    # permanently cross-wires the OTHER thread's keyboard input to this
    # daemon's thread, which looks like (and is) a real input freeze for
    # whatever app was attached. Everything from the first attach onward
    # must be inside the try, so an exception partway through (e.g.
    # target_hwnd having gone stale) still reaches the matching detach.
    current_thread = win32api.GetCurrentThreadId()
    attached_fg = attached_target = False
    fg_thread = target_thread = 0
    try:
        fg_hwnd = win32gui.GetForegroundWindow()
        fg_thread = win32process.GetWindowThreadProcessId(fg_hwnd)[0] if fg_hwnd else 0
        target_thread = win32process.GetWindowThreadProcessId(target_hwnd)[0]

        if fg_thread and fg_thread != current_thread:
            # AttachThreadInput returns None on success (pywin32 convention
            # for void-return Win32 calls) and raises on failure - it is
            # NOT a truthy success flag. Using its return value directly as
            # the success indicator (as this used to) meant `attached_fg`
            # was always None/falsy even on success, so the finally block's
            # `if attached_fg:` never detached - leaking every attach.
            win32process.AttachThreadInput(current_thread, fg_thread, True)
            attached_fg = True
        if target_thread and target_thread != current_thread:
            win32process.AttachThreadInput(current_thread, target_thread, True)
            attached_target = True

        win32gui.BringWindowToTop(target_hwnd)
        win32gui.SetForegroundWindow(target_hwnd)
    except win32gui.error:
        pass
    finally:
        if attached_fg:
            win32process.AttachThreadInput(current_thread, fg_thread, False)
        if attached_target:
            win32process.AttachThreadInput(current_thread, target_thread, False)


def _begin_drag(button, x, y):
    global dragging, drag_button, hwnd, start_x, start_y, win_x, win_y, win_w, win_h, resize_edges

    target = win32gui.WindowFromPoint((x, y))
    if not target:
        return
    root = user32.GetAncestor(target, GA_ROOT) or target
    try:
        left, top, right, bottom = win32gui.GetWindowRect(root)
    except win32gui.error:
        return

    dragging = True
    drag_button = button
    hwnd = root
    start_x, start_y = x, y
    win_x, win_y = left, top
    win_w, win_h = right - left, bottom - top

    if button == "resize":
        # Mirrors niri's resize_edges_under: split the window into thirds
        # and only move the edge(s) nearest the click - clicking dead
        # center does nothing, same as niri.
        frac_x = (x - left) / win_w if win_w else 0
        frac_y = (y - top) / win_h if win_h else 0
        edges = set()
        if frac_x < 1 / 3:
            edges.add("L")
        elif frac_x > 2 / 3:
            edges.add("R")
        if frac_y < 1 / 3:
            edges.add("T")
        elif frac_y > 2 / 3:
            edges.add("B")
        resize_edges = edges

    try:
        _force_foreground(hwnd)
    except win32gui.error:
        pass
    user32.NotifyWinEvent(EVENT_SYSTEM_MOVESIZESTART, hwnd, 0, 0)

    threading.Thread(target=_drag_loop, daemon=True).start()


def _update_drag(x, y):
    dx, dy = x - start_x, y - start_y
    if drag_button == "move":
        win32gui.SetWindowPos(
            hwnd, 0, win_x + dx, win_y + dy, 0, 0,
            SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE,
        )
    else:
        min_size = settings["min_size"]
        new_left, new_top = win_x, win_y
        new_w, new_h = win_w, win_h

        if "L" in resize_edges:
            new_w = max(min_size, win_w - dx)
            new_left = win_x + win_w - new_w
        elif "R" in resize_edges:
            new_w = max(min_size, win_w + dx)

        if "T" in resize_edges:
            new_h = max(min_size, win_h - dy)
            new_top = win_y + win_h - new_h
        elif "B" in resize_edges:
            new_h = max(min_size, win_h + dy)

        win32gui.SetWindowPos(
            hwnd, 0, new_left, new_top, new_w, new_h,
            SWP_NOZORDER | SWP_NOACTIVATE,
        )


def _end_drag():
    global dragging
    # Tiling can't observe which button drove this gesture (GetAsyncKeyState
    # can't see it - see _drag_loop's comment), so tell it directly instead
    # of leaving it to guess from the resulting size/position delta. No
    # synthetic MOVESIZEEND here (unlike the START above) - that used to
    # race this same IPC message for the same gesture, needing a dedup
    # timer on the tiling side; record_drag_kind alone is authoritative for
    # every gesture this module drives.
    send_action("tiling", "record_drag_kind", {"hwnd": hwnd, "kind": drag_button})
    dragging = False


def _drag_loop():
    # Button-up is detected via the hook itself, not GetAsyncKeyState - since
    # our hook suppresses the initiating button-down, Windows never updates
    # the key-state table GetAsyncKeyState reads from, so polling it here
    # always reports "up".
    while dragging:
        x, y = win32api.GetCursorPos()
        _update_drag(x, y)
        time.sleep(0.001)


def _mouse_proc(nCode, wParam, lParam):
    try:
        if nCode == 0:
            move_down = BUTTON_DOWN_MSGS.get(settings["move_button"])
            resize_down = BUTTON_DOWN_MSGS.get(settings["resize_button"])

            if not dragging and wParam in (move_down, resize_down) and _modifier_down():
                info = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                action = "move" if wParam == move_down else "resize"
                _begin_drag(action, info.pt.x, info.pt.y)
                if dragging:
                    return 1
            elif dragging and wParam in (WM_LBUTTONUP, WM_RBUTTONUP):
                _end_drag()
                return 1
    except Exception:
        # MUST still fall through to CallNextHookEx below - a low-level
        # hook that never calls it can stall/break system-wide input.
        logger.exception("mouse hook proc failed")

    return user32.CallNextHookEx(hook_handle, nCode, wParam, lParam)


def _reload(_data=None):
    global settings
    settings = _load_settings()


WM_QUIT = 0x0012
_main_thread_id = None


def _quit(_data=None):
    """IPC "quit" action - posts WM_QUIT to the message-loop thread so
    GetMessageW returns 0 and the process exits (mirrors tiling.events'
    quit_daemon; see its docstring for why this is sufficient cleanup)."""
    if _main_thread_id is not None:
        user32.PostThreadMessageW(_main_thread_id, WM_QUIT, 0, 0)


ACTIONS = {"reload": _reload, "quit": _quit}


def run():
    configure_logging("drag")
    if not ensure_single_instance("drag"):
        return
    try:
        _run()
    except Exception:
        logger.exception("drag daemon crashed")
        raise


def _run():
    global hook_handle, settings, _main_thread_id
    _main_thread_id = win32api.GetCurrentThreadId()

    # Without DPI awareness, Windows can virtualize/scale the coordinates this
    # process sees, causing drag offsets to drift relative to true pixels.
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except (AttributeError, OSError):
        ctypes.windll.user32.SetProcessDPIAware()

    # Raise timer resolution to 1ms so the drag-polling sleep is precise
    # instead of snapping to the default ~15.6ms tick.
    ctypes.windll.winmm.timeBeginPeriod(1)

    settings = _load_settings()
    pointer = HOOKPROC(_mouse_proc)
    hook_handle = user32.SetWindowsHookExW(WH_MOUSE_LL, pointer, kernel32.GetModuleHandleW(None), 0)
    if not hook_handle:
        raise ctypes.WinError()

    threading.Thread(
        target=serve_actions, args=("drag", ACTIONS), name="oriel-drag-ipc", daemon=True
    ).start()

    try:
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    finally:
        user32.UnhookWindowsHookEx(hook_handle)
