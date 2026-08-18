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
    # actually recompute/repaint the frame.
    win32gui.SetWindowPos(
        hwnd, 0, 0, 0, 0, 0,
        win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOZORDER
        | win32con.SWP_NOACTIVATE | win32con.SWP_FRAMECHANGED,
    )


def set_border(hwnd, colorref, corner_style):
    _set_attribute(hwnd, DWMWA_BORDER_COLOR, colorref)
    _set_attribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, CORNER_STYLES.get(corner_style, CORNER_STYLES["rounded"]))
    _force_frame_redraw(hwnd)


def clear_border(hwnd):
    _set_attribute(hwnd, DWMWA_BORDER_COLOR, DWMWA_COLOR_NONE)
    _force_frame_redraw(hwnd)

