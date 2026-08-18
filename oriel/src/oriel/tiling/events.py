"""Event sourcing for the tiling daemon: WinEventHook wiring, the IPC
posted-event queue, and every handler that mutates TilingState.

Single-writer concurrency model: WinEvent callbacks are already delivered
on the thread that registered the hook (WINEVENT_OUTOFCONTEXT posts them
into that thread's message queue), so they're inherently serialized with
each other. IPC commands run on their own thread, so instead of calling
into TilingState directly from there (which is what caused a real race
between an IPC-delivered drag outcome and the WinEvent-driven fallback
guessing at the same thing), it calls post() to enqueue the work and wake
the message-loop thread, which drains and runs it there. That makes
TilingState single-writer by construction rather than by locking + careful
timing reasoning.
"""
import ctypes
import queue
import threading
import time
import logging
from ctypes import wintypes

import win32api
import win32con
import win32gui
import win32process

from oriel.config import get_section
from oriel.tiling import border
from oriel.tiling import geometry
from oriel.tiling import persistence
from oriel.tiling import policy
from oriel.tiling import tree
from oriel.tiling.filters import is_cloaked, is_manageable, load_ignore_rules
from oriel.tiling.state import DEFAULT_BORDER, DEFAULT_GAP, DEFAULT_OUTER_GAP, DEFAULT_RESIZE_STEP, DEFAULT_WORKSPACE

logger = logging.getLogger(__name__)

# How long a hwnd stays in _recently_finalized after record_drag_kind handles
# it, so the WinEvent-driven fallback for the very same drag knows to skip
# instead of redundantly re-processing it. Single-threading (below) removes
# the *race* between these two paths, but both still fire for one logical
# gesture end, so this dedup is still needed - it just no longer has to win
# a timing race to be correct.
RECENTLY_FINALIZED_WINDOW = 2.0

# Bounded retry for the same race recheck_if_pending covers below - kept as
# a backstop, NOT redundant with it: a window that fails is_manageable once
# and then never generates another LOCATIONCHANGE/FOREGROUND (e.g. a second
# window opens right after and steals focus, and the first settles into a
# static position) would otherwise be missed forever. Verified this actually
# happens live - removing this timer caused a real, reproducible miss, not
# just a hypothetical one.
MAX_MANAGEABLE_RETRIES = 5
RETRY_INTERVAL = 0.15

_state = None
_recently_finalized = {}

# hwnds that failed is_manageable() at least once and are waiting to be
# rechecked (see recheck_if_pending) - Firefox in particular fires
# SHOW/NAMECHANGE before finishing its own window styling, so the very first
# check can genuinely be too early. Cleaned up in on_window_destroyed so this
# can't grow unbounded for windows that are never actually manageable.
_pending_manageable_hwnds = set()
_manageable_retry_counts = {}
_manageable_retry_scheduled = set()

# hwnds currently inside a bracketed EVENT_SYSTEM_MOVESIZESTART/END gesture
# (native OS drag or drag.py's custom alt+drag - both emit this bracket) -
# see enforce_tiled_placement, which must never fight a real gesture.
_active_gestures = set()

# hwnd currently outlined by the focus border, or None - lets LOCATIONCHANGE
# cheaply skip re-evaluating the border for the many unrelated windows that
# fire it, only reacting when the bordered window itself moves/resizes.
_bordered_hwnd = None

# --- Posted-event queue (IPC thread -> message-loop thread) ------------------

WM_APP_EVENT = 0x8000 + 1  # WM_APP + 1
_event_queue = queue.Queue()
_main_thread_id = None


def configure(state):
    global _state
    _state = state


def post(handler, *args):
    """Thread-safe: enqueues a call to run on the single message-loop thread
    instead of the calling thread, and wakes that thread's message loop so
    it's processed promptly instead of waiting for the next WinEvent."""
    _event_queue.put((handler, args))
    if _main_thread_id is not None:
        ctypes.windll.user32.PostThreadMessageW(_main_thread_id, WM_APP_EVENT, 0, 0)


def _drain_posted_events():
    while True:
        try:
            handler, args = _event_queue.get_nowait()
        except queue.Empty:
            return
        try:
            handler(*args)
        except Exception:
            logger.exception("posted event handler %s failed", getattr(handler, "__name__", handler))


