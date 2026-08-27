#requires -Version 5.1
<#
.SYNOPSIS
    Set up video analysis (Tierpsy) for WormScan.

.DESCRIPTION
    Reached from the Start Menu shortcut "WormScan - Set up video analysis".

    Only Motility and Crawling need this. Colony Survival and Worm Survival run
    entirely inside WormScan and never touch a container.

    What it does, skipping anything already done:

        1. find a working container engine (docker, podman or nerdctl)
        2. if there is none, install Rancher Desktop via winget
        3. wait for the engine to come up
        4. download the Tierpsy image (several GB, once per machine)
        5. verify

    This is the one part of WormScan that cannot be made fully automatic. It
    needs administrator rights, it may need Windows Subsystem for Linux turned
    on, and that can require a restart. The script detects each of those and
    says plainly what to do rather than failing with a code.
#>
[CmdletBinding()]
param(
    [string] $Image = 'docker.io/tierpsy/tierpsy-tracker:latest',
    [int]    $EngineWaitMinutes = 10,
    [switch] $SkipInstall
)

$ErrorActionPreference = 'Continue'
$ProgressPreference    = 'SilentlyContinue'

function Say    ($m) { Write-Host "  $m" }
function Head   ($m) { Write-Host ""; Write-Host "  $m" -ForegroundColor Cyan; Write-Host "  $('-' * $m.Length)" -ForegroundColor DarkCyan }
function Good   ($m) { Write-Host "  $m" -ForegroundColor Green }
function Warn   ($m) { Write-Host "  $m" -ForegroundColor Yellow }
function Bad    ($m) { Write-Host "  $m" -ForegroundColor Red }

