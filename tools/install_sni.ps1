param([switch]$Force)

$ErrorActionPreference = "Stop"
$version = "v0.0.103"
$asset = "sni-$version-windows-amd64.zip"
$expectedSha256 = "4c0885769518c8b6ed7db038a29fdbdaf28b64c3b54689a5b2e0d6dd33074f87"
$url = "https://github.com/alttpo/sni/releases/download/$version/$asset"
$destination = Join-Path $PSScriptRoot "sni\sni.exe"

if ((Test-Path -LiteralPath $destination) -and -not $Force) {
    Write-Host "SNI already installed."
    exit 0
}

$work = Join-Path ([IO.Path]::GetTempPath()) ("hyrulelink-sni-" + [guid]::NewGuid())
$archive = Join-Path $work $asset
$expanded = Join-Path $work "expanded"
try {
    New-Item -ItemType Directory -Path $expanded -Force | Out-Null
    Write-Host "Downloading SNI $version..."
    Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $archive
    $actual = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $expectedSha256) {
        throw "SNI archive checksum mismatch (got $actual)"
    }
    Expand-Archive -LiteralPath $archive -DestinationPath $expanded -Force
    $exe = Get-ChildItem -LiteralPath $expanded -Filter "sni.exe" -File -Recurse | Select-Object -First 1
    if (-not $exe) {
        throw "The SNI archive did not contain sni.exe"
    }
    New-Item -ItemType Directory -Path (Split-Path $destination) -Force | Out-Null
    Copy-Item -LiteralPath $exe.FullName -Destination $destination -Force
    Write-Host "Installed SNI $version (verified SHA-256)."
}
finally {
    if (Test-Path -LiteralPath $work) {
        Remove-Item -LiteralPath $work -Recurse -Force
    }
}
