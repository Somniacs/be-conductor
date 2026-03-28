'use strict';
const vscode = require('vscode');
const api = require('./api');
const { NAME_PATTERN, AGENTS } = require('./config');

/** Map of session name -> VS Code Terminal instance (for focus-on-click). */
const terminalMap = new Map();

/** Map of session name -> VS Code WebviewPanel instance (for agent sessions). */
const webviewPanels = new Map();

// Clean up terminal references when they close.
vscode.window.onDidCloseTerminal((t) => {
    for (const [name, term] of terminalMap) {
        if (term === t) { terminalMap.delete(name); break; }
    }
});

// ── Session persistence (survives IDE restart) ──────────────────────────
const TRACKED_KEY = 'be-conductor.trackedSessions';
const RUNNING_AT_CLOSE_KEY = 'be-conductor.runningAtClose';
/** @type {vscode.Memento | null} */
let _workspaceState = null;

function setWorkspaceState(state) { _workspaceState = state; }

/** @returns {string[]} tracked session names */
function getTrackedSessions() {
    if (!_workspaceState) return [];
    return _workspaceState.get(TRACKED_KEY, []);
}

/** @returns {string[]} sessions that were running when IDE closed */
function getRunningAtClose() {
    if (!_workspaceState) return [];
    return _workspaceState.get(RUNNING_AT_CLOSE_KEY, []);
}

function setRunningAtClose(names) {
    if (!_workspaceState) return Promise.resolve();
    return _workspaceState.update(RUNNING_AT_CLOSE_KEY, names);
}

function trackSession(name) {
    if (!_workspaceState) return;
    const tracked = getTrackedSessions();
    if (!tracked.includes(name)) {
        tracked.push(name);
        _workspaceState.update(TRACKED_KEY, tracked);
    }
}

function untrackSession(name) {
    if (!_workspaceState) return;
    const tracked = getTrackedSessions().filter(n => n !== name);
    _workspaceState.update(TRACKED_KEY, tracked);
}

function clearTrackedSessions() {
    if (!_workspaceState) return;
    _workspaceState.update(TRACKED_KEY, []);
}

/**
 * Fetch the agent list from the server, falling back to the hardcoded list.
 * @returns {Promise<Array<{label: string, description: string, command: string}>>}
 */
async function fetchAgents() {
    try {
        const cfg = await api.getConfig();
        if (cfg.allowed_commands && cfg.allowed_commands.length > 0) {
            return cfg.allowed_commands.map((c) => ({
                label: c.label || c.command,
                description: c.command,
                command: c.command,
            }));
        }
    } catch {}
    return AGENTS;
}

/**
 * Full multi-step session creation flow.
 * @param {object} [callbacks] - { onSessionCreated: () => void }
 */
async function createSessionFlow(callbacks) {
    // Step 1: Agent picker
    const agents = await fetchAgents();
    const dashboardItem = {
        label: '$(globe)  Open Dashboard',
        description: 'Open be-conductor dashboard in browser',
        _dashboard: true,
    };
    const items = [
        dashboardItem,
        { kind: vscode.QuickPickItemKind.Separator, label: 'Agents' },
        ...agents,
    ];

    const agent = await vscode.window.showQuickPick(items, {
        placeHolder: 'Select an AI agent or open the dashboard',
        title: 'be-conductor',
    });
    if (!agent) return;
    if (agent._dashboard) {
        const { getServerUrl } = require('./config');
        vscode.env.openExternal(vscode.Uri.parse(getServerUrl()));
        return;
    }

    // Step 2: Session name
    const name = await vscode.window.showInputBox({
        prompt: 'Session name',
        placeHolder: 'e.g. feature-auth',
        title: 'be-conductor: Session Name',
        validateInput(value) {
            if (!value || !value.trim()) return 'Session name cannot be empty';
            if (!NAME_PATTERN.test(value.trim()))
                return 'Must start with a letter or digit, max 64 chars (letters, digits, spaces, hyphens, underscores, dots, tildes)';
            return null;
        },
    });
    if (name === undefined) return;
    const trimmed = name.trim();

    // Step 3: Working directory picker
    const folders = vscode.workspace.workspaceFolders || [];
    const dirItems = folders.map((f) => ({
        label: f.name,
        description: f.uri.fsPath,
        _path: f.uri.fsPath,
    }));
    dirItems.push({
        label: '$(folder-opened)  Browse...',
        description: 'Choose a folder',
        _browse: true,
    });

    let selectedCwd;
    if (dirItems.length === 2) {
        // Only one workspace folder + Browse — use workspace folder directly
        selectedCwd = dirItems[0]._path;
    } else {
        const dirPick = await vscode.window.showQuickPick(dirItems, {
            placeHolder: 'Select working directory',
            title: 'be-conductor: Working Directory',
        });
        if (!dirPick) return;
        if (dirPick._browse) {
            const picked = await vscode.window.showOpenDialog({
                canSelectFiles: false,
                canSelectFolders: true,
                canSelectMany: false,
                openLabel: 'Select Working Directory',
            });
            if (!picked || picked.length === 0) return;
            selectedCwd = picked[0].fsPath;
        } else {
            selectedCwd = dirPick._path;
        }
    }

    // Step 4: Worktree toggle (only if git repo)
    let useWorktree = false;
    try {
        const gitInfo = await api.checkGit(selectedCwd);
        if (gitInfo.is_git) {
            const safeName = trimmed.replace(/[^a-zA-Z0-9-]/g, '-');
            const branchPreview = `be-conductor/${safeName}`;
            const wtItems = [
                {
                    label: 'Run normally',
                    description: `On branch ${gitInfo.current_branch || 'unknown'}`,
                    _worktree: false,
                },
                {
                    label: 'Isolate with git worktree',
                    description: `Branch: ${branchPreview}` +
                        (gitInfo.existing_worktrees > 0
                            ? ` (${gitInfo.existing_worktrees} existing)`
                            : ''),
                    _worktree: true,
                },
            ];
            const wtPick = await vscode.window.showQuickPick(wtItems, {
                placeHolder: 'Run in current directory or create an isolated worktree?',
                title: 'be-conductor: Git Worktree',
            });
            if (!wtPick) return;
            useWorktree = wtPick._worktree;
        }
    } catch {
        // Server unreachable or git check failed — skip worktree option
    }

    // Step 5: Run session in terminal (handles server startup, creation, and attach)
    const terminal = vscode.window.createTerminal({
        name: `${trimmed} (${agent.label})`,
        cwd: selectedCwd,
        isTransient: true,
        // Prevent VSCode Python extension from auto-activating a venv.
        // null = unset the variable entirely (empty string is not enough).
        env: { VIRTUAL_ENV: null, CONDA_PREFIX: null, CONDA_DEFAULT_ENV: null },
    });
    terminal.show();

    // Wait briefly for the terminal PTY to be established so the CLI's
    // shutil.get_terminal_size() returns actual dimensions (not 80x24).
    await new Promise(resolve => setTimeout(resolve, 500));

    const cmd = useWorktree
        ? `be-conductor run -w "${agent.command}" "${trimmed}"`
        : `be-conductor run "${agent.command}" "${trimmed}"`;
    terminal.sendText('\x15' + cmd);
    terminalMap.set(trimmed, terminal);
    trackSession(trimmed);

    if (callbacks && callbacks.onSessionCreated) {
        setTimeout(() => callbacks.onSessionCreated(), 1500);
    }
}