function Pause-Then-Exit ($code) {
    Write-Host ""
    Write-Host "  Press any key to close this window."
    try { $null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown') } catch { Start-Sleep 20 }
    exit $code
}

function Test-Engine ($cmd) {
    <# Returns $true only if the engine is BOTH installed and running. #>
    try {
        & $cmd --version *> $null
        if ($LASTEXITCODE -ne 0) { return $false }
        & $cmd info --format '{{json .}}' *> $null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

function Repair-DockerContext {
    <#
        A machine that once had Docker Desktop keeps a "desktop-linux" context,
        and the docker CLI goes on using it after Docker Desktop is gone. Every
        call then fails with

            cannot find the file ... pipe/dockerDesktopLinuxEngine

        which looks exactly like "no engine installed" even though Rancher is
        running perfectly on the standard pipe. Switch to the default context
        and re-test before believing there is nothing here.
    #>
    try {
        $current = (& docker context show 2>$null)
        if ($LASTEXITCODE -ne 0 -or -not $current) { return $false }
        if ($current.Trim() -eq 'default') { return $false }
        Say "  the docker CLI is pointed at the '$($current.Trim())' context, which is not"
        Say "  answering. Trying the default context..."
        & docker context use default *> $null
        if ($LASTEXITCODE -ne 0) { return $false }
        if (Test-Engine 'docker') {
            Good "Switched the docker CLI to the default context."
            return $true
        }
        & docker context use $current.Trim() *> $null   # put it back
        return $false
    } catch { return $false }
}

function Find-Engine {
    foreach ($c in 'docker', 'podman', 'nerdctl') {
        if (Test-Engine $c) { return $c }
    }
    if (Repair-DockerContext) { return 'docker' }
    return $null
}

Clear-Host
Write-Host ""
Write-Host "  ============================================================"
Write-Host "   WormScan - set up video analysis"
Write-Host "  ============================================================"
Say ""
Say "This is only needed for Motility and Crawling."
Say "Colony Survival and Worm Survival already work without it."

# --------------------------------------------------------------------------
Head "Step 1 of 4 - looking for a container engine"

$engine = Find-Engine
if ($engine) {
    Good "Found a working engine: $engine"
    Say  ((& $engine --version) -join ' ')

    # Rancher Desktop set to containerd puts nerdctl on PATH instead of docker.
    # WormScan works with it, but dockerd (moby) is the configuration this has
    # actually been tested against, and switching is one preference rather than
    # a reinstall. Say so once rather than letting a subtle difference surface
    # later as a strange analysis result.
    if ($engine -eq 'nerdctl') {
        Write-Host ""
        Warn "This is the containerd engine (nerdctl), not dockerd."
        Say ""
        Say "WormScan supports it, but dockerd is the tested configuration."
        Say "If anything behaves oddly, switch it in Rancher Desktop:"
        Say "    Preferences -> Container Engine -> dockerd (moby)"
        Say ""
        Say "Images pulled under one engine are not visible to the other, so"
        Say "switching means downloading Tierpsy again. Decide now rather than"
        Say "after the download."
        Write-Host ""
        $keep = Read-Host "  Carry on with nerdctl? [Y/n]"
        if ($keep -and $keep -notmatch '^(y|yes)$') {
            Say ""
            Say "Switch the engine in Rancher Desktop, wait for it to restart,"
            Say "then run this shortcut again."
            Pause-Then-Exit 0
        }
    }
} else {
    Say "No running engine found."

    if ($SkipInstall) { Bad "Nothing to do (-SkipInstall was given)."; Pause-Then-Exit 1 }

    # ---------------------------------------------------------------------
    Head "Step 2 of 4 - installing a container engine"

    # Everything here turns on two questions, in this order:
    #
    #   1. Is WSL already on this machine? Enabling it is the ONE thing that
    #      genuinely requires an administrator, and it is a one-time act.
    #   2. Does this user have administrator rights?
    #
    # If WSL is present, a user with no admin rights can still get there:
    # Podman's MSI supports a per-user install (MSIINSTALLPERUSER=1) and
    # `podman machine init` needs no elevation once WSL exists. WormScan
    # supports podman as a first-class engine, so this is a real route rather
    # than a downgrade.
    #
    # Note winget is NOT usable for that: it has no per-user mode for the
    # podman package regardless of flags, so the MSI is fetched directly.

    # Detected by EXIT CODE, never by output. wsl.exe writes UTF-16, which
    # Windows PowerShell 5.1 often renders as nothing at all, so a blank result
    # says nothing about whether WSL is there.
    #
    # Get-Command first: if wsl.exe is absent entirely, `& wsl` raises a
    # PowerShell error without setting $LASTEXITCODE, which would leave the
    # value from whatever ran before and produce a confident wrong answer.
    $wslPresent = $false
    if (Get-Command wsl.exe -ErrorAction SilentlyContinue) {
        $global:LASTEXITCODE = 0
        & wsl.exe --status *> $null
        $wslPresent = ($LASTEXITCODE -eq 0)
    }
    if ($wslPresent) { Good "Windows Subsystem for Linux is present." }
    else             { Warn "Windows Subsystem for Linux is NOT installed." }

    Say ""
    Say "Two ways to run Tierpsy:"
    Say ""
    Say "  [1] Rancher Desktop  - needs administrator rights"
    Say "  [2] Podman           - no administrator rights needed$(if (-not $wslPresent) { ', once WSL exists' })"
    Say ""

    if (-not $wslPresent) {
        Warn "Neither can work until WSL is enabled, and only an administrator"
        Warn "can do that. It is a single command, run once:"
        Say ""
        Say "      wsl --install"
        Say ""
        Say "Ask whoever administers this computer to run it and restart."
        Say "After that you can finish this yourself with option [2] - nothing"
        Say "else needs administrator rights."
        Say ""
        $anyway = Read-Host "  Try Rancher Desktop anyway (only works if you have admin)? [y/N]"
        if ($anyway -notmatch '^(y|yes)$') { Pause-Then-Exit 1 }
        $choice = '1'
    } else {
        $choice = Read-Host "  Which? [1/2] (press Enter for 2, the one that needs no admin)"
        if (-not $choice) { $choice = '2' }
    }

    if ($choice -eq '1') {
        # ----- Rancher Desktop, via winget (machine scope, needs admin) -----
        if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
            Bad "winget is not available on this machine."
            Say "Install Rancher Desktop by hand from https://rancherdesktop.io/"
            Say "and set Container Engine to 'dockerd (moby)' in its Preferences."
            Pause-Then-Exit 1
        }

        if (-not $wslPresent) {
            Write-Host ""
            Warn "Heads up: Rancher Desktop's installer checks for WSL2 and"
            Warn "stops if it is missing. It will NOT install WSL for you,"
            Warn "despite what winget's dependency list suggests."
            Say ""
            Say "If it refuses, this is the command that fixes it (admin, once):"
            Say "    wsl --install --no-distribution"
            Say "then restart and run this shortcut again."
            Write-Host ""
        }

        Say ""
        Say "Running: winget install SUSE.RancherDesktop"
        Say "(a few hundred MB - please wait)"
        Write-Host ""
        & winget install --id SUSE.RancherDesktop --exact `
            --accept-package-agreements --accept-source-agreements
        $wingetCode = $LASTEXITCODE

        # winget returns non-zero for "it is already installed and there is
        # nothing newer" (-1978335189 UPDATE_NOT_APPLICABLE, and friends). That
        # is the state we WANT, and treating it as a failure sent people off to
        # install Podman they did not need. Rather than chase winget's exit-code
        # list, ask the only question that matters: is Rancher Desktop actually
        # on this machine now?
        if ($wingetCode -ne 0) {
            $rdInstalled = $false
            foreach ($probe in @(
                "$env:ProgramFiles\Rancher Desktop\Rancher Desktop.exe",
                "$env:LOCALAPPDATA\Programs\Rancher Desktop\Rancher Desktop.exe")) {
                if (Test-Path $probe) { $rdInstalled = $true; break }
            }
            if (-not $rdInstalled) {
                try {
                    & winget list --id SUSE.RancherDesktop --exact *> $null
                    $rdInstalled = ($LASTEXITCODE -eq 0)
                } catch { }
            }
            if ($rdInstalled) {
                Write-Host ""
                Good "Rancher Desktop is already installed (winget had nothing to do)."
                $wingetCode = 0
            }
        }

        if ($wingetCode -ne 0) {
            Write-Host ""
            if ($wingetCode -eq 1603 -or $wingetCode -eq -1978335216) {
                Warn "The installer stopped part-way (code $wingetCode)."
                Say ""
                Say "This usually means WSL was just enabled and needs a RESTART,"
                Say "or that the install needed administrator rights it did not have."
                Say ""
                Say "  1. Restart this computer"
                Say "  2. Run this shortcut again"
                Say ""
                Say "If you do not have administrator rights, choose option [2]"
                Say "(Podman) next time - it does not need them."
            } else {
                Bad "winget failed with code $wingetCode."
                Say ""
                Say "If that was a permissions error, run this shortcut again and"
                Say "choose option [2] (Podman), which needs no administrator rights."
            }
            Pause-Then-Exit 1
        }

        Good "Rancher Desktop installed."
        Say ""
        Say "IMPORTANT - do this once, in Rancher Desktop:"
        Say "  Preferences -> Container Engine -> select 'dockerd (moby)'"
        Say ""
        Say "That setting is what lets WormScan talk to it."
        Write-Host ""
        Read-Host "  Start Rancher Desktop, set that, then press Enter here"

    } else {
        # ----- Podman, per-user MSI (no admin) -----
        Say ""
        Say "Installing Podman for your user account only."
        Say "Nothing outside your own profile is touched."
        Write-Host ""

        try {
            $rel = Invoke-RestMethod -UseBasicParsing `
                -Uri 'https://api.github.com/repos/containers/podman/releases/latest' `
                -Headers @{ 'User-Agent' = 'wormscan-setup' }
            $asset = $rel.assets | Where-Object { $_.name -like 'podman-installer-windows-amd64.msi' } |
                     Select-Object -First 1
            if (-not $asset) { throw "no podman-installer-windows-amd64.msi in release $($rel.tag_name)" }

            $msi = Join-Path $env:TEMP $asset.name
            Say "Downloading $($asset.name) ($([math]::Round($asset.size/1MB,1)) MB)..."
            Invoke-WebRequest -UseBasicParsing -Uri $asset.browser_download_url -OutFile $msi
        } catch {
            Bad "Could not download Podman: $($_.Exception.Message)"
            Say ""
            Say "Download it by hand from https://github.com/containers/podman/releases"
            Say "(the file named podman-installer-windows-amd64.msi), then run:"
            Say ""
            Say "    msiexec /i <path to the msi> /qn /norestart MSIINSTALLPERUSER=1 MACHINE_PROVIDER=wsl"
            Pause-Then-Exit 1
        }

        $msiLog = Join-Path $env:TEMP 'wormscan-podman-msi.log'
        Say "Installing (per-user, no administrator prompt)..."
        # MSIINSTALLPERUSER=1 is what keeps this out of Program Files and away
        # from any elevation prompt. MACHINE_PROVIDER=wsl picks the WSL backend
        # over Hyper-V, which WOULD need admin for the first machine.
        $proc = Start-Process msiexec.exe -Wait -PassThru -ArgumentList @(
            '/i', "`"$msi`"", '/qn', '/norestart',
            '/l*v', "`"$msiLog`"",
            'MSIINSTALLPERUSER=1', 'MACHINE_PROVIDER=wsl'
        )
        if ($proc.ExitCode -ne 0) {
            Bad "The Podman installer failed (code $($proc.ExitCode))."
            Say "A detailed log is at: $msiLog"
            Pause-Then-Exit 1
        }
        Good "Podman installed."

        # The MSI puts podman.exe on PATH, but this already-running shell has
        # the old PATH. Rebuild it from the environment rather than telling the
        # user to open a new window.
        $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                    [Environment]::GetEnvironmentVariable('Path', 'User')

        if (-not (Get-Command podman -ErrorAction SilentlyContinue)) {
            Warn "podman is installed but not on this window's PATH yet."
            Say "Close this window, open the shortcut again, and it will continue."
            Pause-Then-Exit 1
        }

        Say ""
        Say "Creating the Podman virtual machine (a few minutes, one time)..."
        & podman machine init 2>&1 | ForEach-Object { Say "  $_" }
        # Already-exists is not an error worth stopping for.
        & podman machine start 2>&1 | ForEach-Object { Say "  $_" }
        Good "Podman machine started."
    }
}

# --------------------------------------------------------------------------
Head "Step 3 of 4 - waiting for the engine to be ready"

if (-not $engine) {
    Say "Rancher Desktop takes a few minutes to start the first time."
    Say "Waiting up to $EngineWaitMinutes minutes..."
    $deadline = (Get-Date).AddMinutes($EngineWaitMinutes)
    while ((Get-Date) -lt $deadline) {
        $engine = Find-Engine
        if ($engine) { break }
        Write-Host "." -NoNewline
        Start-Sleep -Seconds 10
    }
    Write-Host ""
    if (-not $engine) {
        Bad "The engine did not become ready within $EngineWaitMinutes minutes."
        Say ""
        Say "Check that Rancher Desktop is running (look in the system tray),"
        Say "that it does not show an error, and that Container Engine is set"
        Say "to 'dockerd (moby)'. Then run this shortcut again."
        Pause-Then-Exit 1
    }
}
Good "Engine ready: $engine"

# --------------------------------------------------------------------------
Head "Step 4 of 4 - downloading Tierpsy"

& $engine image inspect $Image *> $null
if ($LASTEXITCODE -eq 0) {
    Good "Tierpsy is already downloaded."
} else {
    Say "Downloading $Image"
    Say "This is several GB and only happens once on this machine."
    Write-Host ""
    & $engine pull $Image
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Bad "The download failed."
        Say ""
        Say "Usually this is the network. Try again on a stable connection."
        Say "To retry by hand:"
        Say "    $engine pull $Image"
        Pause-Then-Exit 1
    }
}

# --------------------------------------------------------------------------
Head "Checking"

& $engine image inspect $Image *> $null
if ($LASTEXITCODE -ne 0) { Bad "Tierpsy still is not available. Something is wrong - send David this window."; Pause-Then-Exit 1 }

$fmt = if ($engine -eq 'podman') { '{{.Host.Cpus}} {{.Host.MemTotal}}' } else { '{{.NCPU}} {{.MemTotal}}' }
$res = (& $engine info --format $fmt) -split '\s+'
if ($res.Count -ge 2) {
    $cpus = $res[0]
    $gb   = [math]::Round([double]$res[1] / 1GB, 1)
    Good "Engine reports $cpus CPUs and $gb GB - WormScan sizes its workers from this."
    if ([int]$cpus -le 2) {
        Warn "Only $cpus CPUs. In Rancher Desktop, Preferences -> Virtual Machine,"
        Warn "raising the CPU and memory allocation will make analysis much faster."
    }
}

Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host "   Done - Motility and Crawling are ready to use." -ForegroundColor Green
Write-Host "  ============================================================" -ForegroundColor Green
Say ""
Say "One thing to remember: Rancher Desktop must be running before you"
Say "start a Motility or Crawling analysis. It normally starts with Windows."
Pause-Then-Exit 0
