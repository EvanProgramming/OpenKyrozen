param(
    [Alias("Help")]
    [switch]$ShowHelp
)

$ErrorActionPreference = "Stop"

function Fail([string]$Message) {
    Write-Error $Message
    exit 1
}

function Info([string]$Message) {
    Write-Host "[INFO] $Message" -ForegroundColor DarkGray
}

function Warn([string]$Message) {
    Write-Warning $Message
}

if ($ShowHelp) {
    Write-Output "OpenKyrozen installer: installs the pinned v2.0.4 GitHub release, uv, Python 3.12/3.13, and the Bubble Tea terminal UI."
    exit 0
}

$releaseVersion = "2.0.4"
$releaseTag = "v$releaseVersion"
$goVersion = "1.27.1"
$releaseBaseUrl = "https://github.com/EvanProgramming/OpenKyrozen/releases/download/$releaseTag"
$releaseWheelUrl = "$releaseBaseUrl/openkyrozen-$releaseVersion-py3-none-any.whl"
$tuiSourceUrl = "$releaseBaseUrl/openkyrozen-tui-$releaseVersion.tar.gz"
$tuiChecksumUrl = "$tuiSourceUrl.sha256"
$homeDirectory = if ($env:HOME) { $env:HOME } else { $env:USERPROFILE }
if (-not $homeDirectory) { Fail "HOME or USERPROFILE is not set." }

$interactive = -not [Console]::IsOutputRedirected
if ($interactive) {
    Write-Host "OPENKYROZEN" -ForegroundColor Cyan
    Write-Host "OpenKyrozen computer-native installer" -ForegroundColor White
} else {
    Write-Output "OPENKYROZEN"
    Write-Output "OpenKyrozen computer-native installer"
}
Write-Output ""

$architecture = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
if ($architecture -notin @("X64", "Arm64")) {
    Fail "Unsupported Windows architecture: $architecture"
}
$goSha256 = @{
    X64 = "a3911b5e0e1b1053f25ed0675f4c1c6aad1e2bfcf253df2b9be4caabd2edd95d"
    Arm64 = "13b69b87bb0e83f96bc68560a8cace7f0343b1e03469f1110ea18d17e3234069"
}

$stateDir = Join-Path $homeDirectory ".kyrozen"
try {
    New-Item -ItemType Directory -Force -Path (Join-Path $stateDir "workspace") | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $stateDir "v2") | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $stateDir "bin") | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $stateDir "toolchains\go") | Out-Null
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
    Info "Installing uv..."
    $env:UV_INSTALL_DIR = $localBin
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
        Info "Installing supported Python 3.12..."
        & $uvCommand.Source python install 3.12 *> $null
        if ($LASTEXITCODE -eq 0) {
            $pythonVersion = "3.12"
        } else {
            & $uvCommand.Source python install 3.13 *> $null
            if ($LASTEXITCODE -eq 0) { $pythonVersion = "3.13" }
        }
    }
}
if (-not $pythonVersion) { Fail "Could not install Python 3.12 or 3.13." }

Info "Installing OpenKyrozen $releaseTag with Python $pythonVersion..."
& $uvCommand.Source tool install --python $pythonVersion --force --with fastapi --with uvicorn $releaseWheelUrl
if ($LASTEXITCODE -ne 0) {
    Info "uv cache was incomplete; retrying without the existing cache..."
    & $uvCommand.Source --no-cache tool install --python $pythonVersion --force --with fastapi --with uvicorn $releaseWheelUrl
}
if ($LASTEXITCODE -ne 0) { Fail "OpenKyrozen installation failed from release $releaseTag." }
try { & $uvCommand.Source tool update-shell *> $null } catch { }

$ghBootstrap = Join-Path $localBin "kyrozen-bootstrap-gh.exe"
if (Test-Path $ghBootstrap) {
    & $ghBootstrap *> $null
    if ($LASTEXITCODE -eq 0) {
        Info "GitHub CLI 2.101.0 installed with checksum verification."
    } else {
        Warn "GitHub CLI bootstrap failed; /github login will retry when first used."
    }
}

function Test-GoCompatible([string]$CommandPath) {
    try {
        $text = (& $CommandPath version | Out-String).Trim()
        if ($text -match "go(?<major>\d+)\.(?<minor>\d+)(?:\.(?<patch>\d+))?") {
            $major = [int]$Matches.major
            $minor = [int]$Matches.minor
            $patch = if ($Matches.patch) { [int]$Matches.patch } else { 0 }
            return ($major -gt 1) -or ($major -eq 1 -and ($minor -gt 25 -or ($minor -eq 25 -and $patch -ge 8)))
        }
    } catch { }
    return $false
}

