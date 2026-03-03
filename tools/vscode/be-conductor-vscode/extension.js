'use strict';
const vscode = require('vscode');

const AGENTS = [
    { label: 'claude',   description: 'Claude Code' },
    { label: 'codex',    description: 'OpenAI Codex CLI' },
    { label: 'aider',    description: 'Aider' },
    { label: 'gemini',   description: 'Gemini CLI' },
    { label: 'copilot',  description: 'GitHub Copilot CLI' },
    { label: 'opencode', description: 'OpenCode' },
    { label: 'amp',      description: 'Amp (Sourcegraph)' },
    { label: 'goose',    description: 'Goose (Block)' },
    { label: 'forge',    description: 'Forge' },
    { label: 'cursor',   description: 'Cursor Agent' },
];

const NAME_PATTERN = /^[A-Za-z0-9_-]+$/;

function activate(context) {
    const disposable = vscode.commands.registerCommand('be-conductor.launch', async () => {
        const dashboardItem = { label: '$(globe) Open Dashboard', description: 'Open be-conductor dashboard in browser', _dashboard: true };
        const items = [dashboardItem, { kind: vscode.QuickPickItemKind.Separator, label: 'Agents' }, ...AGENTS];
        const agent = await vscode.window.showQuickPick(items, {
            placeHolder: 'Select an AI agent or open the dashboard',
            title: 'be-conductor',
        });
        if (!agent) return;
        if (agent._dashboard) {
            vscode.env.openExternal(vscode.Uri.parse('http://127.0.0.1:7777'));
            return;
        }

        const name = await vscode.window.showInputBox({
            prompt: 'Session name',
            placeHolder: 'e.g. feature-auth',
            title: 'be-conductor: Session Name',
            validateInput(value) {
                if (!value || !value.trim()) return 'Session name cannot be empty';
                if (!NAME_PATTERN.test(value.trim())) return 'Only letters, digits, hyphens, and underscores';
                return null;
            },
        });
        if (name === undefined) return;

        const cwd = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
        const trimmed = name.trim();

        const terminal = vscode.window.createTerminal({
            name: `${trimmed} (${agent.label})`,
            cwd,
            isTransient: true,
        });
        terminal.show();
        terminal.sendText(`be-conductor run ${agent.label} ${trimmed}`);
    });

    context.subscriptions.push(disposable);

    const dashboardCmd = vscode.commands.registerCommand('be-conductor.openDashboard', () => {
        vscode.env.openExternal(vscode.Uri.parse('http://127.0.0.1:7777'));
    });
    context.subscriptions.push(dashboardCmd);

    const statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
    statusBar.text = '$(terminal) be-conductor';
    statusBar.command = 'be-conductor.launch';
    statusBar.tooltip = 'Launch be-conductor agent session';
    statusBar.show();
    context.subscriptions.push(statusBar);

    const dashboardStatusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 99);
    dashboardStatusBar.text = '$(globe) Dashboard';
    dashboardStatusBar.command = 'be-conductor.openDashboard';
    dashboardStatusBar.tooltip = 'Open be-conductor dashboard in browser';
    dashboardStatusBar.show();
    context.subscriptions.push(dashboardStatusBar);
}

function deactivate() {}

module.exports = { activate, deactivate };
