"""Win32 glue for automatic BSP tiling: owns per-monitor tree state, the
WinEventHook that auto-tiles new/closed windows, and the named-pipe IPC
server that receives focus/move/resize/reload actions from hotkeyd.

New windows are picked up via EVENT_OBJECT_SHOW and inserted into the tree
for whichever monitor the cursor is on, splitting the currently focused
tile. Closed windows are detected via EVENT_OBJECT_DESTROY and pruned
immediately, with a slow polling sweep as a safety net in case an event is
ever missed. At startup, all existing top-level windows are bootstrapped
into their monitor's tree the same way.
"""
import ctypes
import threading
import time
from ctypes import wintypes

import win32api
import win32con
import win32gui

from oriel.config import get_section
from oriel.ipc import serve_actions
from oriel.tiling import tree
from oriel.tiling.filters import is_manageable

DEFAULT_GAP = 8
DEFAULT_OUTER_GAP = {"top": 0, "right": 0, "bottom": 0, "left": 0}
DEFAULT_CLEANUP_INTERVAL = 5.0  # safety-net sweep only; removal is normally event-driven

# How long a hwnd stays in _recently_finalized after _record_drag_kind
# handles it, so the WinEvent-driven _on_move_resize_end for the very same
# drag knows to skip instead of redundantly re-processing it.
RECENTLY_FINALIZED_WINDOW = 2.0

# Per-monitor state. Keyed by monitor handle (int).
_roots = {}
_focused_leaf = {}
_fullscreen_leaf = {}  # monitor -> the one leaf currently fullscreened, or absent

# Live-reloadable settings - module-level so every function sees updates
# immediately after a "reload" action, without threading a value through
# every call site.
_inner_gap = DEFAULT_GAP
_outer_gap = DEFAULT_OUTER_GAP
_cleanup_interval = DEFAULT_CLEANUP_INTERVAL

# hwnd -> monotonic timestamp, set by _record_drag_kind once it has already
# run the finalize logic for that hwnd's drag.
_recently_finalized = {}

_lock = threading.RLock()


def _load_settings():
    tiling = get_section("tiling")
    return {
        "inner_gap": tiling.get("inner_gap", DEFAULT_GAP),
        "outer_gap": {**DEFAULT_OUTER_GAP, **tiling.get("outer_gap", {})},
        "cleanup_interval": tiling.get("cleanup_interval", DEFAULT_CLEANUP_INTERVAL),
    }


def _monitor_of(hwnd):
    # int() is essential here: MonitorFromWindow returns a fresh PyHANDLE
    # object each call that doesn't compare equal by value to a PyHANDLE from
    # a previous call for the same physical monitor - using it directly as a
    # dict key means every window looks like it's on a "new" monitor.
    return int(win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST))


def _monitor_at_cursor():
    return int(win32api.MonitorFromPoint(win32api.GetCursorPos(), win32con.MONITOR_DEFAULTTONEAREST))


TASKBAR_CLASSES = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


def _visible_taskbar_rect(monitor):
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


def _subtract_taskbar(bounds, taskbar_rect):
    ml, mt, mr, mb = bounds
    tl, tt, tr, tb = taskbar_rect
    if (tr - tl) >= (tb - tt):  # wider than tall - docked top or bottom
        return (ml, tb, mr, mb) if tt <= mt else (ml, mt, mr, tt)
    return (tr, mt, mr, mb) if tl <= ml else (ml, mt, tl, mb)  # docked left or right


def _work_area(monitor):
    bounds = _monitor_bounds(monitor)
    taskbar_rect = _visible_taskbar_rect(monitor)
    left, top, right, bottom = _subtract_taskbar(bounds, taskbar_rect) if taskbar_rect else bounds
    return (
        left + _outer_gap["left"],
        top + _outer_gap["top"],
        right - _outer_gap["right"],
        bottom - _outer_gap["bottom"],
    )


def _monitor_bounds(monitor):
    return win32api.GetMonitorInfo(monitor)["Monitor"]  # full bounds, edge-to-edge, no gaps


DWMWA_EXTENDED_FRAME_BOUNDS = 9


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def _frame_margins(hwnd):
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


