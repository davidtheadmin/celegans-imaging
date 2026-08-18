#requires -Version 5.1
<#
.SYNOPSIS
    Build WormScan's two Python environments from the bundled wheels.

.DESCRIPTION
    Run by the installer immediately after the files are copied. Do NOT run it
    by hand: the bundled wheels it installs from are deleted at the end of a
    successful install, so a second run fails with "wheel directory missing".
    Re-run the installer instead.

    Why the environments are built here rather than shipped ready-made: a
    virtual environment records absolute paths to its own interpreter. One
    created on the build machine would point at a directory that does not exist
    on the target. So we ship the ingredients and assemble on arrival.

    Everything installs from <install>\wheels with --no-index, so this step
    does not touch the network at all. If it fails, it failed on this machine's
    disk, not on someone's connection.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $InstallDir
)

# NOT 'Stop'. Native commands with 2>&1 push stderr into the pipeline as
# ErrorRecords, and under 'Stop' the first pip warning would abort the whole
# install. Every step below checks $LASTEXITCODE explicitly instead, which is
# the only thing that actually distinguishes success from failure here.
$ErrorActionPreference = 'Continue'
$ProgressPreference    = 'SilentlyContinue'

$LogFile = Join-Path $InstallDir 'install-log.txt'

function Log ($msg, $colour = 'Gray') {
    $line = "[{0:HH:mm:ss}] {1}" -f (Get-Date), $msg
    Write-Host $line -ForegroundColor $colour
    try { Add-Content -Path $LogFile -Value $line -Encoding UTF8 } catch { }
}

function Fail ($msg) {
    Log "FAILED: $msg" 'Red'
    Write-Host ""
    Write-Host "  Setup could not finish. The log is at:" -ForegroundColor Red
    Write-Host "    $LogFile" -ForegroundColor Red
    Write-Host ""
    Write-Host "  This window stays open for 30 seconds so you can read it." -ForegroundColor Yellow
    Start-Sleep -Seconds 30
    exit 1
}

Set-Content -Path $LogFile -Value "WormScan install log - $(Get-Date -Format 'u')" -Encoding UTF8

# Stamp the build identity into the log. A log that cannot name the installer
# that wrote it is indistinguishable from an older log, and telling those apart
# by eye costs a round trip every time.
$biPath = Join-Path $InstallDir 'app\launcher\_build_info.json'
if (Test-Path $biPath) {
    try {
        $bi = Get-Content $biPath -Raw | ConvertFrom-Json
        Add-Content $LogFile "build     : $($bi.version)"
        Add-Content $LogFile "commit    : $($bi.commit)$(if ($bi.dirty) { ' (dirty tree)' })"
        Add-Content $LogFile "built     : $($bi.built_utc) on $($bi.built_on)"
    } catch {
        Add-Content $LogFile "build     : (could not read _build_info.json: $($_.Exception.Message))"
    }
} else {
    Add-Content $LogFile "build     : (no _build_info.json)"
}
Add-Content $LogFile "installdir: $InstallDir"

Write-Host ""
Write-Host "  Setting up WormScan" -ForegroundColor Cyan
Write-Host "  This takes a few minutes. You do not need to do anything." -ForegroundColor DarkGray
Write-Host ""

$PythonExe  = Join-Path $InstallDir 'python\python.exe'
$AppLauncher= Join-Path $InstallDir 'app\launcher'
$WheelDir   = Join-Path $InstallDir 'wheels'

