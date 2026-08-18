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
from oriel.tiling.filters import could_become_manageable, is_cloaked, is_manageable, load_ignore_rules
from oriel.tiling.state import DEFAULT_BORDER, DEFAULT_GAP, DEFAULT_OUTER_GAP, DEFAULT_RESIZE_STEP, DEFAULT_WORKSPACE

logger = logging.getLogger(__name__)

# Bounded retry for the same race recheck_if_pending covers below - kept as
# a backstop, NOT redundant with it: a window that fails is_manageable once
# and then never generates another LOCATIONCHANGE/FOREGROUND (e.g. a second
# window opens right after and steals focus, and the first settles into a
# static position) would otherwise be missed forever. Verified this actually
# happens live - removing this timer caused a real, reproducible miss, not
# just a hypothetical one. Driven by one shared, message-loop-owned SetTimer
# (see run_message_loop/_tick_manageable_retries), not a threading.Timer per
# attempt - a burst of ephemeral windows (e.g. autocomplete popups) each
# failing is_manageable() once used to spawn a new OS thread per retry.
MAX_MANAGEABLE_RETRIES = 5
RETRY_INTERVAL = 0.15

# How long a hide/cloak notification has to keep looking real before it's
# actually acted on (see on_window_hidden/_tick_pending_hides) - live-
# confirmed some apps (e.g. Windows Terminal) get briefly, spuriously
# cloaked/uncloaked during fast focus switching between two of their own
# windows, well under this window. Reacting immediately removed the tile
# and re-inserted it moments later, visible as the border (and the tile
# itself) flickering out and back for no user-visible reason.
HIDE_DEBOUNCE_SECONDS = 0.2

_state = None

# hwnd -> retry count, for hwnds that failed is_manageable() at least once
# and are waiting to be rechecked (see recheck_if_pending/
# _tick_manageable_retries) - Firefox in particular fires SHOW/NAMECHANGE
# before finishing its own window styling, so the very first check can
# genuinely be too early. Cleaned up in on_window_destroyed so this can't
# grow unbounded for windows that are never actually manageable.
_manageable_retries = {}

# hwnd -> monotonic timestamp of its most recent hide/cloak notification,
# for hwnds waiting out HIDE_DEBOUNCE_SECONDS before on_window_hidden's
# effect is actually applied (see _tick_pending_hides). Cleaned up in
# on_window_destroyed.
_pending_hides = {}

# hwnds currently inside a bracketed EVENT_SYSTEM_MOVESIZESTART/END gesture
# (native OS drag or drag.py's custom alt+drag - both emit this bracket) -
# see enforce_tiled_placement, which must never fight a real gesture.
_active_gestures = set()

# hwnd -> list of monotonic timestamps of recent enforce_tiled_placement
# reflow attempts, pruned to the last ENFORCE_WINDOW_SECONDS. Time-windowed
# rather than a simple consecutive-mismatch counter reset on any match: an
# app that repeatedly restores its own preferred size right after each
# corrective reflow (confirmed live - Firefox growing back by a fixed +41px
# every ~50-90ms, indefinitely) briefly LANDS at the correct rect right
# after each correction, which would reset a consecutive-count-based give-up
# back to zero forever, never actually triggering. Counting attempts within
# a rolling time window instead can't be defeated by that alternation.
# Cleaned up in on_window_destroyed.
MAX_ENFORCE_ATTEMPTS = 1
ENFORCE_WINDOW_SECONDS = 2.0
_enforce_attempt_times = {}

# hwnds that have already had their one allowed frame-margin re-validation
# (see enforce_tiled_placement) - a brand-new window's very first
# frame_margins() query can catch DWM before its extended frame bounds have
# settled to their true value (confirmed live: Firefox cached a stale
# top-margin from an early read, permanently offsetting it from its correct
# position, since frame_margins() is cached forever otherwise). Re-query
# EXACTLY ONCE, the first time a real mismatch is observed for this hwnd -
# tied to an actual signal that something might be off, not a fixed delay
# (a one-shot timer-based retry fired before DWM had actually settled).
# Bounded to once per hwnd so this can't become the same reactive-requery-
# during-an-active-fight loop that caused the earlier Firefox width
# oscillation. Cleaned up in on_window_destroyed.
_margin_revalidated = set()

