'use strict';
const vscode = require('vscode');
const api = require('./api');
const registry = require('./serverRegistry');
const { terminalMap, webviewPanels, attachSession, focusTerminal, openAgentWebview, untrackSession, trackSession } = require('./createSession');

class ServerGroupItem extends vscode.TreeItem {
    constructor(server, sessionCount) {
        super(server.label, vscode.TreeItemCollapsibleState.Expanded);
        this.serverKey = server.key;
        this.description = `(${sessionCount})`;
        this.iconPath = new vscode.ThemeIcon('home', new vscode.ThemeColor('foreground'));
        this.contextValue = 'server-group';
    }
}

class SessionItem extends vscode.TreeItem {
    constructor(session) {
        super(session.name, vscode.TreeItemCollapsibleState.None);
        this.session = session;

        const resumable = isResumable(session);
        const isAgent = session.session_type === 'agent';
        // Native Claude agent vs other providers (OpenCode, etc.).
        // Used to gate operations the provider doesn't support — most
        // notably clone, which has no equivalent on OpenCode's API.
        const isClaudeAgent = isAgent && (session.provider == null || session.provider === 'claude');
        this.description = isAgent ? 'GUI · ' + session.command : session.command;
        this.tooltip = `${session.name}\nCommand: ${session.command}\nType: ${isAgent ? 'GUI' : 'Terminal'}\nStatus: ${session.status}` +
            (session.cwd ? `\nDirectory: ${session.cwd}` : '') +
            (session.worktree ? `\nWorktree: ${session.worktree.branch}` : '') +
            (resumable ? '\nResumable' : '') +
            (session._serverKey ? `\nServer: ${session._serverKey}` : '');

        if (session.worktree) {
            let branch = session.worktree.branch || '';
            if (branch.startsWith('be-conductor/')) branch = branch.substring('be-conductor/'.length);
            this.description += `  $(git-branch) ${branch}`;
            if (session.worktree.commits_ahead > 0) {
                this.description += ` +${session.worktree.commits_ahead}`;
            }
        }

        if (session.status === 'running') {
            const attached = isAgent ? webviewPanels.has(session.name) : terminalMap.has(session.name);
            this.iconPath = new vscode.ThemeIcon('circle-filled', new vscode.ThemeColor('testing.iconPassed'));
            // Distinguish native-Claude agent from other-provider agent
            // in the context value so menu visibility (clone, etc.) can
            // be gated in package.json's `when` clauses. The four base
            // shapes — running / running-attached / running-agent /
            // running-attached-agent — keep their old names for
            // backwards-compat. Non-Claude agent sessions use a
            // distinct '-agent-other' suffix.
            if (attached) {
                this.contextValue = isAgent
                    ? (isClaudeAgent ? 'session-running-attached-agent' : 'session-running-attached-agent-other')
                    : 'session-running-attached';
            } else {
                this.contextValue = isAgent
                    ? (isClaudeAgent ? 'session-running-agent' : 'session-running-agent-other')
                    : 'session-running';
            }
            if (attached) this.description += '  [attached]';
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
        this.command = { command: 'be-conductor.refresh', title: 'Retry' };
    }
}

class SessionTreeProvider {
    constructor() {
        this._onDidChangeTreeData = new vscode.EventEmitter();
        this.onDidChangeTreeData = this._onDidChangeTreeData.event;
        /** @type {Map<string, object[]>} serverKey → sessions */
        this._byServer = new Map();
        this._offline = false;
    }

    refresh() {
        const enabled = registry.getEnabledServers();
        const results = new Map();
        let done = 0;
        let anySuccess = false;

        if (enabled.length === 0) {
            this._byServer = new Map();
            this._offline = true;
            this._onDidChangeTreeData.fire();
            return;
        }

        for (const server of enabled) {
            api.listSessions(server.key)
                .then(sessions => {
                    // Tag each session with its server key
                    for (const s of sessions) s._serverKey = server.key;
                    results.set(server.key, sessions);
                    anySuccess = true;
                })
                .catch(() => {
                    results.set(server.key, []);
                })
                .finally(() => {
                    done++;
                    if (done === enabled.length) {
                        this._byServer = results;
                        this._offline = !anySuccess;
                        // Clean up tracking
                        const allRunning = new Set();
                        for (const sessions of results.values()) {
                            for (const s of sessions) {
                                if (s.status === 'running') allRunning.add(s.name);
                            }
                        }
                        for (const name of terminalMap.keys()) {
                            if (!allRunning.has(name)) terminalMap.delete(name);
                        }
                        for (const name of webviewPanels.keys()) {
                            if (!allRunning.has(name)) webviewPanels.delete(name);
                        }
                        this._onDidChangeTreeData.fire();
                    }
                });
        }
    }

