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
from oriel.tiling.filters import could_become_floating_configured, could_become_manageable, floating_rule_options, get_process_name, is_cloaked, is_floating_configured, is_manageable, load_floating_rules, load_ignore_rules
from oriel.tiling.state import DEFAULT_BORDER, DEFAULT_GAP, DEFAULT_OUTER_GAP, DEFAULT_RESIZE_STEP, DEFAULT_WORKSPACE, DEFAULT_FLOATING, DEFAULT_WORKSPACE_TOGGLE_BACK

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

# Some packaged apps (Calculator, Windows App) restore their own last-used
# window position shortly after being shown, overwriting a single centering
# call - see _delayed_center_floating_window, which keeps re-centering
# against this overall budget until the window holds still (see
# FLOATING_CENTER_STABLE_SECONDS) instead of trusting a single match, so an
# app that settles quickly (e.g. Explorer) still exits almost immediately
# while one that keeps adjusting itself for longer (confirmed live:
# Calculator) gets caught instead of silently missed.
FLOATING_CENTER_MAX_WAIT_SECONDS = 1.5
FLOATING_CENTER_RETRY_INTERVAL = 0.05
# How long the window must stay at the last-applied target, checked on
# every retry, before considering it actually settled - a single instant
# match right after SetWindowPos is meaningless (of course it matches, we
# just set it) and used to make _delayed_center_floating_window always
# exit after exactly one iteration, missing any change the app made on its
# own moments later.
FLOATING_CENTER_STABLE_SECONDS = 0.15

# A single SWP_ASYNCWINDOWPOS z-order request posted right at window-
# creation time can lose a race against whatever else is still settling
# z-order at that exact moment (observed: Explorer occasionally staying
# behind tiled windows) - see _raise_floating_window, which retries
# synchronously against this budget instead of trusting one async post.
FLOATING_RAISE_MAX_WAIT_SECONDS = 0.3
FLOATING_RAISE_RETRY_INTERVAL = 0.05

# Used instead of tiling.outer_gap when anchor-positioning a floating
# window (see _center_floating_window) - outer_gap is a tiled-layout
# spacing setting, not something floating windows should inherit.
ZERO_OUTER_GAP = {"top": 0, "right": 0, "bottom": 0, "left": 0}


# How long a hide/cloak notification has to keep looking real before it's
# actually acted on (see on_window_hidden/_tick_pending_hides) - live-
# confirmed some apps (e.g. Windows Terminal) get briefly, spuriously
# cloaked/uncloaked during fast focus switching between two of their own
# windows, well under this window. Reacting immediately removed the tile
# and re-inserted it moments later, visible as the border (and the tile
# itself) flickering out and back for no user-visible reason.
HIDE_DEBOUNCE_SECONDS = 0.2

# How long a registered autostart process's expected window has to appear
# by (see register_autostart_window/on_window_shown) - generous since MSIX
# apps (e.g. Teams) and browser cold starts can be slow. Process name, not
# PID - MSIX/Store app aliases (ms-teams.exe) often activate through a
# broker process whose PID doesn't match the eventual UI process, so
# matching by the launched exe's name is far more reliable than trying to
# track a specific PID through that indirection.
AUTOSTART_WINDOW_SECONDS = 30.0

_state = None

# hwnd -> retry count, for hwnds that failed is_manageable() at least once
# and are waiting to be rechecked (see recheck_if_pending/
# _tick_manageable_retries) - Firefox in particular fires SHOW/NAMECHANGE
# before finishing its own window styling, so the very first check can
# genuinely be too early. Cleaned up in on_window_destroyed so this can't
# grow unbounded for windows that are never actually manageable.
_manageable_retries = {}

# hwnd -> retry count, for windows that already pass is_manageable() (real
# chrome) but whose process/class matches a floating_rules entry aside
# from its title (see could_become_floating_configured) - held off from
# tiling for a few ticks in case a NAMECHANGE reveals the title that rule
# actually matches, same MANAGEABLE_RETRY_TIMER tick _manageable_retries
# uses (not a separate timer - see _tick_floating_settle_retries). Without
# this, a window that gets tiled first and only later renames into a
# floating_rules match stays tiled forever with no way back to its
# original (pre-tile-resize) size - confirmed live with Teams' meeting
# window during the screen-share transition.
_floating_settle_retries = {}

# process name (lowercased basename, e.g. "ms-teams.exe") -> (workspace,
# monotonic expiry) - registered by oriel.autostart via IPC right after it
# launches a configured app with a "workspace" override, consumed by
# on_window_shown so ONLY that freshly-autostarted instance lands on the
# configured workspace; manually relaunching the same app later (no fresh
# registration) falls through to the normal current-active-workspace
# behavior. Not one-shot - stays live for the whole window so a slow app
# that opens more than one top-level window during its own startup (e.g. a
# separate sign-in window) still gets all of them, expiring naturally
# after AUTOSTART_WINDOW_SECONDS either way.
_pending_autostart_workspace = {}

# hwnd -> monotonic timestamp of its most recent hide/cloak notification,
# for hwnds waiting out HIDE_DEBOUNCE_SECONDS before on_window_hidden's
# effect is actually applied (see _tick_pending_hides). Cleaned up in
# on_window_destroyed.
_pending_hides = {}

# hwnds currently inside a bracketed EVENT_SYSTEM_MOVESIZESTART/END gesture
# (native OS drag or drag.py's custom alt+drag - both emit this bracket) -
# see enforce_tiled_placement, which must never fight a real gesture.
_active_gestures = set()

# hwnds toggle_floating has manually forced OUT of floating (Alt+V on a
# window that matches a floating_rules entry) - tracked in TilingState
# (add_forced_tiled/is_forced_tiled), not here, so it survives a restart
# via persistence.save_monitor/bootstrap_existing_windows - without that,
# on_window_shown's rename_promote migration (see its docstring) undoes
# this choice the moment the window's title next changes (still matches
# the same rule it always did), and a restart would silently re-float it
# too (bootstrap otherwise has no way to know it was ever overridden).

# hwnd -> (left, top, right, bottom) captured by toggle_floating right
# before tiling a floating window, so toggling it back restores exactly
# where/how big it was instead of wherever tiling last left it. Cleared
# once consumed by the reverse toggle, or in on_window_destroyed.
_floating_toggle_rect = {}

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


WM_QUIT = 0x0012


def quit_daemon(_data=None):
    """IPC "quit" action (see tiling/daemon.py's ACTIONS) - posts WM_QUIT to
    the message-loop thread so GetMessageW returns 0, run_message_loop's own
    finally block unhooks everything, and the process exits naturally (all
    background threads - IPC, border worker - are daemon threads, so none
    of them keep the process alive once the main thread returns)."""
    _teardown_for_quit()
    if _main_thread_id is not None:
        ctypes.windll.user32.PostThreadMessageW(_main_thread_id, WM_QUIT, 0, 0)


