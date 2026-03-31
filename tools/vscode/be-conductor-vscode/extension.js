'use strict';
const vscode = require('vscode');
const api = require('./src/api');
const registry = require('./src/serverRegistry');
const { getPollInterval } = require('./src/config');
const { createSessionFlow, attachSession, setWorkspaceState, getTrackedSessions, trackSession, untrackSession, clearTrackedSessions, focusTerminal, openAgentWebview, terminalMap, webviewPanels, getRunningAtClose, setRunningAtClose } = require('./src/createSession');
const { SessionTreeProvider, registerSessionCommands } = require('./src/sessionTree');
const { WorktreeTreeProvider, DiffContentProvider, registerWorktreeCommands } = require('./src/worktreeTree');
const { ServerTreeProvider, registerServerCommands } = require('./src/serverTree');

function activate(context) {
    // ── Server registry + session persistence ────────────────────────────
    registry.init(context.workspaceState);
    setWorkspaceState(context.workspaceState);

    // ── Tree data providers ──────────────────────────────────────────────
    const sessionProvider = new SessionTreeProvider();
    const worktreeProvider = new WorktreeTreeProvider();
    const serverProvider = new ServerTreeProvider();
    const diffProvider = new DiffContentProvider();

    const sessionView = vscode.window.createTreeView('be-conductor.sessions', {
        treeDataProvider: sessionProvider,
    });
    const worktreeView = vscode.window.createTreeView('be-conductor.worktrees', {
        treeDataProvider: worktreeProvider,
    });
    const serverView = vscode.window.createTreeView('be-conductor.servers', {
        treeDataProvider: serverProvider,
    });

    // ── Diff content provider ────────────────────────────────────────────
    context.subscriptions.push(
        vscode.workspace.registerTextDocumentContentProvider('be-conductor-diff', diffProvider),
    );

    // ── Refresh helper ───────────────────────────────────────────────────
    function refreshAll() {
        sessionProvider.refresh();
        worktreeProvider.refresh();
    }

    // ── Polling (only while sidebar is visible) ──────────────────────────
    let pollTimer = null;
    function startPolling() {
        if (pollTimer) return;
        refreshAll();
        pollTimer = setInterval(refreshAll, getPollInterval());
    }
    function stopPolling() {
        if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    }

    sessionView.onDidChangeVisibility(() => {
        if (sessionView.visible || worktreeView.visible) startPolling();
        else stopPolling();
    });
    worktreeView.onDidChangeVisibility(() => {
        if (sessionView.visible || worktreeView.visible) startPolling();
        else stopPolling();
    });

    if (sessionView.visible || worktreeView.visible) startPolling();

    // ── Commands ─────────────────────────────────────────────────────────
    context.subscriptions.push(
        vscode.commands.registerCommand('be-conductor.launch', () =>
            createSessionFlow({ onSessionCreated: refreshAll })
        ),
        vscode.commands.registerCommand('be-conductor.openDashboard', () =>
            vscode.env.openExternal(vscode.Uri.parse(registry.getBaseUrl('local')))
        ),
        vscode.commands.registerCommand('be-conductor.refresh', refreshAll),
    );

    registerSessionCommands(context, sessionProvider);
    registerWorktreeCommands(context, worktreeProvider, diffProvider, refreshAll);
    registerServerCommands(context, serverProvider, refreshAll);

    // ── Status bar ───────────────────────────────────────────────────────
    const statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
    statusBar.command = 'be-conductor.launch';
    statusBar.show();
    context.subscriptions.push(statusBar);

    async function updateStatusBar() {
        try {
            const info = await api.getHealth('local');
            statusBar.text = '$(terminal) be-conductor';
            statusBar.tooltip = `be-conductor v${info.version} — Click to create session`;
            statusBar.backgroundColor = undefined;
        } catch {
            statusBar.text = '$(warning) be-conductor';
            statusBar.tooltip = 'be-conductor server not running';
            statusBar.backgroundColor = new vscode.ThemeColor('statusBarItem.warningBackground');
        }
    }
    updateStatusBar();
    const healthTimer = setInterval(updateStatusBar, 15000);

    // ── Cleanup ──────────────────────────────────────────────────────────
    context.subscriptions.push(
        sessionView,
        worktreeView,
        serverView,
        { dispose: () => { stopPolling(); clearInterval(healthTimer); } },
    );

    // ── Auto-resume tracked sessions ─────────────────────────────────────
    setTimeout(async () => {
        const tracked = getTrackedSessions();
        const wasRunning = new Set(getRunningAtClose());
        setRunningAtClose([]);
        if (tracked.length === 0) return;
        try {
            const sessions = await api.listSessions('local');
            const byName = new Map(sessions.map(s => [s.name, s]));
            const resumed = [];
            const reattached = [];

            for (const name of tracked) {
                const s = byName.get(name);
                if (!s) { untrackSession(name); continue; }
                if (s.status === 'running') {
                    if (s.session_type === 'agent') {
                        openAgentWebview('local', s.id, s.name);
                    } else {
                        attachSession(s.name);
                    }
                    reattached.push(name);
                } else if (s.status === 'exited' && (s.resume_id || s.worktree) && wasRunning.has(name)) {
                    const workDir = s.cwd ||
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
                    await new Promise(resolve => setTimeout(resolve, 500));
                    terminal.sendText(`be-conductor resume "${name}" ; exit`);
                    terminalMap.set(name, terminal);
                    trackSession(name);
                    resumed.push(name);
                } else if (s.status === 'exited' && (s.resume_id || s.worktree)) {
                    // Resumable but wasn't running at close — leave tracked
                } else {
                    untrackSession(name);
                }
            }

            if (resumed.length > 0 || reattached.length > 0) {
                const parts = [];
                if (resumed.length > 0) parts.push(`resumed ${resumed.join(', ')}`);
                if (reattached.length > 0) parts.push(`re-attached ${reattached.join(', ')}`);
                vscode.window.showInformationMessage(`be-conductor: ${parts.join('; ')}`);
                refreshAll();
            }
        } catch {
            // Server not reachable
        }
    }, 3000);
}

async function deactivate() {
    const tracked = getTrackedSessions();
    if (tracked.length === 0) return;

    try {
        const sessions = await api.listSessions('local');
        const running = sessions.filter(s => tracked.includes(s.name) && s.status === 'running');
        if (running.length === 0) return;

        await setRunningAtClose(running.map(s => s.name));
        await Promise.all(running.map(s =>
            api.stopSession('local', s.id, 'graceful').catch(() => {})
        ));
        await new Promise(resolve => setTimeout(resolve, 2000));
    } catch {}
}

module.exports = { activate, deactivate };
