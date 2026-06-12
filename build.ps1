# Build STL Builder: PyInstaller .exe bundle + optional Inno Setup installer.
# Usage: .\build.ps1
#        .\build.ps1 -SkipInstaller

param(
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location $Root

$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Pip = Join-Path $Root ".venv\Scripts\pip.exe"
$PyInstaller = Join-Path $Root ".venv\Scripts\pyinstaller.exe"

function Ensure-Venv {
    if (-not (Test-Path $Python)) {
        Write-Host "Creating virtual environment..."
        python -m venv .venv
    }
}

function Ensure-Deps {
    Write-Host "Installing dependencies..."
    & $Pip install -r requirements.txt pyinstaller --quiet
}

function Build-Exe {
    Write-Host "Building application bundle with PyInstaller..."
    if (Test-Path "dist\STLBuilder") {
        Remove-Item -Recurse -Force "dist\STLBuilder"
    }
    & $PyInstaller --noconfirm STLBuilder.spec
    if (-not (Test-Path "dist\STLBuilder\STLBuilder.exe")) {
        throw "Build failed: dist\STLBuilder\STLBuilder.exe was not created."
    }
    Write-Host "Executable: $Root\dist\STLBuilder\STLBuilder.exe"
}

function Find-InnoSetup {
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )
    foreach ($path in $candidates) {
        if (Test-Path $path) { return $path }
    }
    return $null
}

function Build-Installer {
    $iscc = Find-InnoSetup
    if (-not $iscc) {
        Write-Warning @"
Inno Setup 6 was not found. Install it from https://jrsoftware.org/isdl.php
then re-run: .\build.ps1

The standalone app is already at dist\STLBuilder\STLBuilder.exe
"@
        return
    }
    Write-Host "Compiling installer with Inno Setup..."
    & $iscc "installer\STLBuilder.iss"
    $setup = Get-ChildItem "dist\STLBuilder-Setup-*.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($setup) {
        Write-Host "Installer: $($setup.FullName)"
    } else {
        throw "Installer compile finished but no STLBuilder-Setup-*.exe was found in dist\"
    }
}

Ensure-Venv
Ensure-Deps
Build-Exe

if (-not $SkipInstaller) {
    Build-Installer
}

Write-Host "Done."
