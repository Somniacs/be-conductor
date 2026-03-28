package com.somniacs.beconductor.agent;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.google.gson.reflect.TypeToken;
import com.intellij.openapi.diagnostic.Logger;

import javax.swing.*;
import java.lang.reflect.Type;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.WebSocket;
import java.nio.ByteBuffer;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionStage;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

/**
 * Lightweight WebSocket client for streaming agent session events.
 * <p>
 * Connects to ws://host:port/sessions/{id}/stream?source=jetbrains
 * and dispatches parsed JSON events to a listener on the EDT.
 */
public class AgentWebSocketClient {

    private static final Logger LOG = Logger.getInstance(AgentWebSocketClient.class);
    private static final String DEFAULT_HOST = "127.0.0.1";
    private static final int DEFAULT_PORT = 7777;
    private static final int MAX_RECONNECT_ATTEMPTS = 10;
    private static final long INITIAL_BACKOFF_MS = 1000;
    private static final long MAX_BACKOFF_MS = 30000;

    private final Gson gson = new Gson();
    private final HttpClient httpClient;
    private final AgentEventListener listener;
    private final AtomicReference<WebSocket> wsRef = new AtomicReference<>();
    private final AtomicBoolean closed = new AtomicBoolean(false);
    private final AtomicBoolean intentionallyClosed = new AtomicBoolean(false);
    private final AtomicInteger reconnectAttempts = new AtomicInteger(0);

    private volatile String sessionId;

    /**
     * Listener interface for agent WebSocket events.
     */
    public interface AgentEventListener {
        /** Called when a structured event arrives (on EDT). */
        void onEvent(Map<String, Object> event);
        /** Called when the WebSocket connection is established (on EDT). */
        void onConnected();
        /** Called when the WebSocket connection is lost (on EDT). */
        void onDisconnected();
    }

    public AgentWebSocketClient(AgentEventListener listener) {
        this.listener = listener;
        this.httpClient = HttpClient.newBuilder()
                .version(HttpClient.Version.HTTP_1_1)
                .build();
    }

    /**
     * Connect to the agent session stream.
     */
    public void connect(String sessionId) {
        this.sessionId = sessionId;
        closed.set(false);
        intentionallyClosed.set(false);
        reconnectAttempts.set(0);
        doConnect();
    }

    private void doConnect() {
        if (closed.get()) return;

        String token = getAuthToken();
        String uriStr = "ws://" + DEFAULT_HOST + ":" + DEFAULT_PORT
                + "/sessions/" + encodeUri(sessionId) + "/stream?source=jetbrains";
        if (token != null && !token.isEmpty()) {
            uriStr += "&token=" + encodeUri(token);
        }

        try {
            URI uri = URI.create(uriStr);
            httpClient.newWebSocketBuilder()
                    .buildAsync(uri, new WsListener())
                    .whenComplete((ws, ex) -> {
                        if (ex != null) {
                            LOG.info("be-conductor: WebSocket connection failed: " + ex.getMessage());
                            scheduleReconnect();
                        }
                    });
        } catch (Exception e) {
            LOG.warn("be-conductor: failed to create WebSocket URI", e);
            scheduleReconnect();
        }
    }

    private void scheduleReconnect() {
        if (closed.get() || intentionallyClosed.get()) return;
        int attempt = reconnectAttempts.incrementAndGet();
        if (attempt > MAX_RECONNECT_ATTEMPTS) {
            LOG.info("be-conductor: max reconnect attempts reached");
            SwingUtilities.invokeLater(listener::onDisconnected);
            return;
        }
        long backoff = Math.min(INITIAL_BACKOFF_MS * (1L << (attempt - 1)), MAX_BACKOFF_MS);
        new Thread(() -> {
            try {
                Thread.sleep(backoff);
            } catch (InterruptedException ignored) {
                return;
            }
            doConnect();
        }, "be-conductor-ws-reconnect").start();
    }

    /**
     * Send a raw JSON string over the WebSocket.
     */
    public void send(String json) {
        WebSocket ws = wsRef.get();
        if (ws != null) {
            ws.sendText(json, true);
        }
    }

    /**
     * Send a prompt message to the agent.
     */
    public void sendPrompt(String text) {
        JsonObject msg = new JsonObject();
        msg.addProperty("type", "prompt");
        msg.addProperty("text", text);
        send(gson.toJson(msg));
    }

