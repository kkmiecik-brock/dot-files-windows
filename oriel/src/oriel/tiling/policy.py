"""Pure decision logic for what a finished move/resize gesture should do to
the tree - no Win32 imports, no state mutation, no I/O. Every function here
takes plain geometry values and tree.py objects (themselves already
Win32-free) and returns a small Outcome value describing intent. The caller
(state.TilingState.apply_outcome) is the only place that actually performs
the tree mutation and reflow.

Keeping this pure is what makes it unit-testable the same way tree.py is,
without mocking win32gui/win32api/ctypes anywhere.
"""
from dataclasses import dataclass
from typing import Optional

from oriel.tiling import tree

# Tuning constants for gesture interpretation - centralized here instead of
# scattered as repeated magic numbers across the decision logic.
RESIZE_TOLERANCE = 8  # px; below this, actual==expected counts as "no real change"
DROP_ZONE_FRACTION = 0.20  # how close to a target's edge counts as "split here" vs swap/center
RATIO_MIN = 0.05
RATIO_MAX = 0.95


@dataclass
class NoOp:
    pass


@dataclass
class Swap:
    leaf: tree.Leaf
    target: tree.Leaf


@dataclass
class InsertAtFocused:
    """Dropped onto empty space on another monitor - join its focused tile."""
    dest_monitor: int


@dataclass
class InsertNear:
    """Insert next to `target`: a plain sibling insert if axis is None,
    otherwise as a same-orientation sibling or a newly nested sub-container.
    target_rect is captured at decision time (not recomputed at apply time)
    since it's only used for orientation-by-aspect-ratio in the bare-root-wrap
    case inside tree.insert()."""
    dest_monitor: int
    target: object
    axis: Optional[str]  # None | "horizontal" | "vertical"
    before: bool
    target_rect: tuple


@dataclass
class AdjustRatio:
    parent: object
    index: int
    neighbor_index: int
    new_ratio: float


Outcome = object  # NoOp | Swap | InsertAtFocused | InsertNear | AdjustRatio


def is_move_gesture(kind, actual_rect, expected_rect):
    """kind is "move"/"resize" when known (from drag.py via IPC); None falls
    back to guessing from the size delta (native OS drags that never went
    through the drag module, so no ground-truth kind is available)."""
    if kind is not None:
        return kind == "move"
    dw = (actual_rect[2] - actual_rect[0]) - (expected_rect[2] - expected_rect[0])
    dh = (actual_rect[3] - actual_rect[1]) - (expected_rect[3] - expected_rect[1])
    return abs(dw) <= RESIZE_TOLERANCE and abs(dh) <= RESIZE_TOLERANCE


def decide_move(leaf, monitor, dest_monitor, cursor_pos, search_rects):
    """Decides what a finished MOVE gesture should do: swap with the tile
    under the cursor, insert near it (as a sibling or nested split), or join
    the destination monitor's focused tile if dropped on empty space.
    `search_rects` must be the destination monitor's compute_all_rects()
    result if cross-monitor, else the current monitor's."""
    cross = dest_monitor != monitor
    cx, cy = cursor_pos

    target = next(
        (n for n, r in search_rects.items()
         if isinstance(n, tree.Leaf) and n is not leaf
         and r[0] <= cx <= r[2] and r[1] <= cy <= r[3]),
        None,
    )

    if target is None:
        return InsertAtFocused(dest_monitor) if cross else NoOp()

    tl, tt, tr, tb = search_rects[target]
    tw, th = tr - tl, tb - tt
    h_dist = min(cx - tl, tr - cx) / tw
    v_dist = min(cy - tt, tb - cy) / th

    if h_dist < DROP_ZONE_FRACTION and h_dist <= v_dist:
        axis, before = "horizontal", cx < tl + tw * 0.5
    elif v_dist < DROP_ZONE_FRACTION:
        axis, before = "vertical", cy < tt + th * 0.5
    else:
        axis, before = None, False

    if axis is None and not cross:
        return Swap(leaf, target)
    return InsertNear(dest_monitor, target, axis, before, search_rects[target])


