'use strict';
const http = require('http');
const { URL } = require('url');
const { getServerUrl } = require('./config');

/**
 * Make an HTTP request to the be-conductor API.
 * @param {string} method
 * @param {string} path
 * @param {object} [body]
 * @returns {Promise<any>}
 */
function request(method, path, body) {
    const base = getServerUrl();
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
async function getHealth() { return request('GET', '/health'); }

// Config (allowed commands, default directories)
async function getConfig() { return request('GET', '/config'); }

// Sessions
async function listSessions() { return request('GET', '/sessions'); }
async function getSession(id) { return request('GET', `/sessions/${encodeURIComponent(id)}`); }
async function createSession(body) { return request('POST', '/sessions/run', body); }
async function stopSession(id, mode) {
    return request('POST', `/sessions/${encodeURIComponent(id)}/stop`, { mode: mode || 'kill' });
}
async function deleteSession(id) { return request('DELETE', `/sessions/${encodeURIComponent(id)}`); }
async function resumeSession(id, body) { return request('POST', `/sessions/${encodeURIComponent(id)}/resume`, body); }
async function resizeSession(id, rows, cols) {
    return request('POST', `/sessions/${encodeURIComponent(id)}/resize`, { rows, cols, source: 'vscode' });
}

// Git
async function checkGit(path) { return request('GET', `/git/check?path=${encodeURIComponent(path)}`); }

// Worktrees
async function listWorktrees(repo) {
    const qs = repo ? `?repo=${encodeURIComponent(repo)}` : '';
    return request('GET', `/worktrees${qs}`);
}
async function getWorktree(name) { return request('GET', `/worktrees/${encodeURIComponent(name)}`); }
async function getWorktreeDiff(name, files) {
    const qs = files ? '?files=true' : '';
    return request('GET', `/worktrees/${encodeURIComponent(name)}/diff${qs}`);
}
async function getWorktreeRichDiff(name) {
    return request('GET', `/worktrees/${encodeURIComponent(name)}/diff?format=rich`);
}
async function finalizeWorktree(name) {
    return request('POST', `/worktrees/${encodeURIComponent(name)}/finalize`);
}
async function previewMerge(name) {
    return request('POST', `/worktrees/${encodeURIComponent(name)}/merge/preview`);
}
async function executeMerge(name, strategy, message) {
    return request('POST', `/worktrees/${encodeURIComponent(name)}/merge`, { strategy, message });
}
async function deleteWorktree(name, force) {
    return request('DELETE', `/worktrees/${encodeURIComponent(name)}?force=${!!force}`);
}
async function worktreeGC(dryRun, maxAgeDays) {
    return request('POST', '/worktrees/gc', { dry_run: !!dryRun, max_age_days: maxAgeDays || 7.0 });
}

module.exports = {
    getHealth, getConfig,
    listSessions, getSession, createSession, stopSession, deleteSession,
    resumeSession, resizeSession,
    checkGit,
    listWorktrees, getWorktree, getWorktreeDiff, getWorktreeRichDiff,
    finalizeWorktree, previewMerge, executeMerge, deleteWorktree, worktreeGC,
};
