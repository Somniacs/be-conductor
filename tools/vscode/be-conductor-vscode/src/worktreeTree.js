'use strict';
const vscode = require('vscode');
const path = require('path');
const api = require('./api');

class WorktreeItem extends vscode.TreeItem {
    constructor(worktree) {
        super(worktree.name, vscode.TreeItemCollapsibleState.None);

        this.worktree = worktree;

        const parts = [worktree.branch];
        if (worktree.commits_ahead > 0) parts.push(`${worktree.commits_ahead} ahead`);
        if (worktree.has_changes) parts.push('uncommitted');
        this.description = parts.join(' | ');

        this.tooltip = `${worktree.name}\n` +
            `Branch: ${worktree.branch}\n` +
            `Base: ${worktree.base_branch}\n` +
            `Status: ${worktree.status}\n` +
            `Path: ${worktree.worktree_path}\n` +
            `Commits ahead: ${worktree.commits_ahead}` +
            (worktree.has_changes ? '\nHas uncommitted changes' : '');

        switch (worktree.status) {
            case 'active':
                this.iconPath = new vscode.ThemeIcon('git-branch', new vscode.ThemeColor('charts.blue'));
                this.contextValue = 'worktree-active';
                break;
            case 'finalized':
                this.iconPath = new vscode.ThemeIcon('check', new vscode.ThemeColor('testing.iconPassed'));
                this.contextValue = 'worktree-finalized';
                break;
            case 'orphaned':
            case 'stale':
                this.iconPath = new vscode.ThemeIcon('warning', new vscode.ThemeColor('problemsWarningIcon.foreground'));
                this.contextValue = 'worktree-orphaned';
                break;
            default:
                this.iconPath = new vscode.ThemeIcon('git-branch');
                this.contextValue = `worktree-${worktree.status}`;
        }
    }
}

class WorktreeTreeProvider {
    constructor() {
        this._onDidChangeTreeData = new vscode.EventEmitter();
        this.onDidChangeTreeData = this._onDidChangeTreeData.event;
        this._worktrees = [];
        this._offline = false;
    }

    refresh() {
        api.listWorktrees()
            .then((worktrees) => {
                this._worktrees = worktrees;
                this._offline = false;
                this._onDidChangeTreeData.fire();
            })
            .catch(() => {
                this._worktrees = [];
                this._offline = true;
                this._onDidChangeTreeData.fire();
            });
    }

    getTreeItem(element) {
        return element;
    }

    getChildren() {
        if (this._offline) {
            return [];
        }
        if (this._worktrees.length === 0) {
            return [];
        }
        // Active first, then finalized, then orphaned/stale
        const order = { active: 0, finalized: 1, orphaned: 2, stale: 3 };
        const sorted = [...this._worktrees].sort((a, b) =>
            (order[a.status] ?? 4) - (order[b.status] ?? 4)
        );
        return sorted.map((wt) => new WorktreeItem(wt));
    }
}

/**
 * Show a rich diff for a worktree using VSCode's native diff editor.
 * Falls back to unified diff if the rich endpoint is unavailable.
 * @param {string} name - worktree name
 * @param {DiffContentProvider} diffProvider
 */
async function showRichDiff(name, diffProvider) {
    try {
        const richDiff = await api.getWorktreeRichDiff(name);
        if (!richDiff.files || richDiff.files.length === 0) {
            vscode.window.showInformationMessage(`Worktree "${name}" has no changes.`);
            return;
        }

        // Show each file diff using VSCode's native diff editor
        for (const file of richDiff.files) {
            const baseContent = file.base_content || '';
            const headContent = file.head_content || '';
            const fileName = path.basename(file.path);

            const baseUri = vscode.Uri.parse(
                `be-conductor-diff:/${encodeURIComponent(name)}/base/${encodeURIComponent(file.path)}`
            );
            const headUri = vscode.Uri.parse(
                `be-conductor-diff:/${encodeURIComponent(name)}/head/${encodeURIComponent(file.path)}`
            );

            // Cache the content
            diffProvider.setContent(baseUri, baseContent);
            diffProvider.setContent(headUri, headContent);

            const statusLabel = file.status === 'added' ? 'Added'
                : file.status === 'deleted' ? 'Deleted'
                : file.status === 'renamed' ? 'Renamed'
                : 'Modified';

            await vscode.commands.executeCommand('vscode.diff',
                baseUri,
                headUri,
                `${fileName} (${statusLabel}) - ${name}`,
                { preview: richDiff.files.length === 1 }
            );
        }
    } catch {
        // Fall back to unified diff
        showUnifiedDiff(name, diffProvider);
    }
}

/**
 * Fallback: show unified diff as a text document.
 * @param {string} name
 * @param {DiffContentProvider} diffProvider
 */
async function showUnifiedDiff(name, diffProvider) {
    try {
        const uri = vscode.Uri.parse(
            `be-conductor-diff:/${encodeURIComponent(name)}.diff`
        );
        diffProvider.invalidate(uri);
        const doc = await vscode.workspace.openTextDocument(uri);
        await vscode.window.showTextDocument(doc, { preview: true });
        await vscode.languages.setTextDocumentLanguage(doc, 'diff');
    } catch (e) {
        vscode.window.showErrorMessage(`Failed to load diff: ${e.message}`);
    }
}

/** Virtual document provider for displaying diffs. */
class DiffContentProvider {
    constructor() {
        this._onDidChange = new vscode.EventEmitter();
        this.onDidChange = this._onDidChange.event;
        this._cache = new Map();
    }

    /** Set content for a URI (used by rich diff). */
    setContent(uri, content) {
        this._cache.set(uri.toString(), content);
        this._onDidChange.fire(uri);
    }

