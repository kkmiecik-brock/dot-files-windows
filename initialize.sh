#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOTFILES_SRC="$REPO_ROOT/Users/Default"

# ── Detect Windows username ───────────────────────────────────────────────────

# Try to resolve from the Windows environment via wslvar, fall back to prompt
if command -v wslvar &>/dev/null; then
    WIN_USER="$(wslvar USERNAME 2>/dev/null || true)"
fi

if [[ -z "${WIN_USER:-}" ]]; then
    # Fall back: find the first non-default user dir under /mnt/c/Users
    WIN_USER="$(ls /mnt/c/Users/ | grep -vE '^(Public|Default|Default User|All Users|Administrator)$' | head -1)"
fi

if [[ -z "${WIN_USER:-}" ]]; then
    read -rp "Could not detect Windows username. Enter it manually: " WIN_USER
fi

WIN_HOME="/mnt/c/Users/$WIN_USER"

if [[ ! -d "$WIN_HOME" ]]; then
    echo "ERROR: Windows home not found at $WIN_HOME" >&2
    exit 1
fi

echo "Windows user : $WIN_USER"
echo "Windows home : $WIN_HOME"
echo ""

# ── Copy dotfiles ─────────────────────────────────────────────────────────────

copy_dotfile() {
    local rel="$1"
    local src="$DOTFILES_SRC/$rel"
    local dst="$WIN_HOME/$rel"

    if [[ ! -f "$src" ]]; then
        echo "  SKIP (not found): $rel"
        return
    fi

    mkdir -p "$(dirname "$dst")"
    cp -f "$src" "$dst"
    echo "  OK: $rel"
}

echo "Copying dotfiles..."

# GlazeWM/YASB/Flow Launcher removed for corporate security policy - replaced
# by PowerToys (FancyZones, Grab And Move, PowerToys Run, Keyboard Manager).
copy_dotfile "AppData/Local/Microsoft/PowerToys/settings.json"
copy_dotfile "AppData/Local/Microsoft/PowerToys/FancyZones/settings.json"
copy_dotfile "AppData/Local/Microsoft/PowerToys/PowerToys Run/settings.json"

echo ""
echo "Done! Restart PowerToys to apply changes."
echo "Note: To install PowerToys and set up its Startup shortcut, run initialize.ps1 from PowerShell on Windows."