def _reflow(monitor):
    root = _roots.get(monitor)
    rects = tree.compute_rects(root, _work_area(monitor), _inner_gap)

    fullscreen_leaf = _fullscreen_leaf.get(monitor)
    if fullscreen_leaf is not None:
        rects[fullscreen_leaf] = _monitor_bounds(monitor)

    for leaf, rect in rects.items():
        if not win32gui.IsWindow(leaf.item) or win32gui.IsIconic(leaf.item):
            continue
        left, top, right, bottom = rect
        ml, mt, mr, mb = _frame_margins(leaf.item)
        win32gui.SetWindowPos(
            leaf.item, 0, left - ml, top - mt, (right + mr) - (left - ml), (bottom + mb) - (top - mt),
            win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE,
        )


def _reflow_all():
    for monitor in _roots:
        _reflow(monitor)


def _insert_hwnd(monitor, hwnd):
    target = _focused_leaf.get(monitor)
    rect = tree.compute_rects(_roots.get(monitor), _work_area(monitor), _inner_gap).get(
        target, _work_area(monitor)
    )
    new_root, new_leaf = tree.insert(_roots.get(monitor), target, hwnd, rect)
    _roots[monitor] = new_root
    _focused_leaf[monitor] = new_leaf


def _remove_leaf(monitor, leaf):
    _roots[monitor] = tree.remove(_roots.get(monitor), leaf)
    if _focused_leaf.get(monitor) is leaf:
        _focused_leaf[monitor] = None
    if _fullscreen_leaf.get(monitor) is leaf:
        del _fullscreen_leaf[monitor]


def _find_leaf_any_monitor(hwnd):
    for monitor, root in _roots.items():
        leaf = tree.find_leaf(root, hwnd)
        if leaf is not None:
            return monitor, leaf
    return None, None


def _prune_closed():
    with _lock:
        for monitor, root in list(_roots.items()):
            dead = [leaf for leaf in tree.all_leaves(root) if not win32gui.IsWindow(leaf.item)]
            if not dead:
                continue
            for leaf in dead:
                _remove_leaf(monitor, leaf)
            _reflow(monitor)


def _bootstrap_existing_windows():
    handles = []

    def callback(hwnd, _):
        handles.append(hwnd)
        return True

    win32gui.EnumWindows(callback, None)

    with _lock:
        # Reverse Z-order (bottom-most first) so the most-recently-focused
        # window ends up last-inserted, roughly matching what you'd expect
        # to see "on top" of the initial layout.
        for hwnd in reversed(handles):
            if is_manageable(hwnd):
                _insert_hwnd(_monitor_of(hwnd), hwnd)
        _reflow_all()


def _on_window_shown(hwnd):
    with _lock:
        _, existing = _find_leaf_any_monitor(hwnd)
        if existing is not None or not is_manageable(hwnd):
            return
        # Newly opened windows go to whichever monitor the cursor is on,
        # not wherever Windows happened to place the window initially.
        monitor = _monitor_at_cursor()
        _insert_hwnd(monitor, hwnd)
        _reflow(monitor)


def _on_window_destroyed(hwnd):
    with _lock:
        monitor, leaf = _find_leaf_any_monitor(hwnd)
        if leaf is None:
            return
        _remove_leaf(monitor, leaf)
        _reflow(monitor)


def _focus_direction(direction):
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return
    monitor = _monitor_of(hwnd)

    with _lock:
        current_leaf = tree.find_leaf(_roots.get(monitor), hwnd)
        if current_leaf is None:
            return
        target = tree.find_direction_target(
            _roots.get(monitor), current_leaf, direction, _inner_gap, _work_area(monitor)
        )
        if target is None:
            return
        _focused_leaf[monitor] = target

    if win32gui.IsWindow(target.item):
        win32gui.SetForegroundWindow(target.item)


def _move_direction(direction):
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return
    monitor = _monitor_of(hwnd)

    with _lock:
        current_leaf = tree.find_leaf(_roots.get(monitor), hwnd)
        if current_leaf is None:
            return
        target = tree.find_direction_target(
            _roots.get(monitor), current_leaf, direction, _inner_gap, _work_area(monitor)
        )
        if target is None:
            return
        current_leaf.item, target.item = target.item, current_leaf.item
        _reflow(monitor)


def _resize(delta):
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return
    monitor = _monitor_of(hwnd)

    with _lock:
        leaf = tree.find_leaf(_roots.get(monitor), hwnd)
        if leaf is None or leaf.parent is None:
            return
        tree.resize(leaf, delta)
        _reflow(monitor)


