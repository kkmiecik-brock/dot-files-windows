"""Win32/DWM geometry adapter: monitor bounds, taskbar detection, and the
DPI-awareness + DWM frame-margin compensation needed for gaps to render at
their configured size. No tiling-tree or IPC knowledge lives here - every
function takes an hwnd/monitor/rect and returns a rect.
"""
import ctypes
from ctypes import wintypes

import win32api
import win32con
import win32gui

TASKBAR_CLASSES = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd"}

DWMWA_EXTENDED_FRAME_BOUNDS = 9


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


# hwnd -> (left, top, right, bottom) margins. A window's invisible DWM
# resize/shadow border is effectively constant for its lifetime and costs a
# DWM round-trip to query, unlike its rect/iconic-state which genuinely can
# change at any time from outside oriel - so unlike those, this is safe to
# memoize. Evict via invalidate_frame_margins() when a window closes.
_frame_margin_cache = {}


def ensure_dpi_awareness():
    """Without this, GetWindowRect/SetWindowPos/GetMonitorInfo see
    virtualized (DPI-scaled) coordinates while DwmGetWindowAttribute's
    extended frame bounds do not, so frame_margins() computes bogus margins
    and windows drift outward into overlap. Must be called once, before any
    window enumeration - see drag/daemon.py's run() for the same fix."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except (AttributeError, OSError):
        ctypes.windll.user32.SetProcessDPIAware()


def monitor_bounds(monitor):
    return win32api.GetMonitorInfo(monitor)["Monitor"]  # full bounds, edge-to-edge, no gaps


def monitor_of(hwnd):
    # int() is essential here: MonitorFromWindow returns a fresh PyHANDLE
    # object each call that doesn't compare equal by value to a PyHANDLE from
    # a previous call for the same physical monitor - using it directly as a
    # dict key means every window looks like it's on a "new" monitor.
    return int(win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST))


def monitor_at_cursor():
    return monitor_at_point(win32api.GetCursorPos())


def monitor_at_point(point):
    return int(win32api.MonitorFromPoint(point, win32con.MONITOR_DEFAULTTONEAREST))


def visible_taskbar_rect(monitor):
    # GetMonitorInfo's "Work" rect only reflects a taskbar that's docked AND
    # currently visible in the OS's own eyes - it doesn't get renegotiated
    # just because oriel.taskbar's ShowWindow(SW_HIDE) hid the window, so
    # relying on it directly leaves a stale gap where a hidden taskbar used
    # to be. Find the real, currently-visible taskbar rect ourselves instead.
    found = []

    def callback(hwnd, _):
        try:
            class_name = win32gui.GetClassName(hwnd)
        except win32gui.error:
            return True
        if class_name in TASKBAR_CLASSES and win32gui.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    win32gui.EnumWindows(callback, None)
    for hwnd in found:
        if int(win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)) == monitor:
            return win32gui.GetWindowRect(hwnd)
    return None


def subtract_taskbar(bounds, taskbar_rect):
    ml, mt, mr, mb = bounds
    tl, tt, tr, tb = taskbar_rect
    if (tr - tl) >= (tb - tt):  # wider than tall - docked top or bottom
        return (ml, tb, mr, mb) if tt <= mt else (ml, mt, mr, tt)
    return (tr, mt, mr, mb) if tl <= ml else (ml, mt, tl, mb)  # docked left or right


def work_area(monitor, outer_gap):
    bounds = monitor_bounds(monitor)
    taskbar_rect = visible_taskbar_rect(monitor)
    left, top, right, bottom = subtract_taskbar(bounds, taskbar_rect) if taskbar_rect else bounds
    return (
        left + outer_gap["left"],
        top + outer_gap["top"],
        right - outer_gap["right"],
        bottom - outer_gap["bottom"],
    )


def _query_frame_margins(hwnd):
    """Windows 10/11 gives most windows an invisible resize/shadow border,
    so GetWindowRect (what SetWindowPos positions) is larger on each side
    than the actually-visible DWM frame. Left uncompensated, this makes
    configured gaps look inconsistent - inner gaps get it from both facing
    windows (looking ~2x too big) while outer gaps only get it once.
    Returns (left, top, right, bottom) margins to expand a target rect by
    so the *visible* frame lands exactly on that rect."""
    try:
        al, at, ar, ab = win32gui.GetWindowRect(hwnd)
    except win32gui.error:
        return (0, 0, 0, 0)

    frame = _RECT()
    hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd), ctypes.c_int(DWMWA_EXTENDED_FRAME_BOUNDS),
        ctypes.byref(frame), ctypes.sizeof(frame),
    )
    if hr != 0:
        return (0, 0, 0, 0)

    return (
        max(0, frame.left - al),
        max(0, frame.top - at),
        max(0, ar - frame.right),
        max(0, ab - frame.bottom),
    )


def frame_margins(hwnd):
    margins = _frame_margin_cache.get(hwnd)
    if margins is None:
        margins = _query_frame_margins(hwnd)
        _frame_margin_cache[hwnd] = margins
    return margins


def invalidate_frame_margins(hwnd):
    _frame_margin_cache.pop(hwnd, None)


# hwnd -> (min_width, min_height), in the same frame-inclusive coordinate
# space as GetWindowRect/SetWindowPos. Learned reactively from an actual
# observed resize clamp (see state.py's reflow) rather than queried
# speculatively via WM_GETMINMAXINFO - most windows have no real floor
# worth tracking, so this only grows for the ones that actually enforce one.
_min_size_cache = {}


def learn_min_size(hwnd, width, height):
    """Records/grows hwnd's observed minimum size. Returns True if this
    changes what was already known, so a caller can trigger a re-layout
    only when there's actually new information."""
    existing = _min_size_cache.get(hwnd, (0, 0))
    updated = (max(existing[0], width), max(existing[1], height))
    if updated != existing:
        _min_size_cache[hwnd] = updated
        return True
    return False


def min_size(hwnd):
    return _min_size_cache.get(hwnd, (0, 0))


def invalidate_min_size(hwnd):
    _min_size_cache.pop(hwnd, None)


def expand_rect_for_frame(rect, hwnd):
    """Expands a logical target rect outward by hwnd's own frame margins, so
    the visible DWM frame lands exactly on `rect` once passed to SetWindowPos."""
    left, top, right, bottom = rect
    ml, mt, mr, mb = frame_margins(hwnd)
    return (left - ml, top - mt, right + mr, bottom + mb)


def shrink_rect_for_frame(rect, hwnd):
    """Inverse of expand_rect_for_frame: undoes the frame-margin expansion
    when reading a window's current rect back, so it can be compared
    like-for-like against a margin-less logical rect."""
    left, top, right, bottom = rect
    ml, mt, mr, mb = frame_margins(hwnd)
    return (left + ml, top + mt, right - mr, bottom - mb)