# --- Settings ----------------------------------------------------------------

def _hex_to_colorref(hex_color):
    """'#rrggbb' -> a Win32 COLORREF int (0x00BBGGRR - reversed byte order
    from the hex string's RRGGBB)."""
    value = int(hex_color.lstrip("#"), 16)
    r, g, b = (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF
    return r | (g << 8) | (b << 16)


def _load_settings():
    tiling = get_section("tiling")
    return {
        "inner_gap": tiling.get("inner_gap", DEFAULT_GAP),
        "outer_gap": {**DEFAULT_OUTER_GAP, **tiling.get("outer_gap", {})},
        "resize_step": tiling.get("resize_step", DEFAULT_RESIZE_STEP),
        "workspaces": tiling.get("workspaces", {}),
        "border": {**DEFAULT_BORDER, **tiling.get("border", {})},
    }


def apply_initial_settings():
    settings = _load_settings()
    _state.inner_gap = settings["inner_gap"]
    _state.outer_gap = settings["outer_gap"]
    _state.resize_step = settings["resize_step"]
    _state.workspaces = settings["workspaces"]
    _state.border = settings["border"]
    load_ignore_rules()


def reload_settings(_data=None):
    """Re-reads inner_gap/outer_gap/resize_step/border/ignore_rules from
    config.json and reflows every monitor immediately so the change is
    visible right away."""
    old_workspaces = _state.workspaces
    settings = _load_settings()
    _state.inner_gap = settings["inner_gap"]
    _state.outer_gap = settings["outer_gap"]
    _state.resize_step = settings["resize_step"]
    _state.workspaces = settings["workspaces"]
    _state.border = settings["border"]
    load_ignore_rules()
    _migrate_newly_configured_monitors(old_workspaces)
    _state.reflow_all()
    update_focus_border()


def _migrate_newly_configured_monitors(old_workspaces_config):
    """A monitor that just went from unconfigured to configured via this
    reload has all its existing windows sitting at DEFAULT_WORKSPACE, which
    no hotkey can reach once real workspaces exist - move them to
    workspace 1 (the new active default) instead of stranding them there."""
    for monitor in _state.known_monitors():
        stable_id = geometry.stable_monitor_id(monitor)
        was_configured = stable_id is not None and old_workspaces_config.get(stable_id, 0) > 0
        if _state.workspace_count(monitor) > 0 and not was_configured:
            _state.migrate_workspace(monitor, DEFAULT_WORKSPACE, 1)
            _state.set_active_workspace(monitor, 1)
            _persist_workspace_state(monitor)


def reflow_all(_data=None):
    """Recomputes every monitor's layout without touching settings - for
    triggers that change available work area without changing config, e.g.
    oriel.taskbar's "reflow" notification whenever it hides/shows the
    taskbar."""
    _state.reflow_all()


# --- Focus border --------------------------------------------------------------

def update_focus_border():
    """Applies the native DWM accent-border + corner-rounding highlight to
    whichever window tiling currently considers focused, clearing it from
    whichever window had it before - cleared entirely if the new foreground
    window isn't one of oriel's tiled windows (untracked, or on a workspace
    that isn't currently active on its monitor). Always unconditionally
    re-applies rather than skipping when "nothing changed" per internal
    tracking - enforce_tiled_placement's SetWindowPos calls (which reposition
    a tiled window back onto its tile on every focus/location change) can
    silently reset DWM's border attribute as a side effect, so trusting
    _bordered_hwnd alone isn't reliable; re-asserting is cheap and safe."""
    global _bordered_hwnd
    highlight = None
    if _state.border.get("enabled", True):
        fg = win32gui.GetForegroundWindow()
        if fg and win32gui.IsWindow(fg):
            monitor, workspace, leaf = _state.find_leaf_any_monitor(fg)
            if leaf is not None and workspace == _state.active_workspace(monitor):
                highlight = fg

    if _bordered_hwnd is not None and _bordered_hwnd != highlight and win32gui.IsWindow(_bordered_hwnd):
        border.clear_border(_bordered_hwnd)
    if highlight is not None:
        border.set_border(highlight, _hex_to_colorref(_state.border["color"]), _state.border["corner_style"])
    _bordered_hwnd = highlight


# --- Window lifecycle ---------------------------------------------------------

def bootstrap_existing_windows():
    persisted = persistence.load()
    for handle, _hdc, _rect in win32api.EnumDisplayMonitors():
        monitor = int(handle)
        entry = persistence.entry_for(monitor, persisted)
        if entry is not None:
            _state.set_active_workspace(monitor, entry.get("active", DEFAULT_WORKSPACE))

    handles = []

    def callback(hwnd, _):
        handles.append(hwnd)
        return True

    win32gui.EnumWindows(callback, None)

    # Reverse Z-order (bottom-most first) so the most-recently-focused
    # window ends up last-inserted, roughly matching what you'd expect to
    # see "on top" of the initial layout.
    for hwnd in reversed(handles):
        if is_manageable(hwnd):
            monitor = geometry.monitor_of(hwnd)
            entry = persistence.entry_for(monitor, persisted)
            workspace = entry.get("windows", {}).get(str(hwnd)) if entry else None
            if workspace is None:
                workspace = _state.active_workspace(monitor)
            _state.insert_hwnd(monitor, hwnd, workspace)
    _state.reflow_all()
    update_focus_border()


# --- Display change handling ---------------------------------------------------

DISPLAY_CHANGE_WATCHER_CLASS = "OrielDisplayChangeWatcher"
WM_DISPLAYCHANGE = 0x007E
DISPLAY_CHANGE_DEBOUNCE = 0.5  # coalesces bursts of WM_DISPLAYCHANGE while a resolution change settles

_display_change_timer = None


def _display_watcher_wnd_proc(hwnd, msg, wparam, lparam):
    if msg == WM_DISPLAYCHANGE:
        _schedule_display_resync()
        return 0
    return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)


