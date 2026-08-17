#requires -Version 5.1
<#
.SYNOPSIS
    Build WormScanSetup.exe - the double-click installer.

.DESCRIPTION
    Run this on a Windows machine with internet access. It gathers everything
    an offline install needs and hands it to Inno Setup:

        1. a private copy of CPython (nothing is required on the target machine)
        2. every dependency wheel for BOTH venvs, downloaded with that exact
           interpreter so the wheel tags cannot mismatch
        3. ffmpeg + ffprobe
        4. the launcher source tree and the staging model
        5. a build-identity stamp so an installed copy can name itself

    Nothing is compiled. The app ships as .py files, so a fix can be shipped by
    replacing a file and a retrained model needs no rebuild at all.

.EXAMPLE
    cd packaging
    .\build_installer.ps1

.EXAMPLE
    # Reuse everything already downloaded - the fast path while iterating on
    # the .iss script or the app sources.
    .\build_installer.ps1 -SkipDownload
#>
[CmdletBinding()]
param(
    # Only the MAJOR.MINOR series. The newest matching patch is selected.
    [string] $PythonSeries = '3.13',

    # Version stamped into the installer and shown in the app. Defaults to
    # a date-based build number plus the short git sha.
    [string] $Version = '',

    # Reuse _build\download\ instead of re-fetching (~500 MB).
    [switch] $SkipDownload,

    # Stage the payload but do not run Inno Setup. Useful for inspecting
    # exactly what would ship.
    [switch] $NoCompile,

    # Skip the GitHub lookup and download this exact interpreter archive.
    # The escape hatch for API rate limits or a naming change.
    [string] $PythonUrl = '',

    # Use an ffmpeg zip already on disk instead of downloading one. For
    # networks where the build hosts do not resolve.
    [string] $FfmpegZip = '',

    # Download the ffmpeg zip from this URL instead of the built-in sources.
    [string] $FfmpegUrl = ''
)

# Set when ffmpeg was taken from this machine's PATH rather than a download,
# which means the zip extraction below has nothing to do.
$FfmpegDone = $false

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'   # Invoke-WebRequest is ~10x faster without the bar

# Windows PowerShell 5.1 still negotiates TLS 1.0/1.1 on some builds. GitHub
# and gyan.dev both require 1.2 or better, and the failure looks like a generic
# connection error rather than anything about protocols.
try {
    [Net.ServicePointManager]::SecurityProtocol =
        [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch { }

# --------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------
$PackagingDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$RepoRoot     = Split-Path -Parent $PackagingDir
$BuildDir     = Join-Path $PackagingDir '_build'
$DownloadDir  = Join-Path $BuildDir 'download'
$PayloadDir   = Join-Path $BuildDir 'payload'
$DistDir      = Join-Path $PackagingDir 'dist'

function Write-Step  ($m) { Write-Host ""; Write-Host "==> $m" -ForegroundColor Cyan }
function Write-Info  ($m) { Write-Host "    $m" -ForegroundColor DarkGray }
function Write-Ok    ($m) { Write-Host "    $m" -ForegroundColor Green }
function Die         ($m) { Write-Host ""; Write-Host "ERROR: $m" -ForegroundColor Red; exit 1 }

function Get-HumanSize ($path) {
    if (-not (Test-Path $path)) { return '0 B' }
    $b = (Get-ChildItem $path -Recurse -File -ErrorAction SilentlyContinue |
          Measure-Object -Property Length -Sum).Sum
    if (-not $b) { return '0 B' }
    foreach ($u in 'B','KB','MB','GB') {
        if ($b -lt 1024) { return ('{0:N1} {1}' -f $b, $u) }
        $b = $b / 1024
    }
    return ('{0:N1} TB' -f $b)
}

Write-Host ""
Write-Host "============================================================"
Write-Host "  WormScan installer build"
Write-Host "============================================================"

# --------------------------------------------------------------------------
# 0. Version stamp
# --------------------------------------------------------------------------
Write-Step "Working out the build identity"

$gitSha = ''
$gitDirty = $false
try {
    Push-Location $RepoRoot
    $gitSha = (& git rev-parse HEAD 2>$null)
    $status = (& git status --porcelain 2>$null)
    $gitDirty = -not [string]::IsNullOrWhiteSpace($status)
    Pop-Location
} catch { try { Pop-Location } catch {} }

if (-not $Version) {
    $Version = (Get-Date -Format 'yyyy.MM.dd')
    if ($gitSha) { $Version = "$Version+$($gitSha.Substring(0,7))" }
}
# Inno's AppVersion dislikes '+'
$InnoVersion = ($Version -replace '\+.*$', '')

Write-Info "version : $Version"
Write-Info "commit  : $(if ($gitSha) { $gitSha.Substring(0,7) } else { 'unknown' })"
if ($gitDirty) {
    Write-Host "    WARNING: the working tree has uncommitted changes." -ForegroundColor Yellow
    Write-Host "             This build will not be reproducible from the commit above." -ForegroundColor Yellow
}

# --------------------------------------------------------------------------
# 1. Prerequisites
# --------------------------------------------------------------------------
Write-Step "Checking prerequisites"

$iscc = $null
if (-not $NoCompile) {
    $isccCandidates = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup 6\ISCC.exe",
        "${env:LOCALAPPDATA}\Programs\Inno Setup 6\ISCC.exe"
    )
    $iscc = $isccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $iscc) {
        $cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
        if ($cmd) { $iscc = $cmd.Source }
    }
    if (-not $iscc) {
        Die @"
Inno Setup 6 was not found. It is the tool that turns the payload into
WormScanSetup.exe. Install it once with:

    winget install JRSoftware.InnoSetup

then run this script again. (Or pass -NoCompile to stage the payload only.)
"@
    }
    Write-Ok "Inno Setup: $iscc"
}

