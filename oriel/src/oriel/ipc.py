"""Named-pipe IPC helpers shared between all oriel modules.

hotkeyd is the sole process that calls RegisterHotKey; other modules expose
a serve_actions() listener on their own pipe (\\\\.\\pipe\\oriel-<name>) so
hotkeyd can forward bound hotkey actions to them without needing a copy of
their logic. send_action() is fire-and-forget: if the target module isn't
running, the message is silently dropped rather than raising, so callers
never need to know or care whether the other module is alive.
"""
import json
import logging
import time

import pywintypes
import win32file
import win32pipe

logger = logging.getLogger(__name__)

PIPE_PREFIX = r"\\.\pipe\oriel-"

# serve_actions only ever has ONE pipe instance alive at a time - it must
# fully close and recreate it between every message. A send_action() that
# arrives while the server is mid-cycle (e.g. rapid-fire hotkey presses)
# would otherwise fail CreateFile immediately and be silently dropped, not
# just delayed - observed live losing several rapid workspace-switch
# presses in a row. Retrying briefly covers that normal recreate-cycle
# window (single-digit ms) without meaningfully slowing down the common
# case where the pipe is already listening on the first attempt.
CONNECT_RETRIES = 5
CONNECT_RETRY_DELAY = 0.02


def send_action(target, action, data=None):
    pipe_name = PIPE_PREFIX + target
    handle = None
    for attempt in range(CONNECT_RETRIES):
        try:
            handle = win32file.CreateFile(
                pipe_name, win32file.GENERIC_WRITE, 0, None,
                win32file.OPEN_EXISTING, 0, None,
            )
            break
        except pywintypes.error:
            if attempt == CONNECT_RETRIES - 1:
                return
            time.sleep(CONNECT_RETRY_DELAY)
    try:
        win32file.WriteFile(handle, json.dumps({"action": action, "data": data}).encode("utf-8"))
    except pywintypes.error:
        pass
    finally:
        win32file.CloseHandle(handle)



def serve_actions(name, actions):
    """Blocks forever, listening for {"action": "...", "data": ...} messages
    and dispatching to actions[action](data). data is None when the sender
    didn't include a payload."""
    pipe_name = PIPE_PREFIX + name
    while True:
        pipe = None
        try:
            pipe = win32pipe.CreateNamedPipe(
                pipe_name,
                win32pipe.PIPE_ACCESS_INBOUND,
                win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT,
                1, 65536, 65536, 0, None,
            )
            win32pipe.ConnectNamedPipe(pipe, None)
            _, data = win32file.ReadFile(pipe, 4096)
            message = json.loads(data.decode("utf-8"))
            action = actions.get(message.get("action"))
            if action:
                action(message.get("data"))
        except (pywintypes.error, ValueError):
            pass
        except Exception:
            # Must never let this loop die silently - a daemon thread's
            # unhandled exception is invisible in a console-less pythonw
            # process, and would otherwise kill the IPC listener for good
            # with zero trace (observed live: CreateNamedPipe failing here
            # silently ended the whole pipe after rapid-fire hotkey use).
            logger.exception("serve_actions(%s) loop iteration failed", name)
        finally:
            if pipe is not None:
                win32file.CloseHandle(pipe)