# --------------------------------------------------------------------------
# Free disk space
# --------------------------------------------------------------------------
# Wheels expand about 3x when installed, so the size of what was copied in
# badly understates what is still to come. build_installer.ps1 measures the
# real figure -- the uncompressed size of every wheel plus the payload -- and
# writes it to install-space.json, so this check is arithmetic rather than a
# guess. It matters: pip running out of room mid-torch reports
# "[Errno 28] No space left on device" after several minutes, having already
# written most of a gigabyte that then has to be cleaned up by hand.
$needPeak  = 3.0GB      # fallback if the measurement is missing
$needFinal = 2.2GB
$spacePath = Join-Path $InstallDir 'install-space.json'
if (Test-Path $spacePath) {
    try {
        $sp = Get-Content $spacePath -Raw | ConvertFrom-Json
        if ($sp.peak_bytes)  { $needPeak  = [int64]$sp.peak_bytes }
        if ($sp.final_bytes) { $needFinal = [int64]$sp.final_bytes }
    } catch {
        Log "could not read install-space.json, using defaults: $($_.Exception.Message)" 'Yellow'
    }
}
# 10% headroom: pip needs scratch space of its own while unpacking.
$needPeak = [int64]($needPeak * 1.10)

$free = $null
try {
    $driveLetter = (Split-Path $InstallDir -Qualifier).TrimEnd(':')
    $free = (Get-PSDrive $driveLetter).Free
} catch {
    Log "could not check free disk space: $($_.Exception.Message)" 'Yellow'
}

