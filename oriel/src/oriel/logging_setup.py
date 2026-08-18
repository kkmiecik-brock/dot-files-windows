"""Shared logging setup for oriel daemons - pythonw has no console, so an
unhandled exception previously meant a silent, undiagnosable process exit.
Call configure_logging(name) once, early in each daemon's run(); every
module's logging.getLogger(__name__) call in that process then reaches
this handler via the root logger.
"""
import logging
import logging.handlers
import os

LOG_DIR = os.path.join(os.path.expanduser("~"), ".config", "oriel", "logs")


def configure_logging(name):
    root = logging.getLogger()
    if root.handlers:
        return  # already configured (e.g. run() called more than once)
    os.makedirs(LOG_DIR, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        os.path.join(LOG_DIR, f"{name}.log"), maxBytes=1_000_000, backupCount=3, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.setLevel(logging.INFO)
    root.addHandler(handler)