/**
 * Attach a terminal to a running session.
 * @param {string} name - session name
 * @param {string} [cwd] - working directory (defaults to first workspace folder)
 */
async function attachSession(name, cwd) {
    if (terminalMap.has(name)) {
        // Already attached — just focus
        terminalMap.get(name).show();
        return;
    }

    // Warn if attached elsewhere
    try {
        const api = require('./api');
        const session = await api.getSession(name);
        if (session && session.attached_clients && session.attached_clients.length > 0) {
            const sources = [...new Set(session.attached_clients.map(c => c.source))];
            const choice = await vscode.window.showWarningMessage(
                `"${name}" is already attached in: ${sources.join(', ')}. Open here as well?`,
                'Yes', 'Cancel'
            );
            if (choice !== 'Yes') return;
        }
    } catch (_) {
        // If the check fails, proceed anyway
    }

    const workDir = cwd ||
        (vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders[0]
            ? vscode.workspace.workspaceFolders[0].uri.fsPath
            : undefined);

    const terminal = vscode.window.createTerminal({
        name,
        cwd: workDir,
        isTransient: true,
        env: { VIRTUAL_ENV: '', CONDA_PREFIX: '' },
    });
    terminal.show();
    terminal.sendText('\x15' + `be-conductor attach "${name}" ; exit`);
    terminalMap.set(name, terminal);
    trackSession(name);
}

/**
 * Focus the terminal for a session, if one exists.
 * @param {string} name
 * @returns {boolean} true if terminal was found and focused
 */
function focusTerminal(name) {
    const t = terminalMap.get(name);
    if (t) {
        t.show();
        return true;
    }
    return false;
}

/**
 * Open an agent session in a native webview panel with direct WebSocket connection.
 * @param {string} sessionId - session ID (compound or plain)
 * @param {string} name - session display name
 */
function openAgentWebview(sessionId, name) {
    // If already open, just reveal the panel
    if (webviewPanels.has(name)) {
        webviewPanels.get(name).reveal();
        return;
    }

    const { getServerUrl } = require('./config');
    const baseUrl = getServerUrl();
    // Derive WebSocket URL from server URL
    const wsBase = baseUrl.replace(/^http/, 'ws');
    const wsUrl = `${wsBase}/sessions/${encodeURIComponent(sessionId)}/stream?source=vscode`;

    const panel = vscode.window.createWebviewPanel(
        'be-conductor.agentSession',
        `${name} (SDK)`,
        vscode.ViewColumn.One,
        {
            enableScripts: true,
            retainContextWhenHidden: true,
        }
    );

    panel.webview.html = _buildAgentWebviewHtml(wsUrl, sessionId, name);

    // Handle messages from the webview (e.g. file picker)
    panel.webview.onDidReceiveMessage(async (msg) => {
        if (msg.type === 'pickFile') {
            const uris = await vscode.window.showOpenDialog({
                canSelectFiles: true,
                canSelectFolders: false,
                canSelectMany: true,
                openLabel: 'Attach',
                filters: {
                    'Images': ['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp'],
                    'Text': ['txt', 'csv', 'json', 'md', 'py', 'js', 'ts', 'html', 'css', 'xml', 'yaml', 'yml', 'toml', 'sh', 'log'],
                },
            });
            if (!uris || uris.length === 0) return;
            const fs = require('fs');
            const path = require('path');
            for (const uri of uris) {
                try {
                    const data = fs.readFileSync(uri.fsPath);
                    const base64 = data.toString('base64');
                    const ext = path.extname(uri.fsPath).toLowerCase();
                    const mimeMap = {
                        '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                        '.gif': 'image/gif', '.webp': 'image/webp', '.svg': 'image/svg+xml',
                        '.bmp': 'image/bmp', '.txt': 'text/plain', '.csv': 'text/csv',
                        '.json': 'application/json', '.md': 'text/markdown',
                        '.py': 'text/x-python', '.js': 'text/javascript',
                        '.ts': 'text/typescript', '.html': 'text/html', '.css': 'text/css',
                        '.xml': 'text/xml', '.yaml': 'text/yaml', '.yml': 'text/yaml',
                        '.toml': 'text/toml', '.sh': 'text/x-shellscript', '.log': 'text/plain',
                    };
                    const mimeType = mimeMap[ext] || 'application/octet-stream';
                    panel.webview.postMessage({
                        type: 'fileData',
                        name: path.basename(uri.fsPath),
                        mimeType,
                        data: base64,
                    });
                } catch (err) {
                    vscode.window.showErrorMessage(`Failed to read file: ${err.message}`);
                }
            }
        }
    });

    webviewPanels.set(name, panel);

    panel.onDidDispose(() => {
        webviewPanels.delete(name);
    });
}

