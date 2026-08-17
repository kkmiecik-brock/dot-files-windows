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


def compute_rects(node, rect, gap, min_sizes=None, out=None):
    """Recursively computes each leaf's rect within `rect`, honoring `gap`
    between every adjacent pair of siblings. Returns a dict leaf -> rect.

    `min_sizes` is an optional {item: (min_width, min_height)} dict (see
    events.enforce_tiled_placement/geometry.learn_min_size - populated from
    an actually-observed OS/app resize clamp, not queried speculatively).
    When present, no child is shrunk below its own subtree's minimum along
    the split axis as long as a sibling has slack to give up; if every
    child's minimum together still doesn't fit `rect`, sizes simply don't
    sum to `available` - an honestly-unsatisfiable layout, not hidden."""
    if min_sizes is None:
        min_sizes = {}
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
    axis = "horizontal" if horizontal else "vertical"
    available = (right - left if horizontal else bottom - top) - total_gap

    minimums = [_subtree_min(child, axis, gap, min_sizes) for child in node.children]
    sizes = _distribute_sizes(available, node.ratios, minimums)

    pos = left if horizontal else top
    for child, size in zip(node.children, sizes):
        if horizontal:
            child_rect = (pos, top, pos + size, bottom)
        else:
            child_rect = (left, pos, right, pos + size)
        compute_rects(child, child_rect, gap, min_sizes, out)
        pos += size + gap

    return out


def _subtree_min(node, axis, gap, min_sizes):
    """Minimum pixel size this subtree needs along `axis` ("horizontal" or
    "vertical") - a Leaf's own known minimum, or for a Container: summed
    (plus gaps) if it splits along `axis`, else the max of its children's
    minimums (they're stacked on the cross axis, so each already gets the
    container's full span there)."""
    if node is None:
        return 0
    if isinstance(node, Leaf):
        width, height = min_sizes.get(node.item, (0, 0))
        return width if axis == "horizontal" else height
    child_mins = [_subtree_min(child, axis, gap, min_sizes) for child in node.children]
    if not child_mins:
        return 0
    if node.orientation == axis:
        return sum(child_mins) + gap * (len(child_mins) - 1)
    return max(child_mins)


def _distribute_sizes(available, ratios, minimums):
    """Converts per-child ratios into integer pixel sizes. If every child's
    ratio share already meets its minimum, this is byte-identical to the
    old plain ratio*available split (dead-pixel-free at the far edge).
    Otherwise, space is borrowed from children with slack (share above
    their own minimum) to raise deficient children up to theirs - if that
    still isn't enough to cover everyone's floor, sizes simply don't sum
    to `available` (an overflowing, honestly-unsatisfiable layout) rather
    than something to keep renegotiating forever."""
    n = len(ratios)
    if n == 0:
        return []
    natural = [available * r for r in ratios]
    deficits = [max(0.0, minimums[i] - natural[i]) for i in range(n)]
    total_deficit = sum(deficits)
    if total_deficit == 0:
        sizes = [int(s) for s in natural]
        sizes[-1] += available - sum(sizes)
        return sizes

    slack = [max(0.0, natural[i] - minimums[i]) for i in range(n)]
    total_slack = sum(slack)
    if total_slack > 0:
        factor = min(1.0, total_deficit / total_slack)
        sized = [
            minimums[i] if deficits[i] > 0 else natural[i] - slack[i] * factor
            for i in range(n)
        ]
    else:
        sized = [max(natural[i], minimums[i]) for i in range(n)]
    return [int(s) for s in sized]


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
