'use strict';
const vscode = require('vscode');
const api = require('./api');
const { terminalMap, attachSession, focusTerminal } = require('./createSession');

class SessionItem extends vscode.TreeItem {
    constructor(session) {
        super(session.name, vscode.TreeItemCollapsibleState.None);
        this.session = session;

        const resumable = isResumable(session);
        this.description = session.command;
        this.tooltip = `${session.name}\nCommand: ${session.command}\nStatus: ${session.status}` +
            (session.cwd ? `\nDirectory: ${session.cwd}` : '') +
            (session.worktree ? `\nWorktree: ${session.worktree.branch}` : '') +
            (resumable ? '\nResumable' : '');

        if (session.worktree) {
            let branch = session.worktree.branch || '';
            if (branch.startsWith('be-conductor/')) branch = branch.substring('be-conductor/'.length);
            this.description += `  $(git-branch) ${branch}`;
            if (session.worktree.commits_ahead > 0) {
                this.description += ` +${session.worktree.commits_ahead}`;
            }
        }

        if (session.status === 'running') {
            this.iconPath = new vscode.ThemeIcon('circle-filled', new vscode.ThemeColor('testing.iconPassed'));
            this.contextValue = terminalMap.has(session.name) ? 'session-running-attached' : 'session-running';
        } else if (session.status === 'stopping') {
            this.iconPath = new vscode.ThemeIcon('loading~spin');
            this.contextValue = 'session-stopping';
            this.description += '  [stopping]';
        } else if (resumable) {
            this.iconPath = new vscode.ThemeIcon('debug-restart', new vscode.ThemeColor('testing.iconPassed'));
            this.contextValue = 'session-resumable';
            this.description += '  [resumable]';
        } else {
            this.iconPath = new vscode.ThemeIcon('circle-outline', new vscode.ThemeColor('disabledForeground'));
            this.contextValue = 'session-exited';
        }

        // Click to focus/attach terminal (running sessions only)
        if (session.status === 'running') {
            this.command = {
                command: 'be-conductor.focusSession',
                title: 'Focus Session',
                arguments: [session],
            };
        }
    }
}

function isResumable(session) {
    return session.status === 'exited' &&
        (session.resume_id != null || session.worktree != null);
}

class OfflineItem extends vscode.TreeItem {
    constructor() {
        super('Server offline', vscode.TreeItemCollapsibleState.None);
        this.description = 'Click to retry';
        this.iconPath = new vscode.ThemeIcon('warning', new vscode.ThemeColor('problemsWarningIcon.foreground'));
        this.command = {
            command: 'be-conductor.refresh',
            title: 'Retry',
        };
    }
}

class SessionTreeProvider {
    constructor() {
        this._onDidChangeTreeData = new vscode.EventEmitter();
        this.onDidChangeTreeData = this._onDidChangeTreeData.event;
        this._sessions = [];
        this._offline = false;
    }

    refresh() {
        api.listSessions()
            .then((sessions) => {
                this._sessions = sessions;
                this._offline = false;
                // Clean up terminal tracking for sessions no longer running
                const running = new Set(sessions.filter(s => s.status === 'running').map(s => s.name));
                for (const name of terminalMap.keys()) {
                    if (!running.has(name)) terminalMap.delete(name);
                }
                this._onDidChangeTreeData.fire();
            })
            .catch(() => {
                this._sessions = [];
                this._offline = true;
                this._onDidChangeTreeData.fire();
            });
    }

    getTreeItem(element) {
        return element;
    }

    getChildren() {
        if (this._offline) {
            return [new OfflineItem()];
        }
        if (this._sessions.length === 0) {
            return [];
        }
        // Running first, then stopping, then resumable, then exited
        const order = { running: 0, stopping: 1 };
        const sorted = [...this._sessions].sort((a, b) => {
            const aOrder = order[a.status] ?? (isResumable(a) ? 2 : 3);
            const bOrder = order[b.status] ?? (isResumable(b) ? 2 : 3);
            return aOrder - bOrder;
        });
        return sorted.map((s) => new SessionItem(s));
    }
}

