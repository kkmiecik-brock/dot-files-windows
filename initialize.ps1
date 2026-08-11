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

# -- 1. Scoop -----------------------------------------------------------------

if (-not (Get-Command scoop -ErrorAction SilentlyContinue)) {
    Write-Host "Installing Scoop..." -ForegroundColor Cyan
    Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser -Force
    Invoke-RestMethod -Uri https://get.scoop.sh | Invoke-Expression
} else {
    Write-Host "Scoop already installed." -ForegroundColor Green
}

# -- 2. Buckets ---------------------------------------------------------------

Write-Host "Adding Scoop buckets..." -ForegroundColor Cyan
scoop bucket add extras 2>$null

# -- 3. Apps ------------------------------------------------------------------

$apps = @("glazewm", "yasb", "flow-launcher")
foreach ($app in $apps) {
    if (scoop list $app 2>$null | Select-String $app) {
        Write-Host "$app already installed." -ForegroundColor Green
    } else {
        Write-Host "Installing $app..." -ForegroundColor Cyan
        scoop install $app
    }
}

# GlazeWM pulls in Zebar as a dependency - remove it
if (scoop list zebar 2>$null | Select-String "zebar") {
    Write-Host "Removing Zebar..." -ForegroundColor Cyan
    scoop uninstall zebar
} else {
    Write-Host "Zebar not present, skipping." -ForegroundColor Green
}

# -- 4. Dotfiles --------------------------------------------------------------

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

# Copies a file from the repo's scoop/apps/flow-launcher/current/<RelativePath>
# into the real app-* versioned directory on disk.
function Copy-FlowDotfile {
    param (
        [string]$RelativePath,
        [string]$FlowAppDir
    )
    $src = Join-Path $DotfilesBase "scoop\apps\flow-launcher\current\$RelativePath"
    $dst = Join-Path $FlowAppDir $RelativePath

    if (-not (Test-Path $src)) {
        Write-Warning "Source not found, skipping: $src"
        return
    }

    $dstDir = Split-Path -Parent $dst
    if (-not (Test-Path $dstDir)) {
        New-Item -ItemType Directory -Path $dstDir -Force | Out-Null
    }

    Copy-Item -Path $src -Destination $dst -Force
    Write-Host "Copied: flow-launcher\$RelativePath -> $dst" -ForegroundColor Green
}

Write-Host "`nCopying dotfiles..." -ForegroundColor Cyan

Copy-Dotfile ".glzr\glazewm\config.yaml"
Copy-Dotfile ".glzr\glazewm\launch-teams-delayed.vbs"
Copy-Dotfile ".glzr\glazewm\super-drag.py"
Copy-Dotfile ".config\yasb\config.yaml"
Copy-Dotfile ".config\yasb\styles.css"
Copy-Dotfile ".config\yasb\hide_taskbar.py"

# Stop Flow Launcher before copying its files so it can't overwrite them on exit
if (Get-Process -Name "Flow.Launcher" -ErrorAction SilentlyContinue) {
    Write-Host "Stopping Flow Launcher before copying files..." -ForegroundColor Cyan
    Stop-Process -Name "Flow.Launcher" -Force
    Start-Sleep -Seconds 2
}

$flowCurrentDir = "$env:USERPROFILE\scoop\apps\flow-launcher\current"
$flowAppDir = Get-ChildItem -Path $flowCurrentDir -Directory -Filter "app-*" |
              Sort-Object Name -Descending | Select-Object -First 1
if (-not $flowAppDir) {
    Write-Warning "Could not find Flow Launcher app-* dir under $flowCurrentDir. Skipping Flow Launcher files."
} else {
    Copy-FlowDotfile "UserData\Settings\Settings.json" $flowAppDir.FullName
    Copy-FlowDotfile "UserData\Themes\Catppuccin Mocha.xaml" $flowAppDir.FullName
}

# -- 5. Startup entries --------------------------------------------------------

Write-Host "`nConfiguring startup entries..." -ForegroundColor Cyan

$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$startupApprovedKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
$serializeKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Serialize"

$glazeWmExe = "$env:USERPROFILE\scoop\apps\glazewm\current\glazewm.exe"

# Remove Flow Launcher/Teams Run entries; GlazeWM launches them via startup_commands
foreach ($name in @("FlowLauncher", "Flow.Launcher", "Teams")) {
    Remove-ItemProperty -Path $runKey -Name $name -ErrorAction SilentlyContinue
    Remove-ItemProperty -Path $startupApprovedKey -Name $name -ErrorAction SilentlyContinue
}
Write-Host "  Cleaned up Run key entries for Flow Launcher and Teams." -ForegroundColor Green

# Rancher Desktop is heavy (container/WSL2 backend) and not needed the instant
# you log in - launch it manually via Flow Launcher when you need it.
Remove-ItemProperty -Path $runKey -Name "RancherDesktop" -ErrorAction SilentlyContinue
Remove-ItemProperty -Path $startupApprovedKey -Name "RancherDesktop" -ErrorAction SilentlyContinue
Write-Host "  Disabled Rancher Desktop auto-start." -ForegroundColor Green

# Remove legacy/task-scheduler-based GlazeWM tasks - this org's policy blocks
# Task Scheduler-launched interactive apps (ERROR_ELEVATION_REQUIRED), so
# GlazeWM starts via the Run key instead.
foreach ($oldTask in @("GlazeWM", "GlazeWM Startup", "Flow Launcher Startup")) {
    try {
        Unregister-ScheduledTask -TaskName $oldTask -Confirm:$false -ErrorAction Stop
        Write-Host "  Removed legacy task: $oldTask" -ForegroundColor Green
    } catch { }
}

# The "Run these programs at user logon" policy list (Policies\Explorer\Run)
# and the Startup folder shortcut were both tried and gave no meaningful
# improvement over the regular Run key - the real bottleneck is system
# resource contention during logon, not registry ordering. Use the plain
# Run key for GlazeWM instead.
Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Policies\Explorer\Run" -Name "1" -ErrorAction SilentlyContinue
Remove-Item -Path (Join-Path ([Environment]::GetFolderPath("Startup")) "GlazeWM.lnk") -Force -ErrorAction SilentlyContinue

if (-not (Test-Path $startupApprovedKey)) {
    New-Item -Path $startupApprovedKey -Force | Out-Null
}
Remove-ItemProperty -Path $runKey -Name "GlazeWM" -ErrorAction SilentlyContinue
Set-ItemProperty -Path $runKey -Name "GlazeWM" -Value "`"$glazeWmExe`""
$enabledValue = [byte[]](0x02, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00)
Set-ItemProperty -Path $startupApprovedKey -Name "GlazeWM" -Value $enabledValue -Type Binary
Write-Host "  GlazeWM registered in Run key -> $glazeWmExe" -ForegroundColor Green

# Removes the ~10-30s delay Windows applies to Run key startup apps.
if (-not (Test-Path $serializeKey)) { New-Item -Path $serializeKey -Force | Out-Null }
Set-ItemProperty -Path $serializeKey -Name "StartupDelayInMSec" -Value 0 -Type DWord
Write-Host "  Startup delay set to 0ms." -ForegroundColor Green

Write-Host "`nDone! Log out and back in to start GlazeWM automatically." -ForegroundColor Cyan