def _teardown_for_quit():
    """Runs once right before the process exits - undoes everything this
    daemon did to the desktop that nothing else would ever undo once it's
    gone: clears the focus border, shows every hidden (inactive-workspace)
    tiled window back, and nudges any window whose current rect would now
    sit under the taskbar (oriel.taskbar restores it as part of its own
    "quit" teardown, racing this one - see geometry.taskbar_rect for why
    this can't just check current taskbar visibility). Deliberately does
    NOT re-tile or maximize anything - windows are left wherever they
    currently are/floating, just not left invisible or obscured."""
    global _bordered_hwnd
    if _bordered_hwnd is not None and win32gui.IsWindow(_bordered_hwnd):
        border.clear_border(_bordered_hwnd)
    _bordered_hwnd = None

    for monitor, workspace in _state.all_monitor_workspaces():
        for leaf in tree.all_leaves(_state.root(monitor, workspace)):
            hwnd = leaf.item
            if not win32gui.IsWindow(hwnd):
                continue
            win32gui.ShowWindow(hwnd, win32con.SW_SHOWNA)
            _avoid_taskbar_overlap(hwnd, monitor)

    for monitor, workspace in _state.all_floating_monitor_workspaces():
        for hwnd in list(_state.floating_hwnds(monitor, workspace)):
            if not win32gui.IsWindow(hwnd):
                continue
            win32gui.ShowWindow(hwnd, win32con.SW_SHOWNA)
            _avoid_taskbar_overlap(hwnd, monitor)

    for hwnd in _state.sticky_hwnds():
        # Never hidden by us in the first place (sticky windows skip the
        # (monitor, workspace) hide/show lifecycle entirely - see
        # _add_floating_window), so just the taskbar-overlap nudge, no
        # ShowWindow needed.
        if win32gui.IsWindow(hwnd):
            _avoid_taskbar_overlap(hwnd, geometry.monitor_of(hwnd))


def _avoid_taskbar_overlap(hwnd, monitor):
    """Shrinks/moves hwnd up out from under the taskbar's real screen rect
    if its current bounds overlap it - used only by _teardown_for_quit."""
    rect = geometry.safe_get_window_rect(hwnd)
    taskbar = geometry.taskbar_rect(monitor)
    if rect is None or taskbar is None:
        return
    bounds = geometry.monitor_bounds(monitor)
    safe_left, safe_top, safe_right, safe_bottom = geometry.subtract_taskbar(bounds, taskbar)
    left, top, right, bottom = rect
    new_left, new_top = max(left, safe_left), max(top, safe_top)
    new_right, new_bottom = min(right, safe_right), min(bottom, safe_bottom)
    if (new_left, new_top, new_right, new_bottom) == (left, top, right, bottom):
        return  # already fits, nothing to do
    if new_right <= new_left or new_bottom <= new_top:
        return  # window is bigger than the whole safe area - leave it alone
    try:
        win32gui.SetWindowPos(
            hwnd, 0, new_left, new_top, new_right - new_left, new_bottom - new_top,
            win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE | win32con.SWP_ASYNCWINDOWPOS,
        )
    except win32gui.error:
        pass


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
        "floating": {**DEFAULT_FLOATING, **tiling.get("floating", {})},
        "workspace_toggle_back": tiling.get("workspace_toggle_back", DEFAULT_WORKSPACE_TOGGLE_BACK),
    }


def apply_initial_settings():
    settings = _load_settings()
    _state.inner_gap = settings["inner_gap"]
    _state.outer_gap = settings["outer_gap"]
    _state.resize_step = settings["resize_step"]
    _state.workspaces = settings["workspaces"]
    _state.border = settings["border"]
    _state.floating = settings["floating"]
    _state.workspace_toggle_back = settings["workspace_toggle_back"]
    load_ignore_rules()
    load_floating_rules()


def reload_settings(_data=None):
    """Re-reads inner_gap/outer_gap/resize_step/border/floating/
    ignore_rules/floating_rules from config.json and reflows every monitor
    immediately so the change is visible right away."""
    old_workspaces = _state.workspaces
    settings = _load_settings()
    _state.inner_gap = settings["inner_gap"]
    _state.outer_gap = settings["outer_gap"]
    _state.resize_step = settings["resize_step"]
    _state.workspaces = settings["workspaces"]
    _state.border = settings["border"]
    _state.floating = settings["floating"]
    _state.workspace_toggle_back = settings["workspace_toggle_back"]
    load_ignore_rules()
    load_floating_rules()
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
            else:
                floating_monitor, floating_workspace = _state.find_floating_any_monitor(fg)
                if floating_monitor is not None and floating_workspace == _state.active_workspace(floating_monitor):
                    highlight = fg
                elif _state.is_sticky(fg):
                    # Sticky windows have no (monitor, workspace) of their
                    # own to check against an "active" one - being sticky
                    # at all is sufficient, since they're visible (and
                    # therefore focusable) regardless of which workspace
                    # is currently active.
                    highlight = fg
        if highlight is not None and _state.has_no_border(highlight):
            # Explicit floating_rules "border": false opt-out (see
            # _add_floating_window) - e.g. a screen-share control bar,
            # where oriel's usual accent border/corner-rounding looks out
            # of place on such a small, transient window.
            highlight = None

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
    # hwnds already placed into a tree by the restore pass below - the
    # per-hwnd loop further down skips these entirely instead of re-
    # inserting them via the flat insert_hwnd path, which would rebuild a
    # fresh default 50/50 layout and lose every manual resize/split the
    # user had (see persistence.save_monitor's "trees" entry).
    restored_hwnds = set()
    for handle, _hdc, _rect in win32api.EnumDisplayMonitors():
        monitor = int(handle)
        entry = persistence.entry_for(monitor, persisted)
        if entry is None:
            continue
        _state.set_active_workspace(monitor, entry.get("active", DEFAULT_WORKSPACE))
        forced_tiled_hwnds = set(entry.get("forced_tiled", []))
        for workspace_str, tree_data in entry.get("trees", {}).items():
            root = tree.deserialize(tree_data)
            if root is None:
                continue
            workspace = int(workspace_str)
            # A leaf survives pruning only if its window still exists AND
            # hasn't been reclassified to floating since this was last
            # saved (e.g. a floating_rules edit) - forced_tiled overrides
            # that back to tiled, the same priority order on_window_shown/
            # toggle_floating use everywhere else. Anything pruned out here
            # but still alive falls through to the per-hwnd loop below,
            # which re-evaluates it exactly like a window with no tree
            # history at all.
            alive = {
                leaf.item for leaf in tree.all_leaves(root)
                if win32gui.IsWindow(leaf.item)
                and (leaf.item in forced_tiled_hwnds or not is_floating_configured(leaf.item))
            }
            root = tree.prune_dead_leaves(root, alive)
            if root is None:
                continue
            _state.set_root(monitor, root, workspace)
            leaves = tree.all_leaves(root)
            _state.set_focused_leaf(monitor, leaves[0], workspace)
            for leaf in leaves:
                restored_hwnds.add(leaf.item)
                if leaf.item in forced_tiled_hwnds:
                    _state.add_forced_tiled(leaf.item)
                if workspace != _state.active_workspace(monitor) and win32gui.IsWindowVisible(leaf.item):
                    win32gui.ShowWindow(leaf.item, win32con.SW_HIDE)

    handles = []

    def callback(hwnd, _):
        handles.append(hwnd)
        return True

    win32gui.EnumWindows(callback, None)

    # Reverse Z-order (bottom-most first) so the most-recently-focused
    # window ends up last-inserted, roughly matching what you'd expect to
    # see "on top" of the initial layout.
    for hwnd in reversed(handles):
        if not win32gui.IsWindow(hwnd) or hwnd in restored_hwnds:
            continue
        monitor = geometry.monitor_of(hwnd)
        entry = persistence.entry_for(monitor, persisted)
        workspace = entry.get("windows", {}).get(str(hwnd)) if entry else None
        if workspace is None:
            # Not found under this hwnd's CURRENT monitor's own persisted
            # entry - a monitor's stable id isn't guaranteed to stay the
            # same across sessions (e.g. RDP presents a generic
            # "Remote_Monitor" identity in place of the real one) even
            # though the window itself and its intended workspace didn't
            # change, so search every OTHER persisted monitor entry too
            # before concluding this hwnd has no known assignment at all.
            workspace = persistence.find_hwnd_workspace(hwnd, persisted)

        if workspace is not None:
            # Same cross-monitor-identity concern as workspace above - a
            # manual toggle_floating override (see TilingState.add_forced_
            # tiled) needs to survive even if this monitor's stable id
            # changed since it was last saved.
            forced_tiled = hwnd in (entry.get("forced_tiled", []) if entry else [])
            if not forced_tiled:
                forced_tiled = persistence.find_hwnd_forced_tiled(hwnd, persisted)

            # Known from persisted history - re-track it even if it's
            # currently hidden (e.g. it was on an inactive workspace when
            # oriel last reset/restarted). is_manageable()'s default
            # visibility requirement would otherwise reject it for exactly
            # that reason, silently and permanently orphaning it - nothing
            # else ever re-discovers an already-existing hidden window
            # once bootstrap has skipped it (no new SHOW/UNCLOAKED event
            # is coming for a window that's just sitting there hidden).
            if forced_tiled:
                # toggle_floating forced this OUT of floating despite
                # matching a rule - is_floating_configured has no memory of
                # that on its own, so without this, every restart would
                # silently re-apply the rule and re-float it right back.
                _state.add_forced_tiled(hwnd)
                if is_manageable(hwnd, require_visible=False):
                    _state.insert_hwnd(monitor, hwnd, workspace)
                    if workspace != _state.active_workspace(monitor) and win32gui.IsWindowVisible(hwnd):
                        win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
            elif is_floating_configured(hwnd):
                # Explicit floating_rules match overrides tiling outright,
                # same priority order as on_window_shown - checked ahead of
                # is_manageable() so a fully-chromed match (e.g. Windows
                # App) can't get re-tiled on every restart.
                options = floating_rule_options(hwnd)
                if options["sticky"]:
                    _state.add_sticky(hwnd)
                else:
                    _state.add_floating(monitor, hwnd, workspace)
                    if workspace != _state.active_workspace(monitor) and win32gui.IsWindowVisible(hwnd):
                        win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
                if not options["border"]:
                    _state.add_no_border(hwnd)
            elif is_manageable(hwnd, require_visible=False):
                _state.insert_hwnd(monitor, hwnd, workspace)
                if workspace != _state.active_workspace(monitor) and win32gui.IsWindowVisible(hwnd):
                    win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
            elif could_become_manageable(hwnd):
                # Known history, but not a tileable window (missing chrome/
                # title, or excluded via ignore_rules) - same floating
                # re-tracking as the runtime discovery path, just without
                # re-centering it on every restart.
                _state.add_floating(monitor, hwnd, workspace)
                if workspace != _state.active_workspace(monitor) and win32gui.IsWindowVisible(hwnd):
                    win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
        elif is_floating_configured(hwnd) and win32gui.IsWindowVisible(hwnd):
            # No persisted history, but an explicit floating_rules match -
            # same priority-over-tiling as above, fresh discovery so it
            # goes through the full _add_floating_window path (centering).
            _add_floating_window(hwnd, monitor)
        elif is_manageable(hwnd):
            # No persisted history anywhere - only a genuinely fresh,
            # currently-visible app window qualifies as a new discovery.
            _state.insert_hwnd(monitor, hwnd, _state.active_workspace(monitor))
        elif could_become_manageable(hwnd) and win32gui.IsWindowVisible(hwnd):
            # Never seen before either - a currently-visible real (non-
            # popup) window that just doesn't qualify for tiling. Goes
            # through the full _add_floating_window path (including
            # centering) since it's a fresh discovery, same as the runtime
            # retry-exhaustion path.
            _add_floating_window(hwnd, monitor)
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