$model = Join-Path $RepoRoot 'launcher\vision\models\staging.pt'
if (-not (Test-Path $model)) {
    Die @"
The staging model is missing:

    $model

It is git-ignored, so a fresh clone will not have it. Copy it in from a
machine that has it, then re-run.
"@
}
Write-Ok "staging model: $([math]::Round((Get-Item $model).Length / 1MB, 1)) MB"

if (-not (Test-Path (Join-Path $RepoRoot 'launcher\requirements.txt'))) { Die "launcher\requirements.txt not found - run this from inside the repo." }
if (-not (Test-Path (Join-Path $RepoRoot 'launcher\vision\requirements.txt'))) { Die "launcher\vision\requirements.txt not found." }

# --------------------------------------------------------------------------
# 2. Clean the payload (downloads are kept)
# --------------------------------------------------------------------------
Write-Step "Preparing the staging area"
if (Test-Path $PayloadDir) { Remove-Item $PayloadDir -Recurse -Force }
New-Item -ItemType Directory -Path $PayloadDir  -Force | Out-Null
New-Item -ItemType Directory -Path $DownloadDir -Force | Out-Null
New-Item -ItemType Directory -Path $DistDir     -Force | Out-Null
Write-Info $PayloadDir

# --------------------------------------------------------------------------
# 3. A private CPython
# --------------------------------------------------------------------------
Write-Step "Fetching a standalone CPython $PythonSeries"

$pyArchive = Join-Path $DownloadDir 'cpython-windows.tar.gz'

