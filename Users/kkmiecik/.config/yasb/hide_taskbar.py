import win32gui
import win32con
import time

TASKBAR_CLASSES = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


def _get_taskbar_windows():
    handles = []

    def callback(hwnd, _):
        try:
            class_name = win32gui.GetClassName(hwnd)
        except win32gui.error:
            return True

        if class_name in TASKBAR_CLASSES:
            handles.append(hwnd)

        return True

    win32gui.EnumWindows(callback, None)
    return handles


def hide_taskbar():
    for hwnd in _get_taskbar_windows():
        if not win32gui.IsWindow(hwnd):
            continue

        win32gui.ShowWindow(hwnd, win32con.SW_HIDE)

if __name__ == "__main__":
    try:
        while True:
            hide_taskbar()
            time.sleep(1)
    except KeyboardInterrupt:
        pass
