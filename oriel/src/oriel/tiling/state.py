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

# How many consecutive "learned a bigger minimum, try again" passes reflow()
# allows within one triggering event, before accepting the current
# (possibly overflowing) layout instead of continuing to renegotiate - see
# reflow(). Mirrors this codebase's existing bounded-retry pattern
# (MAX_MANAGEABLE_RETRIES in events.py) rather than retrying unboundedly.
MAX_MIN_SIZE_LEARN_PASSES = 5


class TilingState:
    def __init__(self):
        self._roots = {}
        self._focused_leaf = {}
        self._fullscreen_leaf = {}
        self.inner_gap = DEFAULT_GAP
        self.outer_gap = DEFAULT_OUTER_GAP

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

    def work_area(self, monitor):
        return geometry.work_area(monitor, self.outer_gap)

    def compute_rects(self, monitor, workspace=DEFAULT_WORKSPACE):
        root = self.root(monitor, workspace)
        min_sizes = {leaf.item: geometry.min_size(leaf.item) for leaf in tree.all_leaves(root)}
        return tree.compute_rects(root, self.work_area(monitor), self.inner_gap, min_sizes)

    def insert_hwnd(self, monitor, hwnd, workspace=DEFAULT_WORKSPACE):
        target = self.focused_leaf(monitor, workspace)
        rect = self.compute_rects(monitor, workspace).get(target, self.work_area(monitor))
        new_root, new_leaf = tree.insert(self.root(monitor, workspace), target, hwnd, rect)
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
        geometry.invalidate_min_size(leaf.item)

    def find_leaf_any_monitor(self, hwnd):
        """Returns (monitor, workspace, leaf), or (None, None, None)."""
        for monitor, workspace in list(self._roots):
            leaf = tree.find_leaf(self._roots[(monitor, workspace)], hwnd)
            if leaf is not None:
                return monitor, workspace, leaf
        return None, None, None

    def reflow(self, monitor, workspace=DEFAULT_WORKSPACE, _min_size_pass=0):
        rects = self.compute_rects(monitor, workspace)

        fullscreen_leaf = self.fullscreen_leaf(monitor, workspace)
        if fullscreen_leaf is not None:
            rects[fullscreen_leaf] = geometry.monitor_bounds(monitor)

        learned_new_minimum = False
        for leaf, rect in rects.items():
            if not win32gui.IsWindow(leaf.item) or win32gui.IsIconic(leaf.item):
                continue
            left, top, right, bottom = geometry.expand_rect_for_frame(rect, leaf.item)
            win32gui.SetWindowPos(
                leaf.item, 0, left, top, right - left, bottom - top,
                win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE,
            )
            if leaf is fullscreen_leaf:
                continue  # fullscreen intentionally ignores tile minimums
            try:
                actual_left, actual_top, actual_right, actual_bottom = win32gui.GetWindowRect(leaf.item)
            except win32gui.error:
                continue
            actual_w, actual_h = actual_right - actual_left, actual_bottom - actual_top
            requested_w, requested_h = right - left, bottom - top
            if actual_w > requested_w or actual_h > requested_h:
                # The window refused to shrink to what was asked - it has an
                # enforced minimum size. Remember it so the next layout pass
                # gives it (and only it) that floor instead of fighting
                # forever over the same impossible request (task 13).
                if geometry.learn_min_size(leaf.item, actual_w, actual_h):
                    learned_new_minimum = True

        if learned_new_minimum and _min_size_pass < MAX_MIN_SIZE_LEARN_PASSES:
            self.reflow(monitor, workspace, _min_size_pass + 1)

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
