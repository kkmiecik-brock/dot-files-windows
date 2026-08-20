"""Pure N-ary container tree (i3/GlazeWM-style) - no Win32 dependency, so
this is directly unit-testable without a live Windows session or mocking.

Unlike a strict binary BSP tree, a Container holds any number of children
along a single axis (orientation). Inserting next to an existing sibling
in a Container APPENDS to that same container rather than always creating
a new nested split - a container's direction is chosen once (via
aspect-ratio heuristic, since there's no existing direction to inherit)
when it's first created, and further windows opened in it just keep
joining as siblings, exactly like GlazeWM/i3 behave when you never
manually toggle direction.

`item` is opaque to this module (the caller decides what it represents -
in practice, an hwnd). Rects are plain (left, top, right, bottom) tuples.
"""


class Leaf:
    def __init__(self, item):
        self.item = item
        self.parent = None


class Container:
    def __init__(self, orientation):
        self.orientation = orientation  # "horizontal" = side-by-side, "vertical" = stacked
        self.children = []  # list of Leaf | Container
        self.ratios = []    # parallel list of floats, sums to 1.0
        self.parent = None

    def add_child(self, child, index, ratio):
        child.parent = self
        self.children.insert(index, child)
        self.ratios.insert(index, ratio)

    def remove_child(self, child):
        index = self.children.index(child)
        del self.children[index]
        del self.ratios[index]
        # Redistribute the removed share proportionally among the rest.
        remaining = sum(self.ratios)
        if remaining > 0:
            self.ratios = [r / remaining for r in self.ratios]


def find_leaf(node, item):
    if node is None:
        return None
    if isinstance(node, Leaf):
        return node if node.item == item else None
    for child in node.children:
        found = find_leaf(child, item)
        if found is not None:
            return found
    return None


def all_leaves(node):
    if node is None:
        return []
    if isinstance(node, Leaf):
        return [node]
    leaves = []
    for child in node.children:
        leaves.extend(all_leaves(child))
    return leaves


def compute_rects(node, rect, gap, out=None):
    """Recursively computes each leaf's rect within `rect`, honoring `gap`
    between every adjacent pair of siblings. Returns a dict leaf -> rect.
    Pure ratio-based split - no attempt to detect or respect a window's
    own enforced minimum size (matches GlazeWM's approach: if a particular
    app refuses to shrink to its computed share, that's between it and the
    OS, not something the tree tries to model or compensate for)."""
    if out is None:
        out = {}
    if node is None:
        return out
    if isinstance(node, Leaf):
        out[node] = rect
        return out

    left, top, right, bottom = rect
    n = len(node.children)
    total_gap = gap * (n - 1)
    horizontal = node.orientation == "horizontal"
    available = (right - left if horizontal else bottom - top) - total_gap

    sizes = _distribute_sizes(available, node.ratios)

    pos = left if horizontal else top
    for child, size in zip(node.children, sizes):
        if horizontal:
            child_rect = (pos, top, pos + size, bottom)
        else:
            child_rect = (left, pos, right, pos + size)
        compute_rects(child, child_rect, gap, out)
        pos += size + gap

    return out


def _distribute_sizes(available, ratios):
    """Converts per-child ratios into integer pixel sizes, dead-pixel-free
    at the far edge."""
    n = len(ratios)
    if n == 0:
        return []
    sizes = [int(available * r) for r in ratios]
    sizes[-1] += available - sum(sizes)
    return sizes


def compute_all_rects(node, rect, gap, out=None):
    """Like compute_rects but includes Container nodes in the output dict."""
    if out is None:
        out = {}
    if node is None:
        return out
    out[node] = rect
    if isinstance(node, Leaf):
        return out

    left, top, right, bottom = rect
    n = len(node.children)
    total_gap = gap * (n - 1)
    horizontal = node.orientation == "horizontal"
    available = (right - left if horizontal else bottom - top) - total_gap

    pos = left if horizontal else top
    for i, (child, ratio) in enumerate(zip(node.children, node.ratios)):
        if i == n - 1:
            size = (right if horizontal else bottom) - pos
        else:
            size = int(available * ratio)
        if horizontal:
            child_rect = (pos, top, pos + size, bottom)
        else:
            child_rect = (left, pos, right, pos + size)
        compute_all_rects(child, child_rect, gap, out)
        pos += size + gap

    return out


def insert(root, target_leaf, new_item, rect, before=False):
    """Returns (new_root, new_leaf) after inserting new_item next to
    target_leaf.

    - If root is None, the new leaf becomes the root outright.
    - If target_leaf's parent is a Container, new_item is APPENDED as an
      additional sibling in that same container (matching GlazeWM/i3: new
      windows join the focused container's existing direction).
    - If target_leaf has no parent (it IS the root, as a bare Leaf), a new
      Container is created to hold both, with orientation chosen from
      `rect` (target_leaf's current rect) since there's no existing
      direction to inherit yet.
    """
    new_leaf = Leaf(new_item)

    if root is None:
        return new_leaf, new_leaf

    if target_leaf is None:
        leaves = all_leaves(root)
        target_leaf = leaves[0] if leaves else None
        if target_leaf is None:
            return new_leaf, new_leaf

    parent = target_leaf.parent

    if parent is not None:
        # Join the existing container as an additional sibling.
        index = parent.children.index(target_leaf)
        share = 1.0 / (len(parent.children) + 1)
        for i in range(len(parent.ratios)):
            parent.ratios[i] *= 1 - share
        parent.add_child(new_leaf, index if before else index + 1, share)
        return root, new_leaf

    # target_leaf is a bare root Leaf - wrap it in a new container.
    width, height = rect[2] - rect[0], rect[3] - rect[1]
    orientation = "horizontal" if width >= height else "vertical"
    container = Container(orientation)
    container.add_child(target_leaf, 0, 0.5)
    container.add_child(new_leaf, 1, 0.5)
    return container, new_leaf