def _reload(_data=None):
    """Re-reads inner_gap/outer_gap/cleanup_interval from config.json and
    reflows every monitor immediately so the change is visible right away."""
    global _inner_gap, _outer_gap, _cleanup_interval
    with _lock:
        settings = _load_settings()
        _inner_gap = settings["inner_gap"]
        _outer_gap = settings["outer_gap"]
        _cleanup_interval = settings["cleanup_interval"]
        _reflow_all()


def _toggle_fullscreen(_data=None):
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return
    monitor = _monitor_of(hwnd)

    with _lock:
        leaf = tree.find_leaf(_roots.get(monitor), hwnd)
        if leaf is None:
            return
        if _fullscreen_leaf.get(monitor) is leaf:
            del _fullscreen_leaf[monitor]
        else:
            _fullscreen_leaf[monitor] = leaf
        _reflow(monitor)


def _clamp_and_apply_ratio(container, index, neighbor_index, new_ratio):
    """Moves container.ratios[index] to new_ratio, taking the delta from
    neighbor_index, then renormalizes so all ratios sum to 1.0."""
    new_ratio = max(0.05, min(0.95, new_ratio))
    delta = new_ratio - container.ratios[index]
    if container.ratios[neighbor_index] - delta < 0.05:
        delta = container.ratios[neighbor_index] - 0.05
        new_ratio = container.ratios[index] + delta
    container.ratios[index] = new_ratio
    container.ratios[neighbor_index] -= delta
    total = sum(container.ratios)
    if total > 0:
        container.ratios = [r / total for r in container.ratios]


def _on_move_resize_end(hwnd):
    """WinEvent-driven fallback path - only actually runs the finalize logic
    for gestures that weren't already handled by _record_drag_kind (i.e.
    native OS drags that never went through the drag module). Skips if this
    hwnd was just finalized via IPC, since that path is authoritative and
    racing it against this WinEvent (whichever the OS delivers first) would
    make behavior nondeterministic."""
    with _lock:
        finalized_at = _recently_finalized.pop(hwnd, None)
    if finalized_at is not None and time.monotonic() - finalized_at < RECENTLY_FINALIZED_WINDOW:
        return
    _finalize_move_resize(hwnd, None)