# hwnd currently outlined by the focus border, or None - lets LOCATIONCHANGE
# cheaply skip re-evaluating the border for the many unrelated windows that
# fire it, only reacting when the bordered window itself moves/resizes.
_bordered_hwnd = None

# Single persistent worker (not a fresh threading.Thread per call, and not
# a plain FIFO queue either) applying border-effect work - DwmSetWindowAttribute
# has no async variant and is a cross-process RPC to the DWM compositor, so
# it must never run on the message-loop thread (see update_focus_border). A
# FIFO queue isn't right because a burst of rapid calls (e.g. a window's
# open animation firing many LOCATIONCHANGE events) would queue up a
# growing backlog of blocking DWM calls behind each other, reintroducing
# the freeze. Instead only the LATEST desired (highlight, color,
# corner_style) is ever kept - producers overwrite it and never block, and
# the worker applies whatever is newest whenever it's free, coalescing away
# any superseded intermediate states.
#
# Deliberately NOT tracking which hwnd to clear in the pending tuple - an
# earlier version had producers precompute that too (relative to
# _bordered_hwnd at enqueue time), but that's unsound under coalescing: if
# focus chains through several windows before the worker wakes (e.g.
# C -> A -> B -> A while the worker is mid-throttle-sleep), only the final
# pending entry survives, so an intermediate "clear C" can be coalesced
# away entirely even though C's real border was never actually cleared in
# DWM - live-confirmed as two windows simultaneously showing a border after
# rapid focus switching. Instead the worker tracks applied_hwnd itself
# (below), reflecting only what it has actually told DWM, immune to
# coalescing, and always clears relative to that.
_border_condition = threading.Condition()
_border_pending = None
_border_pending_valid = False

# Coalescing alone isn't enough during a burst (e.g. a window's open
# animation firing many LOCATIONCHANGE events): DWM itself is the
# contended resource this whole feature fights (the original freeze was
# DWM already busy compositing a slow-starting app), so even one thread
# issuing back-to-back blocking DwmSetWindowAttribute/SetWindowPos calls as
# fast as each returns can still pile onto that same contention and
# reintroduce the freeze - reducing thread/queue count doesn't cap *call
# rate*. Spacing consecutive applies apart bounds how often the worker
# hits DWM regardless of how fast events arrive, while still coalescing to
# whatever's newest each time it wakes.
_BORDER_MIN_INTERVAL_SECONDS = 0.15


def _border_worker():
    global _border_pending, _border_pending_valid
    applied_hwnd = None  # only this thread ever writes this - reflects real DWM state
    last_applied = 0.0
    while True:
        with _border_condition:
            while not _border_pending_valid:
                _border_condition.wait()
            highlight, color, corner_style = _border_pending
            _border_pending_valid = False

        remaining = _BORDER_MIN_INTERVAL_SECONDS - (time.monotonic() - last_applied)
        if remaining > 0:
            time.sleep(remaining)
            with _border_condition:
                if _border_pending_valid:
                    # something newer arrived while throttling - apply that instead
                    highlight, color, corner_style = _border_pending
                    _border_pending_valid = False

        try:
            if applied_hwnd is not None and applied_hwnd != highlight and win32gui.IsWindow(applied_hwnd):
                border.clear_border(applied_hwnd)
            if highlight is not None:
                # Always reapplied, even when unchanged from last time (not
                # gated on applied_hwnd != highlight) - DwmSetWindowAttribute's
                # HRESULT is never checked (see _set_attribute) and even a
                # successful call doesn't guarantee DWM actually repaints the
                # frame (see _force_frame_redraw's own comment) - live-
                # confirmed the exact failure this causes: the decision and
                # bookkeeping were both provably correct (GetForegroundWindow()
                # and the last logged apply agreed), yet no border was
                # visible, with nothing left to ever retry it since our own
                # state thought it had already succeeded. Reapplying on every
                # tick (~every RETRY_INTERVAL while something is focused) self
                # -heals that silent failure within one cycle instead of
                # leaving it stuck until the next real focus change.
                border.set_border(highlight, color, corner_style)
            applied_hwnd = highlight
        except Exception:
            logger.exception("border worker failed for applied=%s highlight=%s", applied_hwnd, highlight)
        last_applied = time.monotonic()


threading.Thread(target=_border_worker, daemon=True).start()

