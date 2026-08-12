"""Shared config.json loading for all oriel modules.

config.json lives at ~/.config/oriel/config.json (next to this package's
deployment location, not inside src/oriel/) - keeping it outside the
package means the package itself has no local state, and dotfiles
deployment (initialize.ps1) only needs to sync one file.
"""
import json
import os

CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".config", "oriel", "config.json")


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def get_section(name, defaults=None):
    section = load_config().get(name, {})
    return {**defaults, **section} if defaults else section