def create_display_change_watcher():
    """A hidden, never-shown top-level window purely to receive
    WM_DISPLAYCHANGE - that message is sent to top-level windows, not
    broadcast to threads without one, so oriel needs an actual (invisible)
    window to see it at all. Must be created on the message-loop thread -
    see run_message_loop()."""
    wc = win32gui.WNDCLASS()
    wc.lpfnWndProc = _display_watcher_wnd_proc
    wc.lpszClassName = DISPLAY_CHANGE_WATCHER_CLASS
    win32gui.RegisterClass(wc)
    return win32gui.CreateWindow(
        DISPLAY_CHANGE_WATCHER_CLASS, None, 0, 0, 0, 0, 0, 0, 0, win32api.GetModuleHandle(None), None,
    )


def _schedule_display_resync():
    global _display_change_timer
    if _display_change_timer is not None:
        _display_change_timer.cancel()
    _display_change_timer = threading.Timer(DISPLAY_CHANGE_DEBOUNCE, lambda: post(resync_after_display_change))
    _display_change_timer.start()


def resync_after_display_change(_data=None):
    """A monitor was added/removed, or its resolution/arrangement changed.
    HMONITOR handles can go stale or shift identity across this, and any
    window that was already open (not freshly shown) never generates a new
    on_window_shown to be rediscovered against the new geometry - so this
    rebuilds tiling state from scratch, exactly like restarting the tiling
    daemon would fix, without actually restarting the process."""
    geometry.invalidate_display_caches()
    _state.reset()
    bootstrap_existing_windows()


def on_window_shown(hwnd):
    _monitor, _workspace, existing = _state.find_leaf_any_monitor(hwnd)
    if existing is not None:
        return
    if not is_manageable(hwnd):
        _pending_manageable_hwnds.add(hwnd)
        _maybe_retry_window_shown(hwnd)
        return
    _pending_manageable_hwnds.discard(hwnd)
    _manageable_retry_counts.pop(hwnd, None)
    # Newly opened windows go to whichever monitor the cursor is on, not
    # wherever Windows happened to place the window initially.
    monitor = geometry.monitor_at_cursor()
    workspace = _state.active_workspace(monitor)
    _state.insert_hwnd(monitor, hwnd, workspace)
    _state.reflow(monitor, workspace)
    _persist_workspace_state(monitor)
    update_focus_border()


