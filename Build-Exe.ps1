<#
  Builds DebriefUploader.exe. Must be run ON WINDOWS -- PyInstaller does not
  cross-compile, it bundles the interpreter of the machine it runs on.

    powershell -ExecutionPolicy Bypass -File .\Build-Exe.ps1

  Output: .\dist\DebriefUploader.exe (one file, no Python needed to run it).
#>
param([switch]$NoConsole)

$ErrorActionPreference = "Stop"
Push-Location $PSScriptRoot

python -m pip install --quiet --upgrade pyinstaller
python -m pip install --quiet -r requirements.txt

$args = @(
    "--onefile",
    "--name", "DebriefUploader",
    "--collect-all", "pystray",
    "--icon", "debrief_uploader\resources\app.ico",
    "--add-data", "debrief_uploader\resources;resources",
    "--hidden-import", "pystray._win32",
    "--hidden-import", "PIL._tkinter_finder"
)
# Keep the console by default: it is where sign-in prompts and errors appear,
# and the tray is the newest, least-proven part of this app.
if ($NoConsole) { $args += "--noconsole" }
$args += "app.py"

python -m PyInstaller @args

Pop-Location
Write-Host ""
Write-Host "Built: $PSScriptRoot\dist\DebriefUploader.exe"
Write-Host ""
Write-Host "First run (sign in):   .\dist\DebriefUploader.exe login --email"
Write-Host "Then just double-click it, or:   .\dist\DebriefUploader.exe run --tray"
