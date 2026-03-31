package com.somniacs.beconductor.api;

import com.google.gson.Gson;
import com.google.gson.reflect.TypeToken;
import com.intellij.ide.util.PropertiesComponent;
import com.intellij.openapi.application.ApplicationManager;
import com.intellij.openapi.components.Service;
import com.intellij.openapi.diagnostic.Logger;

import java.lang.reflect.Type;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Objects;

/**
 * Manages the list of be-conductor servers (local + remote).
 * Persisted via IDE PropertiesComponent.
 */
@Service(Service.Level.APP)
public final class ServerRegistry {

    private static final Logger LOG = Logger.getInstance(ServerRegistry.class);
    private static final String STORAGE_KEY = "be-conductor.servers";
    private static final String LOCAL_KEY = "local";
    private static final String DEFAULT_LOCAL_URL = "http://127.0.0.1:7777";
    private static final Gson GSON = new Gson();

    private final List<Server> servers = new ArrayList<>();

    public ServerRegistry() {
        load();
    }

    public static ServerRegistry getInstance() {
        return ApplicationManager.getApplication().getService(ServerRegistry.class);
    }

    // ── Server data class ────────────────────────────────────────────────

    public static class Server {
        public String key;
        public String label;
        public String url;      // null for local
        public boolean enabled;

        public Server() {}

        public Server(String key, String label, String url, boolean enabled) {
            this.key = key;
            this.label = label;
            this.url = url;
            this.enabled = enabled;
        }

        public boolean isLocal() {
            return LOCAL_KEY.equals(key);
        }

        @Override
        public boolean equals(Object o) {
            if (this == o) return true;
            if (!(o instanceof Server s)) return false;
            return Objects.equals(key, s.key);
        }

        @Override
        public int hashCode() {
            return Objects.hashCode(key);
        }
    }

    // ── Queries ──────────────────────────────────────────────────────────

    public synchronized List<Server> getServers() {
        return Collections.unmodifiableList(new ArrayList<>(servers));
    }

    public synchronized List<Server> getEnabledServers() {
        return servers.stream().filter(s -> s.enabled).toList();
    }

    public synchronized boolean isMultiServer() {
        return servers.stream().filter(s -> s.enabled).count() > 1;
    }

    public synchronized Server getServer(String key) {
        return servers.stream().filter(s -> s.key.equals(key)).findFirst().orElse(null);
    }

    // ── URL resolution ───────────────────────────────────────────────────

    /**
     * Resolve an API path to a full HTTP URL for the given server.
     */
    public String serverUrl(String serverKey, String path) {
        if (LOCAL_KEY.equals(serverKey) || serverKey == null) {
            return DEFAULT_LOCAL_URL + path;
        }
        Server server = getServer(serverKey);
        if (server == null || server.url == null) {
            return DEFAULT_LOCAL_URL + path;
        }
        return server.url + path;
    }

    /**
     * Resolve an API path to a full WebSocket URL for the given server.
     */
    public String serverWsUrl(String serverKey, String path) {
        String httpUrl = serverUrl(serverKey, path);
        return httpUrl.replaceFirst("^http", "ws");
    }

    /**
     * Get the base HTTP URL for a server (no path).
     */
    public String getBaseUrl(String serverKey) {
        return serverUrl(serverKey, "");
    }

    // ── Mutations ────────────────────────────────────────────────────────

    public synchronized Server addServer(String url, String label) {
        url = url.replaceAll("/+$", "");
        if (!url.matches("^https?://.*")) {
            url = "http://" + url;
        }
        String key = url.replaceFirst("^https?://", "");
        if (servers.stream().anyMatch(s -> s.key.equals(key))) {
            return null;  // already exists
        }
        Server server = new Server(key, label != null ? label : key, url, true);
        servers.add(server);
        save();
        return server;
    }

    public synchronized boolean removeServer(String key) {
        if (LOCAL_KEY.equals(key)) return false;  // can't remove local
        boolean removed = servers.removeIf(s -> s.key.equals(key));
        if (removed) save();
        return removed;
    }

    public synchronized void setEnabled(String key, boolean enabled) {
        Server server = getServer(key);
        if (server != null) {
            server.enabled = enabled;
            save();
        }
    }

    public synchronized void setLabel(String key, String label) {
        Server server = getServer(key);
        if (server != null) {
            server.label = label;
            save();
        }
    }

    // ── Compound IDs ─────────────────────────────────────────────────────

    public String compoundId(String serverKey, String sessionId) {
        if (!isMultiServer()) return sessionId;
        return serverKey + "::" + sessionId;
    }

    public static String[] parseCompoundId(String id) {
        int sep = id.indexOf("::");
        if (sep < 0) return new String[]{LOCAL_KEY, id};
        return new String[]{id.substring(0, sep), id.substring(sep + 2)};
    }

    // ── Persistence ──────────────────────────────────────────────────────

    private void load() {
        servers.clear();
        try {
            String json = PropertiesComponent.getInstance().getValue(STORAGE_KEY);
            if (json != null && !json.isEmpty()) {
                Type type = new TypeToken<List<Server>>() {}.getType();
                List<Server> loaded = GSON.fromJson(json, type);
                if (loaded != null) {
                    servers.addAll(loaded);
                }
            }
        } catch (Exception e) {
            LOG.warn("Failed to load server registry", e);
        }
        // Ensure local always exists and is first
        if (servers.stream().noneMatch(s -> LOCAL_KEY.equals(s.key))) {
            servers.add(0, new Server(LOCAL_KEY, "This Machine", null, true));
        }
    }

    private void save() {
        try {
            String json = GSON.toJson(servers);
            PropertiesComponent.getInstance().setValue(STORAGE_KEY, json);
        } catch (Exception e) {
            LOG.warn("Failed to save server registry", e);
        }
    }
}
