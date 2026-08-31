"""Persists per-monitor workspace state across an oriel restart: which
workspace was active, and which workspace each currently-tiled window
belongs to. Keyed by each monitor's stable ID (not HMONITOR, which doesn't
survive a restart) and by hwnd (which does survive an oriel restart, since
only oriel's own process restarts here - not the underlying app windows).
Lives outside config.json since this is runtime state, not a user setting.

Known limitation, not solved here: Windows can recycle an hwnd for an
unrelated window - if oriel is stopped long enough for that to happen to a
persisted hwnd, bootstrap could hand its stale workspace to the wrong
window. Narrow edge case, not worth extra validation for.
"""
import json
import os

import win32api
import win32gui

from oriel.tiling import geometry, tree

STATE_PATH = os.path.join(os.path.expanduser("~"), ".config", "oriel", "workspace_state.json")


def load():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def entry_for(monitor, persisted):
    """persisted's entry for `monitor`, or None if unresolvable/absent."""
    stable_id = geometry.stable_monitor_id(monitor)
    if stable_id is None:
        return None
    return persisted.get(stable_id)


def find_hwnd_workspace(hwnd, persisted):
    """Searches every persisted monitor entry (not just one) for hwnd's
    recorded workspace - None if it has no persisted history anywhere.
    Needed because a monitor's stable id isn't guaranteed to stay the same
    across sessions (e.g. an RDP session presents a generic "Remote_Monitor"
    identity instead of the real monitor's), so a window's own current
    monitor may not match whichever identity it was originally recorded
    under, even though the window and its intended workspace didn't
    actually change."""
    for entry in persisted.values():
        workspace = entry.get("windows", {}).get(str(hwnd))
        if workspace is not None:
            return workspace
    return None


def find_hwnd_forced_tiled(hwnd, persisted):
    """Same cross-monitor search as find_hwnd_workspace, for hwnd's
    persisted forced_tiled flag (see save_monitor) - used by bootstrap_
    existing_windows' same monitor-identity-changed fallback path."""
    for entry in persisted.values():
        if hwnd in entry.get("forced_tiled", []):
            return True
    return False


def save_monitor(tiling_state, monitor):
    """Read-modify-write so only this monitor's entry changes - others are
    left exactly as last saved."""
    stable_id = geometry.stable_monitor_id(monitor)
    if stable_id is None:
        return
    data = load()
    hwnd_workspaces = tiling_state.hwnd_workspaces(monitor)
    data[stable_id] = {
        "active": tiling_state.active_workspace(monitor),
        "windows": {str(hwnd): workspace for hwnd, workspace in hwnd_workspaces.items()},
        # Only this monitor's own hwnds (is_forced_tiled has no monitor
        # concept of its own) - see TilingState.add_forced_tiled/
        # bootstrap_existing_windows for why this needs to survive a
        # restart at all (is_floating_configured() would otherwise just
        # re-float it, silently undoing toggle_floating's override).
        "forced_tiled": [hwnd for hwnd in hwnd_workspaces if tiling_state.is_forced_tiled(hwnd)],
        # workspace (str, since JSON object keys must be strings) -> tree.
        # serialize()'s nested dict, for bootstrap_existing_windows to
        # deserialize/prune back into a live tree - the split topology and
        # ratios themselves have no other representation anywhere else
        # (the flat "windows" map above only knows which workspace each
        # hwnd belongs to, nothing about how they're arranged within it).
        "trees": {
            str(workspace): tree.serialize(tiling_state.root(monitor, workspace))
            for (mon, workspace) in tiling_state.all_monitor_workspaces()
            if mon == monitor
        },
    }
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass


def dump_state():
    """Diagnostic CLI helper (see tiling/__main__.py --dump-state): prints
    the persisted workspace_state.json in a human-readable form - cross-
    referenced with which monitor (if any) currently connected owns each
    stable id, and each window's live title/validity, since the raw JSON
    alone is just opaque hwnd numbers. This is a standalone process, not
    the live daemon, so it reads the last-persisted snapshot (see
    save_monitor, called after nearly every state mutation, for how fresh
    that normally is) rather than the daemon's actual in-memory state.
    Also surfaces "ghost" leaves directly: a persisted hwnd that no longer
    exists but is still occupying a tile share in the daemon's tree until
    something notices and removes it (confirmed live: a dropped DESTROY
    event during an RDP reconnect burst can leave one of these behind)."""
    geometry.ensure_dpi_awareness()  # must run before any monitor enumeration - see ensure_dpi_awareness
    persisted = load()
    if not persisted:
        print("(no persisted workspace state)")
        return

    live_by_stable_id = {}
    for handle, _hdc, _rect in win32api.EnumDisplayMonitors():
        monitor = int(handle)
        stable_id = geometry.stable_monitor_id(monitor)
        if stable_id is not None:
            live_by_stable_id[stable_id] = monitor

    for stable_id, entry in persisted.items():
        monitor = live_by_stable_id.get(stable_id)
        status = f"connected, bounds {geometry.monitor_bounds(monitor)}" if monitor is not None else "NOT currently connected"
        print(f"{stable_id}  ({status})")
        print(f"  active workspace: {entry.get('active')}")
        windows = entry.get("windows", {})
        if not windows:
            print("  (no windows)")
        for hwnd_str, workspace in windows.items():
            hwnd = int(hwnd_str)
            if win32gui.IsWindow(hwnd):
                title = win32gui.GetWindowText(hwnd) or "(no title)"
                cls = win32gui.GetClassName(hwnd)
                print(f"  workspace {workspace}: hwnd={hwnd}  {title!r}  class={cls}")
            else:
                print(f"  workspace {workspace}: hwnd={hwnd}  *** STALE - window no longer exists ***")
        print()
