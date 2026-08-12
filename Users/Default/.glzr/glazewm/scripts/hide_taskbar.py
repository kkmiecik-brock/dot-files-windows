import os
import subprocess
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


def _kill_other_instances():
    # If a newer instance is launched while an older one is still running,
    # the newer one wins - kill any other process running this same script.
    script = os.path.abspath(__file__)
    ps_script = (
        "$procs = Get-CimInstance Win32_Process | Where-Object { "
        "($_.Name -match '^python(\\.exe)?$' -or $_.Name -eq 'pythonw.exe') "
        f"-and $_.CommandLine -match [regex]::Escape('{script}') "
        f"-and $_.ProcessId -ne {os.getpid()} }}; "
        "if ($procs) { $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force } }"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_script],
        creationflags=subprocess.CREATE_NO_WINDOW,
        check=False,
    )

if __name__ == "__main__":
    _kill_other_instances()
    try:
        while True:
            hide_taskbar()
            time.sleep(1)
    except KeyboardInterrupt:
        pass