def _finalize_move_resize(hwnd, kind):
    """Absorbs a manual resize into tree ratios so the new size is preserved.
    If only position changed (title-bar drag), snaps back to tile rect.
    kind is "move"/"resize" when known (from drag.py via IPC), or None to
    fall back to guessing from the size delta (native OS drags)."""
    with _lock:
        monitor, leaf = _find_leaf_any_monitor(hwnd)
        if leaf is None or _fullscreen_leaf.get(monitor) is leaf:
            return

        try:
            raw = win32gui.GetWindowRect(hwnd)
        except Exception:
            _reflow(monitor)
            return
        # Undo the same DWM frame-margin expansion _reflow applies, so this
        # compares like-for-like against the tree's margin-less logical rect.
        ml, mt, mr, mb = _frame_margins(hwnd)
        actual = (raw[0] + ml, raw[1] + mt, raw[2] - mr, raw[3] - mb)

        all_rects = tree.compute_all_rects(_roots.get(monitor), _work_area(monitor), _inner_gap)
        expected = all_rects.get(leaf)
        if expected is None:
            _reflow(monitor)
            return

        dw = (actual[2] - actual[0]) - (expected[2] - expected[0])
        dh = (actual[3] - actual[1]) - (expected[3] - expected[1])

        # Trust the button that was actually held over the size-delta guess;
        # only fall back to guessing when the gesture kind wasn't captured
        # (e.g. a native OS drag that didn't go through the drag module).
        is_move = kind == "move" if kind is not None else (abs(dw) <= 8 and abs(dh) <= 8)

        if is_move:
            cx, cy = win32api.GetCursorPos()
            dest_monitor = int(win32api.MonitorFromPoint((cx, cy), win32con.MONITOR_DEFAULTTONEAREST))
            cross = dest_monitor != monitor

            search_rects = (
                tree.compute_all_rects(_roots.get(dest_monitor), _work_area(dest_monitor), _inner_gap)
                if cross else all_rects
            )

            target = next(
                (n for n, r in search_rects.items()
                 if isinstance(n, tree.Leaf) and n is not leaf
                 and r[0] <= cx <= r[2] and r[1] <= cy <= r[3]),
                None,
            )

            if target is None and cross:
                # Dropped onto empty space on another monitor — insert at that monitor's focused tile
                hwnd = leaf.item
                _roots[monitor] = tree.remove(_roots[monitor], leaf)
                if _focused_leaf.get(monitor) is leaf:
                    _focused_leaf[monitor] = None
                _insert_hwnd(dest_monitor, hwnd)
            elif target is not None:
                tl, tt, tr, tb = search_rects[target]
                tw, th = tr - tl, tb - tt
                h_dist = min(cx - tl, tr - cx) / tw
                v_dist = min(cy - tt, tb - cy) / th

                if h_dist < 0.20 and h_dist <= v_dist:
                    axis, before = "horizontal", cx < tl + tw * 0.5
                elif v_dist < 0.20:
                    axis, before = "vertical", cy < tt + th * 0.5
                else:
                    axis, before = None, False

                if axis is None and not cross:
                    leaf.item, target.item = target.item, leaf.item
                else:
                    hwnd = leaf.item
                    _roots[monitor] = tree.remove(_roots[monitor], leaf)
                    if _focused_leaf.get(monitor) is leaf:
                        _focused_leaf[monitor] = None
                    if axis is None:
                        # Cross-monitor center drop: plain sibling insert, orientation from aspect ratio
                        _roots[dest_monitor], nl = tree.insert(
                            _roots[dest_monitor], target, hwnd, search_rects[target]
                        )
                    else:
                        parent = target.parent
                        if parent is not None and parent.orientation == axis:
                            _roots[dest_monitor], nl = tree.insert(
                                _roots[dest_monitor], target, hwnd, search_rects[target], before=before
                            )
                        else:
                            _roots[dest_monitor], nl = tree.insert_nested(
                                _roots[dest_monitor], target, hwnd, axis, before=before
                            )
                    _focused_leaf[dest_monitor] = nl

            _reflow(monitor)
            if cross:
                _reflow(dest_monitor)
            return

        parent = leaf.parent
        if parent is None or len(parent.children) < 2:
            _reflow(monitor)
            return

        parent_rect = all_rects.get(parent)
        if parent_rect is None:
            _reflow(monitor)
            return

        index = parent.children.index(leaf)
        n = len(parent.children)
        total_gap = _inner_gap * (n - 1)

        if parent.orientation == "horizontal" and abs(dw) > 8:
            available = (parent_rect[2] - parent_rect[0]) - total_gap
            if available > 0:
                new_ratio = (actual[2] - actual[0]) / available
                left_moved = abs(actual[0] - expected[0]) > 8
                nbr = (index - 1) if (left_moved and index > 0) else min(index + 1, n - 1)
                _clamp_and_apply_ratio(parent, index, nbr, new_ratio)
        elif parent.orientation == "vertical" and abs(dh) > 8:
            available = (parent_rect[3] - parent_rect[1]) - total_gap
            if available > 0:
                new_ratio = (actual[3] - actual[1]) / available
                top_moved = abs(actual[1] - expected[1]) > 8
                nbr = (index - 1) if (top_moved and index > 0) else min(index + 1, n - 1)
                _clamp_and_apply_ratio(parent, index, nbr, new_ratio)

        _reflow(monitor)


def _record_drag_kind(data):
    """Receives the ground-truth gesture kind directly from drag.py, since
    GetAsyncKeyState can't see the button it suppressed (see drag/daemon.py's
    _drag_loop comment). Runs the finalize logic immediately instead of just
    leaving a hint for the later WinEvent, so there's no race between this
    IPC message and that WinEvent over who processes the drag end first."""
    if not data:
        return
    hwnd = data.get("hwnd")
    kind = data.get("kind")
    if not hwnd or kind not in ("move", "resize"):
        return
    hwnd = int(hwnd)
    _finalize_move_resize(hwnd, kind)
    with _lock:
        _recently_finalized[hwnd] = time.monotonic()


