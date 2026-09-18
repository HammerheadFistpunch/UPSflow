[CmdletBinding()]
param(
    [string]$NssmPath = "",
    [string]$ServiceName = "UPSflow"
)

$ErrorActionPreference = "Stop"

$AppDir = $PSScriptRoot
$Python = Join-Path $AppDir ".venv\Scripts\python.exe"
$Script = Join-Path $AppDir "upsflow.py"

if (-not (Test-Path $Python)) {
    throw "UPSflow Python environment not found at $Python. Run the setup steps in README.md first."
}
if (-not (Test-Path $Script)) {
    throw "upsflow.py not found at $Script."
}

if ([string]::IsNullOrWhiteSpace($NssmPath)) {
    $nssm = Get-Command nssm.exe -ErrorAction SilentlyContinue
    if ($null -eq $nssm) {
        throw "nssm.exe was not found in PATH. Install NSSM and either add it to PATH or pass -NssmPath C:\\path\\to\\nssm.exe."
    }
    $NssmPath = $nssm.Source
}

if (-not (Test-Path $NssmPath)) {
    throw "NSSM executable not found: $NssmPath"
}

# Stopping/removing a not-yet-existing service writes an expected message to stderr.
# On PowerShell 7+, $PSNativeCommandUseErrorActionPreference can turn that stderr
# output into a terminating error when $ErrorActionPreference = "Stop" is set, so
# both are relaxed just for these two calls.
$prevEAP = $ErrorActionPreference
$prevNativeEAP = $PSNativeCommandUseErrorActionPreference
$ErrorActionPreference = "SilentlyContinue"
$PSNativeCommandUseErrorActionPreference = $false

& $NssmPath stop $ServiceName 2>$null | Out-Null
& $NssmPath remove $ServiceName confirm 2>$null | Out-Null

$ErrorActionPreference = $prevEAP
$PSNativeCommandUseErrorActionPreference = $prevNativeEAP

& $NssmPath install $ServiceName $Python
& $NssmPath set $ServiceName AppDirectory $AppDir
& $NssmPath set $ServiceName AppParameters "`"$Script`" monitor"
& $NssmPath set $ServiceName DisplayName "UPSflow EcoFlow Telemetry"
& $NssmPath set $ServiceName Description "Read-only EcoFlow River 2 BLE telemetry service for Keymaster/RFZ."
& $NssmPath set $ServiceName Start SERVICE_AUTO_START
& $NssmPath set $ServiceName AppExit Default Restart
& $NssmPath set $ServiceName AppRestartDelay 5000

# Service output is intentionally discarded; UPSflow does not write runtime log files.
& $NssmPath set $ServiceName AppStdout NUL
& $NssmPath set $ServiceName AppStderr NUL

Write-Host "UPSflow service installed."
Write-Host "  Service: $ServiceName"
Write-Host "  Program: $Python"
Write-Host "  Arguments: $Script monitor"
Write-Host ""
Write-Host "Start it with:  Start-Service $ServiceName"
Write-Host "Check it with:  Get-Service $ServiceName"
