'use strict';
const vscode = require('vscode');

const NAME_PATTERN = /^[a-zA-Z0-9][a-zA-Z0-9 _.~-]{0,63}$/;

const AGENTS = [
    { label: 'claude',   description: 'Claude Code',           command: 'claude' },
    { label: 'codex',    description: 'OpenAI Codex CLI',       command: 'codex' },
    { label: 'aider',    description: 'Aider',                  command: 'aider' },
    { label: 'gemini',   description: 'Gemini CLI',             command: 'gemini' },
    { label: 'copilot',  description: 'GitHub Copilot CLI',     command: 'copilot' },
    { label: 'opencode', description: 'OpenCode',               command: 'opencode' },
    { label: 'amp',      description: 'Amp (Sourcegraph)',      command: 'amp' },
    { label: 'goose',    description: 'Goose (Block)',           command: 'goose' },
    { label: 'forge',    description: 'Forge',                   command: 'forge' },
    { label: 'cursor',   description: 'Cursor Agent',           command: 'cursor' },
];

function getServerUrl() {
    return vscode.workspace.getConfiguration('be-conductor').get('serverUrl', 'http://127.0.0.1:7777');
}

function getPollInterval() {
    return vscode.workspace.getConfiguration('be-conductor').get('pollInterval', 5000);
}

module.exports = { NAME_PATTERN, AGENTS, getServerUrl, getPollInterval };
