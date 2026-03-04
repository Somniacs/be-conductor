'use strict';
const vscode = require('vscode');
const api = require('./src/api');
const { getServerUrl, getPollInterval } = require('./src/config');
const { createSessionFlow, attachSession, setWorkspaceState, getTrackedSessions, untrackSession, clearTrackedSessions } = require('./src/createSession');
const { SessionTreeProvider, registerSessionCommands } = require('./src/sessionTree');
const { WorktreeTreeProvider, DiffContentProvider, registerWorktreeCommands } = require('./src/worktreeTree');

function activate(context) {
    // ── Session persistence ──────────────────────────────────────────────
    setWorkspaceState(context.workspaceState);

    // ── Tree data providers ──────────────────────────────────────────────
    const sessionProvider = new SessionTreeProvider();
    const worktreeProvider = new WorktreeTreeProvider();
    const diffProvider = new DiffContentProvider();

    const sessionView = vscode.window.createTreeView('be-conductor.sessions', {
        treeDataProvider: sessionProvider,
    });
    const worktreeView = vscode.window.createTreeView('be-conductor.worktrees', {
        treeDataProvider: worktreeProvider,
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

    // Start polling when either view becomes visible, stop when both hidden
    sessionView.onDidChangeVisibility(() => {
        if (sessionView.visible || worktreeView.visible) startPolling();
        else stopPolling();
    });
    worktreeView.onDidChangeVisibility(() => {
        if (sessionView.visible || worktreeView.visible) startPolling();
        else stopPolling();
    });

    // Initial refresh if views are visible on activation
    if (sessionView.visible || worktreeView.visible) startPolling();

    // ── Commands ─────────────────────────────────────────────────────────
    context.subscriptions.push(
        vscode.commands.registerCommand('be-conductor.launch', () =>
            createSessionFlow({ onSessionCreated: refreshAll })
        ),
        vscode.commands.registerCommand('be-conductor.openDashboard', () =>
            vscode.env.openExternal(vscode.Uri.parse(getServerUrl()))
        ),
        vscode.commands.registerCommand('be-conductor.refresh', refreshAll),
    );

    registerSessionCommands(context, sessionProvider);
    registerWorktreeCommands(context, worktreeProvider, diffProvider, refreshAll);

    // ── Status bar ───────────────────────────────────────────────────────
    const statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
    statusBar.command = 'be-conductor.launch';
    statusBar.show();
    context.subscriptions.push(statusBar);

    async function updateStatusBar() {
        try {
            const info = await api.getHealth();
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
        { dispose: () => { stopPolling(); clearInterval(healthTimer); } },
    );

    // ── Auto-resume tracked sessions from previous IDE session ──────────
    setTimeout(async () => {
        const tracked = getTrackedSessions();
        if (tracked.length === 0) return;
        try {
            const sessions = await api.listSessions();
            const byName = new Map(sessions.map(s => [s.name, s]));
            const resumed = [];
            const reattached = [];

            for (const name of tracked) {
                const s = byName.get(name);
                if (!s) {
                    // Session gone — drop from tracking
                    untrackSession(name);
                    continue;
                }
                if (s.status === 'running') {
                    // Still running — just re-attach
                    attachSession(s.name);
                    reattached.push(name);
                } else if (s.status === 'exited' && (s.resume_id || s.worktree)) {
                    // Resumable — resume and attach
                    try {
                        await api.resumeSession(s.id);
                        attachSession(s.name);
                        resumed.push(name);
                    } catch {
                        untrackSession(name);
                    }
                } else {
                    // Completed without resume — drop
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
            // Server not reachable — skip silently
        }
    }, 3000);
}

async function deactivate() {
    const tracked = getTrackedSessions();
    if (tracked.length === 0) return;

    // Gracefully stop running sessions so they can print resume tokens.
    try {
        const sessions = await api.listSessions();
        const running = sessions.filter(s => tracked.includes(s.name) && s.status === 'running');
        if (running.length === 0) return;

        await Promise.all(running.map(s =>
            api.stopSession(s.id, 'graceful').catch(() => {})
        ));

        // Wait briefly for resume tokens to be captured.
        await new Promise(resolve => setTimeout(resolve, 2000));
    } catch {
        // Server unreachable — nothing to do
    }
}

module.exports = { activate, deactivate };