# hwnds that just got their first-ever SetWindowPos and need exactly one
# ASYNC follow-up reposition shortly after (see _tick_repaint_nudges) - a
# brand-new window's first paint can occasionally land before its own
# thread has caught up with the posted resize, leaving it visually stuck
# (observed live via screenshot: solid black rect until nudged again).
# GlazeWM hits the identical first-move glitch and fixes it the same way:
# issue SetWindowPos a second time, always async - never by blocking.
_pending_repaint_nudges = set()

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
    tracking, since trusting _bordered_hwnd alone isn't reliable and
    re-asserting is cheap.

    Called both reactively (EVENT_SYSTEM_FOREGROUND, and
    EVENT_OBJECT_LOCATIONCHANGE whenever the bordered window itself moves -
    see _win_event_proc) and continuously via _tick_focus_border() on the
    shared MANAGEABLE_RETRY_TIMER tick (~every RETRY_INTERVAL) - the
    reactive calls give snappy response to real focus changes, but relying
    on WinEvents alone is fragile: rapid focus switching (e.g. alt-tab) can
    leave the border missing entirely, because Windows' own switcher UI
    (ForegroundStaging/XamlExplorerHostIslandWindow) transiently becomes the
    foreground window BETWEEN real app focus events, and if that transient
    "nothing to highlight" read happens to be the last one before events
    stop, nothing was left to trigger a correction (confirmed live - an
    earlier one-shot "settle timer" fix for this was itself just a single
    extra sample, so it could still theoretically land on the same race,
    just less often). Continuously re-deriving this from a fresh
    GetForegroundWindow() read on a fixed cadence removes the dependency on
    WinEvent ordering entirely: whatever the last tick got wrong, the next
    tick - at most RETRY_INTERVAL later - checks the live truth again and
    self-corrects, forever, with no special-cased retry logic needed.

    The actual DwmSetWindowAttribute/SetWindowPos calls run on the single
    persistent _border_worker thread, not here - DwmSetWindowAttribute is a
    cross-process RPC to the DWM compositor with no async variant, and
    live-isolated as a real cause of the Windows Terminal freeze/spinner
    (confirmed via controlled A/B toggling: disabling this alone resolved
    the hang). GlazeWM hits the same "no async DWM call" constraint for
    this identical feature and solves it the same way - dispatch the call
    off its main event-processing thread (their tokio::task::spawn) so a
    slow/blocked DWM call can never stall anything else. Only the decision
    (which hwnd to highlight) and the _bordered_hwnd bookkeeping happen
    inline here - which hwnd to clear is decided by the worker itself
    against its own applied_hwnd, not precomputed here (see _border_worker
    for why)."""
    global _bordered_hwnd, _border_pending, _border_pending_valid
    highlight = None
    if _state.border.get("enabled", True):
        fg = win32gui.GetForegroundWindow()
        if fg and win32gui.IsWindow(fg):
            monitor, workspace, leaf = _state.find_leaf_any_monitor(fg)
            if leaf is not None and workspace == _state.active_workspace(monitor):
                highlight = fg

    color = _hex_to_colorref(_state.border["color"]) if highlight is not None else None
    corner_style = _state.border["corner_style"] if highlight is not None else None

    with _border_condition:
        _border_pending = (highlight, color, corner_style)
        _border_pending_valid = True
        _border_condition.notify()
    _bordered_hwnd = highlight


def _tick_focus_border():
    """Runs on every MANAGEABLE_RETRY_TIMER tick (see run_message_loop),
    same shared timer _tick_manageable_retries/_tick_repaint_nudges use -
    see update_focus_border's docstring for why this continuous re-check
    exists rather than relying on WinEvents (+ a one-shot settle timer)
    alone."""
    update_focus_border()


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
        # Popups/tool-windows/owned-helpers (e.g. WinUI XAML popup hosts and
        # composition bridges behind autocomplete/IME suggestion UI) never
        # pass is_manageable() no matter how many times you check - retrying
        # those flooded this exact path (dozens of hwnds deep) while typing.
        # Only genuinely-initializing app windows (the Firefox timing
        # gotcha) are worth retrying - see _tick_manageable_retries for how.
        if could_become_manageable(hwnd):
            _manageable_retries.setdefault(hwnd, 0)
        return
    _manageable_retries.pop(hwnd, None)
    # Newly opened windows go to whichever monitor the cursor is on, not
    # wherever Windows happened to place the window initially.
    monitor = geometry.monitor_at_cursor()
    workspace = _state.active_workspace(monitor)
    _state.insert_hwnd(monitor, hwnd, workspace)
    _state.reflow(monitor, workspace)
    _pending_repaint_nudges.add(hwnd)
    _persist_workspace_state(monitor)
    update_focus_border()


def _tick_repaint_nudges():
    """Runs on every MANAGEABLE_RETRY_TIMER tick (see run_message_loop),
    same shared timer _tick_manageable_retries uses - no extra thread or
    hook needed. One-shot per hwnd: fires exactly once, then removed,
    regardless of whether a mismatch is found (this isn't drift-correction,
    just a repaint nudge for a window that's already at the right rect)."""
    for hwnd in list(_pending_repaint_nudges):
        _pending_repaint_nudges.discard(hwnd)
        monitor, workspace, leaf = _state.find_leaf_any_monitor(hwnd)
        if leaf is None:
            continue
        _state.forget_requested_rect(hwnd)
        _state.reflow(monitor, workspace)


def _tick_manageable_retries():
    """Runs on every MANAGEABLE_RETRY_TIMER tick (see run_message_loop) -
    one shared, message-loop-owned timer re-checking every still-pending
    hwnd, instead of a separate threading.Timer (a whole OS thread) spawned
    per retry attempt per hwnd. Bounded the same way as before
    (MAX_MANAGEABLE_RETRIES) - a window that fails is_manageable once and
    then never generates another LOCATIONCHANGE/FOREGROUND (e.g. a second
    window opens right after and steals focus) would otherwise be missed
    forever - verified this actually happens live, not just hypothetically."""
    for hwnd in list(_manageable_retries):
        count = _manageable_retries.get(hwnd, 0)
        if not win32gui.IsWindow(hwnd) or count >= MAX_MANAGEABLE_RETRIES:
            _manageable_retries.pop(hwnd, None)
            continue
        _manageable_retries[hwnd] = count + 1
        on_window_shown(hwnd)


def recheck_if_pending(hwnd):
    """EVENT_OBJECT_LOCATIONCHANGE and EVENT_SYSTEM_FOREGROUND are real
    signals that a window is still settling its own setup (still moving
    itself) or has just become genuinely usable (gained focus) - GlazeWM's
    own Windows backend listens to both of these for this same reason.
    Only acts on hwnds already known-pending (already failed is_manageable
    at least once), so this adds no cost to window activity in general."""
    if hwnd in _manageable_retries:
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
        _state.forget_requested_rect(hwnd)
        _state.reflow(monitor, workspace)
        return
    expected = _state.compute_rects(monitor, workspace).get(leaf)
    if expected is None:
        return
    expected = geometry.expand_rect_for_frame(expected, hwnd)
    actual = geometry.safe_get_window_rect(hwnd)
    if actual is None:
        return
    if actual == expected:
        return
    if hwnd not in _margin_revalidated:
        # Give a stale-from-too-early frame margin ONE free re-check before
        # counting this mismatch against the fight budget below - re-derive
        # it now that a real event proves the window still isn't where
        # expected, then immediately reflow with the freshly-queried value.
        # Bounded to once per hwnd (not tied to further mismatches), so this
        # can't turn back into the earlier reactive-requery-during-an-
        # active-fight loop that caused the Firefox width oscillation.
        _margin_revalidated.add(hwnd)
        geometry.invalidate_frame_margins(hwnd)
        _state.forget_requested_rect(hwnd)
        _state.reflow(monitor, workspace)
        return
    # Time-windowed, not a simple consecutive-mismatch counter reset on any
    # match - some apps (confirmed live: Firefox, growing back by a fixed
    # +41px every ~50-90ms) restore their own preferred size right after
    # every corrective reflow, landing at the correct rect just long enough
    # for the NEXT check to see a match before drifting away again - a
    # consecutive-count-based give-up gets reset by that alternation and
    # can never actually trigger, fighting forever. Counting attempts
    # within a rolling window instead can't be defeated by that pattern.
    now = time.monotonic()
    attempts = [t for t in _enforce_attempt_times.get(hwnd, []) if now - t < ENFORCE_WINDOW_SECONDS]
    if len(attempts) >= MAX_ENFORCE_ATTEMPTS:
        # Some apps just won't shrink to their computed tiled share (no
        # attempt is made to detect or query a "real" minimum - see tree.py,
        # matches GlazeWM's approach of not modeling this at all) - stop
        # reactively re-fighting this hwnd once it's clearly not converging.
        # A genuine future layout change still gets a fresh attempt, since
        # that goes through reflow() directly rather than through this list.
        _enforce_attempt_times[hwnd] = attempts
        return
    attempts.append(now)
    _enforce_attempt_times[hwnd] = attempts
    _state.forget_requested_rect(hwnd)
    _state.reflow(monitor, workspace)


def _closest_sibling_leaf(leaf):
    """The leaf that should get focus once `leaf` is removed - its nearest
    remaining sibling in the parent container (previous, else next), or
    the first leaf within it if that sibling is itself a nested container
    rather than a single window. Must be computed BEFORE the leaf is
    actually removed from the tree (see _unmanage) - parent.children still
    needs to include `leaf` to find its position."""
    parent = leaf.parent
    if parent is None:
        return None
    children = parent.children
    index = children.index(leaf)
    if index > 0:
        sibling = children[index - 1]
    elif len(children) > 1:
        sibling = children[index + 1]
    else:
        return None
    leaves = tree.all_leaves(sibling)
    return leaves[0] if leaves else None


def _unmanage(monitor, workspace, leaf):
    """Shared tail of on_window_destroyed/on_window_hidden - both remove a
    leaf from the tree the same way, once their own (different) entry
    guards decide the window is really gone/hidden for good. If the
    removed window was the focused one, hands focus to the closest
    remaining window in the same split instead of leaving it to Windows'
    own next-in-Z-order default, which is often a completely different
    window than whatever visually took over this one's tile."""
    was_focused = leaf.item == _bordered_hwnd
    next_focus = _closest_sibling_leaf(leaf) if was_focused else None
    _state.remove_leaf(monitor, leaf, workspace)
    _state.reflow(monitor, workspace)
    _persist_workspace_state(monitor)
    if next_focus is not None and win32gui.IsWindow(next_focus.item):
        _state.set_focused_leaf(monitor, next_focus, workspace)
        _force_foreground(next_focus.item)
    if was_focused:
        update_focus_border()


def on_window_destroyed(hwnd):
    _manageable_retries.pop(hwnd, None)
    _active_gestures.discard(hwnd)
    _enforce_attempt_times.pop(hwnd, None)
    _pending_repaint_nudges.discard(hwnd)
    _margin_revalidated.discard(hwnd)
    _pending_hides.pop(hwnd, None)
    monitor, workspace, leaf = _state.find_leaf_any_monitor(hwnd)
    if leaf is None:
        return
    _unmanage(monitor, workspace, leaf)


def on_window_hidden(hwnd):
    """A window was hidden (SW_HIDE) or DWM-cloaked (moved to another
    virtual desktop) without being destroyed. Unmanage it the same way a
    destroyed window is, but EVENT_OBJECT_SHOW/UNCLOAKED can re-manage it
    later via on_window_shown - without this, a hidden/cloaked window was a
    permanent "ghost tile", consuming layout space for a window nobody can
    see, with no cleanup path at all.

    Doesn't unmanage immediately - just records the notification and lets
    _tick_pending_hides act on it after HIDE_DEBOUNCE_SECONDS, still true.
    Some apps (confirmed live: Windows Terminal) briefly cloak/uncloak
    themselves during fast focus switching between two of their own
    windows, well under that delay - reacting immediately removed the tile
    and reinserted it moments later, visibly flickering the tile and its
    border for no real reason. A stale hide notification that's since
    reversed is naturally caught by _tick_pending_hides re-checking live
    state before actually unmanaging, exactly like the immediate guard
    this replaces used to."""
    if win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd) and not is_cloaked(hwnd):
        return
    monitor, workspace, leaf = _state.find_leaf_any_monitor(hwnd)
    if leaf is None:
        return
    if workspace != _state.active_workspace(monitor):
        return  # hidden because its workspace isn't active right now - our own switch-away, not a real hide
    _pending_hides[hwnd] = time.monotonic()


