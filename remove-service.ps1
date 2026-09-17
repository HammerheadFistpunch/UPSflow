param(
    [string]$NssmPath = "",
    [string]$ServiceName = "UPSflow"
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($NssmPath)) {
    $nssm = Get-Command nssm.exe -ErrorAction SilentlyContinue
    if ($null -eq $nssm) {
        throw "nssm.exe was not found in PATH. Pass -NssmPath C:\\path\\to\\nssm.exe."
    }
    $NssmPath = $nssm.Source
}

if (-not (Test-Path $NssmPath)) {
    throw "NSSM executable not found: $NssmPath"
}

& $NssmPath stop $ServiceName 2>$null | Out-Null
& $NssmPath remove $ServiceName confirm
Write-Host "UPSflow service removed. The application files and logs were left intact."
