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
 */
@Service(Service.Level.APP)
public final class BeConductorClient {

    private static final Logger LOG = Logger.getInstance(BeConductorClient.class);
    private static final String DEFAULT_BASE_URL = "http://127.0.0.1:7777";
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

    private String getBaseUrl() {
        return DEFAULT_BASE_URL;
    }

    private String getAuthToken() {
        String token = System.getenv("BE_CONDUCTOR_TOKEN");
        if (token == null) token = System.getenv("CONDUCTOR_TOKEN");
        return token;
    }

    private static String encode(String value) {
        // URLEncoder uses '+' for spaces (form encoding), but URL paths need '%20'
        return URLEncoder.encode(value, StandardCharsets.UTF_8).replace("+", "%20");
    }

    // ── Generic request helpers ──────────────────────────────────────────

    private <T> T doGet(String path, Class<T> responseType) throws Exception {
        HttpRequest.Builder builder = HttpRequest.newBuilder()
                .uri(URI.create(getBaseUrl() + path))
                .timeout(TIMEOUT)
                .GET();
        addAuth(builder);
        HttpResponse<String> resp = httpClient.send(builder.build(),
                HttpResponse.BodyHandlers.ofString());
        checkStatus(resp);
        return gson.fromJson(resp.body(), responseType);
    }

    private <T> T doGet(String path, Type responseType) throws Exception {
        HttpRequest.Builder builder = HttpRequest.newBuilder()
                .uri(URI.create(getBaseUrl() + path))
                .timeout(TIMEOUT)
                .GET();
        addAuth(builder);
        HttpResponse<String> resp = httpClient.send(builder.build(),
                HttpResponse.BodyHandlers.ofString());
        checkStatus(resp);
        return gson.fromJson(resp.body(), responseType);
    }

    private <T> T doPost(String path, Object body, Class<T> responseType) throws Exception {
        String json = body != null ? gson.toJson(body) : "{}";
        HttpRequest.Builder builder = HttpRequest.newBuilder()
                .uri(URI.create(getBaseUrl() + path))
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

    private <T> T doPost(String path, Object body, Type responseType) throws Exception {
        String json = body != null ? gson.toJson(body) : "{}";
        HttpRequest.Builder builder = HttpRequest.newBuilder()
                .uri(URI.create(getBaseUrl() + path))
                .timeout(TIMEOUT)
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(json));
        addAuth(builder);
        HttpResponse<String> resp = httpClient.send(builder.build(),
                HttpResponse.BodyHandlers.ofString());
        checkStatus(resp);
        return gson.fromJson(resp.body(), responseType);
    }

    private <T> T doDelete(String path, Class<T> responseType) throws Exception {
        HttpRequest.Builder builder = HttpRequest.newBuilder()
                .uri(URI.create(getBaseUrl() + path))
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

    public ApiModels.HealthResponse getHealth() throws Exception {
        return doGet("/health", ApiModels.HealthResponse.class);
    }

    public boolean isServerRunning() {
        try {
            getHealth();
            return true;
        } catch (Exception e) {
            return false;
        }
    }

    // ── Config ────────────────────────────────────────────────────────────

    public ApiModels.ConfigResponse getConfig() throws Exception {
        return doGet("/config", ApiModels.ConfigResponse.class);
    }

    // ── Sessions ──────────────────────────────────────────────────────────

    public List<ApiModels.SessionResponse> listSessions() throws Exception {
        Type type = new TypeToken<List<ApiModels.SessionResponse>>() {}.getType();
        return doGet("/sessions", type);
    }

    public ApiModels.SessionResponse createSession(ApiModels.RunRequest request) throws Exception {
        return doPost("/sessions/run", request, ApiModels.SessionResponse.class);
    }

    public ApiModels.StatusResponse stopSession(String sessionId, String mode) throws Exception {
        return doPost("/sessions/" + encode(sessionId) + "/stop",
                new ApiModels.StopRequest(mode), ApiModels.StatusResponse.class);
    }

    public ApiModels.StatusResponse deleteSession(String sessionId) throws Exception {
        return doDelete("/sessions/" + encode(sessionId), ApiModels.StatusResponse.class);
    }

    public ApiModels.SessionResponse resumeSession(String sessionId) throws Exception {
        return doPost("/sessions/" + encode(sessionId) + "/resume",
                null, ApiModels.SessionResponse.class);
    }

    // ── Git ───────────────────────────────────────────────────────────────

    public ApiModels.GitCheckResponse checkGit(String path) throws Exception {
        return doGet("/git/check?path=" + encode(path), ApiModels.GitCheckResponse.class);
    }

    // ── Worktrees ─────────────────────────────────────────────────────────

    public List<ApiModels.WorktreeInfo> listWorktrees() throws Exception {
        Type type = new TypeToken<List<ApiModels.WorktreeInfo>>() {}.getType();
        return doGet("/worktrees", type);
    }

    public ApiModels.WorktreeInfo getWorktree(String name) throws Exception {
        return doGet("/worktrees/" + encode(name), ApiModels.WorktreeInfo.class);
    }

    public ApiModels.DiffResponse getWorktreeDiff(String name) throws Exception {
        return doGet("/worktrees/" + encode(name) + "/diff", ApiModels.DiffResponse.class);
    }

    public ApiModels.RichDiffResponse getWorktreeRichDiff(String name) throws Exception {
        return doGet("/worktrees/" + encode(name) + "/diff?format=rich",
                ApiModels.RichDiffResponse.class);
    }

    public ApiModels.WorktreeInfo finalizeWorktree(String name) throws Exception {
        return doPost("/worktrees/" + encode(name) + "/finalize", null, ApiModels.WorktreeInfo.class);
    }

    public ApiModels.MergePreview previewMerge(String name) throws Exception {
        return doPost("/worktrees/" + encode(name) + "/merge/preview", null, ApiModels.MergePreview.class);
    }

    public ApiModels.MergeResult executeMerge(String name, String strategy, String message) throws Exception {
        return doPost("/worktrees/" + encode(name) + "/merge",
                new ApiModels.MergeRequest(strategy, message), ApiModels.MergeResult.class);
    }

    public void deleteWorktree(String name, boolean force) throws Exception {
        doDelete("/worktrees/" + encode(name) + "?force=" + force, Void.class);
    }

    public List<?> worktreeGC(boolean dryRun, double maxAgeDays) throws Exception {
        Type type = new TypeToken<List<Object>>() {}.getType();
        return doPost("/worktrees/gc", new ApiModels.GCRequest(dryRun, maxAgeDays), type);
    }
}
