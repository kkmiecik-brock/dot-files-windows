"""Alt+drag move/resize for any window, with GlazeWM tile-snap integration.

Alt+Left-drag moves the window under the cursor; Alt+Right-drag resizes it.
Manually fires the native interactive-move/resize accessibility events via
NotifyWinEvent so GlazeWM's WinEventHook treats it as a genuine drag and can
snap/re-tile it on release - the same technique AltSnap uses.

Movement is driven by a polling loop (GetCursorPos + SetWindowPos on a tight
sleep interval), mirroring the AHK implementation this replaces. Both a
direct WM_MOUSEMOVE hook reaction and an AltSnap-style worker-thread/message
hybrid were tried and both reintroduced "fighting", so polling is the
proven-working approach.
"""

import ctypes
import os
import subprocess
import threading
import time
from ctypes import wintypes

import win32api
import win32con
import win32gui
import win32process

# Without DPI awareness, Windows can virtualize/scale the coordinates this
# process sees from GetCursorPos/GetWindowRect relative to a monitor's actual
# scaling, while the low-level hook always reports true physical pixels -
# that mismatch is what caused the drag offset to drift/"bounce".
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
except (AttributeError, OSError):
    ctypes.windll.user32.SetProcessDPIAware()

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# Raise the system timer resolution to 1ms so time.sleep(0.001) in the drag
# loop is precise instead of snapping to the default ~15.6ms tick.
ctypes.windll.winmm.timeBeginPeriod(1)

WH_MOUSE_LL = 14
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
VK_MENU = 0x12
GA_ROOT = 2

EVENT_SYSTEM_MOVESIZESTART = 0x000A
EVENT_SYSTEM_MOVESIZEEND = 0x000B

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010

MIN_SIZE = 100


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


def _alt_down():
    return (user32.GetAsyncKeyState(VK_MENU) & 0x8000) != 0


def _force_foreground(target_hwnd):
    # SetForegroundWindow can silently no-op when called from a background
    # process due to Windows' foreground-lock restriction. Temporarily
    # attaching our input queue to both the current foreground window's
    # thread and the target's thread bypasses that restriction reliably.
    current_thread = win32api.GetCurrentThreadId()
    fg_hwnd = win32gui.GetForegroundWindow()
    fg_thread = win32process.GetWindowThreadProcessId(fg_hwnd)[0] if fg_hwnd else 0
    target_thread = win32process.GetWindowThreadProcessId(target_hwnd)[0]

    attached_fg = fg_thread and fg_thread != current_thread and win32process.AttachThreadInput(current_thread, fg_thread, True)
    attached_target = target_thread and target_thread != current_thread and win32process.AttachThreadInput(current_thread, target_thread, True)
    try:
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

    if button == "R":
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
    if drag_button == "L":
        win32gui.SetWindowPos(
            hwnd, 0, win_x + dx, win_y + dy, 0, 0,
            SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE,
        )
    else:
        new_left, new_top = win_x, win_y
        new_w, new_h = win_w, win_h

        if "L" in resize_edges:
            new_w = max(MIN_SIZE, win_w - dx)
            new_left = win_x + win_w - new_w
        elif "R" in resize_edges:
            new_w = max(MIN_SIZE, win_w + dx)

        if "T" in resize_edges:
            new_h = max(MIN_SIZE, win_h - dy)
            new_top = win_y + win_h - new_h
        elif "B" in resize_edges:
            new_h = max(MIN_SIZE, win_h + dy)

        win32gui.SetWindowPos(
            hwnd, 0, new_left, new_top, new_w, new_h,
            SWP_NOZORDER | SWP_NOACTIVATE,
        )


def _end_drag():
    global dragging
    user32.NotifyWinEvent(EVENT_SYSTEM_MOVESIZEEND, hwnd, 0, 0)
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
    if nCode == 0:
        if not dragging and wParam in (WM_LBUTTONDOWN, WM_RBUTTONDOWN) and _alt_down():
            info = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            _begin_drag("L" if wParam == WM_LBUTTONDOWN else "R", info.pt.x, info.pt.y)
            if dragging:
                return 1
        elif dragging and wParam in (WM_LBUTTONUP, WM_RBUTTONUP):
            _end_drag()
            return 1

    return user32.CallNextHookEx(hook_handle, nCode, wParam, lParam)


def _kill_other_instances():
    # If a newer instance is launched while an older one is still running,
    # the newer one wins - kill any other process running this same script.
    script = os.path.abspath(__file__)
    ps_script = (
        "$procs = Get-CimInstance Win32_Process | Where-Object { "
        "($_.Name -match '^python(\\.exe)?$' -or $_.Name -eq 'pythonw.exe') "
        f"-and $_.CommandLine -match [regex]::Escape('{script}') "
        f"-and $_.ProcessId -ne {os.getpid()} }}; "
        "if ($procs) { $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force } }"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_script],
        creationflags=subprocess.CREATE_NO_WINDOW,
        check=False,
    )


def main():
    global hook_handle

    _kill_other_instances()

    pointer = HOOKPROC(_mouse_proc)
    hook_handle = user32.SetWindowsHookExW(WH_MOUSE_LL, pointer, kernel32.GetModuleHandleW(None), 0)
    if not hook_handle:
        raise ctypes.WinError()

    try:
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    finally:
        user32.UnhookWindowsHookEx(hook_handle)


if __name__ == "__main__":
    main()
