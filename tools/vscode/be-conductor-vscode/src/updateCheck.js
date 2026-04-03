'use strict';
const vscode = require('vscode');
const https = require('https');
const path = require('path');
const fs = require('fs');
const os = require('os');

const GITHUB_API = 'https://api.github.com/repos/somniacs/be-conductor/releases/latest';
const VSIX_URL = 'https://github.com/somniacs/be-conductor/releases/latest/download/be-conductor.vsix';

function fetchJSON(url) {
    return new Promise((resolve, reject) => {
        https.get(url, { headers: { 'User-Agent': 'be-conductor-vscode' } }, (res) => {
            if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
                return fetchJSON(res.headers.location).then(resolve, reject);
            }
            let data = '';
            res.on('data', (c) => { data += c; });
            res.on('end', () => {
                try { resolve(JSON.parse(data)); } catch (e) { reject(e); }
            });
        }).on('error', reject);
    });
}

function downloadFile(url, dest) {
    return new Promise((resolve, reject) => {
        https.get(url, { headers: { 'User-Agent': 'be-conductor-vscode' } }, (res) => {
            if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
                return downloadFile(res.headers.location, dest).then(resolve, reject);
            }
            const stream = fs.createWriteStream(dest);
            res.pipe(stream);
            stream.on('finish', () => { stream.close(); resolve(); });
        }).on('error', reject);
    });
}

function compareVersions(a, b) {
    const pa = a.replace(/^v/, '').split('.').map(Number);
    const pb = b.replace(/^v/, '').split('.').map(Number);
    for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
        const na = pa[i] || 0, nb = pb[i] || 0;
        if (na > nb) return 1;
        if (na < nb) return -1;
    }
    return 0;
}

async function checkForUpdate(currentVersion) {
    try {
        const release = await fetchJSON(GITHUB_API);
        const latestVersion = (release.tag_name || '').replace(/^v/, '');
        if (!latestVersion || compareVersions(latestVersion, currentVersion) <= 0) return;

        const choice = await vscode.window.showInformationMessage(
            `be-conductor v${latestVersion} is available (you have v${currentVersion}).`,
            'Update Now', 'Later'
        );
        if (choice !== 'Update Now') return;

        const tmpPath = path.join(os.tmpdir(), 'be-conductor.vsix');
        await vscode.window.withProgress(
            { location: vscode.ProgressLocation.Notification, title: 'Downloading be-conductor update...' },
            async () => { await downloadFile(VSIX_URL, tmpPath); }
        );

        await vscode.commands.executeCommand('workbench.extensions.installExtension', vscode.Uri.file(tmpPath));
        const reload = await vscode.window.showInformationMessage(
            `be-conductor v${latestVersion} installed. Reload to activate.`,
            'Reload Now'
        );
        if (reload === 'Reload Now') {
            vscode.commands.executeCommand('workbench.action.reloadWindow');
        }
    } catch (e) {
        // Silent fail — update check is best-effort
    }
}

module.exports = { checkForUpdate };