ACTIONS = {
    "focus_left": lambda _data=None: _focus_direction("left"),
    "focus_right": lambda _data=None: _focus_direction("right"),
    "focus_up": lambda _data=None: _focus_direction("up"),
    "focus_down": lambda _data=None: _focus_direction("down"),
    "move_left": lambda _data=None: _move_direction("left"),
    "move_right": lambda _data=None: _move_direction("right"),
    "move_up": lambda _data=None: _move_direction("up"),
    "move_down": lambda _data=None: _move_direction("down"),
    "resize_grow": lambda _data=None: _resize(0.05),
    "resize_shrink": lambda _data=None: _resize(-0.05),
    "reload": _reload,
    "toggle_fullscreen": _toggle_fullscreen,
    "record_drag_kind": _record_drag_kind,
}


def _cleanup_loop():
    while True:
        time.sleep(_cleanup_interval)
        _prune_closed()


# --- WinEventHook wiring -----------------------------------------------------

EVENT_OBJECT_DESTROY = 0x8001
EVENT_OBJECT_SHOW = 0x8002
EVENT_OBJECT_NAMECHANGE = 0x800C
EVENT_SYSTEM_MOVESIZEEND = 0x000B
OBJID_WINDOW = 0
CHILDID_SELF = 0
WINEVENT_OUTOFCONTEXT = 0x0000

user32 = ctypes.windll.user32

WINEVENTPROC = ctypes.WINFUNCTYPE(
    None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
    ctypes.c_long, ctypes.c_long, wintypes.DWORD, wintypes.DWORD,
)
user32.SetWinEventHook.restype = wintypes.HANDLE
user32.SetWinEventHook.argtypes = [
    wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE, WINEVENTPROC,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
]
user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]


def _win_event_proc(hWinEventHook, event, hwnd, idObject, idChild, idEventThread, dwmsEventTime):
    if idObject != OBJID_WINDOW or idChild != CHILDID_SELF or not hwnd:
        return
    if event in (EVENT_OBJECT_SHOW, EVENT_OBJECT_NAMECHANGE):
        _on_window_shown(hwnd)
    elif event == EVENT_OBJECT_DESTROY:
        _on_window_destroyed(hwnd)
    elif event == EVENT_SYSTEM_MOVESIZEEND:
        _on_move_resize_end(hwnd)


def run():
    global _inner_gap, _outer_gap, _cleanup_interval

    # Without this, GetWindowRect/SetWindowPos/GetMonitorInfo see virtualized
    # (DPI-scaled) coordinates while DwmGetWindowAttribute's extended frame
    # bounds do not, so _frame_margins computes bogus margins and windows
    # drift outward into overlap - see drag/daemon.py's run() for the same fix.
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except (AttributeError, OSError):
        ctypes.windll.user32.SetProcessDPIAware()

    settings = _load_settings()
    _inner_gap = settings["inner_gap"]
    _outer_gap = settings["outer_gap"]
    _cleanup_interval = settings["cleanup_interval"]

    _bootstrap_existing_windows()

    threading.Thread(target=_cleanup_loop, name="oriel-tiling-cleanup", daemon=True).start()
    threading.Thread(
        target=serve_actions, args=("tiling", ACTIONS), name="oriel-tiling-ipc", daemon=True
    ).start()

    # Keep a reference so the ctypes callback isn't garbage-collected.
    win_event_proc = WINEVENTPROC(_win_event_proc)
    hook_show = user32.SetWinEventHook(
        EVENT_OBJECT_SHOW, EVENT_OBJECT_SHOW, None, win_event_proc, 0, 0, WINEVENT_OUTOFCONTEXT
    )
    hook_destroy = user32.SetWinEventHook(
        EVENT_OBJECT_DESTROY, EVENT_OBJECT_DESTROY, None, win_event_proc, 0, 0, WINEVENT_OUTOFCONTEXT
    )
    hook_namechange = user32.SetWinEventHook(
        EVENT_OBJECT_NAMECHANGE, EVENT_OBJECT_NAMECHANGE, None, win_event_proc, 0, 0, WINEVENT_OUTOFCONTEXT
    )
    hook_movesize = user32.SetWinEventHook(
        EVENT_SYSTEM_MOVESIZEEND, EVENT_SYSTEM_MOVESIZEEND, None, win_event_proc, 0, 0, WINEVENT_OUTOFCONTEXT
    )

    try:
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    finally:
        if hook_show:
            user32.UnhookWinEvent(hook_show)
        if hook_destroy:
            user32.UnhookWinEvent(hook_destroy)
        if hook_namechange:
            user32.UnhookWinEvent(hook_namechange)
        if hook_movesize:
            user32.UnhookWinEvent(hook_movesize)
