[CmdletBinding()]
param(
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$specPath = Join-Path $repositoryRoot "packaging\AiAssistant.spec"
$executablePath = Join-Path $repositoryRoot "dist\AiAssistant\AiAssistant.exe"
$venvPython = Join-Path $repositoryRoot ".venv\Scripts\python.exe"
$python = if (Test-Path -LiteralPath $venvPython) {
    $venvPython
} else {
    (Get-Command python -ErrorAction Stop).Source
}

Push-Location $repositoryRoot
try {
    $arguments = @("-m", "PyInstaller", "--noconfirm")
    if ($Clean) {
        $arguments += "--clean"
    }
    $arguments += $specPath
    & $python @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller exited with code $LASTEXITCODE."
    }
    if (-not (Test-Path -LiteralPath $executablePath -PathType Leaf)) {
        throw "Expected packaged executable was not created."
    }

    $bundleRoot = Split-Path -Parent $executablePath
    $files = Get-ChildItem -LiteralPath $bundleRoot -Recurse -Force -File
    if ($files | Where-Object { $_.Name -ieq ".env.local" }) {
        throw "Secret scan failed: a .env.local file exists in the packaged output."
    }
    foreach ($file in $files) {
        $bytes = [System.IO.File]::ReadAllBytes($file.FullName)
        $text = [System.Text.Encoding]::ASCII.GetString($bytes)
        if ($text.Contains("sk-proj-") -or $text -match "sk-[A-Za-z0-9_-]{24,}") {
            throw "Secret scan failed: an API-key-shaped value exists in the packaged output."
        }
    }

    Write-Host "Built and verified: $executablePath"
} finally {
    Pop-Location
}
