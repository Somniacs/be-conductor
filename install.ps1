# be-conductor — Local orchestration for terminal sessions.
#
# Copyright (c) 2026 Max Rheiner / Somniacs AG
#
# Licensed under the MIT License. You may obtain a copy
# of the license at:
#
#     https://opensource.org/licenses/MIT
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND.

# Smart installer — works as one-liner AND local install.
#   irm https://github.com/somniacs/be-conductor/releases/latest/download/install.ps1 | iex
#   powershell -ExecutionPolicy Bypass -File install.ps1

$ErrorActionPreference = "Stop"

# ── Configuration (change these if the project is renamed) ────────────
$Project     = "be-conductor"
$Repo        = "somniacs/be-conductor"
$ReleaseUrl  = "https://github.com/$Repo/releases/latest/download"
$DataDir     = "$env:USERPROFILE\.$Project"
$TaskName    = "be-conductor"

# Previous name (for migration). Leave empty if not applicable.
$OldProject  = "conductor"
$OldTaskName = "Conductor"
# ──────────────────────────────────────────────────────────────────────

Write-Host "b $Project - install" -ForegroundColor Cyan
Write-Host ""

# ── Check Python 3.10+ ───────────────────────────────────────────────

$pyCmd = $null
foreach ($cmd in @("py", "python3", "python")) {
    try {
        $ver = & $cmd --version 2>&1
        if ($ver -match "Python (\d+)\.(\d+)") {
            $major = [int]$Matches[1]
            $minor = [int]$Matches[2]
            if ($major -ge 3 -and $minor -ge 10) {
                $pyCmd = $cmd
                Write-Host "  Python $major.$minor (via $cmd)" -NoNewline
                Write-Host " OK" -ForegroundColor Green
                break
            }
        }
    } catch {}
}

if (-not $pyCmd) {
    Write-Host "Error: Python 3.10+ is required but not found." -ForegroundColor Red
    Write-Host "Install from https://python.org (check 'Add to PATH' during install)"
    exit 1
}

# ── Install pipx if needed ───────────────────────────────────────────

$hasPipx = $false
try {
    & pipx --version 2>&1 | Out-Null
    $hasPipx = $true
} catch {}

if (-not $hasPipx) {
    Write-Host "  Installing pipx..."
    & $pyCmd -m pip install --user pipx
    & $pyCmd -m pipx ensurepath
    # Refresh PATH for current session
    $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "User") + ";" + $env:PATH
}

Write-Host "  pipx" -NoNewline
Write-Host " OK" -ForegroundColor Green
Write-Host ""

# ── Migrate from previous project name ───────────────────────────────

if ($OldProject -and $OldProject -ne $Project) {
    $oldDataDir = "$env:USERPROFILE\.$OldProject"

    # Stop old server
    try { & $OldProject shutdown -f 2>&1 | Out-Null } catch {}

    # Remove old scheduled task
    $oldTaskNames = @($OldProject)
    if ($OldTaskName) { $oldTaskNames += $OldTaskName }
    foreach ($otn in ($oldTaskNames | Select-Object -Unique)) {
        try {
            $oldTask = Get-ScheduledTask -TaskName $otn -ErrorAction SilentlyContinue
            if ($oldTask) {
                Unregister-ScheduledTask -TaskName $otn -Confirm:$false
                Write-Host "  Removed old scheduled task ($otn)" -NoNewline
                Write-Host " OK" -ForegroundColor Green
            }
        } catch {}
    }

    # Uninstall old package
    try { & pipx uninstall $OldProject 2>&1 | Out-Null } catch {}

    # Migrate data directory
    if ((Test-Path $oldDataDir) -and -not (Test-Path $DataDir)) {
        Move-Item -Path $oldDataDir -Destination $DataDir
        Write-Host "  Migrated $oldDataDir -> $DataDir" -NoNewline
        Write-Host " OK" -ForegroundColor Green
    } elseif ((Test-Path $oldDataDir) -and (Test-Path $DataDir)) {
        Write-Host "  Note: both $oldDataDir and $DataDir exist." -ForegroundColor Yellow
        Write-Host "  Keeping both - merge manually if needed."
    }

    Write-Host ""
}