def insert_at_root(root, new_item, rect):
    """Returns (new_root, new_leaf) after appending new_item as a new
    top-level sibling of root, regardless of which leaf happens to be
    focused/how deeply nested it is - a predictable "always a new edge
    column/row" landing spot for callers that don't want insert()'s usual
    "next to whatever's currently focused" placement (see
    events.move_to_workspace)."""
    new_leaf = Leaf(new_item)

    if root is None:
        return new_leaf, new_leaf

    if isinstance(root, Leaf):
        # Bare root leaf, same "wrap in a new container" case as insert().
        width, height = rect[2] - rect[0], rect[3] - rect[1]
        orientation = "horizontal" if width >= height else "vertical"
        container = Container(orientation)
        container.add_child(root, 0, 0.5)
        container.add_child(new_leaf, 1, 0.5)
        return container, new_leaf

    # root is already a Container - append as an additional top-level child.
    share = 1.0 / (len(root.children) + 1)
    for i in range(len(root.ratios)):
        root.ratios[i] *= 1 - share
    root.add_child(new_leaf, len(root.children), share)
    return root, new_leaf


def remove(root, leaf):
    """Returns the new root after removing leaf. A parent container left
    with exactly one remaining child is collapsed - that child is promoted
    to occupy the parent's own position in the tree."""
    parent = leaf.parent
    if parent is None:
        return None  # leaf was the bare root

    parent.remove_child(leaf)

    if len(parent.children) > 1:
        return root

    # Collapse: promote the lone remaining child to the parent's position.
    remaining = parent.children[0]
    grandparent = parent.parent
    remaining.parent = grandparent
    if grandparent is None:
        return remaining

    index = grandparent.children.index(parent)
    grandparent.children[index] = remaining
    return root


def insert_nested(root, target_leaf, new_item, orientation, before):
    """Wraps target_leaf and a new Leaf(new_item) in a new sub-Container.
    before=True places new_item before target_leaf in the sub-container.
    Returns (new_root, new_leaf)."""
    new_leaf = Leaf(new_item)
    container = Container(orientation)

    parent = target_leaf.parent

    if parent is None:
        # target_leaf is the bare root — new container becomes root
        if before:
            container.add_child(new_leaf, 0, 0.5)
            container.add_child(target_leaf, 1, 0.5)
        else:
            container.add_child(target_leaf, 0, 0.5)
            container.add_child(new_leaf, 1, 0.5)
        return container, new_leaf

    # Splice new container into target_leaf's slot, inheriting its ratio
    index = parent.children.index(target_leaf)
    parent.children[index] = container
    container.parent = parent
    target_leaf.parent = None  # add_child will re-set it

    if before:
        container.add_child(new_leaf, 0, 0.5)
        container.add_child(target_leaf, 1, 0.5)
    else:
        container.add_child(target_leaf, 0, 0.5)
        container.add_child(new_leaf, 1, 0.5)

    return root, new_leaf


def rect_center(rect):
    left, top, right, bottom = rect
    return (left + right) / 2, (top + bottom) / 2


def find_direction_target(root, current_leaf, direction, gap, work_area):
    """Finds the leaf geometrically nearest current_leaf in `direction`
    ("left"/"right"/"up"/"down"), by comparing rect centers."""
    rects = compute_rects(root, work_area, gap)
    if current_leaf not in rects:
        return None

    cx, cy = rect_center(rects[current_leaf])
    best, best_dist = None, None

    for leaf, rect in rects.items():
        if leaf is current_leaf:
            continue
        x, y = rect_center(rect)
        if direction == "left" and x >= cx:
            continue
        if direction == "right" and x <= cx:
            continue
        if direction == "up" and y >= cy:
            continue
        if direction == "down" and y <= cy:
            continue

        dist = (x - cx) ** 2 + (y - cy) ** 2
        if best_dist is None or dist < best_dist:
            best, best_dist = leaf, dist

    return best


def resize(leaf, delta):
    """Grows leaf's ratio by `delta` (shrinks by -delta), taking the
    difference from its immediate next sibling (or previous, if leaf is
    last) - only the shared boundary between two adjacent panes moves,
    matching typical tiling-WM resize semantics rather than a global
    rebalance across all siblings."""
    parent = leaf.parent
    if parent is None or len(parent.children) < 2:
        return

    index = parent.children.index(leaf)
    neighbor_index = index + 1 if index + 1 < len(parent.children) else index - 1

    delta = max(-parent.ratios[index] + 0.05, min(parent.ratios[neighbor_index] - 0.05, delta))
    parent.ratios[index] += delta
    parent.ratios[neighbor_index] -= delta
