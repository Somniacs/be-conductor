'use strict';
const vscode = require('vscode');

const LOCAL_KEY = 'local';
const DEFAULT_LOCAL_URL = 'http://127.0.0.1:7777';
const STORAGE_KEY = 'be-conductor.servers';

let _state = null; // vscode.Memento (workspaceState)

/** @typedef {{ key: string, label: string, url: string|null, enabled: boolean }} Server */

/** @type {Server[]} */
let servers = [];

function init(workspaceState) {
    _state = workspaceState;
    load();
}

function load() {
    servers = [];
    if (_state) {
        try {
            const stored = _state.get(STORAGE_KEY);
            if (Array.isArray(stored)) servers = stored;
        } catch {}
    }
    if (!servers.find(s => s.key === LOCAL_KEY)) {
        servers.unshift({ key: LOCAL_KEY, label: 'This Machine', url: null, enabled: true });
    }
}

function save() {
    if (_state) _state.update(STORAGE_KEY, servers);
}

// ── Queries ──────────────────────────────────────────────────────────

function getServers() { return [...servers]; }

function getEnabledServers() { return servers.filter(s => s.enabled); }

function isMultiServer() { return servers.filter(s => s.enabled).length > 1; }

function getServer(key) { return servers.find(s => s.key === key) || null; }

// ── URL resolution ───────────────────────────────────────────────────

function serverUrl(serverKey, path) {
    if (!serverKey || serverKey === LOCAL_KEY) {
        return (vscode.workspace.getConfiguration('be-conductor').get('serverUrl', DEFAULT_LOCAL_URL)) + path;
    }
    const server = getServer(serverKey);
    if (!server || !server.url) return DEFAULT_LOCAL_URL + path;
    return server.url + path;
}

function getBaseUrl(serverKey) {
    return serverUrl(serverKey, '');
}

// ── Mutations ────────────────────────────────────────────────────────

function addServer(url, label) {
    url = url.replace(/\/+$/, '');
    if (!/^https?:\/\//.test(url)) url = 'http://' + url;
    const key = url.replace(/^https?:\/\//, '');
    if (servers.find(s => s.key === key)) return null;
    const server = { key, label: label || key, url, enabled: true };
    servers.push(server);
    save();
    return server;
}

function removeServer(key) {
    if (key === LOCAL_KEY) return false;
    const idx = servers.findIndex(s => s.key === key);
    if (idx < 0) return false;
    servers.splice(idx, 1);
    save();
    return true;
}

function setEnabled(key, enabled) {
    const s = getServer(key);
    if (s) { s.enabled = enabled; save(); }
}

function setLabel(key, label) {
    const s = getServer(key);
    if (s) { s.label = label; save(); }
}

// ── Compound IDs ─────────────────────────────────────────────────────

function compoundId(serverKey, sessionId) {
    if (!isMultiServer()) return sessionId;
    return serverKey + '::' + sessionId;
}

function parseCompoundId(id) {
    const sep = id.indexOf('::');
    if (sep < 0) return { serverKey: LOCAL_KEY, sessionId: id };
    return { serverKey: id.substring(0, sep), sessionId: id.substring(sep + 2) };
}

module.exports = {
    init, getServers, getEnabledServers, isMultiServer, getServer,
    serverUrl, getBaseUrl, addServer, removeServer, setEnabled, setLabel,
    compoundId, parseCompoundId, LOCAL_KEY,
};
