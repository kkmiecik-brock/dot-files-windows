"""Rules for which windows qualify for auto-tiling.

Primary structural filter mirrors the Windows alt-tab / GlazeWM algorithm:
  1. IsWindowVisible and not iconic
  2. Not DWM-cloaked (hidden on another virtual desktop)
  3. WS_EX_TOOLWINDOW → exclude; WS_EX_APPWINDOW → force-include even if owned
  4. GetAncestor(GA_ROOTOWNER) == hwnd — real app windows own themselves at the
     root of the ownership chain; popups/overlays/helpers are owned by something
     else and are excluded here without needing a process name list
  5. tiling.ignore_rules match — permanently excluded, same tier as the above:
     never tracked, never floating, never retried. For transient popups/shell
     chrome that happen to pass the structural checks above (e.g. a launcher
     popup or right-click context menu).

is_manageable()'s OWN remaining checks (WS_CAPTION + chrome + title) are
separate from the structural gate above - a genuinely-initializing app
window can still gain these moments after first appearing (the Firefox
timing gotcha the retry logic in events.py exists for), unlike the gate's
properties, which describe what KIND of window this fundamentally is and
never change.

tiling.floating_rules is a SEPARATE, independent match (is_floating_configured)
for windows that pass the structural gate but should be workspace-managed
floating windows rather than tiled - e.g. Settings/Calculator dialogs. Checked
by events.py directly (not part of is_manageable), so a match floating-tracks
a window immediately instead of waiting through is_manageable's retry cycle.

Both rule lists are data, not code: config.json's tiling.ignore_rules /
tiling.floating_rules, loaded via load_ignore_rules()/load_floating_rules() -
no per-application conditionals belong in this module. Add a new exclusion
or floating designation by adding a rule to config.json and reloading, not
by editing this file.
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


# Loaded from config.json's tiling.ignore_rules / tiling.floating_rules -
# see load_ignore_rules()/load_floating_rules(). Each rule is a dict of
# field -> value/[values]; every field present in a rule must match for
# that rule to apply (AND), and a window matches the list if ANY rule
# matches (OR across the list). Supported fields:
#   process         - exact process name match (case-insensitive)
#   class            - exact class name match (case-insensitive)
#   class_contains   - substring match against class name (case-insensitive)
#   class_not        - excluded only if class does NOT match (case-insensitive)
#   title            - exact title match (case-insensitive)
#   title_contains   - substring match against title (case-insensitive)
#   title_not_contains - excluded if title DOES contain this substring (case-insensitive)
# Any field's value may be a single string or a list (list = "matches any of").
_ignore_rules = []
_floating_rules = []


def load_ignore_rules():
    global _ignore_rules
    _ignore_rules = get_section("tiling").get("ignore_rules", [])


def load_floating_rules():
    global _floating_rules
    _floating_rules = get_section("tiling").get("floating_rules", [])


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
    if "title_not_contains" in rule and _contains_any(title, rule["title_not_contains"]):
        return False
    return True


def _is_ignored(process_name, class_name, title):
    return any(_rule_matches(rule, process_name, class_name, title) for rule in _ignore_rules)


def _is_floating_configured(process_name, class_name, title):
    return any(_rule_matches(rule, process_name, class_name, title) for rule in _floating_rules)


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


def _window_identity(hwnd):
    return get_process_name(hwnd), geometry.safe_get_class_name(hwnd), geometry.safe_get_window_text(hwnd)


def is_floating_configured(hwnd):
    """Whether config.json's tiling.floating_rules explicitly designates
    this window to float - checked independently of is_manageable/
    could_become_manageable (see events.on_window_shown), so a match
    floating-tracks it immediately instead of waiting through the retry
    cycle that exists for windows not explicitly classified either way."""
    process_name, class_name, title = _window_identity(hwnd)
    return _is_floating_configured(process_name, class_name, title)


def could_become_floating_configured(hwnd):
    """Whether hwnd's process/class alone match a tiling.floating_rules
    entry, ignoring that rule's title/title_contains/title_not_contains
    criteria. A freshly- shown window's title can still be a transitional
    placeholder - Teams' meeting window confirmed live to pass
    is_manageable() immediately with a generic title, then rename moments
    later via NAMECHANGE to something floating_rules actually matches. By
    then it's already been tiled (and resized to its tile share) with no
    way back to its original size, so events.on_window_shown uses this to
    give such a window a few retries (reusing the existing
    MANAGEABLE_RETRY_TIMER tick, not a new timer) before ever committing to
    tiling it."""
    process_name, class_name, _title = _window_identity(hwnd)
    for rule in _floating_rules:
        stripped = {k: v for k, v in rule.items() if k not in ("title", "title_contains", "title_not_contains")}
        if not stripped:
            continue  # rule constrains ONLY by title - nothing to pre-check
        if _rule_matches(stripped, process_name, class_name, ""):
            return True
    return False


FLOATING_RULE_DEFAULTS = {
    "sticky": False,
    "topmost": False,
    "position": "center",
    "border": True,
    "gap": 0,
    "width": None,
    "height": None,
    "center_delay": None,
    "activate": True,
}


def floating_rule_options(hwnd):
    """dict of per-rule floating options from the first tiling.
    floating_rules entry matching hwnd, or FLOATING_RULE_DEFAULTS if none
    match (or the match doesn't set a given field). "sticky" means
    workspace-independent - always visible, never hidden by switch_
    workspace (see events._add_floating_window/TilingState.add_sticky) -
    also positioned immediately rather than through the delayed re-center
    non-sticky windows use, since sticky windows are always freshly-
    discovered (never racing an app's own startup self-repositioning the
    way e.g. Calculator does). "topmost" means HWND_TOPMOST instead of the
    usual HWND_TOP z-order. "position" is an anchor string ("center"
    (default), "top", "bottom", "left", "right", or a hyphenated
    combination like "bottom-right") consumed by events._anchor_position.
    "gap" is the margin (px) kept from whichever edge(s) "position"
    anchors against - either a single number for all edges, or a dict like
    tiling.outer_gap's ({"top":.., "right":.., "bottom":.., "left":..}).
    "width"/"height" (px, optional - default None means "leave it at
    whatever size it already is") force the window to a specific visible
    size instead of just repositioning it. "center_delay" (seconds,
    optional - default None means "use events.FLOATING_CENTER_MAX_WAIT_
    SECONDS") overrides how long the non-sticky delayed-center path keeps
    retrying (it exits as soon as the window actually lands at the target
    rect, so this is a worst-case ceiling, not a fixed wait) - raise it for
    an app confirmed to take longer than that to stop fighting its own
    startup layout. "activate" (default True) also calls SetForegroundWindow
    once the window is raised, giving it real keyboard focus instead of
    just z-order (see events._raise_floating_window) - set false for a
    window that appears on its own (e.g. a Teams meeting/notification
    popping up) rather than from a deliberate user open, where stealing
    focus would be disruptive. "border": false opts a window out of both
    corner-rounding and focus-border highlighting entirely (see
    TilingState.add_no_border) - for small/transient windows (e.g. a
    screen-share control bar) where oriel's usual per-window chrome looks
    out of place."""
    process_name, class_name, title = _window_identity(hwnd)
    for rule in _floating_rules:
        if _rule_matches(rule, process_name, class_name, title):
            return {key: rule.get(key, default) for key, default in FLOATING_RULE_DEFAULTS.items()}
    return dict(FLOATING_RULE_DEFAULTS)


def is_manageable(hwnd, require_visible=True):
    if not _passes_structural_gate(hwnd, require_visible=require_visible):
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

    return True


def _passes_structural_gate(hwnd, require_visible=True):
    """The properties checked here describe what KIND of window this
    fundamentally is - a popup/tool-window/owned-helper (or an explicit
    tiling.ignore_rules match) never becomes a real app window no matter
    how long you wait, unlike WS_CAPTION/chrome/title (checked in
    is_manageable, not here), which a genuinely-initializing app window can
    still gain moments after first appearing (the Firefox timing gotcha
    the retry logic below exists for).

    require_visible=False skips the visibility/iconic check specifically -
    for re-tracking a window bootstrap already has persisted history for
    (see events.bootstrap_existing_windows) that's currently hidden simply
    because it was on an inactive workspace when oriel last reset/
    restarted, not because it's genuinely gone. is_cloaked is still always
    checked either way - that's a different kind of hidden (another
    virtual desktop), handled separately by the hide/cloak lifecycle."""
    if require_visible and (not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd)):
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

    process_name, class_name, title = _window_identity(hwnd)
    if _is_ignored(process_name, class_name, title):
        return False
    return True


def could_become_manageable(hwnd):
    """Whether it's worth retrying is_manageable() later for this hwnd - a
    real app window that hasn't finished applying its own chrome/title yet
    still might pass on a later check, but a window that's structurally a
    popup/tool-window/owned-helper (e.g. WinUI/XAML popup hosts and
    composition bridges used for autocomplete/IME-suggestion UI - observed
    live flooding this exact retry path, dozens of hwnds deep, while typing)
    or matches tiling.ignore_rules never will - retrying those is pure
    wasted work: a threading.Timer thread and a posted event per attempt,
    for something that can never succeed. Also doubles as the "should this
    ever become a floating window" gate (see events._add_floating_window
    callers) - an ignore_rules match means never, not even floating.
    Checked separately from is_manageable() so callers can skip scheduling
    a retry entirely for this class of window."""
    return _passes_structural_gate(hwnd)