    invalidate(uri) {
        this._cache.delete(uri.toString());
        this._onDidChange.fire(uri);
    }

    async provideTextDocumentContent(uri) {
        const key = uri.toString();
        if (this._cache.has(key)) return this._cache.get(key);

        // Fallback: load unified diff for .diff URIs
        const uriPath = uri.path.replace(/^\//, '');
        if (uriPath.endsWith('.diff')) {
            const name = decodeURIComponent(uriPath.replace(/\.diff$/, ''));
            try {
                const data = await api.getWorktreeDiff(name, false);
                const content = data.diff || '(no changes)';
                this._cache.set(key, content);
                return content;
            } catch (e) {
                return `Error loading diff: ${e.message}`;
            }
        }

        // For rich diff URIs, content should already be cached via setContent()
        return '';
    }
}

/**
 * Register worktree tree commands.
 * @param {vscode.ExtensionContext} context
 * @param {WorktreeTreeProvider} provider
 * @param {DiffContentProvider} diffProvider
 * @param {Function} refreshAll - callback to refresh both trees
 */
function registerWorktreeCommands(context, provider, diffProvider, refreshAll) {
    context.subscriptions.push(
        vscode.commands.registerCommand('be-conductor.finalizeWorktree', async (item) => {
            if (!(item instanceof WorktreeItem)) return;
            try {
                const result = await api.finalizeWorktree(item.worktree.name);
                vscode.window.showInformationMessage(
                    `Worktree "${item.worktree.name}" finalized. ${result.commits_ahead || 0} commit(s) ahead.`
                );
                refreshAll();
            } catch (e) {
                vscode.window.showErrorMessage(`Finalize failed: ${e.message}`);
            }
        }),

        vscode.commands.registerCommand('be-conductor.viewDiff', async (item) => {
            if (!(item instanceof WorktreeItem)) return;
            await showRichDiff(item.worktree.name, diffProvider);
        }),

        vscode.commands.registerCommand('be-conductor.mergeWorktree', async (item) => {
            if (!(item instanceof WorktreeItem)) return;

            // Fetch merge preview
            let preview;
            try {
                preview = await api.previewMerge(item.worktree.name);
            } catch (e) {
                vscode.window.showErrorMessage(`Failed to preview merge: ${e.message}`);
                return;
            }

            if (!preview.can_merge) {
                vscode.window.showWarningMessage(preview.message || 'Nothing to merge.');
                return;
            }

            // Build info for the picker
            let detail = `${preview.commits_ahead} commit(s) ahead, ${preview.commits_behind} behind.`;
            if (preview.changed_files) {
                detail += ` ${preview.changed_files.length} file(s) changed.`;
            }
            if (preview.conflict_files && preview.conflict_files.length > 0) {
                detail += ` WARNING: ${preview.conflict_files.length} conflict(s)!`;
            }

            const strategies = [
                { label: 'Squash', description: 'Combine all commits into one', _strategy: 'squash' },
                { label: 'Merge', description: 'Create a merge commit', _strategy: 'merge' },
                { label: 'Rebase', description: 'Replay commits onto base', _strategy: 'rebase' },
            ];

            const picked = await vscode.window.showQuickPick(strategies, {
                placeHolder: detail,
                title: `Merge "${item.worktree.name}" into ${item.worktree.base_branch}`,
            });
            if (!picked) return;

            try {
                const result = await api.executeMerge(item.worktree.name, picked._strategy);
                if (result.success) {
                    vscode.window.showInformationMessage(
                        `Merged "${item.worktree.name}" into ${result.target_branch} (${result.strategy}): ${result.commits_merged} commit(s)`
                    );
                } else {
                    vscode.window.showErrorMessage(`Merge failed: ${result.message}`);
                }
                refreshAll();
            } catch (e) {
                vscode.window.showErrorMessage(`Merge failed: ${e.message}`);
            }
        }),

        vscode.commands.registerCommand('be-conductor.deleteWorktree', async (item) => {
            if (!(item instanceof WorktreeItem)) return;

            const answer = await vscode.window.showWarningMessage(
                `Delete worktree "${item.worktree.name}" and its branch "${item.worktree.branch}"?`,
                { modal: true },
                'Delete',
                'Force Delete'
            );
            if (!answer) return;

            try {
                await api.deleteWorktree(item.worktree.name, answer === 'Force Delete');
                vscode.window.showInformationMessage(`Worktree "${item.worktree.name}" deleted.`);
                refreshAll();
            } catch (e) {
                vscode.window.showErrorMessage(`Delete failed: ${e.message}`);
            }
        }),

        vscode.commands.registerCommand('be-conductor.gcWorktrees', async () => {
            try {
                // Dry run first
                const preview = await api.worktreeGC(true);
                if (!preview || preview.length === 0) {
                    vscode.window.showInformationMessage('No stale worktrees to clean up.');
                    return;
                }

                const names = preview.map((a) => a.name || a.worktree || 'unknown');
                const answer = await vscode.window.showWarningMessage(
                    `Remove ${preview.length} stale worktree(s)?\n${names.join(', ')}`,
                    { modal: true },
                    'Clean Up'
                );
                if (answer !== 'Clean Up') return;

                const result = await api.worktreeGC(false);
                vscode.window.showInformationMessage(`Cleaned up ${result.length} worktree(s).`);
                refreshAll();
            } catch (e) {
                vscode.window.showErrorMessage(`GC failed: ${e.message}`);
            }
        }),
    );
}

module.exports = { WorktreeTreeProvider, DiffContentProvider, registerWorktreeCommands };
