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

# Smart installer — works as curl-piped bootstrap AND local install.
#   curl -fsSL https://github.com/somniacs/be-conductor/releases/latest/download/install.sh | bash
#   ./install.sh          (from cloned repo or extracted tarball)
set -e

# ── Configuration (change these if the project is renamed) ────────────
PROJECT="be-conductor"
REPO="somniacs/be-conductor"
RELEASE_URL="https://github.com/$REPO/releases/latest/download"
DATA_DIR="$HOME/.$PROJECT"
SERVICE_NAME="$PROJECT"
PLIST_LABEL="com.$PROJECT.server"

# Previous name (for migration). Leave empty if not applicable.
OLD_PROJECT="conductor"
# ──────────────────────────────────────────────────────────────────────

echo "♭ $PROJECT — install"
echo ""

# ── Helpers ───────────────────────────────────────────────────────────

download() {
    local url="$1" dest="$2"
    if command -v curl &>/dev/null; then
        curl -fsSL -o "$dest" "$url"
    elif command -v wget &>/dev/null; then
        wget -q -O "$dest" "$url"
    else
        echo "Error: curl or wget is required" >&2
        exit 1
    fi
}

prompt_yn() {
    # Usage: prompt_yn "Question?" Y  → default yes
    #        prompt_yn "Question?" N  → default no
    local question="$1" default="$2" reply
    if [ "$default" = "Y" ]; then
        printf "%s [Y/n] " "$question"
    else
        printf "%s [y/N] " "$question"
    fi
    # When piped from curl, stdin is the script itself — use /dev/tty
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

# ── Check Python 3.10+ ───────────────────────────────────────────────

if ! command -v python3 &>/dev/null; then
    echo "Error: python3 is required but not found."
    echo "Install Python 3.10+ from https://python.org"
    exit 1
fi

PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    echo "Error: Python 3.10+ required, found $PY_VERSION"
    exit 1
fi

echo "  Python $PY_VERSION ✓"

# ── Install pipx if needed ───────────────────────────────────────────

if ! command -v pipx &>/dev/null; then
    echo "  Installing pipx..."
    # On Debian/Ubuntu (PEP 668), pip install is blocked — use apt
    if ls /usr/lib/python3*/EXTERNALLY-MANAGED &>/dev/null 2>&1; then
        echo "  Detected externally-managed Python (PEP 668), using apt..."
        sudo apt install -y pipx
    else
        python3 -m pip install --user pipx
    fi
    python3 -m pipx ensurepath
    export PATH="$HOME/.local/bin:$PATH"
fi

echo "  pipx ✓"
echo ""

# ── Migrate from previous project name ───────────────────────────────

if [ -n "$OLD_PROJECT" ] && [ "$OLD_PROJECT" != "$PROJECT" ]; then
    OLD_DATA_DIR="$HOME/.$OLD_PROJECT"
    OLD_SERVICE_NAME="$OLD_PROJECT"
    OLD_PLIST_LABEL="com.$OLD_PROJECT.server"

    # Stop old server
    if command -v "$OLD_PROJECT" &>/dev/null; then
        echo "Migrating from $OLD_PROJECT..."
        "$OLD_PROJECT" shutdown 2>/dev/null || true
    fi

    # Remove old autostart
    OS="$(uname -s)"
    case "$OS" in
        Linux)
            if command -v systemctl &>/dev/null && [ -f "$HOME/.config/systemd/user/$OLD_SERVICE_NAME.service" ]; then
                systemctl --user stop "$OLD_SERVICE_NAME" 2>/dev/null || true
                systemctl --user disable "$OLD_SERVICE_NAME" 2>/dev/null || true
                rm -f "$HOME/.config/systemd/user/$OLD_SERVICE_NAME.service"
                systemctl --user daemon-reload
                echo "  Removed old systemd service ✓"
            fi
            ;;
        Darwin)
            old_plist="$HOME/Library/LaunchAgents/$OLD_PLIST_LABEL.plist"
            if [ -f "$old_plist" ]; then
                launchctl unload "$old_plist" 2>/dev/null || true
                rm -f "$old_plist"
                echo "  Removed old launchd agent ✓"
            fi
            ;;
    esac

    # Uninstall old package
    if command -v pipx &>/dev/null; then
        pipx uninstall "$OLD_PROJECT" 2>/dev/null || true
    fi

    # Migrate data directory
    if [ -d "$OLD_DATA_DIR" ] && [ ! -d "$DATA_DIR" ]; then
        mv "$OLD_DATA_DIR" "$DATA_DIR"
        echo "  Migrated $OLD_DATA_DIR → $DATA_DIR ✓"
    elif [ -d "$OLD_DATA_DIR" ] && [ -d "$DATA_DIR" ]; then
        echo "  Note: both $OLD_DATA_DIR and $DATA_DIR exist."
        echo "  Keeping both — merge manually if needed."
    fi

    echo ""
fi

# ── Stop running server before upgrade ────────────────────────────────

if command -v "$PROJECT" &>/dev/null; then
    echo "Stopping running server..."
    "$PROJECT" shutdown 2>/dev/null || true
fi

# ── Detect mode: local vs remote ─────────────────────────────────────

