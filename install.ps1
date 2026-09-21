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

# Wrap in a function so 'return' works when piped via iex
# (bare 'exit' in iex context closes the entire PowerShell window)
function Install-BeConductor {

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
    Write-Host "  Python 3.10+ not found. Installing via winget..." -ForegroundColor Yellow
    try {
        winget install Python.Python.3.12 --accept-package-agreements --accept-source-agreements 2>&1 | Out-Null
        # Refresh PATH after install
        $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("PATH", "User") + ";" + $env:PATH
        foreach ($cmd in @("py", "python3", "python")) {
            try {
                $ver = & $cmd --version 2>&1
                if ($ver -match "Python (\d+)\.(\d+)") {
                    $major = [int]$Matches[1]; $minor = [int]$Matches[2]
                    if ($major -ge 3 -and $minor -ge 10) { $pyCmd = $cmd; break }
                }
            } catch {}
        }
    } catch {
        Write-Host "  winget not available." -ForegroundColor Red
    }
    if (-not $pyCmd) {
        Write-Host "Error: Python 3.10+ is required but could not be installed." -ForegroundColor Red
        Write-Host "Install manually from https://python.org (check 'Add to PATH' during install)"
        return
    }
    Write-Host "  Python installed" -NoNewline
    Write-Host " OK" -ForegroundColor Green
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

# ── Helper: a prompt that gives up ───────────────────────────────────
# This script also runs unattended from the update dialog, where Read-Host
# would wait for an answer that never comes. Poll the console instead and
# fall through to the default.
function Read-WithTimeout($prompt, $seconds) {
    Write-Host $prompt -NoNewline
    $buf = ""
    try {
        $sw = [Diagnostics.Stopwatch]::StartNew()
        while ($sw.Elapsed.TotalSeconds -lt $seconds) {
            if ([Console]::KeyAvailable) {
                $k = [Console]::ReadKey($true)
                if ($k.Key -eq "Enter") { break }
                $buf += $k.KeyChar
                Write-Host $k.KeyChar -NoNewline
            }
            Start-Sleep -Milliseconds 100
        }
    } catch {
        # No interactive console (piped install) - treat as no answer.
        $buf = ""
    }
    Write-Host ""
    return $buf
}

# ── Helper: bring the server back up ─────────────────────────────────
# The upgrade stops it, so every exit path has to start it again - otherwise
# an update silently leaves the machine without a server.
function Start-Conductor($exePath) {
    Write-Host "Starting $Project..."
    try {
        & $exePath restart -f 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { & $exePath up }
    } catch {
        try { & $exePath up } catch {}
    }
}

# ── Stop running server before upgrade ────────────────────────────────

$versionBefore = $null
try { $versionBefore = (& $Project --version 2>&1 | Out-String).Trim() } catch {}
$pipxFailed = $false

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
    $pipxFailed = ($LASTEXITCODE -ne 0)
    try { & pipx inject --force $Project claude-agent-sdk 2>&1 | Out-Null } catch {}
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
            return
        }
        Expand-Archive -Path $zipPath -DestinationPath $tmpDir -Force

        Write-Host "Installing $Project..."
        & pipx install (Join-Path $tmpDir $Project) --force
        $pipxFailed = ($LASTEXITCODE -ne 0)
        try { & pipx inject --force $Project claude-agent-sdk 2>&1 | Out-Null } catch {}
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
    $version = (& $Project --version 2>&1 | Out-String).Trim()
    Write-Host "  $Project $version" -NoNewline
    if ($pipxFailed) {
        Write-Host " FAILED" -ForegroundColor Red
        Write-Host "  pipx could not install the new version." -ForegroundColor Red
        if ($versionBefore -and $versionBefore -eq $version) {
            Write-Host "  The version did not change - this is still the old install."
        }
        Write-Host "  On Windows this usually means a $Project process still had files"
        Write-Host "  open. Close any $Project terminals and dashboards, then run the"
        Write-Host "  installer again."
    } else {
        Write-Host " OK" -ForegroundColor Green
    }
    $installed = -not $pipxFailed
} catch {
    Write-Host "  Warning: '$Project' command not found in PATH." -ForegroundColor Yellow
    Write-Host "  Restart your terminal and try again."
}

Write-Host ""

# ── Upgrade claude-agent-sdk in the server's Python ─────────────────
# The server subprocess uses whatever Python runs the `be-conductor`
# CLI (sys.executable).  pipx inject only touches the pipx venv,
# which may not be the same Python — so also upgrade via the CLI
# helper which runs `pip install --upgrade` inside sys.executable.
if ($installed) {
    try {
        Write-Host "Upgrading claude-agent-sdk in the server's Python..."
        & $Project upgrade-sdk 2>&1 | Out-Null
    } catch {}
}