if ($null -ne $free) {
    Log ("free on {0}: {1:N2} GB; this install peaks at {2:N2} GB and settles to {3:N2} GB" -f `
         $driveLetter, ($free/1GB), ($needPeak/1GB), ($needFinal/1GB))

    if ($free -lt $needPeak) {
        $shortGb = ($needPeak - $free) / 1GB
        Write-Host ""
        Write-Host "  Not enough free disk space on drive ${driveLetter}:" -ForegroundColor Red
        Write-Host ""
        Write-Host ("    free now : {0,7:N2} GB" -f ($free/1GB))     -ForegroundColor Yellow
        Write-Host ("    needed   : {0,7:N2} GB" -f ($needPeak/1GB)) -ForegroundColor Yellow
        Write-Host ("    short by : {0,7:N2} GB" -f $shortGb)        -ForegroundColor Red
        Write-Host ""
        Write-Host ("  WormScan settles at about {0:N1} GB once installed, but needs more" -f ($needFinal/1GB)) -ForegroundColor Yellow
        Write-Host "  than that WHILE installing: PyTorch and the other packages are" -ForegroundColor Yellow
        Write-Host "  compressed in the installer and expand to roughly three times" -ForegroundColor Yellow
        Write-Host "  their size on disk." -ForegroundColor Yellow
        Write-Host ""
        Write-Host "  Free up some space and run the installer again." -ForegroundColor Green
        Write-Host "  Emptying the Recycle Bin and Downloads is usually enough." -ForegroundColor Green
        Write-Host ""
        Fail ("insufficient disk space: {0:N2} GB free, {1:N2} GB needed" -f ($free/1GB), ($needPeak/1GB))
    }
}

# --------------------------------------------------------------------------
# Both environments come from the SAME bundled interpreter. That is the
# version unification: the launcher side and the inference side can no longer
# drift onto different Pythons, because there is only one.
# --------------------------------------------------------------------------
# The venvs sit directly under the install root, NOT nested inside app\launcher.
# torch ships a licence tree 167 characters deep, and Windows caps a path at 260
# unless long-path support is enabled machine-wide (which needs admin rights this
# installer deliberately never asks for). Nesting cost 32 characters and made a
# real install fail by four. launcher/paths.py resolves these at run time.
$envs = @(
    @{ Name  = 'launcher'
       Venv  = (Join-Path $InstallDir 'venv')
       Req   = (Join-Path $AppLauncher 'requirements.txt')
       Wheel = (Join-Path $WheelDir 'launcher')
       Note  = 'the application itself' },

    @{ Name  = 'vision'
       Venv  = (Join-Path $InstallDir 'venv-vision')
       Req   = (Join-Path $AppLauncher 'vision\requirements.txt')
       Wheel = (Join-Path $WheelDir 'vision')
       Note  = 'worm staging model (the big one, torch)' }
)

# Refuse a too-deep install directory up front. Without this the failure lands
# three minutes in, mid-torch, as an opaque "[WinError 206] The filename or
# extension is too long" that says nothing about the install PATH being at fault.
$MaxPackagePath = 167    # measured: torch's deepest dist-info licence file
$WindowsMaxPath = 260
foreach ($e in $envs) {
    $sp = Join-Path $e.Venv 'Lib\site-packages'
    $headroom = $WindowsMaxPath - 1 - $MaxPackagePath - $sp.Length
    Log "$($e.Name) site-packages: $($sp.Length) chars, headroom $headroom"
    if ($headroom -lt 0) {
        $short = [math]::Abs($headroom)
        Log "install directory is $short characters too deep" 'Red'
        Write-Host ""
        Write-Host "  The folder WormScan was installed into is too deep for Windows." -ForegroundColor Red
        Write-Host ""
        Write-Host "    $InstallDir" -ForegroundColor Red
        Write-Host ""
        Write-Host "  Windows limits a file path to $WindowsMaxPath characters, and one file" -ForegroundColor Yellow
        Write-Host "  inside PyTorch needs $MaxPackagePath of them by itself." -ForegroundColor Yellow
        Write-Host "  This path has to be $short characters shorter." -ForegroundColor Yellow
        Write-Host ""
        Write-Host "  Fix: uninstall, run the installer again, and choose a shorter" -ForegroundColor Green
        Write-Host "       folder. Something like  C:\WormScan  always works." -ForegroundColor Green
        Write-Host ""
        Write-Host "  (IT could instead enable Windows long-path support, but that needs" -ForegroundColor DarkGray
        Write-Host "   administrator rights and a shorter folder does not.)" -ForegroundColor DarkGray
        Fail "install path too long by $short characters"
    }
}


$step = 0
foreach ($e in $envs) {
    $step++
    Write-Host ""
    Write-Host "  [$step/2] $($e.Name) environment - $($e.Note)" -ForegroundColor Cyan

    if (-not (Test-Path $e.Req))   { Fail "requirements file missing: $($e.Req)" }
    if (-not (Test-Path $e.Wheel)) { Fail "wheel directory missing: $($e.Wheel)" }

    if (Test-Path $e.Venv) {
        Log "removing an existing $($e.Name) environment"
        Remove-Item $e.Venv -Recurse -Force -ErrorAction SilentlyContinue
    }

    Log "creating $($e.Venv)"
    & $PythonExe -m venv $e.Venv 2>&1 | ForEach-Object { Log "  $_" }
    if ($LASTEXITCODE -ne 0) { Fail "could not create the $($e.Name) virtual environment" }

    $venvPy = Join-Path $e.Venv 'Scripts\python.exe'
    if (-not (Test-Path $venvPy)) { Fail "$($e.Name) environment has no python.exe" }

    Log "installing $($e.Name) dependencies from bundled wheels (offline)"
    # --no-index + --find-links: install ONLY from what we shipped. No network,
    # and no chance of silently picking up a different version than the one
    # this build was tested with.
    & $venvPy -m pip install `
        --no-index `
        --find-links $e.Wheel `
        --requirement $e.Req `
        --disable-pip-version-check `
        --no-warn-script-location 2>&1 | ForEach-Object { Log "  $_" }
    if ($LASTEXITCODE -ne 0) { Fail "dependency install failed for the $($e.Name) environment" }

    Log "$($e.Name) environment ready" 'Green'
}

# --------------------------------------------------------------------------
# Verify before declaring success. A venv that exists but cannot import its
# own dependencies is worse than one that failed loudly.
# --------------------------------------------------------------------------
Write-Host ""
Write-Host "  Checking the installation" -ForegroundColor Cyan

$launcherPy = Join-Path $InstallDir 'venv\Scripts\python.exe'
$visionPy   = Join-Path $InstallDir 'venv-vision\Scripts\python.exe'

# The check runs from a SCRIPT FILE rather than python -c '...'.
#
# Windows PowerShell 5.1 strips embedded double quotes when it builds the
# command line for a native program, so a -c argument of
#     import sys; print(QUOTE ok QUOTE)
# arrives at python with the quotes gone, and dies with
#     NameError: name 'ok' is not defined
# That is a failure in the verifier that looks exactly like a failure of the
# thing being verified, which is the worst kind. A script file has no command
# line to mangle, and the module list travels as a single comma-separated
# argument containing no quotes and no spaces.
$verifyPy = Join-Path $env:TEMP 'wormscan-verify.py'
@'
import importlib, sys

failed = []
for name in sys.argv[1].split(","):
    try:
        mod = importlib.import_module(name)
    except Exception as exc:
        failed.append("%s -> %s: %s" % (name, type(exc).__name__, exc))
    else:
        print("%-16s %s" % (name, getattr(mod, "__version__", "")))

if failed:
    print("IMPORT FAILURES:")
    for f in failed:
        print("  " + f)
    sys.exit(1)
sys.exit(0)
'@ | Set-Content -Path $verifyPy -Encoding UTF8

# Wider than before: every module the app imports at startup or first analysis,
# so a missing one surfaces here rather than as a traceback the user sees.
$checks = @(
    @{ Py = $launcherPy; Label = 'launcher'
       Mods = 'customtkinter,pandas,numpy,cv2,skimage,tables,h5py,matplotlib,openpyxl,scipy,tifffile,imagecodecs,requests' },
    @{ Py = $visionPy;   Label = 'vision'
       Mods = 'torch,torchvision,ultralytics,numpy,cv2' }
)

foreach ($c in $checks) {
    Write-Host ""
    Write-Host "  $($c.Label) environment:" -ForegroundColor Cyan
    $out = & $c.Py $verifyPy $c.Mods 2>&1
    $out | ForEach-Object { Log "  $_" }
    if ($LASTEXITCODE -ne 0) {
        Fail "the $($c.Label) environment cannot import its dependencies"
    }
    Log "$($c.Label) imports OK" 'Green'
}

Remove-Item $verifyPy -Force -ErrorAction SilentlyContinue

foreach ($exe in 'ffmpeg.exe', 'ffprobe.exe') {
    $p = Join-Path $InstallDir "ffmpeg\bin\$exe"
    if (Test-Path $p) { Log "$exe present" 'Green' } else { Log "WARNING: $exe missing" 'Yellow' }
}

# The Start Menu has a shortcut to this folder, and paths.py looks in it for
# tunable overrides. Create it now so both work from the first launch instead
# of only after the app has written its first log line.
$UserData = Join-Path $env:APPDATA 'WormScan'
if (-not (Test-Path $UserData)) {
    New-Item -ItemType Directory -Path $UserData -Force | Out-Null
    Log "created data folder $UserData"
}

$model = Join-Path $AppLauncher 'vision\models\staging.pt'
if (Test-Path $model) {
    Log ("staging model present ({0:N1} MB)" -f ((Get-Item $model).Length / 1MB)) 'Green'
} else {
    Log "WARNING: staging model missing - Worm Survival will not run" 'Yellow'
}

# --------------------------------------------------------------------------
# Reclaim the install-time scaffolding.
# --------------------------------------------------------------------------
if (Test-Path $WheelDir) {
    $mb = [math]::Round(((Get-ChildItem $WheelDir -Recurse -File |
           Measure-Object -Property Length -Sum).Sum / 1MB), 0)
    Log "removing $mb MB of installer wheels (no longer needed)"
    Remove-Item $WheelDir -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "  WormScan is ready." -ForegroundColor Green
Write-Host ""
Write-Host "  Colony Survival and Worm Survival work straight away." -ForegroundColor DarkGray
Write-Host "  Motility and Crawling additionally need Tierpsy - use the" -ForegroundColor DarkGray
Write-Host "  'WormScan - Set up video analysis' shortcut in the Start Menu." -ForegroundColor DarkGray
Write-Host ""
Start-Sleep -Seconds 3
exit 0
