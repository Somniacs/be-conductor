'use strict';
const vscode = require('vscode');
const api = require('./api');
const registry = require('./serverRegistry');

/** Cached probe results: serverKey → { version, hostname } or null (offline). */
const probeCache = new Map();

class ServerItem extends vscode.TreeItem {
    constructor(server) {
        super(server.label, vscode.TreeItemCollapsibleState.None);
        this.server = server;
        this.contextValue = server.key === registry.LOCAL_KEY ? 'server-local' : (server.enabled ? 'server-remote' : 'server-disabled');

        const info = probeCache.get(server.key);
        const online = info != null;
        const urlDisplay = server.url || 'localhost:7777';

        this.description = online ? `${urlDisplay}  v${info.version || '?'}` : urlDisplay;
        this.iconPath = new vscode.ThemeIcon(
            online ? 'circle-filled' : (server.enabled ? 'circle-outline' : 'circle-slash'),
            new vscode.ThemeColor(online ? 'testing.iconPassed' : (server.enabled ? 'testing.iconErrored' : 'disabledForeground'))
        );
        this.tooltip = `${server.label}\n${urlDisplay}\n${online ? 'Online' : 'Offline'}${info ? '\nv' + info.version : ''}${!server.enabled ? '\n(disabled)' : ''}`;
    }
}

class ServerTreeProvider {
    constructor() {
        this._onDidChangeTreeData = new vscode.EventEmitter();
        this.onDidChangeTreeData = this._onDidChangeTreeData.event;
    }

    refresh() { this._onDidChangeTreeData.fire(); }

    getTreeItem(element) { return element; }

    getChildren() {
        return registry.getServers().map(s => new ServerItem(s));
    }
}

/** Probe all servers and update cache. */
async function probeAll(provider) {
    const servers = registry.getServers();
    await Promise.all(servers.map(async (server) => {
        try {
            const info = await api.getInfo(server.key);
            probeCache.set(server.key, info);
        } catch {
            probeCache.set(server.key, null);
        }
    }));
    provider.refresh();
}

