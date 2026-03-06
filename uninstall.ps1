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

# Uninstaller — removes be-conductor, autostart configs, and optionally user data.
#   irm https://github.com/somniacs/be-conductor/releases/latest/download/uninstall.ps1 | iex
#   powershell -ExecutionPolicy Bypass -File uninstall.ps1

$ErrorActionPreference = "Stop"

# ── Configuration (must match install.ps1) ────────────────────────────
$Project   = "be-conductor"
$DataDir   = "$env:USERPROFILE\.$Project"
$TaskName  = "Be-Conductor"

# Previous name (for cleanup). Leave empty if not applicable.
$OldProject  = "conductor"
$OldTaskName = "Conductor"
# ──────────────────────────────────────────────────────────────────────

Write-Host "b $Project - uninstall" -ForegroundColor Cyan
Write-Host ""

# ── Stop server ───────────────────────────────────────────────────────

Write-Host "Stopping server..."
try { & $Project shutdown 2>&1 | Out-Null } catch {}
if ($OldProject -and $OldProject -ne $Project) {
    try { & $OldProject shutdown 2>&1 | Out-Null } catch {}
}

# ── Remove autostart ──────────────────────────────────────────────────

# Remove Startup folder VBS script
$startupDir = [System.Environment]::GetFolderPath("Startup")
$vbsPath = Join-Path $startupDir "$Project.vbs"
if (Test-Path $vbsPath) {
    Remove-Item -Force $vbsPath
    Write-Host "  Startup script removed" -NoNewline
    Write-Host " OK" -ForegroundColor Green
}

# Remove old scheduled tasks (from previous installs)
foreach ($name in @($TaskName, $OldProject, $OldTaskName) | Select-Object -Unique) {
    if (-not $name) { continue }
    try {
        $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if ($task) {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Host "  Scheduled task '$name' removed" -NoNewline
            Write-Host " OK" -ForegroundColor Green
        }
    } catch {}
}

# ── Uninstall package ────────────────────────────────────────────────

$hasPipx = $false
try { & pipx --version 2>&1 | Out-Null; $hasPipx = $true } catch {}

if ($hasPipx) {
    Write-Host "Uninstalling $Project via pipx..."
    try { & pipx uninstall $Project 2>&1 | Out-Null } catch {}
    if ($OldProject -and $OldProject -ne $Project) {
        try { & pipx uninstall $OldProject 2>&1 | Out-Null } catch {}
    }
    Write-Host "  pipx package removed" -NoNewline
    Write-Host " OK" -ForegroundColor Green
} else {
    Write-Host "  Warning: pipx not found - you may need to remove $Project manually." -ForegroundColor Yellow
}

# ── User data ─────────────────────────────────────────────────────────

Write-Host ""

# Current data dir
if (Test-Path $DataDir) {
    $answer = Read-Host "Remove all data in $DataDir? (config, sessions, uploads) [y/N]"
    if ($answer -match "^[Yy]") {
        Remove-Item -Recurse -Force $DataDir
        Write-Host "  $DataDir removed" -NoNewline
        Write-Host " OK" -ForegroundColor Green
    } else {
        Write-Host "  Kept $DataDir"
    }
}

# Old data dir (if renamed)
if ($OldProject -and $OldProject -ne $Project) {
    $oldDataDir = "$env:USERPROFILE\.$OldProject"
    if (Test-Path $oldDataDir) {
        $answer = Read-Host "Remove old data in ${oldDataDir}? [y/N]"
        if ($answer -match "^[Yy]") {
            Remove-Item -Recurse -Force $oldDataDir
            Write-Host "  $oldDataDir removed" -NoNewline
            Write-Host " OK" -ForegroundColor Green
        } else {
            Write-Host "  Kept $oldDataDir"
        }
    }
}

if (-not (Test-Path $DataDir) -and (-not $OldProject -or -not (Test-Path "$env:USERPROFILE\.$OldProject"))) {
    Write-Host "  No data directory found"
}

Write-Host ""
Write-Host "$Project has been uninstalled." -ForegroundColor Green
