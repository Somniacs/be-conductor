package com.somniacs.beconductor.api;

import com.google.gson.Gson;
import com.google.gson.reflect.TypeToken;
import com.intellij.openapi.application.ApplicationManager;
import com.intellij.openapi.components.Service;
import com.intellij.openapi.diagnostic.Logger;

import java.lang.reflect.Type;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.List;

/**
 * HTTP client for the be-conductor REST API.
 * Registered as an application-level service (singleton).
 *
 * All public methods accept a {@code serverKey} as the first parameter
 * to route requests to the correct server via {@link ServerRegistry}.
 * Pass {@code "local"} or {@code null} for the local server.
 */
@Service(Service.Level.APP)
public final class BeConductorClient {

    private static final Logger LOG = Logger.getInstance(BeConductorClient.class);
    private static final Duration TIMEOUT = Duration.ofSeconds(10);

    private final HttpClient httpClient;
    private final Gson gson;

    public BeConductorClient() {
        this.httpClient = HttpClient.newBuilder()
                .connectTimeout(TIMEOUT)
                .version(HttpClient.Version.HTTP_1_1)
                .build();
        this.gson = new Gson();
    }

    public static BeConductorClient getInstance() {
        return ApplicationManager.getApplication().getService(BeConductorClient.class);
    }

    private String resolveUrl(String serverKey, String path) {
        return ServerRegistry.getInstance().serverUrl(serverKey, path);
    }

    private String getAuthToken() {
        String token = System.getenv("BE_CONDUCTOR_TOKEN");
        if (token == null) token = System.getenv("CONDUCTOR_TOKEN");
        return token;
    }

    private static String encode(String value) {
        return URLEncoder.encode(value, StandardCharsets.UTF_8).replace("+", "%20");
    }

    // ── Generic request helpers ──────────────────────────────────────────

    private <T> T doGet(String serverKey, String path, Class<T> responseType) throws Exception {
        HttpRequest.Builder builder = HttpRequest.newBuilder()
                .uri(URI.create(resolveUrl(serverKey, path)))
                .timeout(TIMEOUT)
                .GET();
        addAuth(builder);
        HttpResponse<String> resp = httpClient.send(builder.build(),
                HttpResponse.BodyHandlers.ofString());
        checkStatus(resp);
        return gson.fromJson(resp.body(), responseType);
    }

    private <T> T doGet(String serverKey, String path, Type responseType) throws Exception {
        HttpRequest.Builder builder = HttpRequest.newBuilder()
                .uri(URI.create(resolveUrl(serverKey, path)))
                .timeout(TIMEOUT)
                .GET();
        addAuth(builder);
        HttpResponse<String> resp = httpClient.send(builder.build(),
                HttpResponse.BodyHandlers.ofString());
        checkStatus(resp);
        return gson.fromJson(resp.body(), responseType);
    }

