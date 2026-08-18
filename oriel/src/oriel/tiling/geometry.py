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


def safe_get_window_rect(hwnd):
    """GetWindowRect, or None if hwnd is gone/invalid by the time it's called
    - single place for a failure mode every caller needs to handle anyway."""
    try:
        return win32gui.GetWindowRect(hwnd)
    except win32gui.error:
        return None


def safe_get_class_name(hwnd):
    """GetClassName, or "" on failure (see safe_get_window_rect) - "" rather
    than None since callers generally compare/lower() this as a string."""
    try:
        return win32gui.GetClassName(hwnd)
    except win32gui.error:
        return ""


def safe_get_window_text(hwnd):
    """GetWindowText, or "" on failure (see safe_get_class_name)."""
    try:
        return win32gui.GetWindowText(hwnd)
    except win32gui.error:
        return ""


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


# monitor -> stable ID (or None), populated by stable_monitor_id on first
# use. A monitor's HMONITOR and \\.\DISPLAYn device name are both unstable
# across reconnects/port changes, so config.json's per-monitor workspace
# settings key on this EDID-derived ID instead. Cached because it costs two
# Win32 calls and would otherwise get called from every workspace-related
# window open/close/switch - cleared on WM_DISPLAYCHANGE by
# invalidate_display_caches() (see events.py's display-change watcher).
_stable_id_cache = {}


def stable_monitor_id(monitor):
    if monitor not in _stable_id_cache:
        try:
            device_name = win32api.GetMonitorInfo(monitor)["Device"]
            full_id = win32api.EnumDisplayDevices(device_name, 0).DeviceID or None
            _stable_id_cache[monitor] = _shorten_device_id(full_id)
        except Exception:
            _stable_id_cache[monitor] = None
    return _stable_id_cache[monitor]


def _shorten_device_id(device_id):
    """A monitor DeviceID looks like MONITOR\\<model>\\{4d36e96e-...}\\0002 -
    the MONITOR\\ prefix and {4d36e96e-...} are Windows' constant Monitor
    device-class GUID, and the trailing instance number is the LEAST stable
    part of all (RDP virtual displays get a new instance on every reconnect,
    real monitors can shift it too on a driver reinstall/port change) - drop
    both, keeping just <model>, so an RDP session's workspace config survives
    reconnects. Trade-off accepted: two simultaneous monitors sharing the
    exact same model (two identical real monitors, or multi-monitor RDP)
    would collide onto the same id - same class of v1 limitation as the
    identical-monitor-model caveat already documented for the full id."""
    if device_id is None:
        return None
    parts = device_id.split("\\")
    return parts[1] if len(parts) == 4 else device_id


def list_monitors():
    """Diagnostic CLI helper (see tiling/__main__.py --list-monitors): prints
    each connected monitor's stable ID for pasting into config.json's
    tiling.workspaces, since there's no other way to discover them."""
    ensure_dpi_awareness()  # must run before any monitor enumeration - see ensure_dpi_awareness
    for handle, _hdc, _rect in win32api.EnumDisplayMonitors():
        monitor = int(handle)
        info = win32api.GetMonitorInfo(monitor)
        device_name = info["Device"]
        try:
            friendly = win32api.EnumDisplayDevices(device_name, 0).DeviceString
        except Exception:
            friendly = "(unknown)"
        print(f"{device_name}  {friendly}")
        print(f"  bounds: {info['Monitor']}")
        print(f"  stable id: {stable_monitor_id(monitor)}")
        print()


# monitor -> taskbar hwnd, populated by _find_taskbar_hwnd on first use. See
# visible_taskbar_rect for why this is cached.
_taskbar_hwnd_cache = {}


def _find_taskbar_hwnd(monitor):
    found = []

    def callback(hwnd, _):
        class_name = safe_get_class_name(hwnd)
        if class_name in TASKBAR_CLASSES:
            found.append(hwnd)
        return True

    win32gui.EnumWindows(callback, None)
    for hwnd in found:
        if int(win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)) == monitor:
            return hwnd
    return None


def visible_taskbar_rect(monitor):
    # GetMonitorInfo's "Work" rect only reflects a taskbar that's docked AND
    # currently visible in the OS's own eyes - it doesn't get renegotiated
    # just because oriel.taskbar's ShowWindow(SW_HIDE) hid the window, so
    # relying on it directly leaves a stale gap where a hidden taskbar used
    # to be. Find the real, currently-visible taskbar rect ourselves instead.
    #
    # The taskbar's hwnd is cached per monitor (task 12 made the caller of
    # this, enforce_tiled_placement, run unconditionally instead of only
    # briefly after a window opens - profiled live and found this function's
    # full EnumWindows scan, calling GetClassName on every one of a
    # (typically 100s of) top-level windows on the desktop, was ~98% of its
    # cost). The taskbar's hwnd is stable for the life of the explorer.exe
    # process, so only the first call (or one after explorer.exe restarts)
    # needs the full scan - every later call is just two cheap win32 calls
    # on the already-known hwnd. Cleared on WM_DISPLAYCHANGE by
    # invalidate_display_caches() (see events.py's display-change watcher).
    hwnd = _taskbar_hwnd_cache.get(monitor)
    if hwnd is None or not win32gui.IsWindow(hwnd):
        hwnd = _find_taskbar_hwnd(monitor)
        if hwnd is None:
            return None
        _taskbar_hwnd_cache[monitor] = hwnd
    if not win32gui.IsWindowVisible(hwnd):
        return None
    return win32gui.GetWindowRect(hwnd)


def invalidate_display_caches():
    """Call after a WM_DISPLAYCHANGE (monitor added/removed/reconfigured) -
    HMONITOR handles and taskbar/stable-id associations can all go stale
    across one, so drop everything keyed by them and let it get relearned."""
    _taskbar_hwnd_cache.clear()
    _stable_id_cache.clear()


def subtract_taskbar(bounds, taskbar_rect):
    ml, mt, mr, mb = bounds
    tl, tt, tr, tb = taskbar_rect
    if (tr - tl) >= (tb - tt):  # wider than tall - docked top or bottom
        return (ml, tb, mr, mb) if tt <= mt else (ml, mt, mr, tt)
    return (tr, mt, mr, mb) if tl <= ml else (ml, mt, tl, mb)  # docked left or right


def taskbar_rect(monitor):
    """Same hwnd lookup as visible_taskbar_rect but WITHOUT the visibility
    check - for callers that need the taskbar's real screen position
    regardless of whether oriel.taskbar currently has it hidden (e.g. the
    tiling daemon's quit teardown, which runs concurrently with - and isn't
    guaranteed to run after - oriel.taskbar's own "show it again" quit
    teardown, so it can't depend on the taskbar already being visible by
    the time it runs)."""
    hwnd = _taskbar_hwnd_cache.get(monitor)
    if hwnd is None or not win32gui.IsWindow(hwnd):
        hwnd = _find_taskbar_hwnd(monitor)
        if hwnd is None:
            return None
        _taskbar_hwnd_cache[monitor] = hwnd
    return win32gui.GetWindowRect(hwnd)


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
    rect = safe_get_window_rect(hwnd)
    if rect is None:
        return (0, 0, 0, 0)
    al, at, ar, ab = rect

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