# ── Agent CLIs (reported only - never installed from here) ──────────
Write-Host "Agent CLIs on PATH:"
foreach ($agent in @("claude", "codex", "opencode", "lean-ctx")) {
    if (Get-Command $agent -ErrorAction SilentlyContinue) {
        Write-Host "  $agent" -NoNewline
        Write-Host " OK" -ForegroundColor Green
    } elseif ($agent -eq "lean-ctx") {
        Write-Host "  $agent - not found (optional: context compression for profiles)"
        Write-Host "             to use it:  cargo install lean-ctx   (see https://leanctx.com)"
    } else {
        Write-Host "  $agent - not found"
    }
}
Write-Host ""

# ── Claude Desktop / Claude Code (MCP) ──────────────────────────────
# Two separate MCP lists: Claude Desktop's chat side reads
# claude_desktop_config.json, Claude Code (CLI + the Code tab) reads its
# own. install-mcp registers in whichever of the two exists.
$claudeDesktopDir = Join-Path $env:APPDATA "Claude"
$hasClaudeCli = [bool](Get-Command claude -ErrorAction SilentlyContinue)
if ($installed -and ((Test-Path $claudeDesktopDir) -or $hasClaudeCli)) {
    $claudeCfg = Join-Path $claudeDesktopDir "claude_desktop_config.json"
    $claudeCodeCfg = Join-Path $env:USERPROFILE ".claude.json"
    $registered = ((Test-Path $claudeCfg) -and ((Get-Content $claudeCfg -Raw) -match '"be-conductor"')) -or `
                  ((Test-Path $claudeCodeCfg) -and ((Get-Content $claudeCodeCfg -Raw) -match '"be-conductor"\s*:\s*\{'))
    if ($registered) {
        # Already registered - refresh so the command path stays valid.
        try { & $Project install-mcp 2>&1 | Out-Null } catch {}
    } else {
        # A hint, never a question: this script also runs unattended from the
        # update dialog, and a prompt here would hold up the server restart.
        Write-Host "Claude found - to let it conduct be-conductor sessions (MCP), run:"
        Write-Host "  $Project install-mcp"
    }
    Write-Host ""
}

Write-Host ""

# ── Autostart setup (Startup folder) ──────────────────────────────────

if ($installed) {
    $conductorPath = (Get-Command $Project -ErrorAction SilentlyContinue).Source
    if (-not $conductorPath) {
        $conductorPath = "$env:USERPROFILE\.local\bin\$Project.exe"
    }

    # An update should not re-ask a question already answered: when the task
    # exists, refresh it silently. Otherwise ask, but give up after 30s so an
    # unattended update is never left waiting.
    $taskExists = $false
    try { $taskExists = [bool](Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) } catch {}
    if ($taskExists) {
        $answer = "y"
    } else {
        $answer = Read-WithTimeout "Start $Project automatically on login? [Y/n] (30s, then yes) " 30
    }

    if ($answer -eq "" -or $answer -match "^[Yy]") {

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
    } else {
        Write-Host "  Skipped. See docs -> Auto-Start on Boot"
    }

    Start-Conductor $conductorPath
} else {
    # The upgrade stopped the server; if it then failed, put the old one back
    # rather than leaving the machine with nothing running.
    $fallback = (Get-Command $Project -ErrorAction SilentlyContinue).Source
    if ($fallback) { Start-Conductor $fallback }
}

# ── Optional: LeanCTX ────────────────────────────────────────────────
# Dead last, after autostart has the server running again: the build can
# take minutes, and the installer stops the server early on, so anything
# slow placed before that leaves the server down for its duration.

if (-not (Get-Command lean-ctx -ErrorAction SilentlyContinue)) {
    Write-Host "LeanCTX is an optional context-compression layer for agent sessions"
    Write-Host "(https://leanctx.com). be-conductor can use it per profile."
    if (Get-Command cargo -ErrorAction SilentlyContinue) {
        $answer = Read-WithTimeout "  Install it now with cargo? [y/N] (30s, then no) " 30
        if ($answer -match "^[Yy]") {
            Write-Host "  Building lean-ctx (this takes a few minutes)..."
            try {
                & cargo install lean-ctx
                Write-Host "  lean-ctx installed" -NoNewline; Write-Host " OK" -ForegroundColor Green
            } catch {
                Write-Host "  lean-ctx install failed - see https://leanctx.com" -ForegroundColor Yellow
            }
        } else {
            Write-Host "  Skipped - install later with: cargo install lean-ctx"
        }
    } else {
        Write-Host "  To use it: install Rust, then 'cargo install lean-ctx'"
    }
    Write-Host ""
}

Write-Host ""
Write-Host "Done! " -NoNewline -ForegroundColor Green
Write-Host "Run '$Project run claude research' to start a session."
Write-Host "Dashboard: http://127.0.0.1:7777"
Write-Host ""
Write-Host "Tip: run '$Project setup-acp' to enable Codex / Gemini / Claude"
Write-Host "     via the Agent Client Protocol (needs Node.js 20+)."
Write-Host ""
Write-Host "If the command is not found, restart your terminal."

} # end Install-BeConductor

Install-BeConductor