def _maybe_retry_window_shown(hwnd):
    if not win32gui.IsWindow(hwnd) or hwnd in _manageable_retry_scheduled:
        return
    count = _manageable_retry_counts.get(hwnd, 0)
    if count >= MAX_MANAGEABLE_RETRIES:
        _manageable_retry_counts.pop(hwnd, None)
        return
    _manageable_retry_counts[hwnd] = count + 1
    _manageable_retry_scheduled.add(hwnd)

    def _fire():
        _manageable_retry_scheduled.discard(hwnd)
        post(on_window_shown, hwnd)

    threading.Timer(RETRY_INTERVAL, _fire).start()


def recheck_if_pending(hwnd):
    """EVENT_OBJECT_LOCATIONCHANGE and EVENT_SYSTEM_FOREGROUND are real
    signals that a window is still settling its own setup (still moving
    itself) or has just become genuinely usable (gained focus) - GlazeWM's
    own Windows backend listens to both of these for this same reason.
    Only acts on hwnds already known-pending (already failed is_manageable
    at least once), so this adds no cost to window activity in general."""
    if hwnd in _pending_manageable_hwnds:
        on_window_shown(hwnd)


def enforce_tiled_placement(hwnd):
    """oriel owns geometry for every tiled window at all times - apps don't
    get to manage their own bounds. If a tiled window's real monitor or rect
    ever drifts from what its tree leaf expects (an app restoring its own
    remembered position/size, or any other non-drag repositioning), snap it
    straight back. The only exception is an active move/resize gesture
    (tracked in _active_gestures), so a deliberate user drag is never
    fought. Runs on every EVENT_OBJECT_LOCATIONCHANGE/EVENT_SYSTEM_FOREGROUND,
    but is a cheap no-op for any hwnd oriel isn't tiling."""
    if hwnd in _active_gestures:
        return
    monitor, workspace, leaf = _state.find_leaf_any_monitor(hwnd)
    if leaf is None:
        return
    if _state.fullscreen_leaf(monitor, workspace) is leaf:
        return
    if geometry.monitor_of(hwnd) != monitor:
        _state.reflow(monitor, workspace)
        return
    expected = _state.compute_rects(monitor, workspace).get(leaf)
    if expected is None:
        return
    expected = geometry.expand_rect_for_frame(expected, hwnd)
    try:
        actual = win32gui.GetWindowRect(hwnd)
    except win32gui.error:
        return
    if actual == expected:
        return
    actual_w, actual_h = actual[2] - actual[0], actual[3] - actual[1]
    expected_w, expected_h = expected[2] - expected[0], expected[3] - expected[1]
    if actual_w > expected_w or actual_h > expected_h:
        # Clamped bigger than requested - an enforced minimum size. If this
        # doesn't teach us anything new, reflowing again would only ask for
        # the exact same impossible size and get clamped the exact same
        # way - stop here instead of fighting forever (task 13).
        if not geometry.learn_min_size(hwnd, actual_w, actual_h):
            return
    _state.reflow(monitor, workspace)


def on_window_destroyed(hwnd):
    _pending_manageable_hwnds.discard(hwnd)
    _manageable_retry_counts.pop(hwnd, None)
    _active_gestures.discard(hwnd)
    monitor, workspace, leaf = _state.find_leaf_any_monitor(hwnd)
    if leaf is None:
        return
    _state.remove_leaf(monitor, leaf, workspace)
    _state.reflow(monitor, workspace)
    _persist_workspace_state(monitor)
    if hwnd == _bordered_hwnd:
        update_focus_border()


def on_window_hidden(hwnd):
    """A window was hidden (SW_HIDE) or DWM-cloaked (moved to another
    virtual desktop) without being destroyed. Unmanage it the same way a
    destroyed window is, but EVENT_OBJECT_SHOW/UNCLOAKED can re-manage it
    later via on_window_shown - without this, a hidden/cloaked window was a
    permanent "ghost tile", consuming layout space for a window nobody can
    see, with no cleanup path at all."""
    # Guard against stale/out-of-order notifications - a hide/cloak event
    # can still arrive after the window already became visible again, so
    # only actually unmanage once native state confirms it's still hidden.
    if win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd) and not is_cloaked(hwnd):
        return
    monitor, workspace, leaf = _state.find_leaf_any_monitor(hwnd)
    if leaf is None:
        return
    if workspace != _state.active_workspace(monitor):
        return  # hidden because its workspace isn't active right now - our own switch-away, not a real hide
    _state.remove_leaf(monitor, leaf, workspace)
    _state.reflow(monitor, workspace)
    _persist_workspace_state(monitor)
    if hwnd == _bordered_hwnd:
        update_focus_border()


