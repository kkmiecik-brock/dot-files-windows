<#
.SYNOPSIS
    Initializes a Windows environment with PowerToys and dotfile configs.
.DESCRIPTION
    - Installs PowerToys via winget (replaces GlazeWM/YASB/Flow Launcher,
      removed for corporate security policy)
    - Removes leftover GlazeWM/YASB/Flow Launcher scoop installs and
      autostart entries from previous setups
    - Copies PowerToys module settings from this repo into
      %LOCALAPPDATA%\Microsoft\PowerToys
    - Registers PowerToys autostart via a Startup folder shortcut only
      (no registry Run key, no Task Scheduler - see notes below)
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$DotfilesBase = Join-Path $RepoRoot "Users\Default"

# -- 1. PowerToys --------------------------------------------------------------

$powerToysExe = "$env:LOCALAPPDATA\PowerToys\PowerToys.exe"

if (Test-Path $powerToysExe) {
    Write-Host "PowerToys already installed." -ForegroundColor Green
} else {
    Write-Host "Installing PowerToys..." -ForegroundColor Cyan
    winget install --id Microsoft.PowerToys -e --source winget --accept-package-agreements --accept-source-agreements
}

# -- 2. Remove legacy GlazeWM/YASB/Flow Launcher setup -------------------------

Write-Host "`nRemoving legacy GlazeWM/YASB/Flow Launcher setup..." -ForegroundColor Cyan

Get-Process -Name "glazewm", "yasb", "pythonw", "Flow.Launcher" -ErrorAction SilentlyContinue |
    Stop-Process -Force -ErrorAction SilentlyContinue

$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$startupApprovedKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
foreach ($name in @("GlazeWM", "FlowLauncher", "Flow.Launcher", "Teams")) {
    Remove-ItemProperty -Path $runKey -Name $name -ErrorAction SilentlyContinue
    Remove-ItemProperty -Path $startupApprovedKey -Name $name -ErrorAction SilentlyContinue
}
foreach ($oldTask in @("GlazeWM", "GlazeWM Startup", "Flow Launcher Startup")) {
    try {
        Unregister-ScheduledTask -TaskName $oldTask -Confirm:$false -ErrorAction Stop
        Write-Host "  Removed legacy task: $oldTask" -ForegroundColor Green
    } catch { }
}
Remove-Item -Path (Join-Path ([Environment]::GetFolderPath("Startup")) "GlazeWM.lnk") -Force -ErrorAction SilentlyContinue

foreach ($app in @("glazewm", "yasb", "flow-launcher", "zebar")) {
    if (Get-Command scoop -ErrorAction SilentlyContinue) {
        if (scoop list $app 2>$null | Select-String $app) {
            Write-Host "  Uninstalling $app..." -ForegroundColor Cyan
            scoop uninstall $app 2>$null
        }
    }
}
Write-Host "  Legacy setup removed." -ForegroundColor Green

# -- 3. Settings ----------------------------------------------------------------

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

# Stop PowerToys before copying settings so it can't overwrite them on exit
if (Get-Process -Name "PowerToys" -ErrorAction SilentlyContinue) {
    Write-Host "Stopping PowerToys before copying settings..." -ForegroundColor Cyan
    Get-Process -Name "PowerToys*" -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Seconds 2
}

Write-Host "`nCopying PowerToys settings..." -ForegroundColor Cyan

Copy-Dotfile "AppData\Local\Microsoft\PowerToys\settings.json"
Copy-Dotfile "AppData\Local\Microsoft\PowerToys\FancyZones\settings.json"
Copy-Dotfile "AppData\Local\Microsoft\PowerToys\PowerToys Run\settings.json"

# Copies any custom Start Menu shortcuts (.lnk) tracked in the repo - generic,
# so dropping a new shortcut into the repo folder is enough, no script changes.
$startMenuRelPath = "AppData\Roaming\Microsoft\Windows\Start Menu\Programs"
$startMenuSrcDir = Join-Path $DotfilesBase $startMenuRelPath
if (Test-Path $startMenuSrcDir) {
    Get-ChildItem -Path $startMenuSrcDir -Filter "*.lnk" | ForEach-Object {
        Copy-Dotfile (Join-Path $startMenuRelPath $_.Name)
    }
}

# -- 4. Startup entry -------------------------------------------------------------

Write-Host "`nConfiguring startup entry..." -ForegroundColor Cyan

# This org's policy blocks Task Scheduler-launched interactive apps
# (ERROR_ELEVATION_REQUIRED) and we're avoiding registry Run key edits, so
# PowerToys is autostarted the same way as Citrix Workspace/OneNote: a plain
# Startup folder shortcut. Leave PowerToys' own "Start at login" setting off
# (see settings.json "startup": false) so it doesn't try to register itself
# via Task Scheduler.
$startupShortcut = Join-Path ([Environment]::GetFolderPath("Startup")) "PowerToys.lnk"
if (Test-Path $startupShortcut) {
    Write-Host "  Startup shortcut already exists." -ForegroundColor Green
} else {
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($startupShortcut)
    $shortcut.TargetPath = $powerToysExe
    $shortcut.WorkingDirectory = Split-Path $powerToysExe
    $shortcut.Save()
    Write-Host "  Created Startup shortcut -> $powerToysExe" -ForegroundColor Green
}

Write-Host "`nDone! Log out and back in to start PowerToys automatically." -ForegroundColor Cyan
Write-Host "Note: FancyZones (tiling), Grab And Move (window drag), and PowerToys Run" -ForegroundColor Cyan
Write-Host "(Alt+A launcher) are enabled. Keyboard Manager is enabled but its shortcut" -ForegroundColor Cyan
Write-Host "remaps must be added manually via Settings > Keyboard Manager > Remap a" -ForegroundColor Cyan
Write-Host "shortcut (Start App action) - see README for the exact bindings used." -ForegroundColor Cyan