$goCommand = Get-Command go -ErrorAction SilentlyContinue
$goBin = $null
if ($goCommand -and (Test-GoCompatible $goCommand.Source)) {
    $goBin = $goCommand.Source
    Info "Reusing compatible Go toolchain: $((& $goBin version | Out-String).Trim())"
} else {
    $goBin = Join-Path $stateDir "toolchains\go\$goVersion\bin\go.exe"
    if (-not (Test-Path $goBin)) {
        Info "Installing Go $goVersion locally for the terminal UI..."
        $goTemp = Join-Path $env:TEMP ("openkyrozen-go-" + [guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Force -Path $goTemp | Out-Null
        $archive = Join-Path $goTemp "go.zip"
        $goArch = if ($architecture -eq "X64") { "amd64" } else { "arm64" }
        $goUrl = "https://go.dev/dl/go$goVersion.windows-$goArch.zip"
        try {
            Invoke-WebRequest -UseBasicParsing -Uri $goUrl -OutFile $archive
            $actual = (Get-FileHash -Algorithm SHA256 -Path $archive).Hash.ToLowerInvariant()
            if ($actual -ne $goSha256[$architecture].ToLowerInvariant()) {
                Warn "Go checksum verification failed; the terminal UI was not installed."
            } else {
                $extract = Join-Path $goTemp "extract"
                Expand-Archive -Path $archive -DestinationPath $extract
                $target = Join-Path $stateDir "toolchains\go\$goVersion"
                if (-not (Test-Path $target)) {
                    Move-Item -Path (Join-Path $extract "go") -Destination $target
                    $goBin = Join-Path $target "bin\go.exe"
                } else {
                    Warn "A Go toolchain directory already exists but is not usable; keeping it unchanged."
                }
            }
        } catch {
            Warn "Go download or extraction failed; the Rich fallback remains available."
        }
        Remove-Item -LiteralPath $goTemp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Build-Tui {
    if (-not (Test-Path $goBin)) { return $false }
    $tuiTemp = Join-Path $env:TEMP ("openkyrozen-tui-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $tuiTemp | Out-Null
    $archive = Join-Path $tuiTemp "tui.tar.gz"
    $checksumFile = Join-Path $tuiTemp "tui.tar.gz.sha256"
    $output = Join-Path $tuiTemp "openkyrozen-tui.exe"
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $tuiSourceUrl -OutFile $archive
        Invoke-WebRequest -UseBasicParsing -Uri $tuiChecksumUrl -OutFile $checksumFile
        $expected = ((Get-Content -LiteralPath $checksumFile -Raw) -split "\s+")[0].ToLowerInvariant()
        $actual = (Get-FileHash -Algorithm SHA256 -Path $archive).Hash.ToLowerInvariant()
        if (-not $expected -or $actual -ne $expected) { return $false }
        $source = Join-Path $tuiTemp "source"
        New-Item -ItemType Directory -Force -Path $source | Out-Null
        tar -xzf $archive -C $source
        Push-Location $source
        try {
            & $goBin build -trimpath -ldflags "-s -w -X main.version=$releaseVersion" -o $output .
            if ($LASTEXITCODE -ne 0 -or -not (Test-Path $output)) { return $false }
        } finally {
            Pop-Location
        }
        # Move is the commit point: a failed build never replaces the old binary.
        Move-Item -Force -LiteralPath $output -Destination (Join-Path $stateDir "bin\openkyrozen-tui.exe")
        return $true
    } catch {
        return $false
    } finally {
        Remove-Item -LiteralPath $tuiTemp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

if (Build-Tui) {
    Info "Bubble Tea terminal UI installed."
} else {
    Warn "Bubble Tea source asset is unavailable or failed to build; kyrozen will use the Rich fallback."
}

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
if (-not $kyrozenCommand) { Fail "kyrozen was installed but is not on PATH. Restart PowerShell and try again." }

Info "Verifying the installation..."
$installedVersion = (& $kyrozenCommand.Source --version | Out-String).Trim()
Write-Output $installedVersion
if ($installedVersion -notmatch "OpenKyrozen $releaseVersion") {
    Fail "Installed version does not match GitHub release $releaseTag`: $installedVersion"
}
& $kyrozenCommand.Source --help *> $null
Write-Output ""
Write-Output "Installation complete."
Write-Output "  kyrozen                 Start the full-screen terminal UI"
Write-Output "  kyrozen --project .     Work directly in the current project"
Write-Output "  kyrozen-web             Start the local web server"
Write-Output "The first kyrozen launch will guide you through provider setup; this installer never handles API keys."