def _tick_pending_hides():
    """Runs on every MANAGEABLE_RETRY_TIMER tick (see run_message_loop) -
    same shared timer the other _tick_* functions use. Re-checks live state
    rather than trusting the original notification or any captured
    monitor/workspace/leaf, since on_window_shown may have already
    re-managed the hwnd (a fresh leaf) if it came back before this tick, or
    the hwnd may be gone entirely (on_window_destroyed clears it out)."""
    now = time.monotonic()
    for hwnd, seen_at in list(_pending_hides.items()):
        if now - seen_at < HIDE_DEBOUNCE_SECONDS:
            continue
        _pending_hides.pop(hwnd, None)
        if win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd) and not is_cloaked(hwnd):
            continue  # came back within the debounce window - never really left
        monitor, workspace, leaf = _state.find_leaf_any_monitor(hwnd)
        if leaf is None:
            continue
        if workspace != _state.active_workspace(monitor):
            continue
        _unmanage(monitor, workspace, leaf)


# --- Focus / move / resize hotkey commands ------------------------------------

def _force_foreground(target_hwnd):
    """SetForegroundWindow silently fails (Windows' foreground-lock
    restriction) when called from a background process that didn't itself
    receive the triggering input - exactly this daemon's situation, since
    hotkeyd receives the real keypress and forwards it over IPC. Same
    AttachThreadInput trick drag.py's _force_foreground already proved.
    CRITICAL: every AttachThreadInput(..., True) must be matched by a
    (..., False) no matter what happens in between - a stuck attachment
    permanently cross-wires the OTHER thread's keyboard input to this
    daemon's thread, which looks like (and is) a real input freeze for
    whatever app was attached. Nothing between an attach and its matching
    detach may be allowed to raise past this function uncaught."""
    current_thread = win32api.GetCurrentThreadId()
    attached_fg = attached_target = False
    fg_thread = target_thread = 0
    try:
        fg_hwnd = win32gui.GetForegroundWindow()
        fg_thread = win32process.GetWindowThreadProcessId(fg_hwnd)[0] if fg_hwnd else 0
        target_thread = win32process.GetWindowThreadProcessId(target_hwnd)[0]

        if fg_thread and fg_thread != current_thread:
            # AttachThreadInput returns None on success (pywin32 convention
            # for void-return Win32 calls) and raises on failure - it does
            # NOT return a truthy success flag. Treating its return value
            # as the success indicator (as this used to) meant `attached_fg`
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

    raw = geometry.safe_get_window_rect(hwnd)
    if raw is None:
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
    # A NoOp outcome (dropped on empty space, not onto another tile) leaves
    # the tree - and therefore the leaf's computed target rect - completely
    # unchanged, but the window's REAL on-screen position is now wherever it
    # was physically dropped. reflow()'s skip-if-unchanged cache only knows
    # about the target rect, so it would otherwise treat this as "nothing to
    # do" and never re-issue the SetWindowPos that snaps it back.
    _state.forget_requested_rect(hwnd)
    for dirty_monitor, dirty_workspace in dirty:
        _state.reflow(dirty_monitor, dirty_workspace)


