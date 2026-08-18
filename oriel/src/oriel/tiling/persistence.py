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

from oriel.tiling import geometry

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


def save_monitor(tiling_state, monitor):
    """Read-modify-write so only this monitor's entry changes - others are
    left exactly as last saved."""
    stable_id = geometry.stable_monitor_id(monitor)
    if stable_id is None:
        return
    data = load()
    data[stable_id] = {
        "active": tiling_state.active_workspace(monitor),
        "windows": {str(hwnd): workspace for hwnd, workspace in tiling_state.hwnd_workspaces(monitor).items()},
    }
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass
