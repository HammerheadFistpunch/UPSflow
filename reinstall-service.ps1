[CmdletBinding()]
param(
    [string]$NssmPath = "",
    [string]$ServiceName = "UPSflow"
)

# Re-register the service from this directory. This is useful after the entire
# UPSflow folder has been moved: NSSM stores absolute paths in the service, so
# the service must be reinstalled from its new location.
$ErrorActionPreference = "Stop"

$Installer = Join-Path $PSScriptRoot "install-service.ps1"
if (-not (Test-Path $Installer)) {
    throw "install-service.ps1 not found at $Installer"
}

$arguments = @("-ServiceName", $ServiceName)
if (-not [string]::IsNullOrWhiteSpace($NssmPath)) {
    $arguments += @("-NssmPath", $NssmPath)
}

& $Installer @arguments
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "Service paths now point to: $PSScriptRoot"
Write-Host "Start it with: Start-Service $ServiceName"
