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

import pywintypes
import win32file
import win32pipe

logger = logging.getLogger(__name__)

PIPE_PREFIX = r"\\.\pipe\oriel-"


def send_action(target, action, data=None):
    try:
        handle = win32file.CreateFile(
            PIPE_PREFIX + target, win32file.GENERIC_WRITE, 0, None,
            win32file.OPEN_EXISTING, 0, None,
        )
    except pywintypes.error:
        return
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
        pipe = win32pipe.CreateNamedPipe(
            pipe_name,
            win32pipe.PIPE_ACCESS_INBOUND,
            win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT,
            1, 65536, 65536, 0, None,
        )
        try:
            win32pipe.ConnectNamedPipe(pipe, None)
            _, data = win32file.ReadFile(pipe, 4096)
            message = json.loads(data.decode("utf-8"))
            action = actions.get(message.get("action"))
            if action:
                action(message.get("data"))
        except (pywintypes.error, ValueError):
            pass
        except Exception:
            logger.exception("action handler failed for message on pipe %s", pipe_name)
        finally:
            win32file.CloseHandle(pipe)