SCRIPT_DIR=""
# If run directly (not piped), check for pyproject.toml next to script
if [ -n "${BASH_SOURCE[0]:-}" ] && [ "${BASH_SOURCE[0]}" != "bash" ]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/pyproject.toml" ]; then
    # ── Local mode ────────────────────────────────────────────────
    echo "Installing $PROJECT from local source..."
    pipx install -e "$SCRIPT_DIR" --force
else
    # ── Remote mode ───────────────────────────────────────────────
    echo "Downloading latest $PROJECT release..."
    tmpdir=$(mktemp -d)
    trap 'rm -rf "$tmpdir"' EXIT

    if ! download "$RELEASE_URL/$PROJECT.tar.gz" "$tmpdir/$PROJECT.tar.gz"; then
        echo ""
        echo "Error: could not download the release archive."
        echo "If a new version was just published, the build may still be in progress."
        echo "Wait a minute and try again."
        exit 1
    fi
    tar xzf "$tmpdir/$PROJECT.tar.gz" -C "$tmpdir"

    echo "Installing $PROJECT..."
    pipx install "$tmpdir/$PROJECT" --force

    # trap handles cleanup
fi

echo ""

# ── Verify installation ──────────────────────────────────────────────

if ! command -v "$PROJECT" &>/dev/null; then
    # pipx may have installed to a path not yet in PATH
    export PATH="$HOME/.local/bin:$PATH"
fi

if command -v "$PROJECT" &>/dev/null; then
    VERSION=$("$PROJECT" --version 2>/dev/null || echo "unknown")
    echo "  $PROJECT $VERSION ✓"
else
    echo "  Warning: '$PROJECT' command not found in PATH."
    echo "  Restart your terminal or run: source ~/.bashrc  # or ~/.zshrc"
fi

echo ""

# ── Autostart setup ──────────────────────────────────────────────────

setup_autostart_linux() {
    local service_file="$HOME/.config/systemd/user/$SERVICE_NAME.service"
    mkdir -p ~/.config/systemd/user

    cat > "$service_file" << EOF
[Unit]
Description=Conductor Server
After=network.target

[Service]
ExecStart=%h/.local/bin/$PROJECT serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

    systemctl --user daemon-reload
    systemctl --user enable "$SERVICE_NAME"
    systemctl --user restart "$SERVICE_NAME"
    # Survive logout
    loginctl enable-linger "$USER" 2>/dev/null || true
    echo "  systemd service enabled and started ✓"
}

setup_autostart_cron() {
    local conductor_path
    conductor_path=$(command -v "$PROJECT" || echo "$HOME/.local/bin/$PROJECT")
    local cron_entry="@reboot $conductor_path serve >> /tmp/$PROJECT.log 2>&1"

    # Add cron entry if not already present
    if crontab -l 2>/dev/null | grep -qF "$PROJECT serve"; then
        echo "  cron @reboot entry already exists ✓"
    else
        ( crontab -l 2>/dev/null; echo "$cron_entry" ) | crontab -
        echo "  cron @reboot entry added ✓"
    fi

    # Start the server right away
    nohup "$conductor_path" serve >> /tmp/$PROJECT.log 2>&1 &
    echo "  server started ✓"
}

setup_autostart_macos() {
    local conductor_path plist_file
    conductor_path=$(command -v "$PROJECT" || echo "$HOME/.local/bin/$PROJECT")
    plist_file="$HOME/Library/LaunchAgents/$PLIST_LABEL.plist"
    mkdir -p ~/Library/LaunchAgents

    cat > "$plist_file" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$conductor_path</string>
        <string>serve</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/$PROJECT.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/$PROJECT.err</string>
</dict>
</plist>
EOF

    launchctl unload "$plist_file" 2>/dev/null || true
    launchctl load "$plist_file" 2>/dev/null || true
    echo "  launchd agent loaded and started ✓"
}

OS="$(uname -s)"
case "$OS" in
    Linux)
        if command -v systemctl &>/dev/null; then
            if prompt_yn "Start $PROJECT automatically on boot?" Y; then
                setup_autostart_linux
            else
                echo "  Skipped. See docs → Auto-Start on Boot"
            fi
        else
            # Fallback: cron @reboot
            if command -v crontab &>/dev/null; then
                if prompt_yn "Start $PROJECT automatically on boot? (via cron @reboot)" Y; then
                    setup_autostart_cron
                else
                    echo "  Skipped. See docs → Auto-Start on Boot"
                fi
            else
                echo "  Autostart: systemd and cron not found — skipping."
                echo "  See docs → Auto-Start on Boot for alternatives."
            fi
        fi
        ;;
    Darwin)
        if prompt_yn "Start $PROJECT automatically on boot?" Y; then
            setup_autostart_macos
        else
            echo "  Skipped. See docs → Auto-Start on Boot"
        fi
        ;;
    *)
        echo "  Autostart setup is not available for $OS."
        echo "  See docs → Auto-Start on Boot"
        ;;
esac

echo ""
echo "Done! Run '$PROJECT run claude research' to start a session."
echo "Dashboard: http://127.0.0.1:7777"
echo ""
echo "If the command is not found, restart your terminal or run:"
echo "  source ~/.bashrc  # or ~/.zshrc"