def on_move_resize_end(hwnd):
    """WinEvent-driven path for native OS drags (dragging a title bar
    directly) - drag.py's own alt+drag gestures never reach here at all,
    since it no longer fires a synthetic MOVESIZEEND (see its _end_drag
    docstring); record_drag_kind is the sole, authoritative path for those."""
    _active_gestures.discard(hwnd)
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

WM_TIMER = 0x0113
user32.SetTimer.restype = wintypes.UINT
user32.SetTimer.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.UINT, ctypes.c_void_p]
user32.KillTimer.argtypes = [wintypes.HWND, wintypes.UINT]


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
            # Only reapply for the already-bordered window itself, never
            # for unrelated windows' location changes - keeps the border
            # tracking it live even mid-drag (native or alt+drag) now that
            # the coalescing + throttled worker (see _border_worker) bounds
            # the actual DWM call rate regardless of how fast these fire.
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
    # hWnd=None posts WM_TIMER to this (the calling) thread's own message
    # queue instead of any window - the loop below picks it up like any
    # other message, no extra thread needed for the retry backstop.
    manageable_retry_timer_id = user32.SetTimer(None, 0, int(RETRY_INTERVAL * 1000), None)

    try:
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == WM_APP_EVENT:
                _drain_posted_events()
                continue
            if msg.message == WM_TIMER and msg.wParam == manageable_retry_timer_id:
                _tick_manageable_retries()
                _tick_repaint_nudges()
                _tick_pending_hides()
                _tick_focus_border()
                continue
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    finally:
        if manageable_retry_timer_id:
            user32.KillTimer(None, manageable_retry_timer_id)
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
