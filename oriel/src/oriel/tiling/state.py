"""Encapsulates all per-monitor tiling state and the operations that legally
mutate it, behind a small interface - nothing outside this module touches
the underlying dicts directly (information hiding: callers depend on what
the state means, not how it's represented).

Every dict here is keyed by (monitor, workspace) instead of bare monitor,
even though only DEFAULT_WORKSPACE exists today - widening the key now is
free (workspace always defaults to the same constant, zero behavior change)
but avoids a much larger rekeying refactor later if/when real workspace
switching is added. A tiling placement's true identity is (monitor,
workspace); "monitor" alone was always an incomplete approximation of it.
"""
import win32con
import win32gui

from oriel.tiling import geometry
from oriel.tiling import policy
from oriel.tiling import tree

DEFAULT_WORKSPACE = 0

DEFAULT_GAP = 8
DEFAULT_OUTER_GAP = {"top": 0, "right": 0, "bottom": 0, "left": 0}
DEFAULT_RESIZE_STEP = 0.05
DEFAULT_BORDER = {"enabled": True, "color": "#cba6f7", "corner_style": "rounded"}
DEFAULT_FLOATING = {"center_on_open": False}
DEFAULT_WORKSPACE_TOGGLE_BACK = False


class TilingState:
    def __init__(self):
        self._roots = {}
        self._focused_leaf = {}
        self._fullscreen_leaf = {}
        self._active_workspace = {}
        # (monitor, workspace) -> set of hwnd, for windows that aren't part
        # of the tiling tree at all (structurally can't be tiled, or
        # deliberately excluded via ignore_rules) but still get a
        # workspace/hide-show lifecycle like tiled windows do - see
        # events._add_floating_window.
        self._floating = {}
        # hwnd -> True, for floating windows explicitly marked "sticky" in
        # config.json's tiling.floating_rules - unlike _floating (keyed by
        # (monitor, workspace), hidden/shown on every switch_workspace),
        # these are workspace-independent: always visible, never hidden,
        # not tied to any single (monitor, workspace) at all. Kept as its
        # own flat set rather than a magic workspace value, so every other
        # method that iterates _floating by key never needs to special-
        # case it.
        self._sticky = set()
        # hwnd -> True, for windows whose tiling.floating_rules match sets
        # "border": false - opts out of both corner-rounding and focus-
        # border highlighting entirely (see events.update_focus_border/
        # _add_floating_window), for windows where oriel's usual per-window
        # chrome looks out of place (e.g. a small screen-share control bar).
        self._no_border = set()
        # hwnd -> last (left, top, right, bottom) actually requested via
        # SetWindowPos, frame-expanded - lets reflow() skip re-issuing an
        # identical request to a leaf nothing changed for, instead of
        # unconditionally repositioning every window on every call (which
        # was generating a real LOCATIONCHANGE per untouched sibling on
        # every single reflow, each one re-triggering enforce_tiled_
        # placement's own mismatch check - a needless cascade amplifier).
        self._last_requested_rect = {}
        self.inner_gap = DEFAULT_GAP
        self.outer_gap = DEFAULT_OUTER_GAP
        self.resize_step = DEFAULT_RESIZE_STEP
        self.border = DEFAULT_BORDER
        self.floating = DEFAULT_FLOATING
        self.workspace_toggle_back = DEFAULT_WORKSPACE_TOGGLE_BACK
        self.workspaces = {}  # stable monitor id -> configured workspace count

    def reset(self):
        """Drops all monitor/window/workspace-derived state (everything
        keyed by HMONITOR, which can go stale across a display change) -
        settings loaded from config (gaps, resize_step, border, workspaces)
        are untouched. Used to rebuild tiling state in place after a
        WM_DISPLAYCHANGE, without restarting the daemon process."""
        self._roots = {}
        self._focused_leaf = {}
        self._fullscreen_leaf = {}
        self._active_workspace = {}
        self._last_requested_rect = {}
        self._floating = {}
        self._sticky = set()
        self._no_border = set()

    @staticmethod
    def _key(monitor, workspace=DEFAULT_WORKSPACE):
        return (monitor, workspace)

    def root(self, monitor, workspace=DEFAULT_WORKSPACE):
        return self._roots.get(self._key(monitor, workspace))

    def set_root(self, monitor, new_root, workspace=DEFAULT_WORKSPACE):
        self._roots[self._key(monitor, workspace)] = new_root

    def focused_leaf(self, monitor, workspace=DEFAULT_WORKSPACE):
        return self._focused_leaf.get(self._key(monitor, workspace))

    def set_focused_leaf(self, monitor, leaf, workspace=DEFAULT_WORKSPACE):
        self._focused_leaf[self._key(monitor, workspace)] = leaf

    def fullscreen_leaf(self, monitor, workspace=DEFAULT_WORKSPACE):
        return self._fullscreen_leaf.get(self._key(monitor, workspace))

    def set_fullscreen_leaf(self, monitor, leaf, workspace=DEFAULT_WORKSPACE):
        self._fullscreen_leaf[self._key(monitor, workspace)] = leaf

    def clear_fullscreen_leaf(self, monitor, workspace=DEFAULT_WORKSPACE):
        self._fullscreen_leaf.pop(self._key(monitor, workspace), None)

    def active_workspace(self, monitor):
        active = self._active_workspace.get(monitor)
        if active is not None:
            return active
        # 0 is reserved for "unconfigured monitor" and unreachable by any
        # hotkey once real workspaces exist (Alt+1-9,0 only maps to 1-10) -
        # a configured monitor with no explicit active workspace yet
        # defaults to workspace 1, not the unreachable sentinel.
        return 1 if self.workspace_count(monitor) > 0 else DEFAULT_WORKSPACE

    def set_active_workspace(self, monitor, workspace):
        self._active_workspace[monitor] = workspace

    def workspace_count(self, monitor):
        """0 means unconfigured - today's single-implicit-workspace behavior."""
        stable_id = geometry.stable_monitor_id(monitor)
        if stable_id is None:
            return 0
        return self.workspaces.get(stable_id, 0)

    def known_monitors(self):
        return {monitor for monitor, _workspace in self._roots}

    def all_monitor_workspaces(self):
        """Every (monitor, workspace) pair with a root, active or not -
        for callers that need to touch every tracked window regardless of
        which workspace is currently showing (e.g. the quit teardown,
        which un-hides everything, not just the active workspace)."""
        return list(self._roots.keys())

    def floating_hwnds(self, monitor, workspace=DEFAULT_WORKSPACE):
        return self._floating.get(self._key(monitor, workspace), set())

    def add_floating(self, monitor, hwnd, workspace=DEFAULT_WORKSPACE):
        self._floating.setdefault(self._key(monitor, workspace), set()).add(hwnd)

    def remove_floating(self, hwnd):
        """Removes hwnd from wherever it's floating-tracked, if anywhere.
        Returns whether it was found."""
        for hwnds in self._floating.values():
            if hwnd in hwnds:
                hwnds.discard(hwnd)
                return True
        return False

    def find_floating_any_monitor(self, hwnd):
        """Returns (monitor, workspace), or (None, None)."""
        for (monitor, workspace), hwnds in self._floating.items():
            if hwnd in hwnds:
                return monitor, workspace
        return None, None

    def all_floating_monitor_workspaces(self):
        """Every (monitor, workspace) pair with at least one floating
        window - mirrors all_monitor_workspaces for the floating set."""
        return list(self._floating.keys())

    def add_sticky(self, hwnd):
        self._sticky.add(hwnd)

    def remove_sticky(self, hwnd):
        """Returns whether hwnd was sticky-tracked."""
        if hwnd in self._sticky:
            self._sticky.discard(hwnd)
            return True
        return False

    def is_sticky(self, hwnd):
        return hwnd in self._sticky

    def sticky_hwnds(self):
        return list(self._sticky)

    def add_no_border(self, hwnd):
        self._no_border.add(hwnd)

    def remove_no_border(self, hwnd):
        self._no_border.discard(hwnd)

    def has_no_border(self, hwnd):
        return hwnd in self._no_border

    def migrate_workspace(self, monitor, from_workspace, to_workspace):
        """Re-keys everything under (monitor, from_workspace) to (monitor,
        to_workspace) - for a monitor whose workspace config just went from
        unconfigured to configured mid-session, so windows already tiled at
        the now-unreachable DEFAULT_WORKSPACE aren't stranded there. Purely
        internal bookkeeping - windows stay exactly as visible/hidden as
        they already were."""
        from_key, to_key = self._key(monitor, from_workspace), self._key(monitor, to_workspace)
        if from_key in self._roots:
            self._roots[to_key] = self._roots.pop(from_key)
            if from_key in self._focused_leaf:
                self._focused_leaf[to_key] = self._focused_leaf.pop(from_key)
            if from_key in self._fullscreen_leaf:
                self._fullscreen_leaf[to_key] = self._fullscreen_leaf.pop(from_key)
        if from_key in self._floating:
            self._floating[to_key] = self._floating.pop(from_key)

    def work_area(self, monitor):
        return geometry.work_area(monitor, self.outer_gap)

    def compute_rects(self, monitor, workspace=DEFAULT_WORKSPACE):
        root = self.root(monitor, workspace)
        return tree.compute_rects(root, self.work_area(monitor), self.inner_gap)

    def insert_hwnd(self, monitor, hwnd, workspace=DEFAULT_WORKSPACE):
        target = self.focused_leaf(monitor, workspace)
        rect = self.compute_rects(monitor, workspace).get(target, self.work_area(monitor))
        new_root, new_leaf = tree.insert(self.root(monitor, workspace), target, hwnd, rect)
        self.set_root(monitor, new_root, workspace)
        self.set_focused_leaf(monitor, new_leaf, workspace)
        return new_leaf

    def insert_hwnd_at_root(self, monitor, hwnd, workspace=DEFAULT_WORKSPACE):
        """Same as insert_hwnd, but always lands as a new top-level sibling
        of the workspace's root (see tree.insert_at_root) instead of next
        to whatever leaf is currently focused there - used by events.
        move_to_workspace for a predictable landing spot."""
        new_root, new_leaf = tree.insert_at_root(self.root(monitor, workspace), hwnd, self.work_area(monitor))
        self.set_root(monitor, new_root, workspace)
        self.set_focused_leaf(monitor, new_leaf, workspace)
        return new_leaf

    def remove_leaf(self, monitor, leaf, workspace=DEFAULT_WORKSPACE):
        self.set_root(monitor, tree.remove(self.root(monitor, workspace), leaf), workspace)
        if self.focused_leaf(monitor, workspace) is leaf:
            self.set_focused_leaf(monitor, None, workspace)
        if self.fullscreen_leaf(monitor, workspace) is leaf:
            self.clear_fullscreen_leaf(monitor, workspace)
        geometry.invalidate_frame_margins(leaf.item)
        self._last_requested_rect.pop(leaf.item, None)

    def find_leaf_any_monitor(self, hwnd):
        """Returns (monitor, workspace, leaf), or (None, None, None)."""
        for monitor, workspace in list(self._roots):
            leaf = tree.find_leaf(self._roots[(monitor, workspace)], hwnd)
            if leaf is not None:
                return monitor, workspace, leaf
        return None, None, None

    def forget_requested_rect(self, hwnd):
        """Forces the next reflow() to re-issue SetWindowPos for hwnd even
        if its computed target rect hasn't changed - for when the window's
        ACTUAL position drifted from what was last requested (app moved
        itself, restored a remembered position, etc). Without this,
        reflow()'s dedup would see an unchanged target and wrongly assume
        there's nothing to re-assert. Purely a "skip if unchanged" cache -
        reflow() no longer branches sync/async on this, so popping it here
        can no longer affect blocking behavior, only whether a redundant
        SetWindowPos gets skipped."""
        self._last_requested_rect.pop(hwnd, None)

    def hwnd_workspaces(self, monitor):
        """{hwnd: workspace} for every hwnd currently tiled OR floating
        anywhere on `monitor` (across all its workspaces) - used for
        persistence."""
        result = {}
        for (mon, workspace), root in self._roots.items():
            if mon != monitor:
                continue
            for leaf in tree.all_leaves(root):
                result[leaf.item] = workspace
        for (mon, workspace), hwnds in self._floating.items():
            if mon != monitor:
                continue
            for hwnd in hwnds:
                result[hwnd] = workspace
        return result

    def reflow(self, monitor, workspace=DEFAULT_WORKSPACE):
        rects = self.compute_rects(monitor, workspace)

        fullscreen_leaf = self.fullscreen_leaf(monitor, workspace)
        if fullscreen_leaf is not None:
            rects[fullscreen_leaf] = geometry.monitor_bounds(monitor)

        for leaf, rect in rects.items():
            if not win32gui.IsWindow(leaf.item) or win32gui.IsIconic(leaf.item):
                continue
            left, top, right, bottom = geometry.expand_rect_for_frame(rect, leaf.item)
            requested = (left, top, right, bottom)
            if self._last_requested_rect.get(leaf.item) == requested:
                continue  # nothing changed for this leaf - don't re-broadcast a position it's already at
            # SWP_ASYNCWINDOWPOS: without it, SetWindowPos blocks this
            # thread until the TARGET window's own thread processes the
            # request - if that app is busy or still starting up (e.g.
            # mid-keystroke, or a WinUI/XAML app like Windows Terminal still
            # doing heavy startup layout work), this single-threaded
            # daemon's whole message loop stalls with it, unable to reach
            # ANYTHING else queued (hotkeys, IPC, other windows' events)
            # until the target becomes responsive again. Confirmed live
            # twice now: a workspace-switch hotkey stalled 4+ seconds while
            # typing in VS Code, and a synchronous first-positioning call
            # (a since-removed exception that used to live here) compounded
            # into a sustained multi-second freeze against a slow-starting
            # Windows Terminal window. ALWAYS async, no exceptions - GlazeWM's
            # own SetWindowPos wrapper (native_window.rs) does the same,
            # unconditionally, for every call, new window or not.
            #
            # A brand-new window's first paint can still occasionally land
            # before its own thread has caught up with this posted resize
            # (observed live via screenshot: solid black rect until nudged
            # again) - handled by a one-shot ASYNC follow-up reposition in
            # events.py's _tick_repaint_nudges, not by ever blocking here.
            # GlazeWM hits the same first-move glitch (their own comment:
            # "the window might be sized incorrectly after the first move")
            # and fixes it the same way: call SetWindowPos again, still async.
            flags = (
                win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE
                | win32con.SWP_FRAMECHANGED | win32con.SWP_ASYNCWINDOWPOS
            )
            try:
                win32gui.SetWindowPos(leaf.item, 0, left, top, right - left, bottom - top, flags)
            except win32gui.error as exc:
                # ERROR_ACCESS_DENIED (5): the OS transiently rejects
                # SetWindowPos while the target window is mid-transition
                # (e.g. being shown/restored) - not a bug, and the window
                # will get another LOCATIONCHANGE/FOREGROUND event of its
                # own once the transition finishes, which naturally retries
                # this. Letting it propagate would cost a full traceback
                # format+disk-write per occurrence on this single thread -
                # observed live flooding to dozens within a second while
                # switching back to a window mid-show.
                if exc.winerror == 5:
                    continue
                raise
            self._last_requested_rect[leaf.item] = requested

    def reflow_all(self):
        for monitor, workspace in list(self._roots):
            self.reflow(monitor, workspace)

    def apply_outcome(self, monitor, leaf, outcome, workspace=DEFAULT_WORKSPACE):
        """Applies a policy.Outcome to this state - the one place that
        translates a pure decision into actual tree mutations. Returns the
        set of (monitor, workspace) pairs that need reflowing."""
        if isinstance(outcome, policy.Swap):
            outcome.leaf.item, outcome.target.item = outcome.target.item, outcome.leaf.item
            return {(monitor, workspace)}

        if isinstance(outcome, policy.InsertAtFocused):
            hwnd = leaf.item
            self.remove_leaf(monitor, leaf, workspace)
            self.insert_hwnd(outcome.dest_monitor, hwnd, workspace)
            return {(monitor, workspace), (outcome.dest_monitor, workspace)}

        if isinstance(outcome, policy.InsertNear):
            hwnd = leaf.item
            dest_monitor = outcome.dest_monitor
            target = outcome.target
            self.remove_leaf(monitor, leaf, workspace)

            if outcome.axis is None:
                new_root, new_leaf = tree.insert(self.root(dest_monitor, workspace), target, hwnd, outcome.target_rect)
            else:
                parent = target.parent
                if parent is not None and parent.orientation == outcome.axis:
                    new_root, new_leaf = tree.insert(
                        self.root(dest_monitor, workspace), target, hwnd, outcome.target_rect, before=outcome.before
                    )
                else:
                    new_root, new_leaf = tree.insert_nested(
                        self.root(dest_monitor, workspace), target, hwnd, outcome.axis, before=outcome.before
                    )
            self.set_root(dest_monitor, new_root, workspace)
            self.set_focused_leaf(dest_monitor, new_leaf, workspace)
            return {(monitor, workspace), (dest_monitor, workspace)}

        if isinstance(outcome, policy.AdjustRatio):
            policy.clamp_and_apply_ratio(outcome.parent, outcome.index, outcome.neighbor_index, outcome.new_ratio)
            return {(monitor, workspace)}

        return {(monitor, workspace)}  # NoOp