def register_autostart_window(data):
    """Called over IPC (see tiling/daemon.py's "expect_autostart_window"
    action) right after oriel.autostart launches a configured app with a
    "workspace" override - see _pending_autostart_workspace for why this is
    keyed by process name rather than PID."""
    if not data:
        return
    process = data.get("process")
    workspace = data.get("workspace")
    if not process or workspace is None:
        return
    now = time.monotonic()
    # Opportunistic cleanup - registrations only ever happen at startup, so
    # this is the sole place expired entries get pruned.
    for stale in [p for p, (_, expiry) in _pending_autostart_workspace.items() if expiry < now]:
        _pending_autostart_workspace.pop(stale, None)
    _pending_autostart_workspace[process.lower()] = (workspace, now + AUTOSTART_WINDOW_SECONDS)


def _add_floating_window(hwnd, monitor=None):
    """Tracks hwnd as a floating (non-tiled) window - assigned a workspace
    so it hides/shows with workspace switches exactly like a tiled window
    (see switch_workspace/_tick_pending_hides/on_window_destroyed), but
    never inserted into the tiling tree and never affects other windows'
    rects. For windows that structurally can't be tiled (missing chrome/
    title) or are deliberately excluded via ignore_rules (e.g. Settings/
    Calculator/credential dialogs) - previously these got no workspace/
    hide-show lifecycle or focus border at all, staying visible regardless
    of which workspace was active. Kept above tiled windows in Z-order and
    optionally centered on its monitor's work area (config.json's
    tiling.floating.center_on_open).

    A tiling.floating_rules match with "sticky": true (see
    filters.floating_rule_options) skips the (monitor, workspace)-scoped
    floating lifecycle entirely and goes to TilingState.add_sticky instead
    - for windows that belong to no single workspace at all (e.g. a Teams
    meeting window, which Teams recreates as a brand-new hwnd on every
    view-mode toggle, scattering "the same meeting" across whatever
    workspace happened to be active at each toggle - see switch_workspace's
    hide/show loops, which only ever touch _state.floating_hwnds, never
    sticky ones, so these are simply never hidden by a workspace switch).
    Every floating window is always raised via HWND_TOPMOST (see
    _raise_floating_window), so it stays above tiled windows persistently,
    not just once at open. "position" (default "center") picks where on
    the work area it lands when center_on_open is true - see
    _anchor_position for accepted values (e.g. "top", "bottom-right").
    "gap" (default 0) is the margin kept from whichever edge(s) "position"
    anchors against. "width"/
    "height" (default None = leave as-is) force a specific visible size
    instead of just repositioning it. "border": false skips corner-
    rounding and focus-border eligibility entirely (see TilingState.
    add_no_border/update_focus_border) - for small/transient windows (e.g.
    a screen-share control bar) where oriel's usual per-window chrome
    looks out of place."""
    if monitor is None:
        monitor = geometry.monitor_at_cursor()
    options = floating_rule_options(hwnd)
    if options["sticky"]:
        _state.add_sticky(hwnd)
    else:
        workspace = _state.active_workspace(monitor)
        _state.add_floating(monitor, hwnd, workspace)
    if not options["border"]:
        _state.add_no_border(hwnd)
    if win32gui.IsWindow(hwnd):
        if options["border"]:
            threading.Thread(target=_run_logged, args=(border.ensure_rounded, (hwnd,)), daemon=True).start()
        if _state.floating.get("center_on_open", False):
            center_args = (hwnd, monitor, options["position"], options["gap"], options["width"], options["height"])
            delayed_args = center_args + (options["center_delay"],)
            if options["sticky"]:
                # Sticky windows are always a freshly-discovered hwnd (a
                # brand-new Teams meeting/screen-share window, never an
                # already-open one being reclassified), so there's no
                # app-restores-its-own-position race to wait out the way
                # e.g. Calculator has - position it immediately instead of
                # through the delayed re-center below. That immediacy means
                # frame_margins may not have been queried for this hwnd yet
                # at all - invalidate any stale/early cache entry so
                # expand_rect_for_frame (inside _center_floating_window)
                # re-queries DWM's real extended frame bounds instead of a
                # snapshot taken before the window had settled.
                geometry.invalidate_frame_margins(hwnd)
                threading.Thread(target=_run_logged, args=(_center_floating_window, center_args), daemon=True).start()
                # The instant call above can still race the app's own
                # content-driven layout (observed: Teams' WebView
                # re-asserting its own preferred size/position shortly
                # after creation, undoing an immediate placement) whether
                # or not a size override is set - one extra retry-until-
                # settled follow-up (same _delayed_center_floating_window
                # used by the non-sticky path below, not a new timer)
                # catches that without giving up the instant initial
                # placement above.
                threading.Thread(target=_run_logged, args=(_delayed_center_floating_window, delayed_args), daemon=True).start()
            else:
                threading.Thread(target=_run_logged, args=(_delayed_center_floating_window, delayed_args), daemon=True).start()
        threading.Thread(target=_run_logged, args=(_raise_floating_window, (hwnd, options["activate"])), daemon=True).start()
    _persist_workspace_state(monitor)
    update_focus_border()


