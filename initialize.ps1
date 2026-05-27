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
$glazeWmTaskName = "GlazeWM Startup"
$flowLauncherTaskName = "Flow Launcher Startup"

if (-not (Test-Path $startupApprovedKey)) {
    New-Item -Path $startupApprovedKey -Force | Out-Null
}

function Enable-StartupApp {
    param (
        [string]$Name
    )

    $enabledValue = [byte[]](0x02, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00)
    Set-ItemProperty -Path $startupApprovedKey -Name $Name -Value $enabledValue -Type Binary
}

function Register-RunStartupApp {
    param (
        [string]$Name,
        [string]$ExecutablePath
    )

    if (-not (Test-Path $ExecutablePath)) {
        Write-Warning "  Executable not found for $Name`: $ExecutablePath - entry will still be registered."
    }

    Set-ItemProperty -Path $runKey -Name $Name -Value "`"$ExecutablePath`""
    Enable-StartupApp -Name $Name
    Write-Host "  Startup registered: $Name -> $ExecutablePath" -ForegroundColor Green
}

function Remove-StartupTask {
    param (
        [string]$TaskName
    )

    try {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
        Write-Host "  Removed startup task: $TaskName" -ForegroundColor Green
    } catch {
        if ($_.Exception.Message -notmatch "cannot find the file|No MSFT_ScheduledTask objects found") {
            Write-Warning "  Failed to remove ${TaskName}: $($_.Exception.Message)"
        }
    }
}

$glazeWmExe = "$env:USERPROFILE\scoop\apps\glazewm\current\glazewm.exe"
$flowLauncherExe = "$env:USERPROFILE\scoop\apps\flow-launcher\current\Flow.Launcher.exe"

Remove-StartupTask -TaskName $glazeWmTaskName
Remove-StartupTask -TaskName $flowLauncherTaskName

Remove-ItemProperty -Path $runKey -Name "GlazeWM" -ErrorAction SilentlyContinue
Remove-ItemProperty -Path $startupApprovedKey -Name "GlazeWM" -ErrorAction SilentlyContinue
Register-RunStartupApp -Name "GlazeWM" -ExecutablePath $glazeWmExe

if (Test-Path $flowLauncherExe) {
    Remove-ItemProperty -Path $runKey -Name "FlowLauncher" -ErrorAction SilentlyContinue
    Remove-ItemProperty -Path $startupApprovedKey -Name "FlowLauncher" -ErrorAction SilentlyContinue
    Register-RunStartupApp -Name "FlowLauncher" -ExecutablePath $flowLauncherExe
} else {
    Write-Warning "  Flow Launcher executable not found: $flowLauncherExe"
}

Write-Host "`nDone! Log out and back in to start apps automatically." -ForegroundColor Cyan
