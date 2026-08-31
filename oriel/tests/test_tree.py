"""Unit tests for the pure N-ary container tree (no Win32, no mocking needed)."""
from oriel.tiling import tree


def test_first_insert_becomes_bare_root_leaf():
    root, leaf_a = tree.insert(None, None, "A", (0, 0, 100, 100))
    assert root is leaf_a
    assert isinstance(root, tree.Leaf)
    assert leaf_a.item == "A"


def test_second_insert_wraps_both_in_a_container():
    root, leaf_a = tree.insert(None, None, "A", (0, 0, 100, 100))
    root, leaf_b = tree.insert(root, leaf_a, "B", (0, 0, 100, 100))

    assert isinstance(root, tree.Container)
    assert root.children == [leaf_a, leaf_b]
    assert sum(root.ratios) == 1.0


def test_third_insert_joins_same_container_as_sibling_not_a_new_nested_split():
    # This is the core GlazeWM/i3-style behavior: without ever toggling
    # direction, all windows opened while focus stays put pile into the
    # SAME container as N-ary siblings, rather than each getting its own
    # nested binary split.
    root, leaf_a = tree.insert(None, None, "A", (0, 0, 100, 100))
    root, leaf_b = tree.insert(root, leaf_a, "B", (0, 0, 100, 100))
    root, leaf_c = tree.insert(root, leaf_b, "C", (0, 0, 100, 100))

    assert isinstance(root, tree.Container)
    assert root.children == [leaf_a, leaf_b, leaf_c]
    assert len(root.ratios) == 3
    assert abs(sum(root.ratios) - 1.0) < 1e-9


def test_wide_rect_creates_horizontal_container_tall_rect_creates_vertical():
    root, leaf_a = tree.insert(None, None, "A", (0, 0, 100, 100))
    root, _ = tree.insert(root, leaf_a, "B", (0, 0, 200, 100))
    assert root.orientation == "horizontal"

    root2, leaf_a2 = tree.insert(None, None, "A", (0, 0, 100, 100))
    root2, _ = tree.insert(root2, leaf_a2, "B", (0, 0, 100, 200))
    assert root2.orientation == "vertical"


def test_compute_rects_splits_evenly_across_all_siblings():
    root, leaf_a = tree.insert(None, None, "A", (0, 0, 100, 100))
    root, leaf_b = tree.insert(root, leaf_a, "B", (0, 0, 100, 100))
    root, leaf_c = tree.insert(root, leaf_b, "C", (0, 0, 100, 100))

    rects = tree.compute_rects(root, (0, 0, 300, 100), gap=0)
    assert rects[leaf_a] == (0, 0, 100, 100)
    assert rects[leaf_b] == (100, 0, 200, 100)
    assert rects[leaf_c] == (200, 0, 300, 100)


def test_compute_rects_applies_gap_between_every_adjacent_pair():
    root, leaf_a = tree.insert(None, None, "A", (0, 0, 100, 100))
    root, leaf_b = tree.insert(root, leaf_a, "B", (0, 0, 100, 100))
    root, leaf_c = tree.insert(root, leaf_b, "C", (0, 0, 100, 100))

    rects = tree.compute_rects(root, (0, 0, 100, 100), gap=10)
    # 2 internal gaps of 10px each -> 20px total removed from 100px available
    widths = [rects[leaf][2] - rects[leaf][0] for leaf in (leaf_a, leaf_b, leaf_c)]
    assert sum(widths) + 20 == 100


def test_remove_middle_child_keeps_container_with_remaining_siblings():
    root, leaf_a = tree.insert(None, None, "A", (0, 0, 100, 100))
    root, leaf_b = tree.insert(root, leaf_a, "B", (0, 0, 100, 100))
    root, leaf_c = tree.insert(root, leaf_b, "C", (0, 0, 100, 100))

    root = tree.remove(root, leaf_b)

    assert isinstance(root, tree.Container)
    assert set(tree.all_leaves(root)) == {leaf_a, leaf_c}
    assert abs(sum(root.ratios) - 1.0) < 1e-9


def test_remove_down_to_one_child_collapses_container():
    root, leaf_a = tree.insert(None, None, "A", (0, 0, 100, 100))
    root, leaf_b = tree.insert(root, leaf_a, "B", (0, 0, 100, 100))

    root = tree.remove(root, leaf_b)

    # Only one leaf left - the container should have collapsed away,
    # leaving the bare leaf as root (matching the single-window case).
    assert root is leaf_a
    assert leaf_a.parent is None


