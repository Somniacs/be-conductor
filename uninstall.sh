#!/usr/bin/env bash
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
#   curl -fsSL https://github.com/somniacs/be-conductor/releases/latest/download/uninstall.sh | bash
#   ./uninstall.sh
set -e

# ── Configuration (must match install.sh) ─────────────────────────────
PROJECT="be-conductor"
DATA_DIR="$HOME/.$PROJECT"
SERVICE_NAME="$PROJECT"
PLIST_LABEL="com.$PROJECT.server"

# Previous name (for cleanup). Leave empty if not applicable.
OLD_PROJECT="conductor"
# ──────────────────────────────────────────────────────────────────────

echo "♭ $PROJECT — uninstall"
echo ""

# ── Helpers ───────────────────────────────────────────────────────────

prompt_yn() {
    local question="$1" default="$2" reply
    if [ "$default" = "Y" ]; then
        printf "%s [Y/n] " "$question"
    else
        printf "%s [y/N] " "$question"
    fi
    if [ -t 0 ]; then
        read -r reply
    elif [ -e /dev/tty ]; then
        read -r reply </dev/tty
    else
        reply=""
    fi
    case "$reply" in
        [Yy]*) return 0 ;;
        [Nn]*) return 1 ;;
        "")
            [ "$default" = "Y" ] && return 0 || return 1
            ;;
        *) [ "$default" = "Y" ] && return 0 || return 1 ;;
    esac
}

remove_autostart() {
    # Usage: remove_autostart <service_name> <plist_label>
    local svc="$1" plist="$2"
    OS="$(uname -s)"
    case "$OS" in
        Linux)
            if command -v systemctl &>/dev/null && [ -f "$HOME/.config/systemd/user/$svc.service" ]; then
                echo "Removing systemd service ($svc)..."
                systemctl --user stop "$svc" 2>/dev/null || true
                systemctl --user disable "$svc" 2>/dev/null || true
                rm -f "$HOME/.config/systemd/user/$svc.service"
                systemctl --user daemon-reload
                echo "  systemd service removed ✓"
            fi
            # Remove cron @reboot entry if present
            if command -v crontab &>/dev/null && crontab -l 2>/dev/null | grep -qF "$svc serve"; then
                echo "Removing cron @reboot entry ($svc)..."
                crontab -l 2>/dev/null | grep -vF "$svc serve" | crontab -
                echo "  cron entry removed ✓"
            fi
            ;;
        Darwin)
            plist_file="$HOME/Library/LaunchAgents/$plist.plist"
            if [ -f "$plist_file" ]; then
                echo "Removing launchd agent ($plist)..."
                launchctl unload "$plist_file" 2>/dev/null || true
                rm -f "$plist_file"
                echo "  launchd agent removed ✓"
            fi
            ;;
    esac
}

# ── Stop server ───────────────────────────────────────────────────────

echo "Stopping server..."
"$PROJECT" shutdown 2>/dev/null || true
if [ -n "$OLD_PROJECT" ] && [ "$OLD_PROJECT" != "$PROJECT" ]; then
    "$OLD_PROJECT" shutdown 2>/dev/null || true
fi

# ── Remove autostart ─────────────────────────────────────────────────

remove_autostart "$SERVICE_NAME" "$PLIST_LABEL"
if [ -n "$OLD_PROJECT" ] && [ "$OLD_PROJECT" != "$PROJECT" ]; then
    remove_autostart "$OLD_PROJECT" "com.$OLD_PROJECT.server"
fi

# ── Uninstall package ────────────────────────────────────────────────

if command -v pipx &>/dev/null; then
    echo "Uninstalling $PROJECT via pipx..."
    pipx uninstall "$PROJECT" 2>/dev/null || true
    if [ -n "$OLD_PROJECT" ] && [ "$OLD_PROJECT" != "$PROJECT" ]; then
        pipx uninstall "$OLD_PROJECT" 2>/dev/null || true
    fi
    echo "  pipx package removed ✓"
elif command -v pip &>/dev/null; then
    echo "Uninstalling $PROJECT via pip..."
    pip uninstall -y "$PROJECT" 2>/dev/null || true
    if [ -n "$OLD_PROJECT" ] && [ "$OLD_PROJECT" != "$PROJECT" ]; then
        pip uninstall -y "$OLD_PROJECT" 2>/dev/null || true
    fi
    echo "  pip package removed ✓"
else
    echo "  Warning: neither pipx nor pip found — skipping package removal."
    echo "  You may need to remove $PROJECT manually."
fi

# ── User data ─────────────────────────────────────────────────────────

echo ""

# Current data dir
if [ -d "$DATA_DIR" ]; then
    if prompt_yn "Remove all data in $DATA_DIR? (config, sessions, uploads)" N; then
        rm -rf "$DATA_DIR"
        echo "  $DATA_DIR removed ✓"
    else
        echo "  Kept $DATA_DIR"
    fi
fi

# Old data dir (if renamed)
if [ -n "$OLD_PROJECT" ] && [ "$OLD_PROJECT" != "$PROJECT" ]; then
    OLD_DATA_DIR="$HOME/.$OLD_PROJECT"
    if [ -d "$OLD_DATA_DIR" ]; then
        if prompt_yn "Remove old data in $OLD_DATA_DIR?" N; then
            rm -rf "$OLD_DATA_DIR"
            echo "  $OLD_DATA_DIR removed ✓"
        else
            echo "  Kept $OLD_DATA_DIR"
        fi
    fi
fi

if [ ! -d "$DATA_DIR" ] && { [ -z "$OLD_PROJECT" ] || [ ! -d "$HOME/.$OLD_PROJECT" ]; }; then
    echo "  No data directory found"
fi

echo ""
echo "$PROJECT has been uninstalled."