/**
 * Register session tree commands.
 * @param {vscode.ExtensionContext} context
 * @param {SessionTreeProvider} provider
 */
function registerSessionCommands(context, provider) {
    context.subscriptions.push(
        vscode.commands.registerCommand('be-conductor.focusSession', (session) => {
            if (session.status === 'running') {
                // Try to focus existing terminal, otherwise attach
                if (!focusTerminal(session.name)) {
                    attachSession(session.name);
                }
            } else if (isResumable(session)) {
                // Trigger resume via the command
                const items = provider.getChildren();
                const item = items.find(i => i.session && i.session.id === session.id);
                if (item) {
                    vscode.commands.executeCommand('be-conductor.resumeSession', item);
                }
            }
        }),

        vscode.commands.registerCommand('be-conductor.attachSession', async (item) => {
            if (!(item instanceof SessionItem)) return;
            attachSession(item.session.name);
        }),

        vscode.commands.registerCommand('be-conductor.resumeSession', async (item) => {
            if (!(item instanceof SessionItem)) return;
            try {
                await api.resumeSession(item.session.id);
                vscode.window.showInformationMessage(`Resuming "${item.session.name}"...`);
                // Auto-attach terminal
                attachSession(item.session.name);
                setTimeout(() => provider.refresh(), 1000);
            } catch (e) {
                vscode.window.showErrorMessage(`Failed to resume session: ${e.message}`);
            }
        }),

        vscode.commands.registerCommand('be-conductor.stopSession', async (item) => {
            if (!(item instanceof SessionItem)) return;
            const session = item.session;
            // If already stopping, escalate to kill
            const mode = session.status === 'stopping' ? 'kill' : 'graceful';
            try {
                await api.stopSession(session.id, mode);
                vscode.window.showInformationMessage(`Stopping "${session.name}"...`);
                // Poll until session transitions out of "stopping" (up to 15s)
                let attempts = 0;
                const pollInterval = setInterval(async () => {
                    attempts++;
                    provider.refresh();
                    try {
                        const sessions = await api.listSessions();
                        const current = sessions.find(s => s.id === session.id);
                        if (!current || current.status !== 'stopping' || attempts >= 15) {
                            clearInterval(pollInterval);
                            provider.refresh();
                        }
                    } catch {
                        clearInterval(pollInterval);
                    }
                }, 1000);
            } catch (e) {
                vscode.window.showErrorMessage(`Failed to stop session: ${e.message}`);
            }
        }),

        vscode.commands.registerCommand('be-conductor.killSession', async (item) => {
            if (!(item instanceof SessionItem)) return;
            try {
                await api.stopSession(item.session.id, 'kill');
                vscode.window.showInformationMessage(`Killed "${item.session.name}".`);
                setTimeout(() => provider.refresh(), 500);
            } catch (e) {
                vscode.window.showErrorMessage(`Failed to kill session: ${e.message}`);
            }
        }),

        vscode.commands.registerCommand('be-conductor.forgetSession', async (item) => {
            if (!(item instanceof SessionItem)) return;
            const session = item.session;
            try {
                await api.stopSession(session.id, 'forget');
                vscode.window.showInformationMessage(`Forgetting "${session.name}"...`);
                // Poll until session disappears (up to 15s)
                let attempts = 0;
                const pollInterval = setInterval(async () => {
                    attempts++;
                    provider.refresh();
                    try {
                        const sessions = await api.listSessions();
                        const current = sessions.find(s => s.id === session.id);
                        if (!current || attempts >= 15) {
                            clearInterval(pollInterval);
                            provider.refresh();
                        }
                    } catch {
                        clearInterval(pollInterval);
                    }
                }, 1000);
            } catch (e) {
                vscode.window.showErrorMessage(`Failed to forget session: ${e.message}`);
            }
        }),

        vscode.commands.registerCommand('be-conductor.dismissSession', async (item) => {
            if (!(item instanceof SessionItem)) return;
            try {
                await api.deleteSession(item.session.id);
                provider.refresh();
            } catch (e) {
                vscode.window.showErrorMessage(`Failed to dismiss session: ${e.message}`);
            }
        }),
    );
}

module.exports = { SessionTreeProvider, registerSessionCommands };
