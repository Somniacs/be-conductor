package com.somniacs.beconductor.api;

import java.util.List;
import java.util.Map;

/**
 * Data classes mirroring the be-conductor REST API request/response shapes.
 * Serialized with Gson.
 */
public final class ApiModels {

    private ApiModels() {}

    // ── Requests ──────────────────────────────────────────────────────────

    public static class RunRequest {
        public String name;
        public String command;
        public String cwd;
        public boolean worktree;
        public Integer rows;
        public Integer cols;
        public String session_type;  // "pty" or "agent"

        public RunRequest(String name, String command, String cwd, boolean worktree) {
            this.name = name;
            this.command = command;
            this.cwd = cwd;
            this.worktree = worktree;
            this.session_type = "pty";
        }

        public RunRequest(String name, String command, String cwd, boolean worktree, String sessionType) {
            this(name, command, cwd, worktree);
            this.session_type = sessionType != null ? sessionType : "pty";
        }

        public RunRequest(String name, String command, String cwd, boolean worktree, int rows, int cols) {
            this(name, command, cwd, worktree);
            this.rows = rows;
            this.cols = cols;
        }
    }

    public static class StopRequest {
        public String mode;

        public StopRequest(String mode) {
            this.mode = mode;
        }
    }

    public static class MergeRequest {
        public String strategy;
        public String message;

        public MergeRequest(String strategy, String message) {
            this.strategy = strategy;
            this.message = message;
        }
    }

    public static class GCRequest {
        public boolean dry_run;
        public double max_age_days;

        public GCRequest(boolean dryRun, double maxAgeDays) {
            this.dry_run = dryRun;
            this.max_age_days = maxAgeDays;
        }
    }

    public static class CloneRequest {
        public String name;
        public boolean raw;

        public CloneRequest(String name) {
            this.name = name;
        }

        public CloneRequest(String name, boolean raw) {
            this.name = name;
            this.raw = raw;
        }
    }

    // ── Responses ─────────────────────────────────────────────────────────

    public static class HealthResponse {
        public boolean ok;
        public String version;
    }

    public static class ConfigResponse {
        public List<CommandConfig> allowed_commands;
        public List<String> default_directories;
    }

    public static class CommandConfig {
        public String command;
        public String label;
        public String resume_pattern;
        public String resume_flag;
        public String resume_command;
    }

    public static class SessionResponse {
        public String id;
        public String name;
        public String command;
        public String status;
        public Integer pid;
        public Double start_time;
        public String created_at;
        public Integer exit_code;
        public String cwd;
        public String resume_id;
        public String resume_flag;
        public String resume_command;
        public String ws_url;
        public String session_type;  // "pty" or "agent"
        public Map<String, Object> worktree;
        public List<AttachedClient> attached_clients;

        /** @return true if this is an agent (SDK) session rather than a PTY terminal session */
        public boolean isAgent() {
            return "agent".equals(session_type);
        }
    }

    public static class AttachedClient {
        public String client_id;
        public String source;
    }

    public static class StatusResponse {
        public String status;
    }

    public static class CloneResponse {
        public String status;
        public String clone_id;
        public String parent_id;
        public String name;
    }

    public static class GitCheckResponse {
        public boolean is_git;
        public String repo_root;
        public String current_branch;
        public boolean has_remote;
        public int existing_worktrees;
        public int stale_worktrees;
    }

    public static class WorktreeInfo {
        public String name;
        public String repo_path;
        public String worktree_path;
        public String branch;
        public String base_branch;
        public String base_commit;
        public String session_id;
        public String status;
        public double created_at;
        public double last_activity;
        public int commits_ahead;
        public boolean has_changes;
    }

    public static class DiffResponse {
        public String diff;
        public List<Map<String, Object>> files;
    }

    public static class RichDiffResponse {
        public List<FileDiff> files;
    }

    public static class FileDiff {
        public String path;
        public String status;
        public String base_content;
        public String head_content;
    }

    public static class MergePreview {
        public boolean can_merge;
        public int commits_ahead;
        public int commits_behind;
        public List<String> conflict_files;
        public List<Map<String, Object>> changed_files;
        public String message;
    }

    public static class MergeResult {
        public boolean success;
        public String strategy;
        public String merged_branch;
        public String target_branch;
        public int commits_merged;
        public List<String> conflict_files;
        public String message;
    }
}