# --- Focus / move / resize hotkey commands ------------------------------------

def _force_foreground(target_hwnd):
    """SetForegroundWindow silently fails (Windows' foreground-lock
    restriction) when called from a background process that didn't itself
    receive the triggering input - exactly this daemon's situation, since
    hotkeyd receives the real keypress and forwards it over IPC. Same
    AttachThreadInput trick drag.py's _force_foreground already proved."""
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


def focus_direction(direction):
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return
    monitor, workspace, current_leaf = _state.find_leaf_any_monitor(hwnd)
    if current_leaf is None:
        return

    target = tree.find_direction_target(
        _state.root(monitor, workspace), current_leaf, direction, _state.inner_gap, _state.work_area(monitor)
    )
    if target is None:
        return
    _state.set_focused_leaf(monitor, target, workspace)

    if win32gui.IsWindow(target.item):
        _force_foreground(target.item)
        update_focus_border()


def move_direction(direction):
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return
    monitor, workspace, current_leaf = _state.find_leaf_any_monitor(hwnd)
    if current_leaf is None:
        return
    target = tree.find_direction_target(
        _state.root(monitor, workspace), current_leaf, direction, _state.inner_gap, _state.work_area(monitor)
    )
    if target is None:
        return
    current_leaf.item, target.item = target.item, current_leaf.item
    _state.reflow(monitor, workspace)


def resize(delta):
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return
    monitor, workspace, leaf = _state.find_leaf_any_monitor(hwnd)
    if leaf is None or leaf.parent is None:
        return
    tree.resize(leaf, delta)
    _state.reflow(monitor, workspace)


def resize_grow(_data=None):
    resize(_state.resize_step)


def resize_shrink(_data=None):
    resize(-_state.resize_step)


def toggle_fullscreen(_data=None):
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return
    monitor, workspace, leaf = _state.find_leaf_any_monitor(hwnd)
    if leaf is None:
        return
    if _state.fullscreen_leaf(monitor, workspace) is leaf:
        _state.clear_fullscreen_leaf(monitor, workspace)
    else:
        _state.set_fullscreen_leaf(monitor, leaf, workspace)
    _state.reflow(monitor, workspace)
    update_focus_border()


# --- Workspaces ----------------------------------------------------------------

def _persist_workspace_state(monitor):
    if _state.workspace_count(monitor) > 0:
        persistence.save_monitor(_state, monitor)


def _monitor_for_workspace_switch():
    """Which monitor a workspace hotkey should act on: the focused window's
    monitor if there is one, else whichever monitor the cursor is on."""
    hwnd = win32gui.GetForegroundWindow()
    if hwnd:
        return geometry.monitor_of(hwnd)
    return geometry.monitor_at_cursor()


def switch_workspace(monitor, target_workspace):
    """Hides every window on monitor's currently active workspace and shows
    target_workspace's, without touching tree structure at all - a
    workspace switch is a visibility change, not a hide/destroy, so each
    workspace's layout survives switching away from it untouched."""
    current_workspace = _state.active_workspace(monitor)
    if target_workspace == current_workspace or target_workspace > _state.workspace_count(monitor):
        return

    for leaf in tree.all_leaves(_state.root(monitor, current_workspace)):
        if win32gui.IsWindow(leaf.item):
            win32gui.ShowWindow(leaf.item, win32con.SW_HIDE)

    _state.set_active_workspace(monitor, target_workspace)

    for leaf in tree.all_leaves(_state.root(monitor, target_workspace)):
        if win32gui.IsWindow(leaf.item):
            win32gui.ShowWindow(leaf.item, win32con.SW_SHOWNA)
    _state.reflow(monitor, target_workspace)

    focused = _state.focused_leaf(monitor, target_workspace)
    if focused is not None and win32gui.IsWindow(focused.item):
        _force_foreground(focused.item)

    _persist_workspace_state(monitor)
    update_focus_border()


def switch_workspace_action(data):
    if not data or "workspace" not in data:
        return
    switch_workspace(_monitor_for_workspace_switch(), data["workspace"])


