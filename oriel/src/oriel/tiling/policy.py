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


def decide_resize(leaf, actual_rect, expected_rect, all_rects, inner_gap):
    """Decides how to fold a finished RESIZE gesture's size delta into the
    parent container's ratios - which sibling loses/gains the space is
    picked by checking which edge of the window actually moved."""
    parent = leaf.parent
    if parent is None or len(parent.children) < 2:
        return NoOp()

    parent_rect = all_rects.get(parent)
    if parent_rect is None:
        return NoOp()

    dw = (actual_rect[2] - actual_rect[0]) - (expected_rect[2] - expected_rect[0])
    dh = (actual_rect[3] - actual_rect[1]) - (expected_rect[3] - expected_rect[1])

    index = parent.children.index(leaf)
    n = len(parent.children)
    total_gap = inner_gap * (n - 1)

    if parent.orientation == "horizontal" and abs(dw) > RESIZE_TOLERANCE:
        available = (parent_rect[2] - parent_rect[0]) - total_gap
        if available > 0:
            new_ratio = (actual_rect[2] - actual_rect[0]) / available
            left_moved = abs(actual_rect[0] - expected_rect[0]) > RESIZE_TOLERANCE
            nbr = (index - 1) if (left_moved and index > 0) else min(index + 1, n - 1)
            return AdjustRatio(parent, index, nbr, new_ratio)
    elif parent.orientation == "vertical" and abs(dh) > RESIZE_TOLERANCE:
        available = (parent_rect[3] - parent_rect[1]) - total_gap
        if available > 0:
            new_ratio = (actual_rect[3] - actual_rect[1]) / available
            top_moved = abs(actual_rect[1] - expected_rect[1]) > RESIZE_TOLERANCE
            nbr = (index - 1) if (top_moved and index > 0) else min(index + 1, n - 1)
            return AdjustRatio(parent, index, nbr, new_ratio)

    return NoOp()


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
