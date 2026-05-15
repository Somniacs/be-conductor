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
async function fetchAgents(serverKey) {
    try {
        const cfg = await api.getConfig(serverKey);
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
    const registry = require('./serverRegistry');

    // Step 0: Server picker (only when multi-server)
    let selectedServerKey = 'local';
    if (registry.isMultiServer()) {
        const serverItems = registry.getEnabledServers().map(s => ({
            label: s.label,
            description: s.url || 'localhost:7777',
            _key: s.key,
        }));
        const serverPick = await vscode.window.showQuickPick(serverItems, {
            placeHolder: 'Select server',
            title: 'be-conductor: Server',
        });
        if (!serverPick) return;
        selectedServerKey = serverPick._key;
    }

    // Step 1: Agent picker
    const agents = await fetchAgents(selectedServerKey);
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
        vscode.env.openExternal(vscode.Uri.parse(registry.getBaseUrl('local')));
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
        const gitInfo = await api.checkGit(selectedServerKey, selectedCwd);
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

    // Step 5: Session type (GUI is default)
    const typeItems = [
        {
            label: '$(symbol-event) GUI',
            description: 'Interactive panel with structured messages',
            _type: 'agent',
        },
        {
            label: '$(terminal) Terminal',
            description: 'Classic terminal session',
            _type: 'pty',
        },
    ];
    const typePick = await vscode.window.showQuickPick(typeItems, {
        placeHolder: 'Session type',
        title: 'be-conductor: Session Type',
    });
    if (!typePick) return;
    const sessionType = typePick._type;

    // Step 5b: Agent backend picker (GUI mode only)
    // Claude (native) is always available; OpenCode entries are
    // populated from the server's catalogue (one entry per
    // authenticated provider/model combination).
    let agentOptions = null;
    if (sessionType === 'agent') {
        const backendItems = [
            {
                label: 'Claude (native)',
                description: 'Anthropic Claude via the native Agent SDK',
                _provider: 'claude',
            },
        ];
        // ACP agents — launched via the Agent Client Protocol. The
        // catalogue is static; the provider name is passed straight
        // through as agent_options.provider. Adapters are fetched via
        // npx on first use, so nothing needs starting beforehand.
        try {
            const acp = await api.getAcpAgents(selectedServerKey);
            if (acp && acp.agents && acp.agents.length > 0) {
                backendItems.push({
                    kind: vscode.QuickPickItemKind.Separator,
                    label: 'Agent Client Protocol',
                });
                for (const a of acp.agents) {
                    backendItems.push({
                        label: a.label,
                        description: 'via ACP — ' + a.id,
                        _provider: a.id,
                    });
                }
            }
        } catch {
            // Older server without the ACP endpoint — fine, skip.
        }
        try {
            const cat = await api.getAgentProviderModels(selectedServerKey, 'opencode');
            if (cat && cat.models && cat.models.length > 0) {
                backendItems.push({
                    kind: vscode.QuickPickItemKind.Separator,
                    label: `OpenCode at ${cat.url || '127.0.0.1:7798'} — ${cat.models.length} model${cat.models.length === 1 ? '' : 's'}`,
                });
                for (const m of cat.models) {
                    backendItems.push({
                        label: `OpenCode • ${m.label || m.value}`,
                        description: m.value || `${m.provider_id}/${m.model_id}`,
                        _provider: 'opencode',
                        _providerId: m.provider_id,
                        _modelId: m.model_id,
                    });
                }
            } else if (cat && cat.error) {
                backendItems.push({
                    kind: vscode.QuickPickItemKind.Separator,
                    label: `OpenCode unreachable: ${cat.error}`,
                });
            }
        } catch {
            // Server unreachable / no OpenCode endpoint — fine, just stick with Claude.
        }

        const backendPick = await vscode.window.showQuickPick(backendItems, {
            placeHolder: 'Pick the agent backend / model',
            title: 'be-conductor: Agent Backend',
        });
        if (!backendPick) return;
        if (backendPick._provider && backendPick._provider !== 'claude') {
            agentOptions = { provider: backendPick._provider };
            if (backendPick._providerId) agentOptions.opencode_provider_id = backendPick._providerId;
            if (backendPick._modelId) agentOptions.opencode_model_id = backendPick._modelId;
        }
    }

    // Step 6: Create and attach session
    if (sessionType === 'agent') {
        // Create session via API (agent mode) and open in webview
        try {
            // For native Claude we still pass agent.command (the
            // existing path uses the CLI command name). For OpenCode
            // (or any other provider), the backend ignores `command`
            // and reads agent_options to drive the session.
            const body = {
                name: trimmed,
                command: agentOptions ? '' : agent.command,
                cwd: selectedCwd,
                session_type: 'agent',
            };
            if (useWorktree) body.worktree = true;
            if (agentOptions) body.agent_options = agentOptions;
            const session = await api.createSession(selectedServerKey, body);
            const sessionId = session.id || session.session_id || trimmed;
            trackSession(trimmed);
            openAgentWebview(selectedServerKey, sessionId, trimmed);
        } catch (err) {
            vscode.window.showErrorMessage(`Failed to create agent session: ${err.message}`);
        }
    } else {
        // Terminal (PTY) mode — run via CLI in terminal
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
    }

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
function openAgentWebview(serverKey, sessionId, name) {
    // Support old 2-arg call: openAgentWebview(sessionId, name)
    if (arguments.length === 2) { name = sessionId; sessionId = serverKey; serverKey = 'local'; }

    if (webviewPanels.has(name)) {
        webviewPanels.get(name).reveal();
        return;
    }

    const registry = require('./serverRegistry');
    const baseUrl = registry.getBaseUrl(serverKey);
    const wsBase = baseUrl.replace(/^http/, 'ws');
    const agentUrl = `${baseUrl}/agent/${encodeURIComponent(sessionId)}?session=${encodeURIComponent(sessionId)}&ws=${encodeURIComponent(wsBase)}`;

    const panel = vscode.window.createWebviewPanel(
        'be-conductor.agentSession',
        `${name} (Agent)`,
        vscode.ViewColumn.One,
        {
            enableScripts: true,
            retainContextWhenHidden: true,
        }
    );

    // Load the agent view via iframe (server-served HTML)
    panel.webview.html = `<!DOCTYPE html>
<html><head><meta charset="UTF-8"><style>html,body{margin:0;padding:0;width:100%;height:100%;overflow:hidden;}iframe{border:none;width:100%;height:100%;}</style></head>
<body><iframe src="${agentUrl}" allow="clipboard-read; clipboard-write"></iframe></body></html>`;

    // Handle messages from the agent view for IDE integration
    panel.webview.onDidReceiveMessage(async (msg) => {
        if (msg.type === 'openFile') {
            try {
                const doc = await vscode.workspace.openTextDocument(msg.path);
                const editor = await vscode.window.showTextDocument(doc);
                if (msg.line > 0) {
                    const pos = new vscode.Position(msg.line - 1, 0);
                    editor.selection = new vscode.Selection(pos, pos);
                    editor.revealRange(new vscode.Range(pos, pos));
                }
            } catch (e) { /* file not found */ }
        } else if (msg.type === 'pickFile') {
            const uris = await vscode.window.showOpenDialog({
                canSelectFiles: true,
                canSelectFolders: false,
                canSelectMany: true,
                openLabel: 'Attach',
                filters: {
                    'All': ['*'],
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



module.exports = {
    createSessionFlow, attachSession, focusTerminal, openAgentWebview,
    terminalMap, webviewPanels,
    setWorkspaceState, getTrackedSessions, trackSession, untrackSession, clearTrackedSessions,
    getRunningAtClose, setRunningAtClose,
};
