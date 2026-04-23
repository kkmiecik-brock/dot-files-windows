<#
.SYNOPSIS
    Initializes a Windows environment with GlazeWM, YASB, and dotfile configs.
.DESCRIPTION
    - Installs Scoop (if not present)
    - Adds required Scoop buckets
    - Installs GlazeWM and YASB via Scoop
    - Copies dotfiles from this repo into the correct locations under $env:USERPROFILE
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$DotfilesBase = Join-Path $RepoRoot "Users\kkmiecik"

# ── 1. Scoop ─────────────────────────────────────────────────────────────────

if (-not (Get-Command scoop -ErrorAction SilentlyContinue)) {
    Write-Host "Installing Scoop..." -ForegroundColor Cyan
    Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser -Force
    Invoke-RestMethod -Uri https://get.scoop.sh | Invoke-Expression
} else {
    Write-Host "Scoop already installed." -ForegroundColor Green
}

# ── 2. Buckets ────────────────────────────────────────────────────────────────

Write-Host "Adding Scoop buckets..." -ForegroundColor Cyan
scoop bucket add extras 2>$null

# ── 3. Apps ───────────────────────────────────────────────────────────────────

$apps = @("glazewm", "yasb", "flow-launcher")
foreach ($app in $apps) {
    if (scoop list $app 2>$null | Select-String $app) {
        Write-Host "$app already installed." -ForegroundColor Green
    } else {
        Write-Host "Installing $app..." -ForegroundColor Cyan
        scoop install $app
    }
}

# GlazeWM pulls in Zebar as a dependency — remove it
if (scoop list zebar 2>$null | Select-String "zebar") {
    Write-Host "Removing Zebar..." -ForegroundColor Cyan
    scoop uninstall zebar
} else {
    Write-Host "Zebar not present, skipping." -ForegroundColor Green
}

# ── 4. Dotfiles ───────────────────────────────────────────────────────────────

function Copy-Dotfile {
    param (
        [string]$RelativePath
    )
    $src = Join-Path $DotfilesBase $RelativePath
    $dst = Join-Path $env:USERPROFILE $RelativePath

    if (-not (Test-Path $src)) {
        Write-Warning "Source not found, skipping: $src"
        return
    }

    $dstDir = Split-Path -Parent $dst
    if (-not (Test-Path $dstDir)) {
        New-Item -ItemType Directory -Path $dstDir -Force | Out-Null
    }

    Copy-Item -Path $src -Destination $dst -Force
    Write-Host "Copied: $RelativePath" -ForegroundColor Green
}

Write-Host "`nCopying dotfiles..." -ForegroundColor Cyan

Copy-Dotfile ".glzr\glazewm\config.yaml"
Copy-Dotfile ".config\yasb\config.yaml"
Copy-Dotfile ".config\yasb\styles.css"
Copy-Dotfile ".config\yasb\hide_taskbar.py"
Copy-Dotfile "AppData\Roaming\FlowLauncher\Settings\Settings.json"
Copy-Dotfile "AppData\Roaming\FlowLauncher\Themes\Catppuccin Mocha.xaml"

# ── 5. Startup entries ────────────────────────────────────────────────────────

Write-Host "`nConfiguring startup entries..." -ForegroundColor Cyan

$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"

$startupApps = @{
    "GlazeWM"      = "$env:USERPROFILE\scoop\apps\glazewm\current\glazewm.exe"
    "FlowLauncher" = "$env:USERPROFILE\scoop\apps\flow-launcher\current\Flow Launcher.exe"
}

foreach ($name in $startupApps.Keys) {
    $exe = $startupApps[$name]
    if (Test-Path $exe) {
        Set-ItemProperty -Path $runKey -Name $name -Value "`"$exe`""
        Write-Host "  Startup registered: $name" -ForegroundColor Green
    } else {
        Write-Warning "  Executable not found, skipping startup for $name`: $exe"
    }
}

# Remove duplicate shortcuts from shell:Startup folder
$startupFolder = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup"
$shortcutsToRemove = @("GlazeWM.lnk", "Flow Launcher.lnk")
foreach ($shortcut in $shortcutsToRemove) {
    $path = Join-Path $startupFolder $shortcut
    if (Test-Path $path) {
        Remove-Item $path -Force
        Write-Host "  Removed startup shortcut: $shortcut" -ForegroundColor Green
    }
}

Write-Host "`nDone! Log out and back in to start apps automatically." -ForegroundColor Cyan
