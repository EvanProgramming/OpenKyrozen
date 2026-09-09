param(
    [Alias("Help")]
    [switch]$ShowHelp
)

$ErrorActionPreference = "Stop"

function Fail([string]$Message) {
    Write-Error $Message
    exit 1
}

if ($ShowHelp) {
    Write-Output "OpenKyrozen installer: installs the pinned v2.0.1 GitHub release, uv, Python 3.12/3.13, and Web dependencies."
    exit 0
}

$releaseVersion = "2.0.1"
$releaseTag = "v$releaseVersion"
$releaseWheelUrl = "https://github.com/EvanProgramming/OpenKyrozen/releases/download/$releaseTag/openkyrozen-$releaseVersion-py3-none-any.whl"
$homeDirectory = if ($env:HOME) { $env:HOME } else { $env:USERPROFILE }

Write-Output ""
Write-Output "  ____  ____  _____ _   _ ____  _____ _   _  ____  _   _ "
Write-Output " / __ \/ __ \/ ____| \ | |  _ \| ____| \ | |/ ___|| | | |"
Write-Output "| |  | | |  | | |    |  \| | |_) |  _| |  \| | |    | | | |"
Write-Output "| |__| | |__| | |____| |\  |  __/| |___| |\  | |___ | |_| |"
Write-Output " \____/\____/ \_____|_| \_|_|   |_____|_| \_|\____| \___/ "
Write-Output "OpenKyrozen computer-native installer"
Write-Output ""

$architecture = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
if ($architecture -notin @("X64", "Arm64")) {
    Fail "Unsupported Windows architecture: $architecture"
}

$stateDir = Join-Path $homeDirectory ".kyrozen"
try {
    New-Item -ItemType Directory -Force -Path (Join-Path $stateDir "workspace") | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $stateDir "v2") | Out-Null
} catch {
    Fail "Could not create writable OpenKyrozen state directories under $stateDir"
}

try {
    Invoke-WebRequest -UseBasicParsing -Uri $releaseWheelUrl -Method Head -TimeoutSec 20 | Out-Null
} catch {
    Fail "GitHub release asset is unavailable: $releaseTag"
}

$localBin = Join-Path $homeDirectory ".local\bin"
$cargoBin = Join-Path $homeDirectory ".cargo\bin"
$env:Path = "$localBin;$cargoBin;$env:Path"

$uvCommand = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uvCommand) {
    Write-Output "[INFO] Installing uv..."
    Invoke-Expression (Invoke-RestMethod https://astral.sh/uv/install.ps1)
    $env:Path = "$localBin;$cargoBin;$env:Path"
    $uvCommand = Get-Command uv -ErrorAction SilentlyContinue
}
if (-not $uvCommand) {
    Fail "uv was not found after installation. Restart PowerShell and run this installer again."
}

$pythonVersion = $null
& $uvCommand.Source python find 3.12 *> $null
if ($LASTEXITCODE -eq 0) {
    $pythonVersion = "3.12"
} else {
    & $uvCommand.Source python find 3.13 *> $null
    if ($LASTEXITCODE -eq 0) {
        $pythonVersion = "3.13"
    } else {
        Write-Output "[INFO] Installing supported Python 3.12..."
        & $uvCommand.Source python install 3.12 *> $null
        if ($LASTEXITCODE -eq 0) {
            $pythonVersion = "3.12"
        } else {
            & $uvCommand.Source python install 3.13 *> $null
            if ($LASTEXITCODE -eq 0) { $pythonVersion = "3.13" }
        }
    }
}
if (-not $pythonVersion) {
    Fail "Could not install Python 3.12 or 3.13."
}

Write-Output "[INFO] Installing OpenKyrozen $releaseTag from its immutable GitHub release with Python $pythonVersion..."
& $uvCommand.Source tool install --python $pythonVersion --force --with fastapi --with uvicorn $releaseWheelUrl
try { & $uvCommand.Source tool update-shell *> $null } catch { }

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$pathEntries = @()
if ($userPath) { $pathEntries = @($userPath -split ';' | Where-Object { $_ }) }
if (-not ($pathEntries | Where-Object { $_.TrimEnd('\') -ieq $localBin.TrimEnd('\') })) {
    [Environment]::SetEnvironmentVariable("Path", (($pathEntries + $localBin) -join ';'), "User")
}
$env:Path = "$localBin;$env:Path"

$kyrozenCommand = Get-Command kyrozen -ErrorAction SilentlyContinue
if (-not $kyrozenCommand) {
    $candidate = Join-Path $localBin "kyrozen.exe"
    if (Test-Path $candidate) { $kyrozenCommand = Get-Command $candidate }
}
if (-not $kyrozenCommand) {
    Fail "kyrozen was installed but is not on PATH. Restart PowerShell and try again."
}

Write-Output "[INFO] Verifying the installation..."
$installedVersion = (& $kyrozenCommand.Source --version | Out-String).Trim()
Write-Output $installedVersion
if ($installedVersion -notmatch "OpenKyrozen $releaseVersion") {
    Fail "Installed version does not match GitHub release $releaseTag`: $installedVersion"
}
& $kyrozenCommand.Source --help *> $null
Write-Output ""
Write-Output "Installation complete."
Write-Output "  kyrozen                 Start in the global workspace (~/.kyrozen/workspace)"
Write-Output "  kyrozen --project .     Work directly in the current project"
Write-Output "  kyrozen-web             Start the local web server"
Write-Output "The first kyrozen launch will guide you through provider setup; this installer never handles API keys."