    private <T> T doPost(String serverKey, String path, Object body, Class<T> responseType) throws Exception {
        String json = body != null ? gson.toJson(body) : "{}";
        HttpRequest.Builder builder = HttpRequest.newBuilder()
                .uri(URI.create(resolveUrl(serverKey, path)))
                .timeout(TIMEOUT)
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(json));
        addAuth(builder);
        HttpResponse<String> resp = httpClient.send(builder.build(),
                HttpResponse.BodyHandlers.ofString());
        checkStatus(resp);
        if (responseType == Void.class) return null;
        return gson.fromJson(resp.body(), responseType);
    }

    private <T> T doPost(String serverKey, String path, Object body, Type responseType) throws Exception {
        String json = body != null ? gson.toJson(body) : "{}";
        HttpRequest.Builder builder = HttpRequest.newBuilder()
                .uri(URI.create(resolveUrl(serverKey, path)))
                .timeout(TIMEOUT)
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(json));
        addAuth(builder);
        HttpResponse<String> resp = httpClient.send(builder.build(),
                HttpResponse.BodyHandlers.ofString());
        checkStatus(resp);
        return gson.fromJson(resp.body(), responseType);
    }

    private <T> T doDelete(String serverKey, String path, Class<T> responseType) throws Exception {
        HttpRequest.Builder builder = HttpRequest.newBuilder()
                .uri(URI.create(resolveUrl(serverKey, path)))
                .timeout(TIMEOUT)
                .DELETE();
        addAuth(builder);
        HttpResponse<String> resp = httpClient.send(builder.build(),
                HttpResponse.BodyHandlers.ofString());
        checkStatus(resp);
        if (responseType == Void.class) return null;
        return gson.fromJson(resp.body(), responseType);
    }

    private void addAuth(HttpRequest.Builder builder) {
        String token = getAuthToken();
        if (token != null && !token.isEmpty()) {
            builder.header("Authorization", "Bearer " + token);
        }
    }

    private void checkStatus(HttpResponse<String> resp) throws ApiException {
        if (resp.statusCode() < 200 || resp.statusCode() >= 300) {
            String detail = resp.body();
            try {
                var err = gson.fromJson(resp.body(), ErrorResponse.class);
                if (err != null && err.detail != null) detail = err.detail;
            } catch (Exception ignored) {}
            throw new ApiException(resp.statusCode(), detail);
        }
    }

    private static class ErrorResponse {
        String detail;
    }

    public static class ApiException extends Exception {
        public final int statusCode;
        public ApiException(int statusCode, String message) {
            super(message);
            this.statusCode = statusCode;
        }
    }

    // ── Health ────────────────────────────────────────────────────────────

    public ApiModels.HealthResponse getHealth(String serverKey) throws Exception {
        return doGet(serverKey, "/health", ApiModels.HealthResponse.class);
    }

    public boolean isServerRunning(String serverKey) {
        try {
            getHealth(serverKey);
            return true;
        } catch (Exception e) {
            return false;
        }
    }

    /** Convenience: check local server. */
    public boolean isServerRunning() {
        return isServerRunning("local");
    }

    // ── Info ──────────────────────────────────────────────────────────────

    public ApiModels.InfoResponse getInfo(String serverKey) throws Exception {
        return doGet(serverKey, "/info", ApiModels.InfoResponse.class);
    }

    // ── Config ────────────────────────────────────────────────────────────

    public ApiModels.ConfigResponse getConfig(String serverKey) throws Exception {
        return doGet(serverKey, "/config", ApiModels.ConfigResponse.class);
    }

    // ── Sessions ──────────────────────────────────────────────────────────

    public List<ApiModels.SessionResponse> listSessions(String serverKey) throws Exception {
        Type type = new TypeToken<List<ApiModels.SessionResponse>>() {}.getType();
        return doGet(serverKey, "/sessions", type);
    }

    public ApiModels.SessionResponse createSession(String serverKey, ApiModels.RunRequest request) throws Exception {
        return doPost(serverKey, "/sessions/run", request, ApiModels.SessionResponse.class);
    }

    public ApiModels.StatusResponse stopSession(String serverKey, String sessionId, String mode) throws Exception {
        return doPost(serverKey, "/sessions/" + encode(sessionId) + "/stop",
                new ApiModels.StopRequest(mode), ApiModels.StatusResponse.class);
    }

    public ApiModels.StatusResponse deleteSession(String serverKey, String sessionId) throws Exception {
        return doDelete(serverKey, "/sessions/" + encode(sessionId), ApiModels.StatusResponse.class);
    }

    public ApiModels.SessionResponse resumeSession(String serverKey, String sessionId) throws Exception {
        return resumeSession(serverKey, sessionId, 0, 0);
    }

    public ApiModels.SessionResponse resumeSession(String serverKey, String sessionId, int rows, int cols) throws Exception {
        java.util.Map<String, Object> body = null;
        if (rows > 0 && cols > 0) {
            body = new java.util.HashMap<>();
            body.put("rows", rows);
            body.put("cols", cols);
        }
        return doPost(serverKey, "/sessions/" + encode(sessionId) + "/resume",
                body, ApiModels.SessionResponse.class);
    }

    public ApiModels.CloneResponse cloneSession(String serverKey, String sessionId, ApiModels.CloneRequest request) throws Exception {
        return doPost(serverKey, "/sessions/" + encode(sessionId) + "/clone",
                request, ApiModels.CloneResponse.class);
    }

    // ── Git ───────────────────────────────────────────────────────────────

    public ApiModels.GitCheckResponse checkGit(String serverKey, String path) throws Exception {
        return doGet(serverKey, "/git/check?path=" + encode(path), ApiModels.GitCheckResponse.class);
    }

    /**
     * Fetch the OpenCode model catalogue (or any other provider's, in the
     * future) so the new-session dialog can populate a model dropdown.
     * Returns null on any failure — callers should fall back gracefully
     * (Claude-only) when the response is null or models is empty.
     */
    public ApiModels.AgentProviderModelsResponse getAgentProviderModels(String serverKey, String provider) throws Exception {
        return doGet(serverKey, "/agent-providers/" + encode(provider) + "/models",
                ApiModels.AgentProviderModelsResponse.class);
    }

    /**
     * Fetch the ACP agent catalogue (acp-claude, acp-codex, acp-gemini)
     * so the new-session dialog can list the Agent Client Protocol
     * backends. Returns null on failure — callers fall back gracefully.
     */
    public ApiModels.AcpAgentsResponse getAcpAgents(String serverKey) throws Exception {
        return doGet(serverKey, "/agent-providers/acp/agents",
                ApiModels.AcpAgentsResponse.class);
    }

    // ── Worktrees ─────────────────────────────────────────────────────────

    public List<ApiModels.WorktreeInfo> listWorktrees(String serverKey) throws Exception {
        Type type = new TypeToken<List<ApiModels.WorktreeInfo>>() {}.getType();
        return doGet(serverKey, "/worktrees", type);
    }

    public ApiModels.WorktreeInfo getWorktree(String serverKey, String name) throws Exception {
        return doGet(serverKey, "/worktrees/" + encode(name), ApiModels.WorktreeInfo.class);
    }

    public ApiModels.DiffResponse getWorktreeDiff(String serverKey, String name) throws Exception {
        return doGet(serverKey, "/worktrees/" + encode(name) + "/diff", ApiModels.DiffResponse.class);
    }

    public ApiModels.RichDiffResponse getWorktreeRichDiff(String serverKey, String name) throws Exception {
        return doGet(serverKey, "/worktrees/" + encode(name) + "/diff?format=rich",
                ApiModels.RichDiffResponse.class);
    }

    public ApiModels.WorktreeInfo finalizeWorktree(String serverKey, String name) throws Exception {
        return doPost(serverKey, "/worktrees/" + encode(name) + "/finalize", null, ApiModels.WorktreeInfo.class);
    }

    public ApiModels.MergePreview previewMerge(String serverKey, String name) throws Exception {
        return doPost(serverKey, "/worktrees/" + encode(name) + "/merge/preview", null, ApiModels.MergePreview.class);
    }

    public ApiModels.MergeResult executeMerge(String serverKey, String name, String strategy, String message) throws Exception {
        return doPost(serverKey, "/worktrees/" + encode(name) + "/merge",
                new ApiModels.MergeRequest(strategy, message), ApiModels.MergeResult.class);
    }

    public void deleteWorktree(String serverKey, String name, boolean force) throws Exception {
        doDelete(serverKey, "/worktrees/" + encode(name) + "?force=" + force, Void.class);
    }

    public List<?> worktreeGC(String serverKey, boolean dryRun, double maxAgeDays) throws Exception {
        Type type = new TypeToken<List<Object>>() {}.getType();
        return doPost(serverKey, "/worktrees/gc", new ApiModels.GCRequest(dryRun, maxAgeDays), type);
    }

    // ── Tailscale ─────────────────────────────────────────────────────────

    public List<ApiModels.TailscalePeer> getTailscalePeers(String serverKey) throws Exception {
        Type type = new TypeToken<List<ApiModels.TailscalePeer>>() {}.getType();
        return doGet(serverKey, "/tailscale/peers", type);
    }
}