    /**
     * Send an interrupt signal to the agent.
     */
    public void sendInterrupt() {
        JsonObject msg = new JsonObject();
        msg.addProperty("type", "interrupt");
        send(gson.toJson(msg));
    }

    /**
     * Set the agent permission mode.
     */
    public void setMode(String mode) {
        JsonObject msg = new JsonObject();
        msg.addProperty("type", "set_mode");
        msg.addProperty("mode", mode);
        send(gson.toJson(msg));
    }

    /**
     * Set the agent effort level.
     */
    public void setEffort(String effort) {
        JsonObject msg = new JsonObject();
        msg.addProperty("type", "set_effort");
        msg.addProperty("effort", effort);
        send(gson.toJson(msg));
    }

    /**
     * Close the WebSocket connection. No reconnection will be attempted.
     */
    public void close() {
        closed.set(true);
        intentionallyClosed.set(true);
        WebSocket ws = wsRef.getAndSet(null);
        if (ws != null) {
            try {
                ws.sendClose(WebSocket.NORMAL_CLOSURE, "closing");
            } catch (Exception ignored) {}
        }
    }

    private String getAuthToken() {
        String token = System.getenv("BE_CONDUCTOR_TOKEN");
        if (token == null) token = System.getenv("CONDUCTOR_TOKEN");
        return token;
    }

    private static String encodeUri(String value) {
        return java.net.URLEncoder.encode(value, java.nio.charset.StandardCharsets.UTF_8)
                .replace("+", "%20");
    }

    // ── WebSocket listener ──────────────────────────────────────────────

    private class WsListener implements WebSocket.Listener {

        private final StringBuilder textBuffer = new StringBuilder();

        @Override
        public void onOpen(WebSocket webSocket) {
            wsRef.set(webSocket);
            reconnectAttempts.set(0);
            SwingUtilities.invokeLater(listener::onConnected);
            webSocket.request(1);
        }

        @Override
        public CompletionStage<?> onText(WebSocket webSocket, CharSequence data, boolean last) {
            textBuffer.append(data);
            if (last) {
                String text = textBuffer.toString();
                textBuffer.setLength(0);
                processMessage(text);
            }
            webSocket.request(1);
            return CompletableFuture.completedFuture(null);
        }

        @Override
        public CompletionStage<?> onClose(WebSocket webSocket, int statusCode, String reason) {
            wsRef.set(null);
            SwingUtilities.invokeLater(listener::onDisconnected);
            if (!intentionallyClosed.get()) {
                scheduleReconnect();
            }
            return CompletableFuture.completedFuture(null);
        }

        @Override
        public void onError(WebSocket webSocket, Throwable error) {
            LOG.info("be-conductor: WebSocket error: " + error.getMessage());
            wsRef.set(null);
            SwingUtilities.invokeLater(listener::onDisconnected);
            if (!intentionallyClosed.get()) {
                scheduleReconnect();
            }
        }

        @Override
        public CompletionStage<?> onPing(WebSocket webSocket, ByteBuffer message) {
            webSocket.request(1);
            return CompletableFuture.completedFuture(null);
        }

        @Override
        public CompletionStage<?> onPong(WebSocket webSocket, ByteBuffer message) {
            webSocket.request(1);
            return CompletableFuture.completedFuture(null);
        }
    }

    // ── Message parsing ─────────────────────────────────────────────────

    private void processMessage(String text) {
        try {
            JsonObject json = JsonParser.parseString(text).getAsJsonObject();
            String type = json.has("type") ? json.get("type").getAsString() : null;

            if ("ping".equals(type)) {
                return; // Ignore keepalive pings
            }

            if ("history".equals(type)) {
                // History replay: array of past events
                Type listType = new TypeToken<List<Map<String, Object>>>() {}.getType();
                List<Map<String, Object>> messages = gson.fromJson(
                        json.get("messages"), listType);
                if (messages != null) {
                    SwingUtilities.invokeLater(() -> {
                        for (Map<String, Object> event : messages) {
                            listener.onEvent(event);
                        }
                    });
                }
                return;
            }

            // Single event
            Type mapType = new TypeToken<Map<String, Object>>() {}.getType();
            Map<String, Object> event = gson.fromJson(text, mapType);
            if (event != null) {
                SwingUtilities.invokeLater(() -> listener.onEvent(event));
            }
        } catch (Exception e) {
            LOG.info("be-conductor: failed to parse WebSocket message: " + e.getMessage());
        }
    }
}
