param(
    [string]$NssmPath = "",
    [string]$ServiceName = "UPSflow"
)

$ErrorActionPreference = "Stop"

$AppDir = $PSScriptRoot
$Python = Join-Path $AppDir ".venv\Scripts\python.exe"
$Script = Join-Path $AppDir "upsflow.py"
$LogDir = Join-Path $AppDir "logs"

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

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

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

# monitor's screen-refresh output is intentionally discarded when running as a service.
# Python logging goes to stderr and is retained below for diagnostics.
& $NssmPath set $ServiceName AppStdout NUL
& $NssmPath set $ServiceName AppStderr (Join-Path $LogDir "service-error.log")
& $NssmPath set $ServiceName AppRotateFiles 1
& $NssmPath set $ServiceName AppRotateOnline 1
& $NssmPath set $ServiceName AppRotateBytes 10485760

Write-Host "UPSflow service installed."
Write-Host "  Service: $ServiceName"
Write-Host "  Program: $Python"
Write-Host "  Arguments: $Script monitor"
Write-Host "  Error log: $(Join-Path $LogDir 'service-error.log')"
Write-Host ""
Write-Host "Start it with:  Start-Service $ServiceName"
Write-Host "Check it with:  Get-Service $ServiceName"