def _resize_ancestor(leaf, orientation):
    """Walks up from `leaf` to the nearest ancestor container split along
    `orientation` with a sibling to trade space against. A leaf's own
    direct parent only ever runs ONE axis - e.g. one of several windows
    stacked vertically in a column has a "vertical" parent with no concept
    of width at all, so a width-resize on it needs to reach past that
    parent to the "horizontal" container the whole column is itself a
    child of. Every leaf inside a single-orientation container shares that
    container's bounds on the OTHER axis (a vertical split never touches
    left/right), so the dragged leaf's own actual/expected rect is exactly
    the resized column's/row's rect too - nothing else needs recomputing.
    Returns (container, its child that leads down to `leaf`), or
    (None, None) if no such ancestor exists (e.g. only ever one column)."""
    child = leaf
    node = leaf.parent
    while node is not None:
        if node.orientation == orientation and len(node.children) >= 2:
            return node, child
        child = node
        node = node.parent
    return None, None


def decide_resize(leaf, actual_rect, expected_rect, all_rects, inner_gap):
    """Decides how to fold a finished RESIZE gesture's size delta into
    container ratios - which container (see _resize_ancestor) and which
    sibling loses/gains the space is picked by checking which edge of the
    window actually moved. Width and height are resolved independently and
    BOTH applied when both changed (a corner drag does exactly this) -
    returns a list of zero, one, or two AdjustRatio outcomes, not a single
    Outcome, since either axis can need its own separate container/ratio
    update against a different ancestor (see _resize_ancestor)."""
    dw = (actual_rect[2] - actual_rect[0]) - (expected_rect[2] - expected_rect[0])
    dh = (actual_rect[3] - actual_rect[1]) - (expected_rect[3] - expected_rect[1])
    adjustments = []

    if abs(dw) > RESIZE_TOLERANCE:
        container, child = _resize_ancestor(leaf, "horizontal")
        if container is not None:
            container_rect = all_rects.get(container)
            if container_rect is not None:
                n = len(container.children)
                available = (container_rect[2] - container_rect[0]) - inner_gap * (n - 1)
                if available > 0:
                    index = container.children.index(child)
                    new_ratio = (actual_rect[2] - actual_rect[0]) / available
                    left_moved = abs(actual_rect[0] - expected_rect[0]) > RESIZE_TOLERANCE
                    nbr = (index - 1) if (left_moved and index > 0) else min(index + 1, n - 1)
                    adjustments.append(AdjustRatio(container, index, nbr, new_ratio))
    if abs(dh) > RESIZE_TOLERANCE:
        container, child = _resize_ancestor(leaf, "vertical")
        if container is not None:
            container_rect = all_rects.get(container)
            if container_rect is not None:
                n = len(container.children)
                available = (container_rect[3] - container_rect[1]) - inner_gap * (n - 1)
                if available > 0:
                    index = container.children.index(child)
                    new_ratio = (actual_rect[3] - actual_rect[1]) / available
                    top_moved = abs(actual_rect[1] - expected_rect[1]) > RESIZE_TOLERANCE
                    nbr = (index - 1) if (top_moved and index > 0) else min(index + 1, n - 1)
                    adjustments.append(AdjustRatio(container, index, nbr, new_ratio))

    return adjustments


def clamp_and_apply_ratio(container, index, neighbor_index, new_ratio):
    """Moves container.ratios[index] to new_ratio, taking the delta from
    neighbor_index, then renormalizes so all ratios sum to 1.0."""
    new_ratio = max(RATIO_MIN, min(RATIO_MAX, new_ratio))
    delta = new_ratio - container.ratios[index]
    if container.ratios[neighbor_index] - delta < RATIO_MIN:
        delta = container.ratios[neighbor_index] - RATIO_MIN
        new_ratio = container.ratios[index] + delta
    container.ratios[index] = new_ratio
    container.ratios[neighbor_index] -= delta
    total = sum(container.ratios)
    if total > 0:
        container.ratios = [r / total for r in container.ratios]