def move_to_workspace(target_workspace):
    """Reassigns the focused window to target_workspace on its own monitor.
    The view stays on the current workspace - the window just disappears,
    matching i3's default "move, don't follow" behavior."""
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return
    monitor, current_workspace, leaf = _state.find_leaf_any_monitor(hwnd)
    if leaf is None or current_workspace == target_workspace or target_workspace > _state.workspace_count(monitor):
        return

    _state.remove_leaf(monitor, leaf, current_workspace)
    _state.insert_hwnd(monitor, hwnd, target_workspace)
    _state.reflow(monitor, current_workspace)
    _state.reflow(monitor, target_workspace)
    win32gui.ShowWindow(hwnd, win32con.SW_HIDE)

    _persist_workspace_state(monitor)
    update_focus_border()


def move_to_workspace_action(data):
    if not data or "workspace" not in data:
        return
    move_to_workspace(data["workspace"])


# --- Move/resize gesture finalize --------------------------------------------

def finalize_move_resize(hwnd, kind):
    """Absorbs a manual resize into tree ratios so the new size is preserved.
    If only position changed, treats it as a move (swap/insert/snap back).
    kind is "move"/"resize" when known (from drag.py via IPC), or None to
    fall back to guessing from the size delta (native OS drags)."""
    monitor, workspace, leaf = _state.find_leaf_any_monitor(hwnd)
    if leaf is None or _state.fullscreen_leaf(monitor, workspace) is leaf:
        return

    try:
        raw = win32gui.GetWindowRect(hwnd)
    except Exception:
        _state.reflow(monitor, workspace)
        return
    actual = geometry.shrink_rect_for_frame(raw, hwnd)

    all_rects = tree.compute_all_rects(_state.root(monitor, workspace), _state.work_area(monitor), _state.inner_gap)
    expected = all_rects.get(leaf)
    if expected is None:
        _state.reflow(monitor, workspace)
        return

    if policy.is_move_gesture(kind, actual, expected):
        cursor_pos = win32api.GetCursorPos()
        dest_monitor = geometry.monitor_at_point(cursor_pos)
        cross = dest_monitor != monitor

        search_rects = (
            tree.compute_all_rects(_state.root(dest_monitor, workspace), _state.work_area(dest_monitor), _state.inner_gap)
            if cross else all_rects
        )

        outcome = policy.decide_move(leaf, monitor, dest_monitor, cursor_pos, search_rects)
    else:
        outcome = policy.decide_resize(leaf, actual, expected, all_rects, _state.inner_gap)

    dirty = _state.apply_outcome(monitor, leaf, outcome, workspace)
    for dirty_monitor, dirty_workspace in dirty:
        _state.reflow(dirty_monitor, dirty_workspace)


def on_move_resize_end(hwnd):
    """WinEvent-driven fallback path - only actually runs the finalize logic
    for gestures that weren't already handled by record_drag_kind (i.e.
    native OS drags that never went through the drag module). Skips if this
    hwnd was just finalized via IPC, since that path is authoritative and
    (before single-threading this) racing it against this WinEvent made
    behavior nondeterministic."""
    _active_gestures.discard(hwnd)
    finalized_at = _recently_finalized.pop(hwnd, None)
    if finalized_at is not None and time.monotonic() - finalized_at < RECENTLY_FINALIZED_WINDOW:
        return
    finalize_move_resize(hwnd, None)


def on_move_resize_start(hwnd):
    _active_gestures.add(hwnd)


def record_drag_kind(data):
    """Receives the ground-truth gesture kind directly from drag.py, since
    GetAsyncKeyState can't see the button it suppressed (see drag/daemon.py's
    _drag_loop comment). Runs the finalize logic immediately instead of just
    leaving a hint for the later WinEvent to maybe pick up."""
    if not data:
        return
    hwnd = data.get("hwnd")
    kind = data.get("kind")
    if not hwnd or kind not in ("move", "resize"):
        return
    hwnd = int(hwnd)
    _active_gestures.discard(hwnd)
    finalize_move_resize(hwnd, kind)
    _recently_finalized[hwnd] = time.monotonic()


# --- WinEventHook wiring -------------------------------------------------------

