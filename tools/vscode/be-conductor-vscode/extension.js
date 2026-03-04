'use strict';
const vscode = require('vscode');
const api = require('./src/api');
const { getServerUrl, getPollInterval } = require('./src/config');
const { createSessionFlow } = require('./src/createSession');
const { SessionTreeProvider, registerSessionCommands } = require('./src/sessionTree');
const { WorktreeTreeProvider, DiffContentProvider, registerWorktreeCommands } = require('./src/worktreeTree');

function activate(context) {
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
}

function deactivate() {}

module.exports = { activate, deactivate };