# ── Stop running server before upgrade ────────────────────────────────

try { & $Project shutdown -f 2>&1 | Out-Null } catch {}

# ── Detect mode: local vs remote ─────────────────────────────────────

$scriptDir = $null
try {
    if ($MyInvocation.MyCommand.Path) {
        $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    }
} catch {}

if ($scriptDir -and (Test-Path (Join-Path $scriptDir "pyproject.toml"))) {
    # ── Local mode ────────────────────────────────────────────────
    Write-Host "Installing $Project from local source..."
    & pipx install -e $scriptDir --force
} else {
    # ── Remote mode ───────────────────────────────────────────────
    Write-Host "Downloading latest $Project release..."
    $tmpDir = Join-Path ([System.IO.Path]::GetTempPath()) "$Project-install-$(Get-Random)"
    New-Item -ItemType Directory -Path $tmpDir -Force | Out-Null

    try {
        $zipPath = Join-Path $tmpDir "$Project.zip"
        try {
            Invoke-WebRequest -Uri "$ReleaseUrl/$Project.zip" -OutFile $zipPath -UseBasicParsing
        } catch {
            Write-Host ""
            Write-Host "Error: could not download the release archive." -ForegroundColor Red
            Write-Host "If a new version was just published, the build may still be in progress."
            Write-Host "Wait a minute and try again."
            exit 1
        }
        Expand-Archive -Path $zipPath -DestinationPath $tmpDir -Force

        Write-Host "Installing $Project..."
        & pipx install (Join-Path $tmpDir $Project) --force
    } finally {
        Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue
    }
}

Write-Host ""

# ── Verify installation ──────────────────────────────────────────────

# Refresh PATH in case pipx just added it
$env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "User") + ";" + $env:PATH

$installed = $false
try {
    $version = & $Project --version 2>&1
    Write-Host "  $Project $version" -NoNewline
    Write-Host " OK" -ForegroundColor Green
    $installed = $true
} catch {
    Write-Host "  Warning: '$Project' command not found in PATH." -ForegroundColor Yellow
    Write-Host "  Restart your terminal and try again."
}

Write-Host ""

# ── Autostart setup (Startup folder) ──────────────────────────────────

if ($installed) {
    $answer = Read-Host "Start $Project automatically on login? [Y/n]"
    if ($answer -eq "" -or $answer -match "^[Yy]") {
        $conductorPath = (Get-Command $Project -ErrorAction SilentlyContinue).Source
        if (-not $conductorPath) {
            $conductorPath = "$env:USERPROFILE\.local\bin\$Project.exe"
        }

        # Clean up legacy autostart (old scheduled task, VBS, shortcut)
        try {
            $oldTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
            if ($oldTask) {
                Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
            }
        } catch {}
        $startupDir = [System.Environment]::GetFolderPath("Startup")
        foreach ($ext in @("vbs", "lnk")) {
            $old = Join-Path $startupDir "$Project.$ext"
            if (Test-Path $old) { Remove-Item $old -Force }
        }

        # Scheduled task — runs hidden, no console window flash
        $action   = New-ScheduledTaskAction -Execute $conductorPath -Argument "up"
        $trigger  = New-ScheduledTaskTrigger -AtLogOn
        $trigger.UserId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan)
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null

        Write-Host "  Autostart configured (scheduled task)" -NoNewline
        Write-Host " OK" -ForegroundColor Green

        # Start the server now
        & $conductorPath up
    } else {
        Write-Host "  Skipped. See docs -> Auto-Start on Boot"
    }
}

Write-Host ""
Write-Host "Done! " -NoNewline -ForegroundColor Green
Write-Host "Run '$Project run claude research' to start a session."
Write-Host "Dashboard: http://127.0.0.1:7777"
Write-Host ""
Write-Host "If the command is not found, restart your terminal."