def _run_logged(target, args):
    """Plain threading.Thread swallows exceptions silently under
    pythonw.exe (no console for the default excepthook to print to, and
    nothing else writes them to our log file) - wraps a thread target so a
    failure actually shows up in tiling.log instead of vanishing without a
    trace, same as everything already logged via the WinEventHook wrapper."""
    try:
        target(*args)
    except Exception:
        logger.exception("background floating-window thread failed: %s%s", target.__name__, args)


def _force_foreground(hwnd):
    """Plain SetForegroundWindow from a background process (this daemon,
    with no recent user input tied to its own thread) is routinely
    silently ignored by Windows' focus-stealing prevention - retrying the
    same call again doesn't help since it's a permission check, not a
    timing race. AttachThreadInput to whatever currently owns the
    foreground momentarily shares input state between the two threads,
    which is the standard workaround Windows itself expects for this."""
    current_thread = win32api.GetCurrentThreadId()
    fg = win32gui.GetForegroundWindow()
    fg_thread = win32process.GetWindowThreadProcessId(fg)[0] if fg else 0
    attached = bool(fg_thread) and fg_thread != current_thread and ctypes.windll.user32.AttachThreadInput(
        current_thread, fg_thread, True
    )
    try:
        win32gui.SetForegroundWindow(hwnd)
    except win32gui.error:
        pass
    finally:
        if attached:
            ctypes.windll.user32.AttachThreadInput(current_thread, fg_thread, False)


def _raise_floating_window(hwnd, activate):
    """Repeatedly raises hwnd to the top of the topmost z-order band
    (HWND_TOPMOST, unconditionally - not just an opt-in, see filters.
    floating_rule_options) until it's confirmed on top of everything else
    in that band or the budget runs out, instead of trusting a single
    SWP_ASYNCWINDOWPOS post to win whatever z-order race is still settling
    right at window-creation time. If activate, also gives it real
    keyboard focus via _force_foreground once (after the first successful
    raise, not on every retry) - windows launched from a background
    trigger (e.g. a hotkey daemon, not the user directly clicking
    taskbar/Start) don't reliably get Windows' normal auto-activation."""
    deadline = time.monotonic() + FLOATING_RAISE_MAX_WAIT_SECONDS
    activated = not activate
    while win32gui.IsWindow(hwnd):
        try:
            win32gui.SetWindowPos(
                hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE,
            )
        except win32gui.error:
            pass
        if not activated:
            _force_foreground(hwnd)
            activated = True
        if win32gui.GetWindow(hwnd, win32con.GW_HWNDPREV) == 0:
            return
        if time.monotonic() >= deadline:
            return
        time.sleep(FLOATING_RAISE_RETRY_INTERVAL)


def _normalize_gap(gap):
    """A floating_rules "gap" value is either a single number (applied to
    all four edges) or a dict like tiling.outer_gap's - normalizes either
    form to a (top, right, bottom, left) tuple."""
    if isinstance(gap, dict):
        return gap.get("top", 0), gap.get("right", 0), gap.get("bottom", 0), gap.get("left", 0)
    return gap, gap, gap, gap


