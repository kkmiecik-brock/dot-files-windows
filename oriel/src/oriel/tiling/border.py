"""Highlights the focused window using Windows 11's native per-window DWM
attributes - an accent border color plus corner rounding - instead of a
custom-drawn overlay. Requires Windows 11 22H2+ (build 22621+); on older
Windows, DwmSetWindowAttribute simply returns a failure HRESULT for these
attribute IDs and this becomes a silent no-op, not a crash.
"""
import ctypes
from ctypes import wintypes

import win32con
import win32gui

DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_BORDER_COLOR = 34
DWMWA_COLOR_NONE = 0xFFFFFFFE  # sentinel: no accent border / OS default
DWMWCP_ROUND = 2  # Windows 11's normal default corner look, independent of CORNER_STYLES config

CORNER_STYLES = {"square": 1, "rounded": 2, "small_rounded": 3}

dwmapi = ctypes.windll.dwmapi


def _set_attribute(hwnd, attribute, value):
    v = ctypes.c_int(value)
    dwmapi.DwmSetWindowAttribute(wintypes.HWND(hwnd), attribute, ctypes.byref(v), ctypes.sizeof(v))


def _force_frame_redraw(hwnd):
    # DWM can skip repainting the non-client frame (where the border lives)
    # if it doesn't detect the attribute change as needing one - reliably
    # observed re-applying the SAME color after clear_border() silently not
    # re-appearing on refocus otherwise. SWP_FRAMECHANGED forces DWM to
    # actually recompute/repaint the frame. Skip entirely for an already-
    # hidden window - Windows rejects this SetWindowPos combination on one
    # (observed: ERROR_INVALID_PARAMETER when clearing a border right after
    # workspace-switch hides the window) and there's nothing to redraw anyway.
    if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd):
        return
    try:
        win32gui.SetWindowPos(
            hwnd, 0, 0, 0, 0, 0,
            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOZORDER
            | win32con.SWP_NOACTIVATE | win32con.SWP_FRAMECHANGED | win32con.SWP_ASYNCWINDOWPOS,
        )
    except win32gui.error:
        pass


def set_border(hwnd, colorref, corner_style):
    _set_attribute(hwnd, DWMWA_BORDER_COLOR, colorref)
    _set_attribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, CORNER_STYLES.get(corner_style, CORNER_STYLES["rounded"]))
    _force_frame_redraw(hwnd)


def ensure_rounded(hwnd):
    """Applies Windows 11's normal rounded-corner look without touching
    border color at all - for a window that's never been through
    set_border/clear_border yet (e.g. autostarted straight onto a hidden,
    inactive workspace, never focused) and would otherwise keep whatever
    corner state the app itself set by default (observed: Teams starts
    non-round). Called once when a window is first managed (see
    events.on_window_shown), independent of the focus-border feature."""
    _set_attribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND)
    _force_frame_redraw(hwnd)


def clear_border(hwnd):
    _set_attribute(hwnd, DWMWA_BORDER_COLOR, DWMWA_COLOR_NONE)
    # set_border can leave DWMWA_WINDOW_CORNER_PREFERENCE at a non-round
    # value (e.g. corner_style="square"/"small_rounded") - without
    # resetting it here too, an unfocused window stays stuck looking that
    # way forever instead of Windows 11's normal rounded look. Some apps
    # (observed: Teams) also appear to end up non-round on their own the
    # first time they're seen, before ever being focused/set_border'd at
    # all - resetting unconditionally on every clear (not just when
    # transitioning from a known non-default value) covers that case too.
    _set_attribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND)
    _force_frame_redraw(hwnd)