if ($SkipDownload -and (Test-Path $pyArchive)) {
    Write-Info "reusing $pyArchive"
} elseif ($PythonUrl) {
    Write-Info "downloading the URL you supplied"
    Invoke-WebRequest -Uri $PythonUrl -OutFile $pyArchive -UseBasicParsing
} else {
    # python-build-standalone publishes redistributable Windows builds. Ask the
    # GitHub API for the newest release rather than hardcoding a URL that rots.
    Write-Info "asking github for the latest python-build-standalone release..."
    $headers = @{ 'User-Agent' = 'wormscan-build' }
    if ($env:GITHUB_TOKEN) { $headers['Authorization'] = "Bearer $env:GITHUB_TOKEN" }

    try {
        $rel = Invoke-RestMethod -Headers $headers `
            -Uri 'https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest'
    } catch {
        Die @"
Could not reach the GitHub API to find a Python build.

    $($_.Exception.Message)

If this is rate limiting (60 requests/hour without a token), either set
GITHUB_TOKEN, or pick an asset by hand from

    https://github.com/astral-sh/python-build-standalone/releases/latest

and pass it directly:

    .\build_installer.ps1 -PythonUrl "<the .tar.gz url>"
"@
    }

    # Matched loosely on purpose. The asset naming has changed across the
    # project's history - the date has lived both after the version behind a
    # '+' and at the very end behind a '-' - so pinning one exact shape is how
    # this script rots. Require only what actually matters, then rank.
    $candidates = $rel.assets | Where-Object {
        $_.name -like "cpython-$PythonSeries.*"     -and
        $_.name -like '*x86_64-pc-windows-msvc*'    -and
        $_.name -like '*install_only*'              -and
        $_.name -like '*.tar.gz'                    -and
        $_.name -notlike '*.sha256'
    }

    # Prefer the full install_only over install_only_stripped: stripped drops
    # debug symbols, which is a saving we do not need and a debugging aid we
    # might.
    $asset = $candidates | Sort-Object `
        @{ Expression = { if ($_.name -like '*install_only_stripped*') { 1 } else { 0 } } }, `
        @{ Expression = { $_.name }; Descending = $true } |
        Select-Object -First 1

    if (-not $asset) {
        $available = ($rel.assets | Where-Object { $_.name -like '*windows*' } |
                      Select-Object -First 15 | ForEach-Object { "    $($_.name)" }) -join "`n"
        Die @"
No CPython $PythonSeries Windows x86_64 'install_only' asset in release
'$($rel.tag_name)'.

Windows assets that ARE in that release:
$available

Either pass a different -PythonSeries, or pick one by hand and pass
-PythonUrl "<url>".
"@
    }

    $sizeMb = [math]::Round($asset.size / 1MB, 1)
    Write-Info "downloading $($asset.name)"
    Write-Info "  $sizeMb MB"
    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $pyArchive -Headers $headers -UseBasicParsing
}

$pythonDir = Join-Path $PayloadDir 'python'
Write-Info "extracting..."

# Extract to a scratch dir and then locate python.exe, rather than assuming a
# fixed number of leading path components. install_only archives have shipped
# with and without a top-level 'python/' wrapper; finding the interpreter is
# both simpler and immune to that.
$pyTmp = Join-Path $BuildDir 'python-tmp'
if (Test-Path $pyTmp) { Remove-Item $pyTmp -Recurse -Force }
New-Item -ItemType Directory -Path $pyTmp -Force | Out-Null

# tar ships with Windows 10 1803+.
& tar -xzf $pyArchive -C $pyTmp
if ($LASTEXITCODE -ne 0) { Die "tar failed to extract $pyArchive" }

$foundPy = Get-ChildItem $pyTmp -Recurse -Filter 'python.exe' -File |
           Sort-Object { $_.FullName.Length } | Select-Object -First 1
if (-not $foundPy) { Die "no python.exe anywhere inside $pyArchive" }

$srcRoot = $foundPy.Directory.FullName
Write-Info "interpreter root: $srcRoot"
New-Item -ItemType Directory -Path $pythonDir -Force | Out-Null
& robocopy $srcRoot $pythonDir /E /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { Die "robocopy failed staging the interpreter (exit $LASTEXITCODE)" }
$global:LASTEXITCODE = 0
Remove-Item $pyTmp -Recurse -Force

$payloadPy = Join-Path $pythonDir 'python.exe'
if (-not (Test-Path $payloadPy)) { Die "python.exe missing after staging" }

$pyVer = (& $payloadPy -c "import sys; print('.'.join(map(str, sys.version_info[:3])))")
Write-Ok "bundled interpreter: Python $pyVer"

# Both venvs come from this one interpreter. That IS the version unification:
# the launcher and the vision side can no longer drift apart.
# 'Continue' around these two: a native command's stderr redirected with 2>&1
# arrives as ErrorRecords, which under 'Stop' would abort the build on a
# routine pip notice. Exit codes are what we actually check.
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& $payloadPy -m ensurepip --upgrade 2>&1 | Out-Null
& $payloadPy -m pip install --quiet --upgrade pip 2>&1 | Out-Null
$ErrorActionPreference = $prevEAP