    getTreeItem(element) { return element; }

    getChildren(element) {
        if (this._offline) return [new OfflineItem()];

        const multiServer = registry.isMultiServer();

        // Top level
        if (!element) {
            if (multiServer) {
                // Show server groups
                const groups = [];
                for (const server of registry.getEnabledServers()) {
                    const sessions = this._byServer.get(server.key) || [];
                    groups.push(new ServerGroupItem(server, sessions.length));
                }
                return groups;
            }
            // Single server — flat list
            return this._sortedItems(this._byServer.get(registry.LOCAL_KEY) || []);
        }

        // Children of a server group
        if (element instanceof ServerGroupItem) {
            const sessions = this._byServer.get(element.serverKey) || [];
            return this._sortedItems(sessions);
        }

        return [];
    }

    _sortedItems(sessions) {
        if (sessions.length === 0) return [];
        const order = { running: 0, stopping: 1 };
        const sorted = [...sessions].sort((a, b) => {
            const aOrder = order[a.status] ?? (isResumable(a) ? 2 : 3);
            const bOrder = order[b.status] ?? (isResumable(b) ? 2 : 3);
            return aOrder - bOrder;
        });
        return sorted.map(s => new SessionItem(s));
    }
}

/**
 * Register session tree commands.
 */
function registerSessionCommands(context, provider) {
    context.subscriptions.push(
        vscode.commands.registerCommand('be-conductor.focusSession', (session) => {
            const sk = session._serverKey || 'local';
            if (session.status === 'running') {
                if (session.session_type === 'agent') {
                    openAgentWebview(sk, session.id, session.name);
                } else {
                    if (!focusTerminal(session.name)) attachSession(session.name);
                }
            } else if (isResumable(session)) {
                const items = provider.getChildren();
                // Flatten if multi-server
                const allItems = items.flatMap(i => i instanceof ServerGroupItem ? (provider.getChildren(i) || []) : [i]);
                const item = allItems.find(i => i.session && i.session.id === session.id);
                if (item) vscode.commands.executeCommand('be-conductor.resumeSession', item);
            }
        }),

        vscode.commands.registerCommand('be-conductor.attachSession', async (item) => {
            if (!(item instanceof SessionItem)) return;
            const sk = item.session._serverKey || 'local';
            if (item.session.session_type === 'agent') {
                openAgentWebview(sk, item.session.id, item.session.name);
            } else {
                attachSession(item.session.name);
            }
        }),

        vscode.commands.registerCommand('be-conductor.resumeSession', async (item) => {
            if (!(item instanceof SessionItem)) return;
            const session = item.session;
            const sk = session._serverKey || 'local';

            if (session.session_type === 'agent') {
                try {
                    await api.resumeSession(sk, session.id);
                    vscode.window.showInformationMessage(`Resuming "${session.name}"...`);
                    setTimeout(() => {
                        openAgentWebview(sk, session.id, session.name);
                        provider.refresh();
                    }, 1500);
                } catch (e) {
                    vscode.window.showErrorMessage(`Failed to resume session: ${e.message}`);
                }
                return;
            }

            if (terminalMap.has(session.name)) {
                terminalMap.get(session.name).show();
                return;
            }
            const workDir = session.cwd ||
                (vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders[0]
                    ? vscode.workspace.workspaceFolders[0].uri.fsPath
                    : undefined);
            const terminal = vscode.window.createTerminal({
                name: session.name,
                cwd: workDir,
                isTransient: true,
                env: { VIRTUAL_ENV: null, CONDA_PREFIX: null, CONDA_DEFAULT_ENV: null },
            });
            terminal.show();
            await new Promise(resolve => setTimeout(resolve, 500));
            terminal.sendText('\x15' + `be-conductor resume "${session.name}" ; exit`);
            terminalMap.set(session.name, terminal);
            trackSession(session.name);
            vscode.window.showInformationMessage(`Resuming "${session.name}"...`);
            setTimeout(() => provider.refresh(), 1000);
        }),

        vscode.commands.registerCommand('be-conductor.stopSession', async (item) => {
            if (!(item instanceof SessionItem)) return;
            const session = item.session;
            const sk = session._serverKey || 'local';
            const mode = session.status === 'stopping' ? 'kill' : 'graceful';
            try {
                await api.stopSession(sk, session.id, mode);
                vscode.window.showInformationMessage(`Stopping "${session.name}"...`);
                let attempts = 0;
                const pollInterval = setInterval(async () => {
                    attempts++;
                    provider.refresh();
                    try {
                        const sessions = await api.listSessions(sk);
                        const current = sessions.find(s => s.id === session.id);
                        if (!current || current.status !== 'stopping' || attempts >= 15) {
                            clearInterval(pollInterval);
                            provider.refresh();
                        }
                    } catch { clearInterval(pollInterval); }
                }, 1000);
            } catch (e) {
                vscode.window.showErrorMessage(`Failed to stop session: ${e.message}`);
            }
        }),

        vscode.commands.registerCommand('be-conductor.killSession', async (item) => {
            if (!(item instanceof SessionItem)) return;
            const sk = item.session._serverKey || 'local';
            try {
                await api.stopSession(sk, item.session.id, 'kill');
                untrackSession(item.session.name);
                vscode.window.showInformationMessage(`Killed "${item.session.name}".`);
                setTimeout(() => provider.refresh(), 500);
            } catch (e) {
                vscode.window.showErrorMessage(`Failed to kill session: ${e.message}`);
            }
        }),

        vscode.commands.registerCommand('be-conductor.forgetSession', async (item) => {
            if (!(item instanceof SessionItem)) return;
            const session = item.session;
            const sk = session._serverKey || 'local';
            try {
                await api.stopSession(sk, session.id, 'forget');
                untrackSession(session.name);
                vscode.window.showInformationMessage(`Forgetting "${session.name}"...`);
                let attempts = 0;
                const pollInterval = setInterval(async () => {
                    attempts++;
                    provider.refresh();
                    try {
                        const sessions = await api.listSessions(sk);
                        const current = sessions.find(s => s.id === session.id);
                        if (!current || attempts >= 15) { clearInterval(pollInterval); provider.refresh(); }
                    } catch { clearInterval(pollInterval); }
                }, 1000);
            } catch (e) {
                vscode.window.showErrorMessage(`Failed to forget session: ${e.message}`);
            }
        }),

        vscode.commands.registerCommand('be-conductor.dismissSession', async (item) => {
            if (!(item instanceof SessionItem)) return;
            const sk = item.session._serverKey || 'local';
            try {
                await api.deleteSession(sk, item.session.id);
                untrackSession(item.session.name);
                provider.refresh();
            } catch (e) {
                vscode.window.showErrorMessage(`Failed to dismiss session: ${e.message}`);
            }
        }),

        vscode.commands.registerCommand('be-conductor.cloneSession', async (item) => {
            if (!(item instanceof SessionItem)) return;
            const session = item.session;
            const sk = session._serverKey || 'local';
            const name = await vscode.window.showInputBox({
                prompt: 'Name for the cloned session',
                value: `${session.name}-clone`,
                validateInput(value) {
                    const v = (value || '').trim();
                    if (!v) return 'Name is required';
                    if (!/^[a-zA-Z0-9][a-zA-Z0-9 _.~-]{0,63}$/.test(v))
                        return 'Invalid name';
                    return null;
                },
            });
            if (!name) return;
            try {
                await api.cloneSession(sk, session.id, { name: name.trim() });
                vscode.window.showInformationMessage(`Cloning "${session.name}" into "${name.trim()}"...`);
                let attempts = 0;
                const pollInterval = setInterval(async () => {
                    attempts++;
                    provider.refresh();
                    try {
                        const sessions = await api.listSessions(sk);
                        const cloned = sessions.find(s => s.name === name.trim() && s.status === 'running');
                        if (cloned || attempts >= 90) { clearInterval(pollInterval); provider.refresh(); }
                    } catch { if (attempts >= 90) clearInterval(pollInterval); }
                }, 1000);
            } catch (e) {
                vscode.window.showErrorMessage(`Failed to clone session: ${e.message}`);
            }
        }),
    );
}

module.exports = { SessionTreeProvider, registerSessionCommands };
