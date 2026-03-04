'use strict';
const vscode = require('vscode');
const api = require('./api');
const { NAME_PATTERN, AGENTS } = require('./config');

/** Map of session name -> VS Code Terminal instance (for focus-on-click). */
const terminalMap = new Map();

// Clean up terminal references when they close.
vscode.window.onDidCloseTerminal((t) => {
    for (const [name, term] of terminalMap) {
        if (term === t) { terminalMap.delete(name); break; }
    }
});

// ── Session persistence (survives IDE restart) ──────────────────────────
const TRACKED_KEY = 'be-conductor.trackedSessions';
/** @type {vscode.Memento | null} */
let _workspaceState = null;

function setWorkspaceState(state) { _workspaceState = state; }

/** @returns {string[]} tracked session names */
function getTrackedSessions() {
    if (!_workspaceState) return [];
    return _workspaceState.get(TRACKED_KEY, []);
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
    const cmd = useWorktree
        ? `be-conductor run -w "${agent.command}" "${trimmed}"`
        : `be-conductor run "${agent.command}" "${trimmed}"`;

    const terminal = vscode.window.createTerminal({
        name: `${trimmed} (${agent.label})`,
        cwd: selectedCwd,
        isTransient: true,
    });
    terminal.show();
    terminal.sendText(cmd);
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
function attachSession(name, cwd) {
    if (terminalMap.has(name)) {
        // Already attached — just focus
        terminalMap.get(name).show();
        return;
    }

    const workDir = cwd ||
        (vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders[0]
            ? vscode.workspace.workspaceFolders[0].uri.fsPath
            : undefined);

    const terminal = vscode.window.createTerminal({
        name,
        cwd: workDir,
        isTransient: true,
    });
    terminal.show();
    terminal.sendText(`be-conductor attach "${name}" ; exit`);
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

module.exports = {
    createSessionFlow, attachSession, focusTerminal, terminalMap,
    setWorkspaceState, getTrackedSessions, trackSession, untrackSession, clearTrackedSessions,
};