/** Register all server-related commands. */
function registerServerCommands(context, provider, onServersChanged) {
    // Probe on activation
    probeAll(provider);
    // Re-probe every 30s
    const probeTimer = setInterval(() => probeAll(provider), 30000);
    context.subscriptions.push({ dispose: () => clearInterval(probeTimer) });

    context.subscriptions.push(
        vscode.commands.registerCommand('be-conductor.addServer', async () => {
            // Build a pick list: Tailscale peers + manual entry option
            const items = [];

            // Try Tailscale discovery first
            try {
                const peers = await api.getTailscalePeers('local');
                if (peers && peers.length > 0) {
                    const existingKeys = new Set(registry.getServers().map(s => s.key));
                    for (const p of peers) {
                        const key = p.ip + ':7777';
                        if (existingKeys.has(key)) continue;
                        items.push({
                            label: `$(server) ${p.hostname || p.ip}`,
                            description: p.ip + (p.online ? '' : '  (offline)'),
                            _peer: p,
                        });
                    }
                    if (items.length > 0) {
                        items.unshift({ kind: vscode.QuickPickItemKind.Separator, label: 'Tailscale Machines' });
                    }
                }
            } catch {
                // No Tailscale — skip silently
            }

            items.push({ kind: vscode.QuickPickItemKind.Separator, label: '' });
            items.push({
                label: '$(edit) Enter URL manually...',
                description: 'Type a hostname or IP address',
                _manual: true,
            });

            const picked = await vscode.window.showQuickPick(items, {
                placeHolder: 'Select a machine or enter URL manually',
                title: 'Add Server',
            });
            if (!picked) return;

            let url, label;
            if (picked._manual) {
                url = await vscode.window.showInputBox({
                    prompt: 'Server URL',
                    placeHolder: 'e.g. 192.168.1.50:7777 or my-machine.tail1234.ts.net:7777',
                });
                if (!url) return;

                // Probe for hostname
                try {
                    const testUrl = url.replace(/\/+$/, '');
                    const fullUrl = /^https?:\/\//.test(testUrl) ? testUrl : 'http://' + testUrl;
                    const key = fullUrl.replace(/^https?:\/\//, '');
                    const info = await api.getInfo(key).catch(() => null);
                    if (info && info.hostname) label = info.hostname;
                } catch {}

                if (!label) {
                    label = await vscode.window.showInputBox({
                        prompt: 'Label for this server',
                        value: url.replace(/^https?:\/\//, '').replace(/:\d+$/, ''),
                    });
                }
            } else if (picked._peer) {
                url = 'http://' + picked._peer.ip + ':7777';
                label = picked._peer.hostname || picked._peer.ip;
            } else {
                return;
            }

            const server = registry.addServer(url, label);
            if (!server) {
                vscode.window.showWarningMessage('Server already exists.');
                return;
            }
            provider.refresh();
            probeAll(provider);
            if (onServersChanged) onServersChanged();
            vscode.window.showInformationMessage(`Added server "${label || url}"`);
        }),

        vscode.commands.registerCommand('be-conductor.removeServer', async (item) => {
            if (!item || !item.server || item.server.key === registry.LOCAL_KEY) return;
            const confirm = await vscode.window.showWarningMessage(
                `Remove server "${item.server.label}"?`, { modal: true }, 'Remove'
            );
            if (confirm !== 'Remove') return;
            registry.removeServer(item.server.key);
            probeCache.delete(item.server.key);
            provider.refresh();
            if (onServersChanged) onServersChanged();
        }),

        vscode.commands.registerCommand('be-conductor.toggleServer', async (item) => {
            if (!item || !item.server || item.server.key === registry.LOCAL_KEY) return;
            registry.setEnabled(item.server.key, !item.server.enabled);
            provider.refresh();
            if (onServersChanged) onServersChanged();
        }),

        vscode.commands.registerCommand('be-conductor.renameServer', async (item) => {
            if (!item || !item.server) return;
            const newLabel = await vscode.window.showInputBox({
                prompt: 'New label',
                value: item.server.label,
            });
            if (newLabel && newLabel.trim()) {
                registry.setLabel(item.server.key, newLabel.trim());
                provider.refresh();
            }
        }),

        vscode.commands.registerCommand('be-conductor.probeServer', async (item) => {
            if (!item || !item.server) return;
            try {
                const info = await api.getInfo(item.server.key);
                probeCache.set(item.server.key, info);
                vscode.window.showInformationMessage(`${item.server.label}: online (v${info.version})`);
            } catch (e) {
                probeCache.set(item.server.key, null);
                vscode.window.showWarningMessage(`${item.server.label}: offline — ${e.message}`);
            }
            provider.refresh();
        }),

        vscode.commands.registerCommand('be-conductor.scanTailscale', async () => {
            try {
                const peers = await api.getTailscalePeers('local');
                if (!peers || peers.length === 0) {
                    vscode.window.showInformationMessage('No Tailscale peers found.');
                    return;
                }
                const existingKeys = new Set(registry.getServers().map(s => s.key));
                const available = peers.filter(p => {
                    const key = p.ip + ':7777';
                    return !existingKeys.has(key) && p.online;
                });
                if (available.length === 0) {
                    vscode.window.showInformationMessage('All Tailscale peers already added.');
                    return;
                }
                const picks = available.map(p => ({
                    label: p.hostname || p.ip,
                    description: p.ip,
                    peer: p,
                }));
                const selected = await vscode.window.showQuickPick(picks, {
                    placeHolder: 'Select peers to add',
                    canPickMany: true,
                });
                if (!selected || selected.length === 0) return;
                for (const pick of selected) {
                    registry.addServer('http://' + pick.peer.ip + ':7777', pick.label);
                }
                provider.refresh();
                probeAll(provider);
                if (onServersChanged) onServersChanged();
            } catch (e) {
                vscode.window.showErrorMessage('Tailscale scan failed: ' + e.message);
            }
        }),
    );
}

module.exports = { ServerTreeProvider, registerServerCommands };
