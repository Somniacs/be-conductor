'use strict';
const http = require('http');
const { URL } = require('url');
const registry = require('./serverRegistry');

/**
 * Make an HTTP request to a be-conductor server.
 * @param {string} serverKey - Server key (e.g. 'local' or '192.168.1.50:7777')
 * @param {string} method
 * @param {string} path
 * @param {object} [body]
 * @returns {Promise<any>}
 */
function request(serverKey, method, path, body) {
    const base = registry.getBaseUrl(serverKey);
    const url = new URL(path, base);
    const token = process.env.BE_CONDUCTOR_TOKEN || process.env.CONDUCTOR_TOKEN || null;

    return new Promise((resolve, reject) => {
        const options = {
            hostname: url.hostname,
            port: url.port,
            path: url.pathname + url.search,
            method,
            headers: { 'Content-Type': 'application/json' },
            timeout: 10000,
        };
        if (token) {
            options.headers['Authorization'] = `Bearer ${token}`;
        }

        const req = http.request(options, (res) => {
            let data = '';
            res.on('data', (chunk) => { data += chunk; });
            res.on('end', () => {
                if (res.statusCode >= 200 && res.statusCode < 300) {
                    try {
                        resolve(JSON.parse(data));
                    } catch {
                        resolve(data);
                    }
                } else {
                    let detail = data;
                    try { detail = JSON.parse(data).detail || data; } catch {}
                    reject(new Error(`${res.statusCode}: ${detail}`));
                }
            });
        });
        req.on('error', reject);
        req.on('timeout', () => { req.destroy(); reject(new Error('Request timed out')); });
        if (body) req.write(JSON.stringify(body));
        req.end();
    });
}

// Health / connectivity
async function getHealth(sk) { return request(sk || 'local', 'GET', '/health'); }
async function getInfo(sk) { return request(sk || 'local', 'GET', '/info'); }

// Config
async function getConfig(sk) { return request(sk || 'local', 'GET', '/config'); }

/**
 * Fetch the OpenCode model catalogue. Returns an object with shape
 *   { models: [{value, label, provider_id, model_id, current}], url, error }
 * or null on error. Callers should fall back to Claude-only when the
 * response is null or models is empty.
 */
async function getAgentProviderModels(sk, provider) {
    return request(sk || 'local', 'GET', `/agent-providers/${encodeURIComponent(provider)}/models`);
}

// Sessions
async function listSessions(sk) { return request(sk || 'local', 'GET', '/sessions'); }
async function getSession(sk, id) { return request(sk, 'GET', `/sessions/${encodeURIComponent(id)}`); }
async function createSession(sk, body) { return request(sk, 'POST', '/sessions/run', body); }
async function stopSession(sk, id, mode) {
    return request(sk, 'POST', `/sessions/${encodeURIComponent(id)}/stop`, { mode: mode || 'kill' });
}
async function deleteSession(sk, id) { return request(sk, 'DELETE', `/sessions/${encodeURIComponent(id)}`); }
async function resumeSession(sk, id, body) { return request(sk, 'POST', `/sessions/${encodeURIComponent(id)}/resume`, body); }
async function resizeSession(sk, id, rows, cols) {
    return request(sk, 'POST', `/sessions/${encodeURIComponent(id)}/resize`, { rows, cols, source: 'vscode' });
}
async function cloneSession(sk, id, body) {
    return request(sk, 'POST', `/sessions/${encodeURIComponent(id)}/clone`, body);
}

// Git
async function checkGit(sk, path) { return request(sk, 'GET', `/git/check?path=${encodeURIComponent(path)}`); }

// Worktrees
async function listWorktrees(sk, repo) {
    const qs = repo ? `?repo=${encodeURIComponent(repo)}` : '';
    return request(sk || 'local', 'GET', `/worktrees${qs}`);
}
async function getWorktree(sk, name) { return request(sk, 'GET', `/worktrees/${encodeURIComponent(name)}`); }
async function getWorktreeDiff(sk, name, files) {
    const qs = files ? '?files=true' : '';
    return request(sk, 'GET', `/worktrees/${encodeURIComponent(name)}/diff${qs}`);
}
async function getWorktreeRichDiff(sk, name) {
    return request(sk, 'GET', `/worktrees/${encodeURIComponent(name)}/diff?format=rich`);
}
async function finalizeWorktree(sk, name) {
    return request(sk, 'POST', `/worktrees/${encodeURIComponent(name)}/finalize`);
}
async function previewMerge(sk, name) {
    return request(sk, 'POST', `/worktrees/${encodeURIComponent(name)}/merge/preview`);
}
async function executeMerge(sk, name, strategy, message) {
    return request(sk, 'POST', `/worktrees/${encodeURIComponent(name)}/merge`, { strategy, message });
}
async function deleteWorktree(sk, name, force) {
    return request(sk, 'DELETE', `/worktrees/${encodeURIComponent(name)}?force=${!!force}`);
}
async function worktreeGC(sk, dryRun, maxAgeDays) {
    return request(sk || 'local', 'POST', '/worktrees/gc', { dry_run: !!dryRun, max_age_days: maxAgeDays || 7.0 });
}

// Tailscale
async function getTailscalePeers(sk) { return request(sk || 'local', 'GET', '/tailscale/peers'); }

module.exports = {
    getHealth, getInfo, getConfig, getAgentProviderModels,
    listSessions, getSession, createSession, stopSession, deleteSession,
    resumeSession, resizeSession, cloneSession,
    checkGit,
    listWorktrees, getWorktree, getWorktreeDiff, getWorktreeRichDiff,
    finalizeWorktree, previewMerge, executeMerge, deleteWorktree, worktreeGC,
    getTailscalePeers,
};
