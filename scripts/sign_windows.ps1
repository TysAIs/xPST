param(
    [Parameter(Mandatory = $true)]
    [string]$Path,

    [string]$CertificatePath = $env:WINDOWS_CERTIFICATE_PATH,
    [string]$CertificatePassword = $env:WINDOWS_CERTIFICATE_PASSWORD,
    [string]$TimestampUrl = "http://timestamp.digicert.com"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $Path)) {
    throw "Artifact not found: $Path"
}

$signtool = Get-Command signtool.exe -ErrorAction SilentlyContinue
if (-not $signtool) {
    $kits = Get-ChildItem "C:\Program Files (x86)\Windows Kits\10\bin" -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending |
        Select-Object -First 1
    if ($kits) {
        $signtool = $kits.FullName
    }
}

if (-not $signtool) {
    throw "signtool.exe not found. Install Windows SDK or add signtool.exe to PATH."
}

if ($signtool -isnot [string]) {
    $signtool = $signtool.Source
}

if ($CertificatePath) {
    if (-not (Test-Path -LiteralPath $CertificatePath)) {
        throw "Certificate file not found: $CertificatePath"
    }

    & $signtool sign /fd SHA256 /tr $TimestampUrl /td SHA256 /f $CertificatePath /p $CertificatePassword $Path
} else {
    & $signtool sign /fd SHA256 /tr $TimestampUrl /td SHA256 /a $Path
}

& $signtool verify /pa /v $Path
Write-Host "Signed and verified: $Path"