/**
 * Build self-contained HTML for the agent webview panel.
 * @param {string} wsUrl - WebSocket URL for the agent stream
 * @param {string} sessionId - session ID
 * @param {string} name - session display name
 * @returns {string} HTML content
 */
function _buildAgentWebviewHtml(wsUrl, sessionId, name) {
    return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body {
    height: 100%; overflow: hidden;
    background: var(--vscode-editor-background, #1e1e1e);
    color: var(--vscode-editor-foreground, #cccccc);
    font-family: var(--vscode-font-family, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif);
    font-size: 13px;
}
#app { display: flex; flex-direction: column; height: 100%; }
#messages {
    flex: 1; overflow-y: auto; padding: 12px 16px;
    scroll-behavior: smooth;
}
#messages::-webkit-scrollbar { width: 8px; }
#messages::-webkit-scrollbar-thumb { background: var(--vscode-scrollbarSlider-background, #555); border-radius: 4px; }
#messages::-webkit-scrollbar-thumb:hover { background: var(--vscode-scrollbarSlider-hoverBackground, #777); }

.msg-block { margin: 14px 0; }
.msg-label { font-size: 11px; color: var(--vscode-descriptionForeground, #8b949e); margin-bottom: 4px; font-weight: 600; }
.user-msg {
    padding: 10px 14px; border-radius: 8px; border-left: 3px solid var(--vscode-button-background, #4a6cf7);
    background: color-mix(in srgb, var(--vscode-button-background, #4a6cf7) 12%, var(--vscode-editor-background, #1e1e1e));
    white-space: pre-wrap; line-height: 1.5;
}
.assistant-text { line-height: 1.6; }
.assistant-text strong { font-weight: 600; }
.assistant-text em { font-style: italic; }
.assistant-text code {
    background: var(--vscode-textCodeBlock-background, #1a1f2b);
    padding: 2px 5px; border-radius: 3px; font-size: 0.9em;
}
.assistant-text .md-codeblock {
    margin: 8px 0; border-radius: 6px; overflow: hidden;
    border: 1px solid var(--vscode-input-border, #30363d);
}
.assistant-text .md-codeblock-lang {
    font-size: 11px; color: var(--vscode-descriptionForeground, #666);
    padding: 4px 10px;
    background: color-mix(in srgb, var(--vscode-editor-background, #1e1e1e) 80%, var(--vscode-editor-foreground, #ccc));
    border-bottom: 1px solid var(--vscode-input-border, #30363d);
}
.assistant-text .md-codeblock pre {
    margin: 0; padding: 10px 12px;
    background: var(--vscode-textCodeBlock-background, #0d1117);
    overflow-x: auto; font-size: 13px; line-height: 1.45;
}
.assistant-text .md-list-item { padding-left: 16px; }
.assistant-text .md-header { font-weight: 600; margin: 12px 0 4px; }
.assistant-text .md-header-1 { font-size: 18px; margin: 16px 0 8px; }
.assistant-text .md-header-2 { font-size: 16px; margin: 14px 0 6px; }
.assistant-text .md-header-3 { font-size: 15px; }
.assistant-text .md-paragraph-break { height: 8px; }

details { margin: 8px 0; border: 1px solid var(--vscode-input-border, #30363d); border-radius: 6px; overflow: hidden; }
details summary {
    padding: 6px 10px; cursor: pointer; user-select: none; font-size: 12px;
    background: color-mix(in srgb, var(--vscode-editor-background, #1e1e1e) 85%, var(--vscode-editor-foreground, #ccc));
    color: var(--vscode-descriptionForeground, #8b949e);
}
details summary:hover { opacity: 0.85; }
details.thinking-block .thinking-content {
    padding: 8px 10px; font-size: 12px; white-space: pre-wrap;
    color: var(--vscode-descriptionForeground, #8b949e);
    max-height: 300px; overflow-y: auto;
}
details.tool-block { border-color: var(--vscode-input-border, #30363d); }
details.tool-block summary {
    padding: 8px 12px; font-size: 13px; display: flex; align-items: center; gap: 6px;
    color: var(--vscode-editor-foreground, #e6edf3);
}
details.tool-block summary .tool-name { color: #f0a020; font-weight: 600; }
details.tool-block summary .tool-summary {
    color: var(--vscode-descriptionForeground, #8b949e); font-weight: 400; font-size: 12px;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
details.tool-block .tool-input {
    padding: 8px 12px;
    background: var(--vscode-textCodeBlock-background, #0d1117);
}
details.tool-block .tool-input pre {
    margin: 0; font-size: 12px; color: var(--vscode-descriptionForeground, #8b949e);
    overflow-x: auto; max-height: 200px; white-space: pre-wrap; line-height: 1.4;
}
.tool-result {
    margin: 4px 0 8px; padding: 8px 12px; border-radius: 0 0 6px 6px;
    background: var(--vscode-textCodeBlock-background, #0d1117);
    border-top: 1px solid var(--vscode-input-border, #30363d);
    font-size: 12px; color: var(--vscode-descriptionForeground, #8b949e);
    max-height: 300px; overflow-y: auto; white-space: pre-wrap; line-height: 1.4;
}
.tool-result.error {
    border-top-color: rgba(248, 81, 73, 0.2);
    color: var(--vscode-errorForeground, #f85149);
}
.result-panel {
    margin: 16px 0; padding: 10px 14px; border-radius: 8px; font-size: 13px;
    background: color-mix(in srgb, #3fb950 10%, var(--vscode-editor-background, #1e1e1e));
    border: 1px solid color-mix(in srgb, #3fb950 25%, transparent);
    color: #3fb950;
}
.result-panel .meta { font-size: 11px; color: var(--vscode-descriptionForeground, #8b949e); margin-top: 4px; }
.error-panel {
    margin: 12px 0; padding: 10px 14px; border-radius: 8px; font-size: 13px;
    background: color-mix(in srgb, var(--vscode-errorForeground, #f85149) 10%, var(--vscode-editor-background, #1e1e1e));
    border: 1px solid color-mix(in srgb, var(--vscode-errorForeground, #f85149) 25%, transparent);
    color: var(--vscode-errorForeground, #f85149);
}
.session-end {
    margin: 16px 0; padding: 10px; text-align: center; font-size: 12px;
    color: var(--vscode-descriptionForeground, #8b949e);
    border-top: 1px solid var(--vscode-input-border, #21262d);
}
.status-msg {
    padding: 20px; text-align: center;
    color: var(--vscode-descriptionForeground, #8b949e);
}

/* Input area */
#input-area {
    flex-shrink: 0;
    background: color-mix(in srgb, var(--vscode-editor-background, #1e1e1e) 85%, var(--vscode-editor-foreground, #ccc));
    border-top: 1px solid var(--vscode-input-border, #30363d);
    padding: 8px 12px;
}
#attachment-preview {
    display: none; flex-wrap: wrap; gap: 6px; margin-bottom: 6px;
}
#attachment-preview .att-item {
    display: flex; align-items: center; gap: 4px;
    background: var(--vscode-input-background, #0d1117);
    border: 1px solid var(--vscode-input-border, #30363d);
    border-radius: 6px; padding: 3px 8px; font-size: 11px;
}
#attachment-preview .att-item img { height: 20px; width: 20px; object-fit: cover; border-radius: 3px; }
#attachment-preview .att-remove {
    background: none; border: none; color: var(--vscode-descriptionForeground, #8b949e);
    cursor: pointer; font-size: 14px; padding: 0 2px;
}
#attachment-preview .att-remove:hover { color: var(--vscode-errorForeground, #f85149); }
#input-box {
    display: flex; align-items: flex-end; gap: 0;
    background: var(--vscode-input-background, #0d1117);
    border: 1px solid var(--vscode-input-border, #30363d);
    border-radius: 10px; padding: 4px;
}
#input-box:focus-within { border-color: var(--vscode-focusBorder, #007fd4); }
#attach-btn, #mode-btn, #send-btn {
    border: none; cursor: pointer; display: flex; align-items: center; justify-content: center; flex-shrink: 0;
}
#attach-btn {
    background: none; color: var(--vscode-descriptionForeground, #8b949e);
    padding: 6px; border-radius: 6px;
}
#attach-btn:hover { color: var(--vscode-editor-foreground, #e0e0e0); background: var(--vscode-list-hoverBackground, #1a1f2b); }
#prompt-input {
    flex: 1; resize: none; background: transparent;
    color: var(--vscode-input-foreground, var(--vscode-editor-foreground, #e0e0e0));
    border: none; outline: none; padding: 6px 8px;
    font-family: inherit; font-size: 13px; height: 38px; max-height: 200px; line-height: 1.4;
}
#right-group { display: flex; align-items: center; gap: 4px; flex-shrink: 0; position: relative; }
#mode-btn {
    background: none; color: var(--vscode-descriptionForeground, #8b949e);
    border: 1px solid var(--vscode-input-border, #30363d);
    border-radius: 6px; padding: 3px 8px; font-size: 11px;
    gap: 4px; white-space: nowrap;
}
#mode-btn:hover { color: var(--vscode-editor-foreground); border-color: var(--vscode-descriptionForeground, #666); }
#send-btn {
    background: var(--vscode-button-background, #2563eb);
    color: var(--vscode-button-foreground, white);
    border-radius: 50%; width: 28px; height: 28px;
}
#send-btn:hover { opacity: 0.85; }
#send-btn.busy { background: #b91c1c; }
#send-btn.busy:hover { background: #dc2626; }

/* Mode popup */
#mode-popup {
    display: none; position: absolute; bottom: 100%; right: 0; margin-bottom: 8px;
    background: color-mix(in srgb, var(--vscode-editor-background, #1e1e1e) 90%, var(--vscode-editor-foreground, #ccc));
    border: 1px solid var(--vscode-input-border, #30363d);
    border-radius: 10px; padding: 8px 0; min-width: 300px; z-index: 100;
    box-shadow: 0 8px 24px rgba(0,0,0,0.4);
}
.popup-header {
    padding: 8px 14px; font-size: 12px; font-weight: 600;
    color: var(--vscode-descriptionForeground, #8b949e);
}
.popup-mode-item {
    padding: 8px 14px; cursor: pointer; display: flex; align-items: flex-start; gap: 10px;
}
.popup-mode-item:hover { background: var(--vscode-list-hoverBackground, #2a2f3a); }
.popup-mode-item.active { background: color-mix(in srgb, var(--vscode-button-background, #2563eb) 15%, transparent); }
.popup-mode-item .mode-icon { color: var(--vscode-descriptionForeground, #8b949e); flex-shrink: 0; margin-top: 2px; }
.popup-mode-item .mode-label { font-size: 13px; font-weight: 500; }
.popup-mode-item .mode-desc { font-size: 11px; color: var(--vscode-descriptionForeground, #8b949e); margin-top: 2px; }
.popup-mode-item .mode-check { color: var(--vscode-button-background, #4a6cf7); flex-shrink: 0; margin-top: 2px; }
.popup-sep { height: 1px; background: var(--vscode-input-border, #30363d); margin: 6px 0; }
.effort-row {
    padding: 8px 14px; display: flex; align-items: center; gap: 10px;
}
.effort-row .effort-icon { color: var(--vscode-descriptionForeground, #8b949e); flex-shrink: 0; }
.effort-row .effort-label { flex: 1; font-size: 13px; }
.effort-toggle {
    display: flex; gap: 2px; background: var(--vscode-input-background, #0d1117);
    border-radius: 8px; padding: 2px; border: 1px solid var(--vscode-input-border, #30363d);
}
.effort-toggle button {
    background: transparent; color: var(--vscode-descriptionForeground, #8b949e);
    border: none; border-radius: 6px; padding: 2px 8px; font-size: 11px; cursor: pointer; font-weight: 500;
}
.effort-toggle button.active {
    background: var(--vscode-button-background, #2563eb);
    color: var(--vscode-button-foreground, white);
}
</style>
</head>
<body>
<div id="app">
    <div id="messages"><div class="status-msg" id="status">Connecting to agent...</div></div>
    <div id="input-area">
        <div id="attachment-preview"></div>
        <div id="input-box">
            <button id="attach-btn" title="Attach file or image">
                <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor"><path d="M8 2a.75.75 0 01.75.75v4.5h4.5a.75.75 0 010 1.5h-4.5v4.5a.75.75 0 01-1.5 0v-4.5h-4.5a.75.75 0 010-1.5h4.5v-4.5A.75.75 0 018 2z"/></svg>
            </button>
            <textarea id="prompt-input" rows="1" placeholder="Message Claude..."></textarea>
            <div id="right-group">
                <div id="mode-popup">
                    <div class="popup-header">Modes</div>
                    <!-- populated by JS -->
                </div>
                <button id="mode-btn" title="Modes &amp; Effort"></button>
                <button id="send-btn" title="Send (Enter)"></button>
            </div>
        </div>
    </div>
</div>
<script>
(function() {
    const vscode = acquireVsCodeApi();
    const WS_URL = ${JSON.stringify(wsUrl)};
    const SESSION_ID = ${JSON.stringify(sessionId)};

    const messagesEl = document.getElementById('messages');
    const statusEl = document.getElementById('status');
    const textarea = document.getElementById('prompt-input');
    const sendBtn = document.getElementById('send-btn');
    const attachBtn = document.getElementById('attach-btn');
    const modeBtnEl = document.getElementById('mode-btn');
    const modePopup = document.getElementById('mode-popup');
    const attachPreview = document.getElementById('attachment-preview');

    let ws = null;
    let busy = false;
    let reconnectAttempts = 0;
    const MAX_RECONNECT = 5;
    const RECONNECT_DELAY = 3000;
    let attachments = [];

    // --- SVG Icons ---
    const SEND_ICON = '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 12V4M4 7l4-4 4 4"/></svg>';
    const STOP_ICON = '<svg width="10" height="10" viewBox="0 0 10 10"><rect width="10" height="10" rx="1.5" fill="currentColor"/></svg>';

    // --- Modes & Effort ---
    const MODES = [
        {value:'default', label:'Ask before edits', desc:'Claude will ask for approval before making each edit',
         icon:'<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M8 2a5 5 0 00-5 5c0 1.5.7 2.8 1.7 3.7L5 14h6l.3-3.3A5 5 0 008 2z"/><path d="M6 14v1h4v-1"/></svg>'},
        {value:'acceptEdits', label:'Edit automatically', desc:'Claude will edit your selected text or the whole file',
         icon:'<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M4 13h8M5.5 3.5l5 5M4 10l-1 3 3-1 7-7-2-2z"/></svg>'},
        {value:'plan', label:'Plan mode', desc:'Claude will explore the code and present a plan before editing',
         icon:'<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="1.5" width="10" height="13" rx="1.5"/><path d="M6 5h4M6 8h4M6 11h2"/></svg>'},
    ];
    const EFFORTS = ['low','medium','high'];
    let modeIdx = 0;
    let effortIdx = 2;

    // --- Helpers ---
    function escHtml(str) {
        if (typeof str !== 'string') return '';
        return str.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }

    const BT = String.fromCharCode(96); // backtick
    const fencedRe = new RegExp(BT+BT+BT+'(\\\\w*)\\\\n([\\\\s\\\\S]*?)'+BT+BT+BT, 'g');
    const inlineCodeRe = new RegExp(BT+'([^'+BT+']+)'+BT, 'g');
    function mdToHtml(text) {
        if (!text) return '';
        let h = text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
        h = h.replace(fencedRe, function(_, lang, code) {
            const label = lang ? '<div class="md-codeblock-lang">' + lang + '</div>' : '';
            return '<div class="md-codeblock">' + label + '<pre><code>' + code + '</code></pre></div>';
        });
        h = h.replace(inlineCodeRe, '<code>$1</code>');
        h = h.replace(/\\*\\*(.+?)\\*\\*/g, '<strong>$1</strong>');
        h = h.replace(/\\*(.+?)\\*/g, '<em>$1</em>');
        h = h.replace(/^### (.+)$/gm, '<div class="md-header md-header-3">$1</div>');
        h = h.replace(/^## (.+)$/gm, '<div class="md-header md-header-2">$1</div>');
        h = h.replace(/^# (.+)$/gm, '<div class="md-header md-header-1">$1</div>');
        h = h.replace(/^- (.+)$/gm, '<div class="md-list-item">&#8226; $1</div>');
        h = h.replace(/^(\\d+)\\. (.+)$/gm, '<div class="md-list-item">$1. $2</div>');
        h = h.replace(/\\n\\n/g, '<div class="md-paragraph-break"></div>');
        h = h.replace(/\\n/g, '<br>');
        return h;
    }

    function toolIcon(name) {
        const s = 'width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"';
        const icons = {
            'Bash':      '<svg '+s+'><rect x="2" y="2" width="12" height="12" rx="2"/><path d="M5 6l2 2-2 2M9 10h2"/></svg>',
            'Read':      '<svg '+s+'><path d="M3 2h7l3 3v9H3z"/><path d="M10 2v3h3"/></svg>',
            'Write':     '<svg '+s+'><path d="M3 2h7l3 3v9H3z"/><path d="M10 2v3h3"/><path d="M6 9h4"/></svg>',
            'Edit':      '<svg '+s+'><path d="M11 2l3 3-9 9H2v-3z"/></svg>',
            'Glob':      '<svg '+s+'><circle cx="7" cy="7" r="4"/><path d="M10 10l4 4"/></svg>',
            'Grep':      '<svg '+s+'><circle cx="7" cy="7" r="4"/><path d="M10 10l4 4"/></svg>',
            'WebFetch':  '<svg '+s+'><circle cx="8" cy="8" r="6"/><path d="M2 8h12M8 2c-2 2-2 10 0 12M8 2c2 2 2 10 0 12"/></svg>',
            'WebSearch': '<svg '+s+'><circle cx="8" cy="8" r="6"/><path d="M2 8h12M8 2c-2 2-2 10 0 12M8 2c2 2 2 10 0 12"/></svg>',
            'Agent':     '<svg '+s+'><circle cx="8" cy="5" r="3"/><path d="M3 14c0-3 2-5 5-5s5 2 5 5"/></svg>',
            'TodoWrite': '<svg '+s+'><rect x="3" y="2" width="10" height="12" rx="1"/><path d="M6 6l1.5 1.5L10 5M6 10h4"/></svg>',
        };
        return icons[name] || '<svg '+s+'><circle cx="8" cy="8" r="5"/><path d="M8 5v3l2 1"/></svg>';
    }

    function toolInputSummary(name, input) {
        if (!input) return '';
        if (name === 'Bash' && input.command) return input.command;
        if (name === 'Read' && input.file_path) return input.file_path;
        if (name === 'Write' && input.file_path) return input.file_path;
        if (name === 'Edit' && input.file_path) return input.file_path;
        if (name === 'Glob' && input.pattern) return input.pattern;
        if (name === 'Grep' && input.pattern) return input.pattern;
        if (name === 'WebFetch' && input.url) return input.url;
        if (name === 'WebSearch' && input.query) return input.query;
        try { return JSON.stringify(input).slice(0, 120); } catch { return ''; }
    }

    // --- Rendering ---
    function renderEvent(event) {
        // Remove status placeholder
        if (statusEl && statusEl.parentNode) statusEl.remove();

        const etype = event.type;

        if (etype === 'user_message') {
            const div = document.createElement('div');
            div.className = 'msg-block';
            div.innerHTML = '<div class="msg-label">You</div>' +
                '<div class="user-msg">' + escHtml(event.content || '') + '</div>';
            messagesEl.appendChild(div);

        } else if (etype === 'assistant_message') {
            const wrapper = document.createElement('div');
            wrapper.className = 'msg-block';
            const label = document.createElement('div');
            label.className = 'msg-label';
            label.textContent = 'Claude';
            wrapper.appendChild(label);

            for (const block of (event.content || [])) {
                if (block.type === 'text') {
                    const div = document.createElement('div');
                    div.className = 'assistant-text';
                    div.innerHTML = mdToHtml(block.text);
                    wrapper.appendChild(div);

                } else if (block.type === 'thinking') {
                    const details = document.createElement('details');
                    details.className = 'thinking-block';
                    details.innerHTML = '<summary>Thinking...</summary>' +
                        '<div class="thinking-content">' + escHtml(block.thinking || '') + '</div>';
                    wrapper.appendChild(details);

                } else if (block.type === 'tool_use') {
                    const details = document.createElement('details');
                    details.className = 'tool-block';
                    details.open = true;
                    const toolName = block.tool || 'tool';
                    const inputSum = toolInputSummary(toolName, block.input);
                    let inputText;
                    try { inputText = JSON.stringify(block.input, null, 2); } catch { inputText = String(block.input); }
                    details.innerHTML =
                        '<summary><span>' + toolIcon(toolName) + '</span>' +
                        '<span class="tool-name">' + escHtml(toolName) + '</span>' +
                        '<span class="tool-summary">' + escHtml(inputSum) + '</span></summary>' +
                        '<div class="tool-input"><pre>' + escHtml(inputText) + '</pre></div>';
                    wrapper.appendChild(details);

                } else if (block.type === 'tool_result') {
                    const div = document.createElement('div');
                    div.className = 'tool-result' + (block.is_error ? ' error' : '');
                    const content = String(block.content || '');
                    div.textContent = content.length > 3000 ? content.slice(0, 3000) + '\\n...(truncated)' : content;
                    wrapper.appendChild(div);
                }
            }
            messagesEl.appendChild(wrapper);

        } else if (etype === 'result') {
            setBusy(false);
            const div = document.createElement('div');
            div.className = 'result-panel';
            let html = '<strong>Done</strong>';
            if (event.result) html += ' &mdash; ' + escHtml(event.result).slice(0, 200);
            const meta = [];
            if (event.num_turns) meta.push(event.num_turns + ' turns');
            if (event.duration_ms) meta.push((event.duration_ms / 1000).toFixed(1) + 's');
            if (event.total_cost_usd) meta.push('$' + Number(event.total_cost_usd).toFixed(4));
            if (meta.length) html += '<div class="meta">' + meta.join(' &middot; ') + '</div>';
            div.innerHTML = html;
            messagesEl.appendChild(div);

        } else if (etype === 'error') {
            setBusy(false);
            const div = document.createElement('div');
            div.className = 'error-panel';
            div.innerHTML = '<strong>Error</strong> &mdash; ' + escHtml(event.error || 'Unknown error');
            messagesEl.appendChild(div);

        } else if (etype === 'session_end') {
            setBusy(false);
            const div = document.createElement('div');
            div.className = 'session-end';
            div.textContent = 'Session ended (exit ' + (event.exit_code != null ? event.exit_code : 0) + ')';
            messagesEl.appendChild(div);
        }

        // Auto-scroll to bottom (unless user scrolled up)
        if (messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 100) {
            messagesEl.scrollTop = messagesEl.scrollHeight;
        }
    }

    // --- Busy state ---
    function setBusy(b) {
        busy = b;
        sendBtn.innerHTML = b ? STOP_ICON : SEND_ICON;
        sendBtn.className = b ? 'busy' : '';
        sendBtn.title = b ? 'Stop agent' : 'Send (Enter)';
    }

    // --- Send prompt ---
    function sendPrompt() {
        const text = textarea.value.trim();
        if (!text && !attachments.length) return;
        renderEvent({type: 'user_message', content: text || '(attachment)'});
        if (ws && ws.readyState === WebSocket.OPEN) {
            const msg = {type: 'prompt', text: text};
            if (attachments.length) {
                msg.attachments = attachments.map(a => ({name: a.name, type: a.mimeType, data: a.data}));
            }
            ws.send(JSON.stringify(msg));
            setBusy(true);
        } else {
            renderEvent({type: 'error', error: 'Not connected to agent'});
        }
        textarea.value = '';
        textarea.style.height = '38px';
        attachments = [];
        refreshAttachments();
        textarea.focus();
    }

    // --- Attachments ---
    function refreshAttachments() {
        attachPreview.innerHTML = '';
        if (!attachments.length) { attachPreview.style.display = 'none'; return; }
        attachPreview.style.display = 'flex';
        attachments.forEach((att, idx) => {
            const item = document.createElement('div');
            item.className = 'att-item';
            if (att.mimeType && att.mimeType.startsWith('image/')) {
                const img = document.createElement('img');
                img.src = 'data:' + att.mimeType + ';base64,' + att.data;
                img.alt = att.name;
                item.appendChild(img);
            }
            const nameSpan = document.createElement('span');
            nameSpan.textContent = att.name;
            item.appendChild(nameSpan);
            const removeBtn = document.createElement('button');
            removeBtn.className = 'att-remove';
            removeBtn.innerHTML = '&times;';
            removeBtn.addEventListener('click', () => { attachments.splice(idx, 1); refreshAttachments(); });
            item.appendChild(removeBtn);
            attachPreview.appendChild(item);
        });
    }

    // --- File picker via extension host ---
    attachBtn.addEventListener('click', () => {
        vscode.postMessage({type: 'pickFile'});
    });

    window.addEventListener('message', (e) => {
        const msg = e.data;
        if (msg.type === 'fileData') {
            attachments.push({name: msg.name, mimeType: msg.mimeType, data: msg.data});
            refreshAttachments();
        }
    });

    // --- WebSocket ---
    function connect() {
        if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
        ws = new WebSocket(WS_URL);

        ws.onopen = () => {
            reconnectAttempts = 0;
            if (statusEl && statusEl.parentNode) {
                statusEl.textContent = 'Connected. Waiting for response...';
            }
            textarea.focus();
        };

        ws.onmessage = (event) => {
            if (typeof event.data !== 'string') return;
            try {
                const msg = JSON.parse(event.data);
                if (msg.type === 'history') {
                    for (const m of (msg.messages || [])) renderEvent(m);
                } else if (msg.type === 'ping') {
                    return;
                } else {
                    renderEvent(msg);
                }
            } catch {}
        };

        ws.onclose = () => {
            ws = null;
            if (reconnectAttempts < MAX_RECONNECT) {
                reconnectAttempts++;
                setTimeout(connect, RECONNECT_DELAY);
            } else {
                const div = document.createElement('div');
                div.className = 'error-panel';
                div.innerHTML = '<strong>Disconnected</strong> &mdash; could not reconnect after ' + MAX_RECONNECT + ' attempts';
                messagesEl.appendChild(div);
            }
        };

        ws.onerror = () => {};
    }

    // --- Input handlers ---
    textarea.addEventListener('input', () => {
        textarea.style.height = '38px';
        textarea.style.height = Math.min(200, textarea.scrollHeight) + 'px';
    });
    textarea.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            if (busy) return;
            sendPrompt();
        }
    });
    sendBtn.innerHTML = SEND_ICON;
    sendBtn.addEventListener('click', () => {
        if (busy) {
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({type: 'interrupt'}));
            }
        } else {
            sendPrompt();
        }
    });

    // --- Mode popup ---
    function buildModePopup() {
        modePopup.innerHTML = '<div class="popup-header">Modes</div>';
        MODES.forEach((m, idx) => {
            const item = document.createElement('div');
            item.className = 'popup-mode-item' + (idx === modeIdx ? ' active' : '');
            item.innerHTML =
                '<div class="mode-icon">' + m.icon + '</div>' +
                '<div style="flex:1;"><div class="mode-label">' + m.label + '</div>' +
                '<div class="mode-desc">' + m.desc + '</div></div>' +
                '<div class="mode-check" style="display:' + (idx === modeIdx ? 'block' : 'none') + ';">' +
                '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M3 8l3.5 3.5L13 5"/></svg></div>';
            item.addEventListener('click', () => {
                modeIdx = idx;
                updateModeUI();
                if (ws && ws.readyState === WebSocket.OPEN) {
                    ws.send(JSON.stringify({type: 'set_mode', mode: m.value}));
                }
                modePopup.style.display = 'none';
            });
            modePopup.appendChild(item);
        });

        const sep = document.createElement('div');
        sep.className = 'popup-sep';
        modePopup.appendChild(sep);

        const effortRow = document.createElement('div');
        effortRow.className = 'effort-row';
        effortRow.innerHTML =
            '<div class="effort-icon"><svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2 10h2l2-4 3 8 2-6h3"/></svg></div>' +
            '<div class="effort-label">Effort</div>';
        const toggle = document.createElement('div');
        toggle.className = 'effort-toggle';
        EFFORTS.forEach((e, idx) => {
            const btn = document.createElement('button');
            btn.textContent = e.charAt(0).toUpperCase();
            btn.title = e.charAt(0).toUpperCase() + e.slice(1) + ' effort';
            if (idx === effortIdx) btn.className = 'active';
            btn.addEventListener('click', () => {
                effortIdx = idx;
                updateEffortUI();
                if (ws && ws.readyState === WebSocket.OPEN) {
                    ws.send(JSON.stringify({type: 'set_effort', effort: EFFORTS[idx]}));
                }
            });
            toggle.appendChild(btn);
        });
        effortRow.appendChild(toggle);
        modePopup.appendChild(effortRow);
    }

    function updateModeUI() {
        const m = MODES[modeIdx];
        modeBtnEl.innerHTML = m.icon.replace('width="16"','width="14"').replace('height="16"','height="14"') +
            ' <span>' + m.label.split(' ').pop() + '</span>';
        buildModePopup();
    }

    function updateEffortUI() {
        const btns = modePopup.querySelectorAll('.effort-toggle button');
        btns.forEach((btn, i) => { btn.className = i === effortIdx ? 'active' : ''; });
    }

    modeBtnEl.addEventListener('click', (e) => {
        e.stopPropagation();
        modePopup.style.display = modePopup.style.display === 'none' ? 'block' : 'none';
    });
    document.addEventListener('click', () => { modePopup.style.display = 'none'; });
    modePopup.addEventListener('click', (e) => e.stopPropagation());

    // --- Init ---
    updateModeUI();
    connect();
})();
</script>
</body>
</html>`;
}

module.exports = {
    createSessionFlow, attachSession, focusTerminal, openAgentWebview,
    terminalMap, webviewPanels,
    setWorkspaceState, getTrackedSessions, trackSession, untrackSession, clearTrackedSessions,
    getRunningAtClose, setRunningAtClose,
};
