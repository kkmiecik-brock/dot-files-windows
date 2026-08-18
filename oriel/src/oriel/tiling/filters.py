"""Rules for which windows qualify for auto-tiling.

Primary structural filter mirrors the Windows alt-tab / GlazeWM algorithm:
  1. IsWindowVisible and not iconic
  2. Not DWM-cloaked (hidden on another virtual desktop)
  3. WS_EX_TOOLWINDOW → exclude; WS_EX_APPWINDOW → force-include even if owned
  4. GetAncestor(GA_ROOTOWNER) == hwnd — real app windows own themselves at the
     root of the ownership chain; popups/overlays/helpers are owned by something
     else and are excluded here without needing a process name list
  5. WS_CAPTION + at least one resize/chrome style

The IGNORE_* lists are a secondary safety net for apps that happen to pass all
structural checks but still shouldn't be tiled (e.g. WPF splash screens that
set a full title and minimize/maximize buttons before the real window appears).
These exclusions are data, not code: they live in config.json's
tiling.ignore_rules, loaded via load_ignore_rules() - no per-application
conditionals belong in this module. Add a new exclusion by adding a rule to
config.json and reloading, not by editing this file.
"""
import ctypes
import ctypes.wintypes
import os

import win32api
import win32con
import win32gui
import win32process

from oriel.config import get_section
from oriel.tiling import geometry

_user32 = ctypes.windll.user32
_user32.GetAncestor.restype = ctypes.wintypes.HWND
_user32.GetAncestor.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.UINT]
_GA_ROOTOWNER = 3
_DWMWA_CLOAKED = 14


def _get_root_owner(hwnd):
    return _user32.GetAncestor(hwnd, _GA_ROOTOWNER)


def is_cloaked(hwnd):
    result = ctypes.c_int(0)
    ctypes.windll.dwmapi.DwmGetWindowAttribute(
        hwnd, _DWMWA_CLOAKED, ctypes.byref(result), ctypes.sizeof(result)
    )
    return result.value != 0


# Loaded from config.json's tiling.ignore_rules - see load_ignore_rules().
# Each rule is a dict of field -> value/[values]; every field present in a
# rule must match for that rule to apply (AND), and a window is excluded if
# ANY rule matches (OR across the list). Supported fields:
#   process         - exact process name match (case-insensitive)
#   class            - exact class name match (case-insensitive)
#   class_contains   - substring match against class name (case-insensitive)
#   class_not        - excluded only if class does NOT match (case-insensitive)
#   title            - exact title match (case-insensitive)
#   title_contains   - substring match against title (case-insensitive)
# Any field's value may be a single string or a list (list = "matches any of").
_ignore_rules = []


def load_ignore_rules():
    global _ignore_rules
    _ignore_rules = get_section("tiling").get("ignore_rules", [])


def _as_list(value):
    return value if isinstance(value, list) else [value]


def _equals_any(value, spec):
    value = value.lower()
    return any(value == candidate.lower() for candidate in _as_list(spec))


def _contains_any(value, spec):
    value = value.lower()
    return any(candidate.lower() in value for candidate in _as_list(spec))


def _rule_matches(rule, process_name, class_name, title):
    if "process" in rule and not _equals_any(process_name, rule["process"]):
        return False
    if "class" in rule and not _equals_any(class_name, rule["class"]):
        return False
    if "class_contains" in rule and not _contains_any(class_name, rule["class_contains"]):
        return False
    if "class_not" in rule and _equals_any(class_name, rule["class_not"]):
        return False
    if "title" in rule and not _equals_any(title, rule["title"]):
        return False
    if "title_contains" in rule and not _contains_any(title, rule["title_contains"]):
        return False
    return True


def _is_ignored(process_name, class_name, title):
    return any(_rule_matches(rule, process_name, class_name, title) for rule in _ignore_rules)


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def get_process_name(hwnd):
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        handle = win32api.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    except win32api.error:
        return ""
    try:
        return os.path.basename(win32process.GetModuleFileNameEx(handle, 0)).lower()
    except win32api.error:
        return ""
    finally:
        win32api.CloseHandle(handle)


def is_manageable(hwnd):
    if not _passes_structural_gate(hwnd):
        return False
    style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
    if not (style & win32con.WS_CAPTION):
        return False
    # Require at least one "real app" chrome element; WS_SYSMENU is intentionally
    # excluded because Electron apps (VS Code, etc.) don't set it.
    APP_CHROME = win32con.WS_MINIMIZEBOX | win32con.WS_MAXIMIZEBOX | win32con.WS_THICKFRAME
    if not (style & APP_CHROME):
        return False

    title = geometry.safe_get_window_text(hwnd)
    if not title:
        return False

    class_name = geometry.safe_get_class_name(hwnd)
    process_name = get_process_name(hwnd)
    if _is_ignored(process_name, class_name, title):
        return False

    return True


def _passes_structural_gate(hwnd):
    """The properties checked here describe what KIND of window this
    fundamentally is - a popup/tool-window/owned-helper never becomes a
    real app window no matter how long you wait, unlike WS_CAPTION/chrome/
    title (checked in is_manageable, not here), which a genuinely-
    initializing app window can still gain moments after first appearing
    (the Firefox timing gotcha the retry logic below exists for)."""
    if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
        return False
    if is_cloaked(hwnd):
        return False

    ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
    app_window = bool(ex_style & win32con.WS_EX_APPWINDOW)
    if ex_style & win32con.WS_EX_TOOLWINDOW and not app_window:
        return False
    # Owned windows (popups, helpers, overlays) excluded unless WS_EX_APPWINDOW
    if not app_window and _get_root_owner(hwnd) != hwnd:
        return False
    return True


def could_become_manageable(hwnd):
    """Whether it's worth retrying is_manageable() later for this hwnd - a
    real app window that hasn't finished applying its own chrome/title yet
    still might pass on a later check, but a window that's structurally a
    popup/tool-window/owned-helper (e.g. WinUI/XAML popup hosts and
    composition bridges used for autocomplete/IME-suggestion UI - observed
    live flooding this exact retry path, dozens of hwnds deep, while typing)
    never will - retrying those is pure wasted work: a threading.Timer
    thread and a posted event per attempt, for something that can never
    succeed. Checked separately from is_manageable() so callers can skip
    scheduling a retry entirely for this class of window."""
    return _passes_structural_gate(hwnd)
