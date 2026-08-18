"""Entry point for the tiling daemon: wires TilingState, event handling, and
the IPC command table together, then runs the message loop. All the actual
logic lives in state.py (tree state + mutation), policy.py (pure move/resize
decision logic), events.py (event sourcing + WinEventHook), and geometry.py
(Win32/DWM adapters) - this file is deliberately thin.
"""
import threading
import logging

from oriel.ipc import serve_actions
from oriel.logging_setup import configure_logging
from oriel.tiling import events
from oriel.tiling import geometry
from oriel.tiling.state import TilingState

logger = logging.getLogger(__name__)

ACTIONS = {
    "focus_left": lambda _data=None: events.post(events.focus_direction, "left"),
    "focus_right": lambda _data=None: events.post(events.focus_direction, "right"),
    "focus_up": lambda _data=None: events.post(events.focus_direction, "up"),
    "focus_down": lambda _data=None: events.post(events.focus_direction, "down"),
    "move_left": lambda _data=None: events.post(events.move_direction, "left"),
    "move_right": lambda _data=None: events.post(events.move_direction, "right"),
    "move_up": lambda _data=None: events.post(events.move_direction, "up"),
    "move_down": lambda _data=None: events.post(events.move_direction, "down"),
    "resize_grow": lambda _data=None: events.post(events.resize_grow),
    "resize_shrink": lambda _data=None: events.post(events.resize_shrink),
    "reload": lambda _data=None: events.post(events.reload_settings),
    "toggle_fullscreen": lambda _data=None: events.post(events.toggle_fullscreen),
    "record_drag_kind": lambda data=None: events.post(events.record_drag_kind, data),
    "reflow": lambda _data=None: events.post(events.reflow_all),
    "switch_workspace": lambda data=None: events.post(events.switch_workspace_action, data),
    "move_to_workspace": lambda data=None: events.post(events.move_to_workspace_action, data),
}


def run():
    configure_logging("tiling")
    try:
        # Must happen before any window/monitor enumeration - see geometry.py's
        # ensure_dpi_awareness() docstring for why.
        geometry.ensure_dpi_awareness()

        state = TilingState()
        events.configure(state)
        events.apply_initial_settings()
        events.bootstrap_existing_windows()

        threading.Thread(
            target=serve_actions, args=("tiling", ACTIONS), name="oriel-tiling-ipc", daemon=True
        ).start()

        events.run_message_loop()
    except Exception:
        logger.exception("tiling daemon crashed")
        raise


