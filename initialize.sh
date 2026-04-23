#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOTFILES_SRC="$REPO_ROOT/Users/kkmiecik"

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

copy_dotfile ".glzr/glazewm/config.yaml"
copy_dotfile ".config/yasb/config.yaml"
copy_dotfile ".config/yasb/styles.css"
copy_dotfile ".config/yasb/hide_taskbar.py"
copy_dotfile "AppData/Roaming/FlowLauncher/Settings/Settings.json"
copy_dotfile "AppData/Roaming/FlowLauncher/Themes/Catppuccin Mocha.xaml"

echo ""
echo "Done! Restart GlazeWM, YASB, and Flow Launcher to apply changes."
echo "Note: To install apps (Scoop, GlazeWM, YASB, Flow Launcher), run initialize.ps1 from PowerShell on Windows."
