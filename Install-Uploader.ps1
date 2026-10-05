<#
  Installs the GamingDiver Debrief Uploader as a per-user Scheduled Task that
  starts at logon. No admin rights needed -- same posture as the crash-report
  kit in tools/wows-crash-report.

  Usage (from this folder):
    powershell -ExecutionPolicy Bypass -File .\Install-Uploader.ps1
    powershell -ExecutionPolicy Bypass -File .\Install-Uploader.ps1 -Uninstall
#>
param(
    [switch]$Uninstall,
    [switch]$NoTray,
    [string]$PythonExe
)

$ErrorActionPreference = "Stop"
$taskName = "GamingDiver Debrief Uploader"

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed the scheduled task. Your staged replays and settings are"
    Write-Host "untouched in $env:LOCALAPPDATA\GamingDiver\DebriefUploader."
    return
}

# --- find Python ------------------------------------------------------------
if (-not $PythonExe) {
    $PythonExe = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
}
if (-not $PythonExe) {
    Write-Error "Python not found. Install Python 3.9+ from python.org (tick 'Add python.exe to PATH'), then re-run this."
}
Write-Host "Using Python: $PythonExe"

# --- install to a stable location -------------------------------------------
$home_dir = Join-Path $env:LOCALAPPDATA "GamingDiver\DebriefUploader"
$target   = Join-Path $home_dir "app"
$venv     = Join-Path $home_dir "venv"
New-Item -ItemType Directory -Force -Path $target | Out-Null
Copy-Item -Path (Join-Path $PSScriptRoot "debrief_uploader") -Destination $target -Recurse -Force
Copy-Item -Path (Join-Path $PSScriptRoot "requirements.txt") -Destination $target -Force
Write-Host "Installed to: $target"

# --- one virtualenv, shared with Start-DebriefUploader.cmd ------------------
$venvPy = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "Creating the Python environment..."
    & $PythonExe -m venv $venv
    if ($LASTEXITCODE -ne 0) { Write-Error "Could not create the virtualenv." }
}
& $venvPy -m pip install --quiet --upgrade pip
& $venvPy -m pip install --quiet -r (Join-Path $target "requirements.txt")
if ($LASTEXITCODE -ne 0) { Write-Warning "pip install reported a problem - check the output above." }

# --- first-run setup --------------------------------------------------------
Push-Location $target
& $venvPy -m debrief_uploader setup
$setupCode = $LASTEXITCODE
Pop-Location
if ($setupCode -ne 0) {
    Write-Warning "Could not find your replays or screenshots folder automatically."
    Write-Warning "Set them by hand, e.g.:"
    Write-Warning "  & `"$venvPy`" -m debrief_uploader setup --replay-dir `"C:\XboxGames\World of Warships - Legends\Content\replays`" --shot-dir `"$env:USERPROFILE\Pictures\Screenshots`""
}

# --- the task ---------------------------------------------------------------
$argList = "-m debrief_uploader run --quiet"
if (-not $NoTray) { $argList = "-m debrief_uploader run --tray --quiet" }

$pythonwExe = Join-Path $venv "Scripts\pythonw.exe"
if (-not (Test-Path $pythonwExe)) { $pythonwExe = $venvPy }
$action  = New-ScheduledTaskAction -Execute $pythonwExe -Argument $argList -WorkingDirectory $target
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Force | Out-Null

Write-Host ""
Write-Host "Installed. It will start automatically when you log in."
Write-Host ""
Write-Host "Sign in once:"
Write-Host "  cd `"$target`"; & `"$venvPy`" -m debrief_uploader login --email"
Write-Host ""
Write-Host "Then start it now without logging out:"
Write-Host "  Start-ScheduledTask -TaskName `"$taskName`""
Write-Host ""
Write-Host "Useful any time:"
Write-Host "  & `"$venvPy`" -m debrief_uploader status   # what it has seen"
Write-Host "  & `"$venvPy`" -m debrief_uploader review   # resolve anything it wasn't sure about"
