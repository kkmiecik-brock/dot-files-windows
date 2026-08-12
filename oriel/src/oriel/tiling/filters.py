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
"""
import ctypes
import ctypes.wintypes
import os

import win32api
import win32con
import win32gui
import win32process

_user32 = ctypes.windll.user32
_user32.GetAncestor.restype = ctypes.wintypes.HWND
_user32.GetAncestor.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.UINT]
_GA_ROOTOWNER = 3
_DWMWA_CLOAKED = 14


def _get_root_owner(hwnd):
    return _user32.GetAncestor(hwnd, _GA_ROOTOWNER)


def _is_cloaked(hwnd):
    result = ctypes.c_int(0)
    ctypes.windll.dwmapi.DwmGetWindowAttribute(
        hwnd, _DWMWA_CLOAKED, ctypes.byref(result), ctypes.sizeof(result)
    )
    return result.value != 0

IGNORE_CLASSES = {
    "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW",
    "Windows.UI.Core.CoreWindow", "ForegroundStaging", "MultitaskingViewFrame",
    "TaskListThumbnailWnd", "SysShadow", "tooltips_class32", "#32768",
    "XamlExplorerHostIslandWindow", "SnagIt9Editor",
}
IGNORE_PROCESSES = {
    "explorer.exe", "flow.launcher.exe", "taskmgr.exe", "windows365.exe",
    "searchhost.exe", "shellexperiencehost.exe", "startmenuexperiencehost.exe",
    "textinputhost.exe", "systemsettings.exe",
    "logioverlay.exe", "logioptions.exe",
    "powertoys.quickaccess.exe", "microsoft.cmdpal.ui.exe",
    "selfservice.exe",
}
IGNORE_TITLES = {"calculator"}

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _get_window_text(hwnd):
    try:
        return win32gui.GetWindowText(hwnd)
    except win32gui.error:
        return ""


def _get_class_name(hwnd):
    try:
        return win32gui.GetClassName(hwnd)
    except win32gui.error:
        return ""


def _get_process_name(hwnd):
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


def _extra_ignore_rules(process_name, class_name, title):
    # Ported from this machine's prior GlazeWM window_rules ignore list.
    if process_name == "ms-teams.exe" and "microsoft teams notification" in title.lower():
        return True
    if process_name == "code.exe" and class_name == "Chrome_WidgetWin_0":
        return True
    if process_name in ("msrdc.exe", "windows365.exe") and (
        "credential" in class_name.lower() or class_name == "#32770"
    ):
        return True
    if process_name in ("firefox.exe", "msedge.exe", "chrome.exe") and (
        class_name == "#32770"
        or any(word in title.lower() for word in ("save as", "open", "download"))
    ):
        return True
    if process_name in ("excel.exe", "winword.exe", "powerpnt.exe"):
        main_class = {"excel.exe": "XLMAIN", "winword.exe": "OpusApp", "powerpnt.exe": "PPTFrameClass"}
        if class_name != main_class[process_name]:
            return True
    # UWP apps (Settings, Mail, etc.) are hosted inside the shared
    # ApplicationFrameHost.exe process, so process-name checks can't target
    # them individually - match on class + title instead.
    if class_name == "ApplicationFrameWindow" and title == "Settings":
        return True
    return False


def is_manageable(hwnd):
    if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
        return False
    if _is_cloaked(hwnd):
        return False

    ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
    app_window = bool(ex_style & win32con.WS_EX_APPWINDOW)
    if ex_style & win32con.WS_EX_TOOLWINDOW and not app_window:
        return False
    # Owned windows (popups, helpers, overlays) excluded unless WS_EX_APPWINDOW
    if not app_window and _get_root_owner(hwnd) != hwnd:
        return False
    style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
    if not (style & win32con.WS_CAPTION):
        return False
    # Require at least one "real app" chrome element; WS_SYSMENU is intentionally
    # excluded because Electron apps (VS Code, etc.) don't set it.
    APP_CHROME = win32con.WS_MINIMIZEBOX | win32con.WS_MAXIMIZEBOX | win32con.WS_THICKFRAME
    if not (style & APP_CHROME):
        return False

    title = _get_window_text(hwnd)
    if not title:
        return False
    if title.lower() in IGNORE_TITLES:
        return False

    class_name = _get_class_name(hwnd)
    if class_name in IGNORE_CLASSES:
        return False

    process_name = _get_process_name(hwnd)
    if process_name in IGNORE_PROCESSES:
        return False
    if _extra_ignore_rules(process_name, class_name, title):
        return False

    return True