def _anchor_position(position, area, width, height, gap=0):
    """Resolves a floating_rules "position" string (e.g. "center",
    "top", "bottom-right") to a (left, top) point within `area` for a
    window of the given size - each of the two axes is independently
    center/min/max, so any hyphenated combination of a vertical
    (top/bottom) and horizontal (left/right) keyword works, defaulting to
    center on whichever axis isn't mentioned. `gap` insets whichever
    edge(s) are actually anchored against - it has no effect on an axis
    left centered, since there's no single edge to keep a margin from."""
    area_left, area_top, area_right, area_bottom = area
    gap_top, gap_right, gap_bottom, gap_left = _normalize_gap(gap)
    keywords = position.lower().split("-")
    if "top" in keywords:
        top = area_top + gap_top
    elif "bottom" in keywords:
        top = area_bottom - height - gap_bottom
    else:
        top = area_top + max(0, ((area_bottom - area_top) - height) // 2)
    if "left" in keywords:
        left = area_left + gap_left
    elif "right" in keywords:
        left = area_right - width - gap_right
    else:
        left = area_left + max(0, ((area_right - area_left) - width) // 2)
    return left, top


def _center_floating_window(hwnd, monitor, position="center", gap=0, width=None, height=None):
    """Computes and applies the anchor position (and optional forced size)
    for a floating window - returns the exact (left, top, right, bottom)
    it targeted (in raw GetWindowRect terms), or None if hwnd's rect
    couldn't be read at all, so callers (_delayed_center_floating_window)
    can tell whether the window actually landed where intended."""
    if win32gui.GetWindowPlacement(hwnd)[1] == win32con.SW_SHOWMAXIMIZED:
        # A maximized window ignores a plain SetWindowPos resize/move - the
        # WS_MAXIMIZE state itself dictates its displayed geometry, not
        # whatever rect we ask for - confirmed live: Explorer opening onto
        # its own remembered maximized state (unrelated to oriel, just
        # whatever the user last left an Explorer window as) stayed
        # fullscreen no matter what width/height a floating_rules entry
        # configured. Must restore first so everything below actually takes.
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    rect = geometry.safe_get_window_rect(hwnd)
    if rect is None:
        return None
    # GetWindowRect includes the invisible DWM resize/shadow border, so
    # centering on it directly leaves the *visible* frame off-center by
    # whatever that border's margins are - same issue geometry.py already
    # compensates for on the tiled path (see expand_rect_for_frame).
    left, top, right, bottom = geometry.shrink_rect_for_frame(rect, hwnd)
    current_width, current_height = right - left, bottom - top
    target_width = width if width is not None else current_width
    target_height = height if height is not None else current_height
    resizing = width is not None or height is not None
    if resizing:
        # Some apps (observed: Teams' compact meeting view while sharing)
        # enforce their own minimum tracking size and silently clamp a
        # too-small SetWindowPos request instead of honoring it - resize
        # first, then re-read what size the window actually ended up at,
        # so the anchor position below is computed against reality instead
        # of the (possibly rejected) ask, which otherwise leaves it
        # anchored as if it were the requested size while it's actually
        # bigger, overflowing past the intended edge/gap.
        try:
            win32gui.SetWindowPos(
                hwnd, 0, 0, 0, target_width, target_height,
                win32con.SWP_NOMOVE | win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE,
            )
        except win32gui.error:
            pass
        actual_rect = geometry.safe_get_window_rect(hwnd)
        if actual_rect is not None:
            a_left, a_top, a_right, a_bottom = geometry.shrink_rect_for_frame(actual_rect, hwnd)
            target_width, target_height = a_right - a_left, a_bottom - a_top
    # Deliberately NOT _state.work_area(monitor) - tiling.outer_gap is a
    # tiled-layout spacing concept, and floating windows (this anchor
    # positioning) aren't part of that layout at all, so they anchor flush
    # against the real usable area (monitor bounds minus taskbar only),
    # inset only by this rule's own "gap" if any.
    area = geometry.work_area(monitor, ZERO_OUTER_GAP)
    new_left, new_top = _anchor_position(position, area, target_width, target_height, gap)
    target_left, target_top, target_right, target_bottom = geometry.expand_rect_for_frame(
        (new_left, new_top, new_left + target_width, new_top + target_height), hwnd
    )
    flags = win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE
    if not resizing:
        flags |= win32con.SWP_NOSIZE
    try:
        win32gui.SetWindowPos(
            hwnd, 0, target_left, target_top,
            target_right - target_left if resizing else 0,
            target_bottom - target_top if resizing else 0,
            flags,
        )
    except win32gui.error:
        pass
    return target_left, target_top, target_right, target_bottom


def _delayed_center_floating_window(hwnd, monitor, position="center", gap=0, width=None, height=None, center_delay=None):
    """Runs on its own one-off background thread (see _add_floating_window)
    - never the message-loop thread. Re-issues the centering call every
    FLOATING_CENTER_RETRY_INTERVAL until the window has held still at
    whatever _center_floating_window last targeted for a full
    FLOATING_CENTER_STABLE_SECONDS in a row, or center_delay (default
    FLOATING_CENTER_MAX_WAIT_SECONDS) has elapsed - checking for a mere
    match right after applying it is worthless (of course it matches, we
    just set it via SetWindowPos) and used to cause this to always exit
    after exactly one iteration, silently missing an app (confirmed live:
    Calculator) that changes its own size/position again shortly after our
    first call. Requiring it to stay put for a stretch catches that: an
    app that settles its own startup layout quickly (e.g. Explorer) still
    exits almost immediately, one that keeps moving itself keeps getting
    re-centered against whatever its current size actually is until it
    stops or the budget runs out."""
    deadline = time.monotonic() + (FLOATING_CENTER_MAX_WAIT_SECONDS if center_delay is None else center_delay)
    stable_since = None
    while win32gui.IsWindow(hwnd):
        target = _center_floating_window(hwnd, monitor, position, gap, width, height)
        if target is not None and geometry.safe_get_window_rect(hwnd) == target:
            if stable_since is None:
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= FLOATING_CENTER_STABLE_SECONDS:
                return
        else:
            stable_since = None
        if time.monotonic() >= deadline:
            return
        time.sleep(FLOATING_CENTER_RETRY_INTERVAL)


def on_window_shown(hwnd):
    if not win32gui.IsWindow(hwnd):
        # A burst of SHOW events for windows that are already gone by the
        # time this runs - confirmed live during an RDP reconnect, which
        # fires a flood of these for short-lived internal windows within
        # the same tick. GetParent (and everything below) would otherwise
        # throw on an invalid handle - each one costing a full traceback
        # format + disk write on this single event-loop thread, backlogging
        # real events queued behind them.
        return
    if win32gui.GetParent(hwnd):
        # A genuine top-level window never has a parent (an owned window
        # uses GW_OWNER, not GetParent) - a non-zero parent means this is a
        # child object that merely fires its own WinEvents, e.g. WinUI/
        # UWP's Windows.UI.Core.CoreWindow living inside an
        # ApplicationFrameWindow frame and sharing its exact title.
        # Confirmed live: an unscoped floating_rules title match caught
        # exactly this for Calculator, and SetWindowPos-ing a child with
        # screen coordinates corrupts its position (child coordinates are
        # relative to the PARENT's client area, not the screen), breaking
        # its rendering. Bails out here specifically because
        # is_floating_configured below has no other structural validation
        # at all (unlike is_manageable, which at least checks chrome/
        # ownership) - a badly-scoped rule shouldn't be able to reach it.
        return
    _monitor, _workspace, existing = _state.find_leaf_any_monitor(hwnd)
    if existing is not None:
        if not _state.is_forced_tiled(hwnd) and is_floating_configured(hwnd) and floating_rule_options(hwnd)["rename_promote"]:
            # Already tiled, but its identity now matches floating_rules -
            # confirmed live with Teams' compact meeting window: it can
            # pass is_manageable() and get tiled on its very first SHOW
            # (before Teams has renamed it), then fire NAMECHANGE moments
            # later with the title floating_rules actually matches. Without
            # this, that NAMECHANGE would just hit the early-return above
            # forever - once tiled, always tiled - so this migrates it out
            # the same way toggle_floating's tiled->floating branch does.
            # Gated on rename_promote (see its docstring) since the SAME
            # NAMECHANGE mechanism otherwise also hijacked Teams' ordinary
            # already-tiled main window the instant the user switched
            # channels/chats, which reshapes its title into the exact same
            # "<name> | Microsoft Teams" pattern a genuine ad-hoc meeting
            # popup uses.
            next_focus = _closest_sibling_leaf(existing)
            _state.remove_leaf(_monitor, existing, _workspace)
            _state.reflow(_monitor, _workspace)
            if next_focus is not None:
                _state.set_focused_leaf(_monitor, next_focus, _workspace)
            _add_floating_window(hwnd, _monitor)
        return
    floating_monitor, floating_workspace = _state.find_floating_any_monitor(hwnd)
    if floating_monitor is not None:
        if floating_workspace != _state.active_workspace(floating_monitor):
            # Re-activating an existing singleton app (e.g. Settings, whose
            # window was hidden on some other workspace) fires this same
            # SHOW/NAMECHANGE path but on an hwnd oriel already tracks - left
            # as a no-op, the window silently stays hidden with no way for
            # the user to tell where it went. Bring its own workspace into
            # view instead, the same as switching to it directly.
            switch_workspace(floating_monitor, floating_workspace)
        return
    if _state.is_sticky(hwnd):
        return
    if is_floating_configured(hwnd):
        # Explicit tiling.floating_rules match takes priority over tiling
        # outright, even for windows that would otherwise pass
        # is_manageable() cleanly (e.g. Windows App's fully-chromed
        # MainWindow) - checked ahead of the is_manageable() gate below so
        # it can't ever get tiled first.
        _floating_settle_retries.pop(hwnd, None)
        _add_floating_window(hwnd)
        return
    if not is_manageable(hwnd):
        # Popups/tool-windows/owned-helpers (e.g. WinUI XAML popup hosts and
        # composition bridges behind autocomplete/IME suggestion UI), or an
        # explicit tiling.ignore_rules match, never pass is_manageable() no
        # matter how many times you check - retrying those flooded this
        # exact path (dozens of hwnds deep) while typing. Only genuinely-
        # initializing app windows (the Firefox timing gotcha) are worth
        # retrying - see _tick_manageable_retries for how.
        if not could_become_manageable(hwnd):
            return
        _manageable_retries.setdefault(hwnd, 0)
        return
    if could_become_floating_configured(hwnd):
        # Passes is_manageable() (real chrome) right now, but its process/
        # class matches a floating_rules entry whose title criteria isn't
        # satisfied YET - give it a few ticks in case a NAMECHANGE renames
        # it into a match (see _tick_floating_settle_retries) before ever
        # committing to tiling. Bounded the same way _manageable_retries
        # is: on exhaustion, whatever it currently is just proceeds
        # through the normal tiling path below.
        count = _floating_settle_retries.get(hwnd, 0)
        if count < MAX_MANAGEABLE_RETRIES:
            _floating_settle_retries[hwnd] = count + 1
            return
        _floating_settle_retries.pop(hwnd, None)
    _manageable_retries.pop(hwnd, None)
    # One-off background thread, not the persistent _border_worker/queue -
    # unlike focus changes (high-frequency, hence that whole coalescing
    # mechanism), a window is only ever newly-shown once, so per-call
    # thread spawn here is cheap and can't turn into a storm. Keeps this
    # DwmSetWindowAttribute/SetWindowPos call off the message-loop thread
    # like every other DWM call in this module (see update_focus_border's
    # docstring for why that's a hard rule).
    threading.Thread(target=border.ensure_rounded, args=(hwnd,), daemon=True).start()
    # Newly opened windows go to whichever monitor the cursor is on, not
    # wherever Windows happened to place the window initially.
    monitor = geometry.monitor_at_cursor()
    active_workspace = _state.active_workspace(monitor)
    workspace = active_workspace

    pending = _pending_autostart_workspace.get(get_process_name(hwnd))
    if pending is not None:
        override_workspace, expiry = pending
        if expiry >= time.monotonic() and override_workspace <= _state.workspace_count(monitor):
            workspace = override_workspace

    _state.insert_hwnd(monitor, hwnd, workspace)
    _state.reflow(monitor, workspace)
    _strip_topmost(hwnd)
    if workspace != active_workspace and win32gui.IsWindow(hwnd):
        # Landed on a workspace that isn't the one currently visible on this
        # monitor - keep it out of sight until switch_workspace brings that
        # workspace into view, same as any other tiled window not on the
        # active workspace (see switch_workspace's own hide/show pairing).
        win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
    elif win32gui.IsWindow(hwnd):
        # A window launched from a background process (e.g. hotkeyd's
        # os.startfile via a hotkey) doesn't reliably get real OS foreground
        # focus on its own - Windows' foreground-lock restriction can deny
        # it, the same restriction _force_foreground exists to bypass
        # elsewhere in this file. Confirmed live: opening a new Windows
        # Terminal via Alt+T left GetForegroundWindow() still pointing at
        # whatever was focused before, so it never got the focus border -
        # not a border bug, the window just never actually became focused.
        _force_foreground(hwnd)
    _pending_repaint_nudges.add(hwnd)
    _persist_workspace_state(monitor)
    update_focus_border()


def _tick_ghost_sweep():
    """Runs on every MANAGEABLE_RETRY_TIMER tick (see run_message_loop) -
    catches any tracked leaf whose window has already gone invalid without
    ever generating another event to trigger enforce_tiled_placement's own
    self-heal (e.g. its DESTROY event was dropped, and it's just sitting
    there, never focused/moved again to trigger a recheck) - left alone, a
    leaf like this silently occupies a full tile share forever. Cheap: just
    an IsWindow check per currently-tracked leaf, typically very few."""
    for monitor, workspace in list(_state.all_monitor_workspaces()):
        for leaf in list(tree.all_leaves(_state.root(monitor, workspace))):
            if not win32gui.IsWindow(leaf.item):
                _unmanage(monitor, workspace, leaf)


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
        if not win32gui.IsWindow(hwnd):
            _manageable_retries.pop(hwnd, None)
            continue
        if count >= MAX_MANAGEABLE_RETRIES:
            _manageable_retries.pop(hwnd, None)
            # Never became a full tiled window across every retry - most
            # likely a legitimate dialog/utility window that structurally
            # can't be tiled (missing chrome/title) or is deliberately
            # excluded via ignore_rules, not a real app still initializing
            # (that case would have succeeded within the retries above).
            # Track it as floating instead of abandoning it outright, so it
            # still gets a workspace/hide-show lifecycle and focus border.
            if could_become_manageable(hwnd):
                _add_floating_window(hwnd)
            continue
        _manageable_retries[hwnd] = count + 1
        on_window_shown(hwnd)


def _tick_floating_settle_retries():
    """Runs on every MANAGEABLE_RETRY_TIMER tick (see run_message_loop) -
    the SAME shared timer _tick_manageable_retries uses, not a separate
    one. See _floating_settle_retries for why this exists. Retry counting
    and exhaustion both happen inside on_window_shown itself (mirroring
    how count is tracked for _manageable_retries) - this just re-invokes
    it for every hwnd still waiting to see if it settles into a
    floating_rules match."""
    for hwnd in list(_floating_settle_retries):
        if not win32gui.IsWindow(hwnd):
            _floating_settle_retries.pop(hwnd, None)
            continue
        on_window_shown(hwnd)


def _reraise_sticky_windows():
    """Some apps (confirmed live: Teams) set WS_EX_TOPMOST on their own
    windows by default, independent of anything oriel does - so a sticky
    window (also topmost, via _raise_floating_window) isn't uniquely on
    top of everything, it's competing with other topmost windows for
    relative order within that same band. oriel only raises it once, at
    creation - activating any OTHER topmost window (e.g. clicking back
    into Teams' main chat/calendar window) moves that one to the top of
    the band instead, visually burying the sticky one with nothing to
    re-assert it afterward. Runs on every EVENT_SYSTEM_FOREGROUND (a real
    focus change, not just window activity) to win that race back - z-
    order only, no activate, so it never steals focus from whatever the
    user just clicked into."""
    for hwnd in _state.sticky_hwnds():
        if win32gui.IsWindow(hwnd):
            threading.Thread(target=_raise_floating_window, args=(hwnd, False), daemon=True).start()


def recheck_if_pending(hwnd):
    """EVENT_OBJECT_LOCATIONCHANGE and EVENT_SYSTEM_FOREGROUND are real
    signals that a window is still settling its own setup (still moving
    itself) or has just become genuinely usable (gained focus) - GlazeWM's
    own Windows backend listens to both of these for this same reason.
    Only acts on hwnds already known-pending (already failed is_manageable
    at least once), so this adds no cost to window activity in general."""
    if hwnd in _manageable_retries:
        on_window_shown(hwnd)


def _strip_topmost(hwnd):
    """oriel's tiled windows are never meant to be topmost - some apps
    (confirmed live: Teams' main Chat/Calendar window) set WS_EX_TOPMOST on
    their own windows independent of anything oriel does, which lets a
    merely-tiled window compete in the topmost z-order band against
    oriel's actual floating/sticky windows and sometimes win, burying them.
    Called once when a window is newly tiled, and again on every
    enforce_tiled_placement check in case an app sets this later (e.g. a
    meeting reminder popping up on an already-tiled window)."""
    if not win32gui.IsWindow(hwnd):
        return
    exstyle = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
    if not (exstyle & win32con.WS_EX_TOPMOST):
        return
    try:
        win32gui.SetWindowPos(
            hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE,
        )
    except win32gui.error:
        pass


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
    if not win32gui.IsWindow(hwnd):
        # A tracked leaf whose window is already gone - normally
        # on_window_destroyed cleans this up via its own DESTROY event, but
        # that event can be dropped entirely under load (confirmed live:
        # during the same RDP-reconnect burst that was flooding this
        # function with crashes from geometry.monitor_of() blowing up on a
        # dead hwnd below). Left unmanaged, a leaf like this silently
        # occupies a full tile share forever, shrinking every real sibling
        # for no visible reason - self-heal here instead of leaving it a
        # permanent ghost.
        _unmanage(monitor, workspace, leaf)
        return
    _strip_topmost(hwnd)
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
    _floating_settle_retries.pop(hwnd, None)
    _active_gestures.discard(hwnd)
    _enforce_attempt_times.pop(hwnd, None)
    _pending_repaint_nudges.discard(hwnd)
    _margin_revalidated.discard(hwnd)
    _pending_hides.pop(hwnd, None)
    _state.remove_forced_tiled(hwnd)
    _floating_toggle_rect.pop(hwnd, None)
    _state.remove_no_border(hwnd)
    if _state.remove_sticky(hwnd):
        if hwnd == _bordered_hwnd:
            update_focus_border()
        return
    if _state.remove_floating(hwnd):
        if hwnd == _bordered_hwnd:
            update_focus_border()
        return
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
        monitor, workspace = _state.find_floating_any_monitor(hwnd)
        if monitor is None:
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
        if leaf is not None:
            if workspace != _state.active_workspace(monitor):
                continue
            _unmanage(monitor, workspace, leaf)
            continue
        monitor, workspace = _state.find_floating_any_monitor(hwnd)
        if monitor is None or workspace != _state.active_workspace(monitor):
            continue
        _state.remove_floating(hwnd)
        if hwnd == _bordered_hwnd:
            update_focus_border()


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


def toggle_floating(_data=None):
    """Alt+V - manually moves the focused window between tiled and floating
    on its own monitor/workspace, without closing or refocusing it (unlike
    _unmanage, the window itself never goes away, so there's no sibling-
    focus handoff to do - it just keeps whatever OS focus it already had)."""
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return
    monitor, workspace, leaf = _state.find_leaf_any_monitor(hwnd)
    if leaf is not None:
        # Same "who takes over this tile" logic _unmanage uses, but only
        # for tiling's own internal bookkeeping (compute BEFORE removal) -
        # OS focus stays on hwnd itself, now floating.
        next_focus = _closest_sibling_leaf(leaf)
        _state.remove_leaf(monitor, leaf, workspace)
        _state.reflow(monitor, workspace)
        if next_focus is not None:
            _state.set_focused_leaf(monitor, next_focus, workspace)
        _state.add_floating(monitor, hwnd, workspace)
        _state.remove_forced_tiled(hwnd)
        threading.Thread(target=border.ensure_rounded, args=(hwnd,), daemon=True).start()
        saved_rect = _floating_toggle_rect.pop(hwnd, None)
        if saved_rect is not None and win32gui.IsWindow(hwnd):
            left, top, right, bottom = saved_rect
            try:
                win32gui.SetWindowPos(
                    hwnd, 0, left, top, right - left, bottom - top,
                    win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE,
                )
            except win32gui.error:
                pass
        # Every OTHER floating window is raised via this same helper (see
        # _add_floating_window) - toggle_floating's plain HWND_TOP bump
        # only won the top of the normal z-order band for a moment, never
        # actually topmost, so it lost out to real floating/sticky windows.
        # activate=False: the window is already focused (this hotkey acts
        # on GetForegroundWindow), nothing to steal back.
        threading.Thread(target=_run_logged, args=(_raise_floating_window, (hwnd, False)), daemon=True).start()
    else:
        monitor, workspace = _state.find_floating_any_monitor(hwnd)
        if monitor is None:
            return
        rect = geometry.safe_get_window_rect(hwnd)
        if rect is not None:
            _floating_toggle_rect[hwnd] = rect
        _state.remove_floating(hwnd)
        _state.insert_hwnd(monitor, hwnd, workspace)
        _state.reflow(monitor, workspace)
        if is_floating_configured(hwnd):
            # Only rules would otherwise keep re-floating this hwnd on its
            # next rename - nothing to override for a window that was never
            # rule-matched to begin with.
            _state.add_forced_tiled(hwnd)
    _persist_workspace_state(monitor)
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


def _capture_zorder(hwnds):
    """Returns the subset of `hwnds` currently on screen, ordered topmost
    first - via one EnumWindows scan, which itself enumerates top-level
    windows in real Z-order (not a per-window property query, just a
    membership check per hwnd, so this is cheap even scanning every
    desktop window). Used by switch_workspace to snapshot a workspace's
    real stacking order right before hiding it, so switching back later
    can restore it instead of resetting to tree/set-iteration order."""
    wanted = set(hwnds)
    order = []

    def callback(hwnd, _):
        if hwnd in wanted:
            order.append(hwnd)
        return True

    win32gui.EnumWindows(callback, None)
    return order


# (monitor, workspace) -> [hwnd, ...] topmost first, captured by
# switch_workspace right before hiding that workspace - see _capture_zorder.
_workspace_zorder = {}

# monitor -> workspace switched away FROM last, for switch_workspace's
# optional "pressing the current workspace's own key again returns to
# whatever workspace you were on before" behavior (tiling.
# workspace_toggle_back) - updated on every actual switch, including a
# toggle-back itself, so pressing the same key repeatedly just alternates
# between the two workspaces.
_previous_workspace = {}


def switch_workspace(monitor, target_workspace):
    """Hides every window on monitor's currently active workspace and shows
    target_workspace's, without touching tree structure at all - a
    workspace switch is a visibility change, not a hide/destroy, so each
    workspace's layout survives switching away from it untouched."""
    current_workspace = _state.active_workspace(monitor)
    if target_workspace == current_workspace:
        if not _state.workspace_toggle_back:
            return
        target_workspace = _previous_workspace.get(monitor)
        if target_workspace is None or target_workspace == current_workspace:
            return
    if target_workspace > _state.workspace_count(monitor):
        return
    _previous_workspace[monitor] = current_workspace

    current_hwnds = [leaf.item for leaf in tree.all_leaves(_state.root(monitor, current_workspace))]
    current_hwnds += list(_state.floating_hwnds(monitor, current_workspace))
    _workspace_zorder[(monitor, current_workspace)] = _capture_zorder(current_hwnds)

    for leaf in tree.all_leaves(_state.root(monitor, current_workspace)):
        if win32gui.IsWindow(leaf.item):
            win32gui.ShowWindow(leaf.item, win32con.SW_HIDE)
    for hwnd in list(_state.floating_hwnds(monitor, current_workspace)):
        if win32gui.IsWindow(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_HIDE)

    _state.set_active_workspace(monitor, target_workspace)

    for leaf in tree.all_leaves(_state.root(monitor, target_workspace)):
        if win32gui.IsWindow(leaf.item):
            win32gui.ShowWindow(leaf.item, win32con.SW_SHOWNA)
    _state.reflow(monitor, target_workspace)
    for hwnd in list(_state.floating_hwnds(monitor, target_workspace)):
        if win32gui.IsWindow(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_SHOWNA)

    # Restore this workspace's last-known relative stacking order (bottom-
    # most first, so each subsequent HWND_TOP layers on top of the one
    # before it, ending with whatever was topmost before back on top) -
    # without this, tiled windows always come back in tree order and
    # floating windows in arbitrary set-iteration order, silently
    # reshuffling every switch. Falls back to whatever ShowWindow just
    # left them at if this workspace has never been switched away from
    # before (nothing captured yet).
    #
    # Deliberately NOT SWP_ASYNCWINDOWPOS here, unlike nearly every other
    # SetWindowPos call in this module - an async Z-order change is only
    # POSTED to the target window's own thread's queue, not applied
    # immediately, and different windows belong to different threads/
    # processes with no ordering guarantee between them. Issuing these
    # async scrambled the intended bottom-to-top sequence in practice
    # (confirmed live: floating windows ended up behind tiled ones after a
    # switch). switch_workspace only runs on an explicit hotkey press, not
    # a hot/frequent path, so a synchronous call here is an acceptable,
    # deliberate exception to the "always async" rule - see reflow()'s own
    # comment for why that rule exists elsewhere.
    for hwnd in reversed(_workspace_zorder.get((monitor, target_workspace), [])):
        if win32gui.IsWindow(hwnd):
            try:
                win32gui.SetWindowPos(
                    hwnd, win32con.HWND_TOP, 0, 0, 0, 0,
                    win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE,
                )
            except win32gui.error:
                pass

    # Whichever window was topmost when this workspace was last left (see
    # _capture_zorder, captured before hiding) should get real OS focus
    # back too - _state.focused_leaf only ever tracks a TILED leaf
    # (floating hwnds are plain tracked hwnds, never tree Leaf objects), so
    # unconditionally using it here always force-focuses a tiled window
    # even when a floating one was actually topmost/focused before,
    # undoing the z-order restore above via _force_foreground's own
    # BringWindowToTop. Falls back to focused_leaf only the first time
    # this workspace is ever switched to (nothing captured yet).
    saved_order = _workspace_zorder.get((monitor, target_workspace), [])
    to_focus = saved_order[0] if saved_order else None
    if to_focus is None:
        focused = _state.focused_leaf(monitor, target_workspace)
        to_focus = focused.item if focused is not None else None
    if to_focus is not None and win32gui.IsWindow(to_focus):
        _force_foreground(to_focus)

    _persist_workspace_state(monitor)
    update_focus_border()


def switch_workspace_action(data):
    if not data or "workspace" not in data:
        return
    switch_workspace(_monitor_for_workspace_switch(), data["workspace"])


def move_to_workspace(target_workspace):
    """Reassigns the focused window to target_workspace on its own
    monitor, then switches that monitor's view to target_workspace too -
    you follow the window instead of it just disappearing (i3's default
    "move, don't follow" behavior, which this used to match, wasn't
    wanted here). Handles a floating window the same as a tiled one (just
    re-filed under target_workspace's floating set instead of re-inserted
    into its tree) - a sticky window has no single workspace of its own to
    move to at all (see find_floating_any_monitor, which never finds one),
    so this is naturally a no-op for those."""
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return
    monitor, current_workspace, leaf = _state.find_leaf_any_monitor(hwnd)
    if leaf is not None:
        if current_workspace == target_workspace or target_workspace > _state.workspace_count(monitor):
            return
        _state.remove_leaf(monitor, leaf, current_workspace)
        # Always a new top-level sibling of target_workspace's root, not next
        # to whatever leaf happens to be focused there (insert_hwnd's usual
        # behavior) - a predictable landing spot (always a new edge column/
        # row) regardless of that workspace's focus history.
        _state.insert_hwnd_at_root(monitor, hwnd, target_workspace)
        _state.reflow(monitor, current_workspace)
    else:
        monitor, current_workspace = _state.find_floating_any_monitor(hwnd)
        if monitor is None or current_workspace == target_workspace or target_workspace > _state.workspace_count(monitor):
            return
        _state.remove_floating(hwnd)
        _state.add_floating(monitor, hwnd, target_workspace)

    switch_workspace(monitor, target_workspace)
    # switch_workspace restores whichever window was topmost/focused the
    # LAST time target_workspace was visible, which usually isn't the one
    # just moved here - force it to the front instead, since "move to
    # workspace" following along should mean landing on the moved window,
    # not whatever used to have focus there.
    if win32gui.IsWindow(hwnd):
        _force_foreground(hwnd)


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
    floating_monitor, floating_workspace = _state.find_floating_any_monitor(hwnd)
    if floating_monitor is not None:
        _resync_floating_monitor(hwnd, floating_monitor)
        return

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

    dest_workspace = workspace
    if policy.is_move_gesture(kind, actual, expected):
        cursor_pos = win32api.GetCursorPos()
        dest_monitor = geometry.monitor_at_point(cursor_pos)
        cross = dest_monitor != monitor
        # Workspace numbers are independent per monitor (see tiling.
        # workspaces), not one index shared across every monitor - reusing
        # the origin's workspace for a cross-monitor drop can land it on a
        # workspace that isn't the one actually visible on that monitor
        # (confirmed live: it then never gets hidden, and just sits on top
        # of whatever workspace IS visible there, overlapping it).
        dest_workspace = _state.active_workspace(dest_monitor) if cross else workspace

        search_rects = (
            tree.compute_all_rects(_state.root(dest_monitor, dest_workspace), _state.work_area(dest_monitor), _state.inner_gap)
            if cross else all_rects
        )

        outcome = policy.decide_move(leaf, monitor, dest_monitor, cursor_pos, search_rects)
        dirty = _state.apply_outcome(monitor, leaf, outcome, workspace, dest_workspace)
    else:
        # Unlike decide_move's single Outcome, this is a list (0/1/2 items)
        # since a corner drag can need BOTH a width and a height adjustment,
        # each against its own (possibly different) ancestor container -
        # see policy.decide_resize/_resize_ancestor.
        adjustments = policy.decide_resize(leaf, actual, expected, all_rects, _state.inner_gap)
        dirty = {(monitor, workspace)}
        for adjustment in adjustments:
            dirty |= _state.apply_outcome(monitor, leaf, adjustment, workspace)

    # An empty/NoOp resize (dropped on empty space, not onto another tile)
    # leaves the tree - and therefore the leaf's computed target rect -
    # completely unchanged, but the window's REAL on-screen position is now
    # wherever it was physically dropped. reflow()'s skip-if-unchanged cache
    # only knows about the target rect, so it would otherwise treat this as
    # "nothing to do" and never re-issue the SetWindowPos that snaps it back.
    _state.forget_requested_rect(hwnd)
    for dirty_monitor, dirty_workspace in dirty:
        _state.reflow(dirty_monitor, dirty_workspace)


def _resync_floating_monitor(hwnd, floating_monitor):
    """Floating windows have no tree leaf, so the tiled logic above never
    runs for them - dragging one to a different monitor otherwise left it
    tracked under its ORIGINAL monitor/workspace forever (confirmed live:
    switching workspaces on the origin monitor kept hiding it even after it
    visually lived on a different monitor). Re-derive its real monitor from
    where it actually is now and re-file it under that monitor's own active
    workspace, the same landing rule cross-monitor tiled drops already use."""
    if not win32gui.IsWindow(hwnd):
        return
    current_monitor = geometry.monitor_of(hwnd)
    if current_monitor is None or current_monitor == floating_monitor:
        return
    _state.remove_floating(hwnd)
    _state.add_floating(current_monitor, hwnd, _state.active_workspace(current_monitor))
    _persist_workspace_state(floating_monitor)
    _persist_workspace_state(current_monitor)


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
            _reraise_sticky_windows()
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
                # Each tick isolated in its own try/except - unlike
                # _win_event_proc's single guard around one event's handler,
                # a single uncaught exception here would otherwise take the
                # WHOLE daemon down (confirmed live twice: GetCursorPos
                # raising ERROR_ACCESS_DENIED during a secure-desktop prompt,
                # and SetWindowPos raising ERROR_INVALID_PARAMETER for a
                # window in some transitional state) - nothing before this
                # ever caught it, since run_message_loop itself has no
                # handler and daemon.run()'s only wraps the whole process
                # lifetime, logging then re-raising. Separate try/excepts per
                # tick (not one around all six) also means one persistently
                # failing tick can't starve the others of their own turn.
                for _tick in (
                    _tick_manageable_retries, _tick_floating_settle_retries, _tick_repaint_nudges,
                    _tick_pending_hides, _tick_focus_border, _tick_ghost_sweep,
                ):
                    try:
                        _tick()
                    except Exception:
                        logger.exception("timer tick failed: %s", _tick.__name__)
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
