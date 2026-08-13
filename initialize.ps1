<#
.SYNOPSIS
    Initializes a Windows environment with PowerToys and oriel configs.
.DESCRIPTION
    - Installs PowerToys via winget
    - Installs Python 3 and pywin32 dependency for oriel
    - Copies oriel desktop-management package to %USERPROFILE%\.config\oriel
    - Copies PowerToys settings from this repo into %LOCALAPPDATA%\Microsoft\PowerToys
    - Removes any leftover GlazeWM/YASB/Flow Launcher autostart entries
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$DotfilesBase = Join-Path $RepoRoot "Users\Default"

function Copy-Dotfile {
    param([string]$RelativePath)
    $src = Join-Path $DotfilesBase $RelativePath
    $dst = Join-Path $env:USERPROFILE $RelativePath
    if (-not (Test-Path $src)) { Write-Warning "Source not found, skipping: $src"; return }
    New-Item -ItemType Directory -Path (Split-Path -Parent $dst) -Force | Out-Null
    Copy-Item -Path $src -Destination $dst -Force
    Write-Host "  Copied: $RelativePath" -ForegroundColor Green
}

# -- 1. Remove any leftover GlazeWM/YASB/Flow Launcher autostart entries -------

Write-Host "Cleaning up legacy autostart entries..." -ForegroundColor Cyan
Get-Process -Name "glazewm","yasb","Flow.Launcher" -ErrorAction SilentlyContinue |
    Stop-Process -Force -ErrorAction SilentlyContinue
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
foreach ($name in @("GlazeWM","FlowLauncher","Flow.Launcher")) {
    Remove-ItemProperty -Path $runKey -Name $name -ErrorAction SilentlyContinue
}
foreach ($task in @("GlazeWM","GlazeWM Startup","Flow Launcher Startup")) {
    Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
}
Remove-Item (Join-Path ([Environment]::GetFolderPath("Startup")) "GlazeWM.lnk") -Force -ErrorAction SilentlyContinue
Write-Host "  Done." -ForegroundColor Green

# -- 2. PowerToys --------------------------------------------------------------

$powerToysExe = "$env:LOCALAPPDATA\PowerToys\PowerToys.exe"
if (Test-Path $powerToysExe) {
    Write-Host "`nPowerToys already installed." -ForegroundColor Green
} else {
    Write-Host "`nInstalling PowerToys..." -ForegroundColor Cyan
    winget install --id Microsoft.PowerToys -e --source winget --accept-package-agreements --accept-source-agreements
}

# -- 3. Python + oriel dependencies --------------------------------------------

Write-Host "`nChecking Python..." -ForegroundColor Cyan
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "  Installing Python 3..." -ForegroundColor Cyan
    winget install --id Python.Python.3.13 -e --source winget --accept-package-agreements --accept-source-agreements
}
Write-Host "  Installing pywin32..." -ForegroundColor Cyan
python -m pip install --quiet pywin32

# -- 4. Oriel package ----------------------------------------------------------

Write-Host "`nCopying oriel..." -ForegroundColor Cyan
$orielSrc = Join-Path $RepoRoot "oriel"
$orielDst = Join-Path $env:USERPROFILE ".config\oriel"
New-Item -ItemType Directory -Path $orielDst -Force | Out-Null
# docs/ is repo-only documentation (research notes, task lists) - deliberately
# not deployed, so copy exactly what's needed to run instead of a blanket "*".
foreach ($item in "src", "tests", "main.py", "pyproject.toml", "config.json") {
    Copy-Item -Path (Join-Path $orielSrc $item) -Destination $orielDst -Recurse -Force
}
Write-Host "  Copied to $orielDst" -ForegroundColor Green

# -- 5. PowerToys settings -----------------------------------------------------

Write-Host "`nCopying PowerToys settings..." -ForegroundColor Cyan
Get-Process -Name "PowerToys*" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1

Copy-Dotfile "AppData\Local\Microsoft\PowerToys\settings.json"
Copy-Dotfile "AppData\Local\Microsoft\PowerToys\FancyZones\settings.json"
Copy-Dotfile "AppData\Local\Microsoft\PowerToys\PowerToys Run\settings.json"

$startMenuRelPath = "AppData\Roaming\Microsoft\Windows\Start Menu\Programs"
$startMenuSrcDir = Join-Path $DotfilesBase $startMenuRelPath
if (Test-Path $startMenuSrcDir) {
    Get-ChildItem $startMenuSrcDir -Filter "*.lnk" | ForEach-Object {
        Copy-Dotfile (Join-Path $startMenuRelPath $_.Name)
    }
}

Write-Host "`nDone." -ForegroundColor Cyan