EVENT_OBJECT_DESTROY = 0x8001
EVENT_OBJECT_SHOW = 0x8002
EVENT_OBJECT_NAMECHANGE = 0x800C
EVENT_OBJECT_LOCATIONCHANGE = 0x800B
EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_MOVESIZESTART = 0x000A
EVENT_SYSTEM_MOVESIZEEND = 0x000B
EVENT_OBJECT_HIDE = 0x8003
EVENT_OBJECT_CLOAKED = 0x8017
EVENT_OBJECT_UNCLOAKED = 0x8018
OBJID_WINDOW = 0
CHILDID_SELF = 0
WINEVENT_OUTOFCONTEXT = 0x0000

# Other events GlazeWM's Windows backend also hooks (see
# packages/wm-platform/src/platform_impl/windows/window_listener.rs) that we
# don't currently need but may want later:
#   EVENT_SYSTEM_MINIMIZESTART (0x0016) - window minimized
#   EVENT_SYSTEM_MINIMIZEEND (0x0017)   - window restored from minimized

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
    try:
        if event in (EVENT_OBJECT_SHOW, EVENT_OBJECT_NAMECHANGE, EVENT_OBJECT_UNCLOAKED):
            on_window_shown(hwnd)
        elif event == EVENT_OBJECT_DESTROY:
            on_window_destroyed(hwnd)
        elif event in (EVENT_OBJECT_HIDE, EVENT_OBJECT_CLOAKED):
            on_window_hidden(hwnd)
        elif event == EVENT_SYSTEM_MOVESIZEEND:
            on_move_resize_end(hwnd)
        elif event == EVENT_SYSTEM_MOVESIZESTART:
            on_move_resize_start(hwnd)
        elif event == EVENT_SYSTEM_FOREGROUND:
            recheck_if_pending(hwnd)
            enforce_tiled_placement(hwnd)
            update_focus_border()
        elif event == EVENT_OBJECT_LOCATIONCHANGE:
            recheck_if_pending(hwnd)
            enforce_tiled_placement(hwnd)
            if hwnd == _bordered_hwnd:
                update_focus_border()
    except Exception:
        logger.exception("WinEvent handler failed for event=%s hwnd=%s", event, hwnd)


def run_message_loop():
    """Registers the WinEventHooks and pumps messages until WM_QUIT. Must be
    called from the same thread that will own all TilingState mutations -
    the IPC thread reaches this thread only via post()."""
    global _main_thread_id
    _main_thread_id = win32api.GetCurrentThreadId()

    create_display_change_watcher()

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
    hook_movesize_start = user32.SetWinEventHook(
        EVENT_SYSTEM_MOVESIZESTART, EVENT_SYSTEM_MOVESIZESTART, None, win_event_proc, 0, 0, WINEVENT_OUTOFCONTEXT
    )
    hook_locationchange = user32.SetWinEventHook(
        EVENT_OBJECT_LOCATIONCHANGE, EVENT_OBJECT_LOCATIONCHANGE, None, win_event_proc, 0, 0, WINEVENT_OUTOFCONTEXT
    )
    hook_foreground = user32.SetWinEventHook(
        EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, None, win_event_proc, 0, 0, WINEVENT_OUTOFCONTEXT
    )
    hook_hide = user32.SetWinEventHook(
        EVENT_OBJECT_HIDE, EVENT_OBJECT_HIDE, None, win_event_proc, 0, 0, WINEVENT_OUTOFCONTEXT
    )
    hook_cloaked = user32.SetWinEventHook(
        EVENT_OBJECT_CLOAKED, EVENT_OBJECT_CLOAKED, None, win_event_proc, 0, 0, WINEVENT_OUTOFCONTEXT
    )
    hook_uncloaked = user32.SetWinEventHook(
        EVENT_OBJECT_UNCLOAKED, EVENT_OBJECT_UNCLOAKED, None, win_event_proc, 0, 0, WINEVENT_OUTOFCONTEXT
    )

    try:
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == WM_APP_EVENT:
                _drain_posted_events()
                continue
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
        if hook_movesize_start:
            user32.UnhookWinEvent(hook_movesize_start)
        if hook_locationchange:
            user32.UnhookWinEvent(hook_locationchange)
        if hook_foreground:
            user32.UnhookWinEvent(hook_foreground)
        if hook_hide:
            user32.UnhookWinEvent(hook_hide)
        if hook_cloaked:
            user32.UnhookWinEvent(hook_cloaked)
        if hook_uncloaked:
            user32.UnhookWinEvent(hook_uncloaked)
