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
    # Usage: prompt_yn "Question?" Y 30  → give up after 30s and take the default
    local question="$1" default="$2" timeout="${3:-}" reply rt=()
    [ -n "$timeout" ] && rt=(-t "$timeout")
    if [ "$default" = "Y" ]; then
        printf "%s [Y/n]%s " "$question" "${timeout:+ (${timeout}s)}"
    else
        printf "%s [y/N]%s " "$question" "${timeout:+ (${timeout}s)}"
    fi
    # When piped from curl, stdin is the script itself — use /dev/tty.
    # A timeout matters there: this script also runs unattended from the
    # update dialog, where nobody is present to answer.
    if [ -t 0 ]; then
        read "${rt[@]}" -r reply || reply=""
    elif [ -e /dev/tty ]; then
        read "${rt[@]}" -r reply </dev/tty || reply=""
        [ -n "$timeout" ] && echo ""
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
        "$OLD_PROJECT" shutdown -f 2>/dev/null || true
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

PIPX_FAILED=0
VERSION_BEFORE="$(command -v "$PROJECT" >/dev/null 2>&1 && "$PROJECT" --version 2>/dev/null || true)"

if command -v "$PROJECT" &>/dev/null; then
    echo "Stopping running server..."
    "$PROJECT" shutdown -f 2>/dev/null || true
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
    pipx install -e "$SCRIPT_DIR" --force || PIPX_FAILED=1
    pipx inject --force "$PROJECT" claude-agent-sdk 2>/dev/null || true
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
    pipx install "$tmpdir/$PROJECT" --force || PIPX_FAILED=1
    pipx inject --force "$PROJECT" claude-agent-sdk 2>/dev/null || true

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

# ── Upgrade claude-agent-sdk in the server's Python ─────────────────
# The server subprocess uses whatever Python runs the `be-conductor`
# CLI (sys.executable).  pipx inject only touches the pipx venv,
# which may not be the same Python — so also upgrade via the CLI
# helper which runs `pip install --upgrade` inside sys.executable.
if command -v "$PROJECT" &>/dev/null; then
    echo "Upgrading claude-agent-sdk in the server's Python..."
    "$PROJECT" upgrade-sdk 2>/dev/null || true
fi

# ── Agent CLIs (reported only — never installed from here) ───────────
echo "Agent CLIs on PATH:"
for agent in claude codex opencode lean-ctx; do
    if command -v "$agent" &>/dev/null; then
        echo "  $agent ✓"
    else
        case "$agent" in
            lean-ctx) echo "  $agent — not found (optional: context compression for profiles)" ;;
            *)        echo "  $agent — not found" ;;
        esac
    fi
done
echo ""

# ── Claude Desktop / Claude Code (MCP) ───────────────────────────────
# Two separate MCP lists: Claude Desktop's chat side reads
# claude_desktop_config.json, Claude Code (CLI + the Code tab) reads its
# own. `install-mcp` registers in whichever of the two exists.
case "$(uname -s)" in
    Darwin) CLAUDE_DESKTOP_DIR="$HOME/Library/Application Support/Claude" ;;
    *)      CLAUDE_DESKTOP_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/Claude" ;;
esac
if command -v "$PROJECT" &>/dev/null && { [ -d "$CLAUDE_DESKTOP_DIR" ] || command -v claude &>/dev/null; }; then
    if grep -q '"be-conductor"' "$CLAUDE_DESKTOP_DIR/claude_desktop_config.json" 2>/dev/null \
       || grep -q '"be-conductor": *{' "$HOME/.claude.json" 2>/dev/null; then
        # Already registered — refresh so the command path stays valid.
        "$PROJECT" install-mcp >/dev/null 2>&1 || true
    else
        # A hint, never a question: this script also runs unattended from the
        # update dialog, and a prompt here would hold up the server restart.
        echo "Claude found — to let it conduct be-conductor sessions (MCP), run:"
        echo "  $PROJECT install-mcp"
    fi
    echo ""
fi

# ── Did the upgrade actually land? ───────────────────────────────────

if [ "$PIPX_FAILED" = "1" ]; then
    echo ""
    echo "Error: pipx could not install the new version."
    VERSION_NOW="$(command -v "$PROJECT" >/dev/null 2>&1 && "$PROJECT" --version 2>/dev/null || true)"
    if [ -n "$VERSION_BEFORE" ] && [ "$VERSION_BEFORE" = "$VERSION_NOW" ]; then
        echo "The version did not change — this is still the old install."
    fi
    echo "Close anything still using $PROJECT and run the installer again."
    echo ""
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

    # Start the server right away (or restart to pick up new version)
    "$conductor_path" restart -f 2>/dev/null || "$conductor_path" up
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
            if prompt_yn "Start $PROJECT automatically on boot?" Y 30; then
                setup_autostart_linux
            else
                echo "  Skipped. See docs → Auto-Start on Boot"
            fi
        else
            # Fallback: cron @reboot
            if command -v crontab &>/dev/null; then
                if prompt_yn "Start $PROJECT automatically on boot? (via cron @reboot)" Y 30; then
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
        if prompt_yn "Start $PROJECT automatically on boot?" Y 30; then
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

# ── The upgrade stopped the server, so start it again ────────────────
# Whatever the autostart answer was, and even if the upgrade failed: the
# machine should not be left without a server because of an update.
if command -v "$PROJECT" &>/dev/null; then
    if ! "$PROJECT" status 2>/dev/null | grep -qi "url"; then
        echo "Starting $PROJECT..."
        "$PROJECT" restart -f 2>/dev/null || "$PROJECT" up || true
    fi
fi

# ── Optional: LeanCTX ────────────────────────────────────────────────
# Dead last, after autostart has the server running again: the build can
# take minutes, and the installer stops the server early on, so anything
# slow placed before the restart leaves the server down for its duration.
# Times out into "no", so an unattended update is never blocked by it.
if ! command -v lean-ctx &>/dev/null; then
    echo "LeanCTX is an optional context-compression layer for agent sessions"
    echo "(https://leanctx.com). be-conductor can use it per profile."
    reply=""
    if command -v cargo &>/dev/null; then
        if [ -t 0 ]; then
            printf "  Install it now with cargo? [y/N] (30s, then no) "
            read -t 30 -r reply || true
            echo ""
        elif [ -e /dev/tty ]; then
            printf "  Install it now with cargo? [y/N] (30s, then no) " > /dev/tty
            read -t 30 -r reply < /dev/tty || true
            echo "" > /dev/tty
        fi
        case "$reply" in
            [Yy]*)
                echo "  Building lean-ctx (this takes a few minutes)..."
                cargo install lean-ctx && echo "  lean-ctx installed ✓" \
                    || echo "  lean-ctx install failed — see https://leanctx.com"
                ;;
            *) echo "  Skipped — install later with: cargo install lean-ctx" ;;
        esac
    else
        echo "  To use it: install Rust, then 'cargo install lean-ctx'"
    fi
    echo ""
fi

echo ""
echo "Done! Run '$PROJECT run claude research' to start a session."
echo "Dashboard: http://127.0.0.1:7777"
echo ""
echo "Tip: run '$PROJECT setup-acp' to enable Codex / Gemini / Claude"
echo "     via the Agent Client Protocol (needs Node.js 20+)."
echo ""
echo "If the command is not found, restart your terminal or run:"
echo "  source ~/.bashrc  # or ~/.zshrc"
