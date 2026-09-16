[CmdletBinding()]
param(
    [switch]$SkipDockerBuild,
    [switch]$RebuildSandbox,
    [switch]$SkipCodexCheck
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $ProjectRoot

function Require-Command {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [string]$InstallHint
    )

    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Command '$Name' was not found. $InstallHint"
    }
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Label,
        [Parameter(Mandatory = $true, ValueFromRemainingArguments = $true)]
        [string[]]$Command
    )

    Write-Host "[SETUP] $Label"
    $Executable = $Command[0]
    $Arguments = @($Command | Select-Object -Skip 1)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed (exit code: $LASTEXITCODE)"
    }
}

Require-Command "uv" "Install uv from https://docs.astral.sh/uv/getting-started/installation/."
Require-Command "docker" "Install Docker Desktop and start it in Linux container mode."

if (-not (Test-Path -LiteralPath ".env")) {
    Copy-Item -LiteralPath ".env.example" -Destination ".env"
    Write-Host "[SETUP] Copied .env.example to .env."
} else {
    Write-Host "[SETUP] Keeping the existing .env."
}

Invoke-Checked "Syncing the Python environment and packages" "uv" "sync"
Invoke-Checked "Checking Docker" "docker" "info" "--format" "{{.ServerVersion}}"

if (-not $SkipDockerBuild) {
    & docker image inspect ctf-sandbox *> $null
    $ImageExists = $LASTEXITCODE -eq 0
    if ($RebuildSandbox -or -not $ImageExists) {
        Invoke-Checked "Building the ctf-sandbox image" "docker" "build" "-f" "sandbox/Dockerfile.sandbox" "-t" "ctf-sandbox" "."
    } else {
        Write-Host "[SETUP] Reusing the existing ctf-sandbox image."
    }
}

if (-not $SkipCodexCheck) {
    $CodexCheck = "import asyncio; from backend.codex_cli import prepare_codex_cli; from backend.config import Settings; print(asyncio.run(prepare_codex_cli(Settings().codex_cli_path)))"
    Invoke-Checked "Checking Codex CLI login" ".venv\Scripts\python.exe" "-c" $CodexCheck
}

Write-Host ""
Write-Host "[DONE] This computer is ready to run CTF Agent."
Write-Host "[NEXT] .\run-ctf-agent.bat"