# --------------------------------------------------------------------------
# 4. Wheels for both venvs
# --------------------------------------------------------------------------
Write-Step "Downloading dependency wheels (offline install payload)"

$wheelRoot = Join-Path $PayloadDir 'wheels'
$wheelSets = @(
    @{ Name = 'launcher'; Req = (Join-Path $RepoRoot 'launcher\requirements.txt') },
    @{ Name = 'vision';   Req = (Join-Path $RepoRoot 'launcher\vision\requirements.txt') }
)

foreach ($set in $wheelSets) {
    $out = Join-Path $wheelRoot $set.Name
    $cache = Join-Path $DownloadDir "wheels-$($set.Name)"
    New-Item -ItemType Directory -Path $out -Force | Out-Null

    # The cache is keyed on the requirements file's content. Without this,
    # editing a requirement and re-running with -SkipDownload silently reuses
    # wheels resolved from the OLD file -- the change appears to take effect
    # and does not.
    $reqHash = (Get-FileHash $set.Req -Algorithm SHA256).Hash
    $stamp   = Join-Path $cache '.req-sha256'
    $cacheValid = (Test-Path $cache) -and
                  (Get-ChildItem $cache -File -Filter '*.whl' -ErrorAction SilentlyContinue) -and
                  (Test-Path $stamp) -and
                  ((Get-Content $stamp -Raw).Trim() -eq $reqHash)

    if ($SkipDownload -and -not $cacheValid -and (Test-Path $cache)) {
        Write-Info "$($set.Name): requirements changed since the cache was made - re-resolving"
    }

    if ($SkipDownload -and $cacheValid) {
        Write-Info "$($set.Name): reusing cached wheels"
        Copy-Item (Join-Path $cache '*.whl') $out -Force
    } else {
        Write-Info "$($set.Name): resolving $($set.Req)"
        # No --platform / --python-version: we are running the very interpreter
        # that will be installed, on the very OS it will run on, so markers and
        # wheel tags resolve correctly. (--platform would break here: it does
        # not set platform_system, so torch's Linux-only nvidia dependencies
        # get pulled into the resolution and fail.)
        & $payloadPy -m pip download `
            --requirement $set.Req `
            --dest $out `
            --only-binary=:all:
        if ($LASTEXITCODE -ne 0) { Die "pip download failed for the $($set.Name) requirement set." }

        if (Test-Path $cache) { Remove-Item $cache -Recurse -Force }
        New-Item -ItemType Directory -Path $cache -Force | Out-Null
        Copy-Item (Join-Path $out '*.whl') $cache -Force
        Set-Content -Path $stamp -Value $reqHash -Encoding ASCII
    }
    $n = (Get-ChildItem $out -File).Count
    Write-Ok "$($set.Name): $n wheels, $(Get-HumanSize $out)"
}

# Requirements like "pandas>=2.0.0" resolve to whatever is newest on the day of
# the build, so two installers built a month apart can ship different science.
# Record exactly what went in. The install itself is deterministic given the
# payload (--no-index --find-links), so this manifest fully describes it.
$manifest = Join-Path $PayloadDir 'wheels-manifest.txt'
"WormScan wheel manifest - version $Version - built $((Get-Date).ToUniversalTime().ToString('u'))" |
    Set-Content $manifest -Encoding UTF8
foreach ($set in $wheelSets) {
    Add-Content $manifest ""
    Add-Content $manifest "[$($set.Name)]"
    Get-ChildItem (Join-Path $wheelRoot $set.Name) -File |
        Sort-Object Name | ForEach-Object { Add-Content $manifest "  $($_.Name)" }
}
Write-Info "recorded exact versions in wheels-manifest.txt"

# How much free disk the install will actually need. Wheels expand roughly
# 3x when installed, so the payload size alone badly understates it -- a 2 GB
# guess passed and then pip died on torch with "[Errno 28] No space left on
# device". Measure it instead: sum the UNCOMPRESSED size of every wheel, add
# the payload that gets copied, and let postinstall check against that.
Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
$unpackedBytes = 0
foreach ($set in $wheelSets) {
    foreach ($whl in Get-ChildItem (Join-Path $wheelRoot $set.Name) -Filter '*.whl' -File) {
        try {
            $zip = [IO.Compression.ZipFile]::OpenRead($whl.FullName)
            $unpackedBytes += ($zip.Entries | Measure-Object -Property Length -Sum).Sum
            $zip.Dispose()
        } catch {
            Write-Host "      could not measure $($whl.Name): $($_.Exception.Message)" -ForegroundColor Yellow
        }
    }
}

# Loud about anything that moved a MAJOR version, because those change
# behaviour rather than just numbers.
$pandasWhl = Get-ChildItem (Join-Path $wheelRoot 'launcher') -Filter 'pandas-*' -File | Select-Object -First 1
if ($pandasWhl -and $pandasWhl.Name -match 'pandas-(\d+)\.') {
    if ([int]$Matches[1] -ge 3) {
        Write-Host "    NOTE: this build resolved pandas $($Matches[1]).x from 'pandas>=2.0.0'." -ForegroundColor Yellow
        Write-Host "          If your dev environment is on pandas 2.x then the installed app" -ForegroundColor Yellow
        Write-Host "          is NOT running what you tested. Pin it in launcher/requirements.txt" -ForegroundColor Yellow
        Write-Host "          if that matters to you." -ForegroundColor Yellow
    }
}

# --------------------------------------------------------------------------
# 5. ffmpeg + ffprobe
# --------------------------------------------------------------------------
Write-Step "Fetching ffmpeg and ffprobe"

# Bundled rather than installed: the motility pipeline needs BOTH binaries, and
# imageio-ffmpeg (already a dependency) ships ffmpeg but NOT ffprobe, which the
# fps and duration probes both use. Bundling also means no PATH surgery on the
# user's machine and no winget step during install.
$ffZip = Join-Path $DownloadDir 'ffmpeg-win64.zip'

# Several sources, tried in order. gyan.dev is the canonical Windows build host
# but it is a single small domain and does not resolve on every network -- some
# university DNS filters block it outright. BtbN's builds live on GitHub
# Releases, which is already known to work here because the interpreter came
# from there, so it goes first.
#
# Both are GPL builds, and that is required, not incidental: render_video.py
# encodes with libx264, which is GPL-only. An LGPL build would install fine and
# then fail at the first render.
$ffSources = @(
    @{ Name = 'GitHub (BtbN)'
       Url  = 'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip' },
    @{ Name = 'gyan.dev'
       Url  = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' }
)

if ($FfmpegZip) {
    if (-not (Test-Path $FfmpegZip)) { Die "-FfmpegZip does not exist: $FfmpegZip" }
    Write-Info "using the zip you supplied: $FfmpegZip"
    Copy-Item $FfmpegZip $ffZip -Force
} elseif ($SkipDownload -and (Test-Path $ffZip)) {
    Write-Info "reusing $ffZip"
} else {
    if ($FfmpegUrl) { $ffSources = @(@{ Name = 'your -FfmpegUrl'; Url = $FfmpegUrl }) }

    $got = $false
    foreach ($src in $ffSources) {
        Write-Info "trying $($src.Name)"
        try {
            Invoke-WebRequest -Uri $src.Url -OutFile $ffZip -UseBasicParsing
            Write-Ok "downloaded from $($src.Name)"
            $got = $true
            break
        } catch {
            Write-Host "      failed: $($_.Exception.Message)" -ForegroundColor Yellow
            if (Test-Path $ffZip) { Remove-Item $ffZip -Force -ErrorAction SilentlyContinue }
        }
    }

    if (-not $got) {
        # Last resort: an ffmpeg already on this machine. Not ideal -- we then
        # ship whatever version happens to be installed rather than a known one
        # -- but a working installer beats a blocked build, and it is announced
        # rather than silent.
        $localFf = Get-Command ffmpeg.exe  -ErrorAction SilentlyContinue
        $localFp = Get-Command ffprobe.exe -ErrorAction SilentlyContinue
        if ($localFf -and $localFp) {
            Write-Host "    Could not download. Falling back to the ffmpeg already on this PATH:" -ForegroundColor Yellow
            Write-Host "      $($localFf.Source)" -ForegroundColor Yellow
            Write-Host "      $($localFp.Source)" -ForegroundColor Yellow
            Write-Host "    The installer will ship THAT build, whatever version it is." -ForegroundColor Yellow
            $ffBinDst = Join-Path $PayloadDir 'ffmpeg\bin'
            New-Item -ItemType Directory -Path $ffBinDst -Force | Out-Null
            Copy-Item $localFf.Source $ffBinDst -Force
            Copy-Item $localFp.Source $ffBinDst -Force
            $script:FfmpegDone = $true
        } else {
            Die @"
Could not obtain ffmpeg from any source.

Every download failed, and there is no ffmpeg/ffprobe on this machine's PATH
to fall back to. The last error was a DNS or connection failure, which usually
means a network or firewall restriction rather than anything wrong with the
build.

Three ways forward, easiest first:

  1. Download the zip by hand on any machine that can reach it:
         https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip
     then point the build at it:
         .\build_installer.ps1 -SkipDownload -FfmpegZip "C:\path\to\that.zip"

  2. Install ffmpeg locally and let the build copy it:
         winget install Gyan.FFmpeg
     then re-run:
         .\build_installer.ps1 -SkipDownload

  3. Supply a different mirror:
         .\build_installer.ps1 -SkipDownload -FfmpegUrl "<url of a win64 GPL zip>"

-SkipDownload reuses everything already fetched, so none of the 500 MB you
have just downloaded is wasted.
"@
        }
    }
}

if (-not $FfmpegDone) {

$ffTmp = Join-Path $BuildDir 'ffmpeg-tmp'
if (Test-Path $ffTmp) { Remove-Item $ffTmp -Recurse -Force }
Expand-Archive -Path $ffZip -DestinationPath $ffTmp -Force

$ffBinSrc = Get-ChildItem $ffTmp -Recurse -Directory |
    Where-Object { $_.Name -eq 'bin' } | Select-Object -First 1
if (-not $ffBinSrc) { Die "no bin\ directory inside the ffmpeg archive" }

$ffBinDst = Join-Path $PayloadDir 'ffmpeg\bin'
New-Item -ItemType Directory -Path $ffBinDst -Force | Out-Null
foreach ($exe in 'ffmpeg.exe', 'ffprobe.exe') {
    $src = Join-Path $ffBinSrc.FullName $exe
    if (-not (Test-Path $src)) { Die "$exe missing from the ffmpeg archive" }
    Copy-Item $src $ffBinDst -Force
}
# The licence must travel with the binaries.
$ffLicense = Get-ChildItem $ffTmp -Recurse -File |
    Where-Object { $_.Name -match '^(LICENSE|COPYING)' } | Select-Object -First 1
if ($ffLicense) { Copy-Item $ffLicense.FullName (Join-Path $PayloadDir 'ffmpeg\LICENSE.txt') -Force }
Remove-Item $ffTmp -Recurse -Force

}  # end if (-not $FfmpegDone)

$ffBinCheck = Join-Path $PayloadDir 'ffmpeg\bin'
foreach ($exe in 'ffmpeg.exe', 'ffprobe.exe') {
    if (-not (Test-Path (Join-Path $ffBinCheck $exe))) { Die "$exe is missing from the payload" }
}
Write-Ok "ffmpeg + ffprobe: $(Get-HumanSize $ffBinCheck)"

# --------------------------------------------------------------------------
# 6. Application sources
# --------------------------------------------------------------------------
Write-Step "Staging the application"

$appDir = Join-Path $PayloadDir 'app'
New-Item -ItemType Directory -Path $appDir -Force | Out-Null

# robocopy is the only sane recursive copy with excludes on Windows.
# Exit codes 0-7 are success; 8+ is a real failure.
$exclDirs = @('.venv', '.venv-vision', '__pycache__', '.git', '_build', 'dist',
              '.viewer_cache', 'worm_diagnostics')
$roboArgs = @(
    (Join-Path $RepoRoot 'launcher'), (Join-Path $appDir 'launcher'),
    '/E', '/NFL', '/NDL', '/NJH', '/NJS', '/NP',
    '/XD'
) + $exclDirs + @('/XF', '*.pyc', '*.pyo')

& robocopy @roboArgs | Out-Null
if ($LASTEXITCODE -ge 8) { Die "robocopy failed copying launcher\ (exit $LASTEXITCODE)" }
$global:LASTEXITCODE = 0

# The model is git-ignored and excluded from nothing above only because its
# directory exists - copy it explicitly so a missing one is a loud failure
# rather than a silently model-less install.
$modelDst = Join-Path $appDir 'launcher\vision\models'
New-Item -ItemType Directory -Path $modelDst -Force | Out-Null
Copy-Item $model $modelDst -Force

# AGPL-3.0 requires the licence to travel with the work, and the third-party
# notices cover the GPL ffmpeg binaries and everything else redistributed here.
# An installer without these is not a compliant distribution.
foreach ($doc in 'LICENSE', 'THIRD-PARTY-NOTICES.md') {
    $src = Join-Path $RepoRoot $doc
    if (-not (Test-Path $src)) { Die "$doc is missing from the repo root - it must ship with the installer." }
    Copy-Item $src $PayloadDir -Force
}
Write-Ok "licences staged"

# Build identity, read back by launcher/paths.py.
$buildInfo = [ordered]@{
    version     = $Version
    commit      = $gitSha
    dirty       = $gitDirty
    built_utc   = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    python      = $pyVer
    built_on    = $env:COMPUTERNAME
}
$buildInfo | ConvertTo-Json | Set-Content -Path (Join-Path $appDir 'launcher\_build_info.json') -Encoding UTF8

# Ship the manifest inside the app so an installed copy can name its own contents.
if (Test-Path $manifest) { Copy-Item $manifest (Join-Path $appDir 'launcher\_wheels-manifest.txt') -Force }

Write-Ok "application: $(Get-HumanSize $appDir)"

# Peak = everything copied in, plus both venvs, before wheels\ is reclaimed.
$payloadBytes = (Get-ChildItem $PayloadDir -Recurse -File | Measure-Object -Property Length -Sum).Sum
$wheelBytes   = (Get-ChildItem $wheelRoot  -Recurse -File | Measure-Object -Property Length -Sum).Sum
$peakBytes    = $payloadBytes + $unpackedBytes
$finalBytes   = $payloadBytes - $wheelBytes + $unpackedBytes

@{
    peak_bytes  = [int64]$peakBytes
    final_bytes = [int64]$finalBytes
    version     = $Version
} | ConvertTo-Json | Set-Content (Join-Path $PayloadDir 'install-space.json') -Encoding UTF8

Write-Info ("install needs {0:N2} GB peak, {1:N2} GB once settled" -f ($peakBytes/1GB), ($finalBytes/1GB))

Write-Step "Payload summary"
foreach ($part in 'python', 'wheels', 'ffmpeg', 'app') {
    $p = Join-Path $PayloadDir $part
    Write-Info ("{0,-10} {1,10}" -f $part, (Get-HumanSize $p))
}
Write-Host ("    {0,-10} {1,10}" -f 'TOTAL', (Get-HumanSize $PayloadDir)) -ForegroundColor White

if ($NoCompile) {
    Write-Host ""
    Write-Host "Payload staged (-NoCompile given). Nothing was compiled." -ForegroundColor Yellow
    Write-Host "  $PayloadDir"
    exit 0
}

# --------------------------------------------------------------------------
# 7. Compile the installer
# --------------------------------------------------------------------------
Write-Step "Building the installer with Inno Setup"

$iss = Join-Path $PackagingDir 'wormscan.iss'
& $iscc `
    "/DPayloadDir=$PayloadDir" `
    "/DAppVersion=$InnoVersion" `
    "/DFullVersion=$Version" `
    "/DOutputDir=$DistDir" `
    $iss
if ($LASTEXITCODE -ne 0) { Die "Inno Setup failed (exit $LASTEXITCODE)" }

$setup = Get-ChildItem $DistDir -Filter 'WormScanSetup*.exe' |
         Sort-Object LastWriteTime -Descending | Select-Object -First 1

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  Done" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  $($setup.FullName)"
Write-Host "  $([math]::Round($setup.Length / 1MB, 1)) MB   version $Version"
Write-Host ""
Write-Host "  Copy that one file to the test machine and double-click it."
Write-Host "  No admin rights and no Python are needed there."
Write-Host ""