def test_remove_last_leaf_empties_tree():
    root, leaf_a = tree.insert(None, None, "A", (0, 0, 100, 100))
    root = tree.remove(root, leaf_a)
    assert root is None


def test_find_direction_target_picks_nearest_neighbor():
    root, leaf_a = tree.insert(None, None, "A", (0, 0, 100, 100))
    root, leaf_b = tree.insert(root, leaf_a, "B", (0, 0, 100, 100))

    target = tree.find_direction_target(root, leaf_a, "right", gap=0, work_area=(0, 0, 100, 100))
    assert target is leaf_b

    target = tree.find_direction_target(root, leaf_b, "left", gap=0, work_area=(0, 0, 100, 100))
    assert target is leaf_a

    assert tree.find_direction_target(root, leaf_a, "up", gap=0, work_area=(0, 0, 100, 100)) is None


def test_resize_only_affects_shared_boundary_with_neighbor():
    root, leaf_a = tree.insert(None, None, "A", (0, 0, 100, 100))
    root, leaf_b = tree.insert(root, leaf_a, "B", (0, 0, 100, 100))
    root, leaf_c = tree.insert(root, leaf_b, "C", (0, 0, 100, 100))

    ratios_before = list(root.ratios)
    tree.resize(leaf_a, 0.1)

    # leaf_a grew, its immediate neighbor (leaf_b) shrank by the same
    # amount, leaf_c (not adjacent to leaf_a) is untouched.
    index_a, index_b, index_c = 0, 1, 2
    assert root.ratios[index_a] == ratios_before[index_a] + 0.1
    assert root.ratios[index_b] == ratios_before[index_b] - 0.1
    assert root.ratios[index_c] == ratios_before[index_c]


def test_serialize_deserialize_round_trips_a_nested_tree():
    root, leaf_a = tree.insert(None, None, "A", (0, 0, 100, 100))
    root, leaf_b = tree.insert(root, leaf_a, "B", (0, 0, 200, 100))
    root, leaf_c = tree.insert_nested(root, leaf_b, "C", "vertical", before=False)
    tree.resize(leaf_a, 0.1)

    data = tree.serialize(root)
    rebuilt = tree.deserialize(data)

    assert [leaf.item for leaf in tree.all_leaves(rebuilt)] == ["A", "B", "C"]
    assert isinstance(rebuilt, tree.Container)
    assert rebuilt.orientation == root.orientation
    assert rebuilt.ratios == root.ratios
    # Nested sub-container's own orientation/ratios also round-trip.
    nested_original = [c for c in root.children if isinstance(c, tree.Container)][0]
    nested_rebuilt = [c for c in rebuilt.children if isinstance(c, tree.Container)][0]
    assert nested_rebuilt.orientation == nested_original.orientation
    assert nested_rebuilt.ratios == nested_original.ratios
    # Every leaf's parent link is wired correctly, not left as None.
    for leaf in tree.all_leaves(rebuilt):
        assert leaf.parent is not None


def test_serialize_deserialize_handles_bare_root_leaf():
    root, _ = tree.insert(None, None, "A", (0, 0, 100, 100))
    rebuilt = tree.deserialize(tree.serialize(root))
    assert isinstance(rebuilt, tree.Leaf)
    assert rebuilt.item == "A"


def test_deserialize_none_or_empty_returns_none():
    assert tree.deserialize(None) is None
    assert tree.deserialize({}) is None


def test_prune_dead_leaves_removes_only_dead_ones_and_renormalizes():
    root, leaf_a = tree.insert(None, None, "A", (0, 0, 100, 100))
    root, leaf_b = tree.insert(root, leaf_a, "B", (0, 0, 100, 100))
    root, leaf_c = tree.insert(root, leaf_b, "C", (0, 0, 100, 100))

    root = tree.prune_dead_leaves(root, alive_items={"A", "C"})

    assert set(tree.all_leaves(root)) == {leaf_a, leaf_c}
    assert abs(sum(root.ratios) - 1.0) < 1e-9


def test_prune_dead_leaves_collapses_down_to_bare_root():
    root, leaf_a = tree.insert(None, None, "A", (0, 0, 100, 100))
    root, leaf_b = tree.insert(root, leaf_a, "B", (0, 0, 100, 100))

    root = tree.prune_dead_leaves(root, alive_items={"A"})

    assert root is leaf_a
    assert leaf_a.parent is None


def test_prune_dead_leaves_can_empty_the_tree_entirely():
    root, leaf_a = tree.insert(None, None, "A", (0, 0, 100, 100))
    assert tree.prune_dead_leaves(root, alive_items=set()) is None

