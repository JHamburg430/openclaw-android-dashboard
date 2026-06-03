package ai.openclaw.dashboard;

import android.os.Build;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.TimeUnit;

import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.WebSocket;
import okhttp3.WebSocketListener;

final class OpenClawClient {
    interface Listener {
        void onStatus(String status);
        void onConnected(JSONObject hello);
        void onDashboard(JSONObject dashboard);
        void onLog(String message);
        void onError(String message);
    }

    private static final String CLIENT_ID = "openclaw-android";
    private final OkHttpClient http = new OkHttpClient.Builder()
            .pingInterval(20, TimeUnit.SECONDS)
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(0, TimeUnit.MILLISECONDS)
            .build();
    private final IdentityStore identityStore;
    private final Listener listener;
    private final Map<String, Pending> pending = new ConcurrentHashMap<>();

    private WebSocket socket;
    private Config config;
    private IdentityStore.Identity identity;

    OpenClawClient(IdentityStore identityStore, Listener listener) {
        this.identityStore = identityStore;
        this.listener = listener;
    }

    synchronized void connect(Config config) {
        disconnect();
        this.config = config;
        try {
            this.identity = identityStore.loadOrCreate();
            Request request = new Request.Builder().url(config.gatewayUrl).build();
            listener.onStatus("Connecting");
            socket = http.newWebSocket(request, new WebSocketListener() {
                @Override public void onOpen(WebSocket webSocket, Response response) {
                    listener.onStatus("Waiting for gateway challenge");
                }

                @Override public void onMessage(WebSocket webSocket, String text) {
                    handleMessage(webSocket, text);
                }

                @Override public void onClosed(WebSocket webSocket, int code, String reason) {
                    listener.onStatus("Disconnected: " + code + " " + reason);
                }

                @Override public void onFailure(WebSocket webSocket, Throwable t, Response response) {
                    listener.onError(t.getMessage() == null ? t.toString() : t.getMessage());
                    listener.onStatus("Connection failed");
                }
            });
        } catch (Exception e) {
            listener.onError(e.getMessage());
        }
    }

    synchronized void disconnect() {
        for (Pending p : pending.values()) p.reject("Disconnected");
        pending.clear();
        if (socket != null) {
            socket.close(1000, "user disconnect");
            socket = null;
        }
    }

    void refreshDashboard() {
        if (socket == null) return;
        JSONObject dashboard = new JSONObject();
        call("health", new JSONObject(), payload -> {
            put(dashboard, "health", payload);
            listener.onDashboard(dashboard);
        });
        call("node.list", new JSONObject(), payload -> {
            put(dashboard, "nodes", payload);
            listener.onDashboard(dashboard);
        });
        call("cron.list", new JSONObject(), payload -> {
            put(dashboard, "cron", payload);
            listener.onDashboard(dashboard);
        });
        call("sessions.list", new JSONObject(), payload -> {
            put(dashboard, "sessions", payload);
            listener.onDashboard(dashboard);
        });
    }

    private void handleMessage(WebSocket webSocket, String text) {
        try {
            JSONObject frame = new JSONObject(text);
            String type = frame.optString("type", "");
            if ("event".equals(type)) {
                String event = frame.optString("event", "");
                if ("connect.challenge".equals(event)) {
                    String nonce = frame.optJSONObject("payload").optString("nonce", "");
                    sendConnect(webSocket, nonce);
                    return;
                }
                if ("node.invoke.request".equals(event)) {
                    handleNodeInvoke(frame.optJSONObject("payload"));
                    return;
                }
                if ("tick".equals(event)) return;
                listener.onLog(event);
                return;
            }
            if ("res".equals(type)) {
                Pending p = pending.remove(frame.optString("id", ""));
                if (p == null) return;
                if (frame.optBoolean("ok")) p.resolve(frame.optJSONObject("payload"));
                else {
                    JSONObject error = frame.optJSONObject("error");
                    p.reject(error == null ? "Request failed" : error.optString("message", error.toString()));
                }
            }
        } catch (Exception e) {
            listener.onError("Protocol parse failed: " + e.getMessage());
        }
    }

    private void sendConnect(WebSocket webSocket, String nonce) throws Exception {
        String role = "operator";
        JSONArray scopes = scopesArray();
        String authToken = emptyToNull(config.gatewayToken);
        String bootstrapToken = authToken == null && emptyToNull(config.password) == null ? emptyToNull(config.bootstrapToken) : null;
        String deviceToken = identityStore.getDeviceToken();
        if (authToken == null && bootstrapToken == null && emptyToNull(config.password) == null && deviceToken != null) {
            authToken = deviceToken;
        }
        long signedAtMs = System.currentTimeMillis();
        String signatureToken = firstNonEmpty(authToken, deviceToken, bootstrapToken);
        String platform = normalizeMetadata("android");
        String family = normalizeMetadata(deviceFamily());
        String payload = "v3|" + identity.deviceId + "|" + CLIENT_ID + "|node|" + role + "|" +
                join(scopes) + "|" + signedAtMs + "|" + (signatureToken == null ? "" : signatureToken) + "|" +
                nonce + "|" + platform + "|" + family;
        String signature = IdentityStore.sign(identity.privateKey, payload);

        JSONObject auth = new JSONObject();
        if (authToken != null) auth.put("token", authToken);
        if (bootstrapToken != null) auth.put("bootstrapToken", bootstrapToken);
        if (emptyToNull(config.password) != null) auth.put("password", config.password.trim());

        JSONObject client = new JSONObject()
                .put("id", CLIENT_ID)
                .put("displayName", config.displayName)
                .put("version", "1.0")
                .put("platform", platform)
                .put("deviceFamily", family)
                .put("mode", "node")
                .put("instanceId", identity.deviceId);
        JSONObject device = new JSONObject()
                .put("id", identity.deviceId)
                .put("publicKey", identity.publicKeyRawBase64Url)
                .put("signature", signature)
                .put("signedAt", signedAtMs)
                .put("nonce", nonce);
        JSONObject params = new JSONObject()
                .put("minProtocol", 4)
                .put("maxProtocol", 4)
                .put("client", client)
                .put("caps", new JSONArray().put("android.dashboard").put("android.node"))
                .put("commands", new JSONArray().put("system.ping").put("system.status"))
                .put("auth", auth.length() == 0 ? JSONObject.NULL : auth)
                .put("role", role)
                .put("scopes", scopes)
                .put("device", device);

        request("connect", params, payloadJson -> {
            JSONObject authInfo = payloadJson == null ? null : payloadJson.optJSONObject("auth");
            if (authInfo != null && authInfo.optString("deviceToken", null) != null) {
                identityStore.saveDeviceToken(authInfo.optString("deviceToken"), authInfo.optJSONArray("scopes") == null ? "[]" : authInfo.optJSONArray("scopes").toString());
            }
            listener.onStatus("Connected as node");
            listener.onConnected(payloadJson == null ? new JSONObject() : payloadJson);
            refreshDashboard();
        }, error -> {
            listener.onError("Connect rejected: " + error);
            listener.onStatus("Pairing or auth required");
        });
    }

    private void handleNodeInvoke(JSONObject payload) throws Exception {
        if (payload == null) return;
        String invokeId = payload.optString("invokeId", payload.optString("id", ""));
        String command = payload.optString("command", "");
        JSONObject result = new JSONObject()
                .put("ok", true)
                .put("command", command)
                .put("deviceId", identity.deviceId)
                .put("displayName", config.displayName)
                .put("platform", "android")
                .put("battery", JSONObject.NULL)
                .put("ts", System.currentTimeMillis());
        JSONObject params = new JSONObject()
                .put("invokeId", invokeId)
                .put("nodeId", identity.deviceId)
                .put("ok", true)
                .put("result", result);
        request("node.invoke.result", params, ignored -> listener.onLog("Handled " + command), error -> listener.onError("Invoke reply failed: " + error));
    }

    private void call(String method, JSONObject params, Callback callback) {
        request(method, params, callback, error -> listener.onLog(method + ": " + error));
    }

    private void request(String method, JSONObject params, Callback callback, ErrorCallback errorCallback) {
        WebSocket ws = socket;
        if (ws == null) {
            errorCallback.error("Not connected");
            return;
        }
        try {
            String id = UUID.randomUUID().toString();
            pending.put(id, new Pending(callback, errorCallback));
            JSONObject frame = new JSONObject()
                    .put("type", "req")
                    .put("id", id)
                    .put("method", method)
                    .put("params", params == null ? new JSONObject() : params);
            ws.send(frame.toString());
        } catch (Exception e) {
            errorCallback.error(e.getMessage());
        }
    }

    private JSONArray scopesArray() throws Exception {
        String storedScopes = identityStore.getDeviceTokenScopesJson();
        if (identityStore.getDeviceToken() != null && storedScopes != null && storedScopes.length() > 2) {
            return new JSONArray(storedScopes);
        }
        return new JSONArray().put("operator.admin");
    }

    private static String join(JSONArray array) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < array.length(); i++) {
            if (i > 0) sb.append(',');
            sb.append(array.optString(i));
        }
        return sb.toString();
    }

    private static void put(JSONObject object, String key, JSONObject value) {
        try {
            object.put(key, value == null ? JSONObject.NULL : value);
        } catch (Exception ignored) {
        }
    }

    private static String firstNonEmpty(String... values) {
        for (String value : values) {
            String normalized = emptyToNull(value);
            if (normalized != null) return normalized;
        }
        return null;
    }

    private static String emptyToNull(String value) {
        if (value == null) return null;
        String trimmed = value.trim();
        return trimmed.isEmpty() ? null : trimmed;
    }

    private static String deviceFamily() {
        String model = Build.MANUFACTURER + " " + Build.MODEL;
        return model.trim().isEmpty() ? "Android" : model.trim();
    }

    private static String normalizeMetadata(String value) {
        if (value == null) return "";
        return value.trim().toLowerCase(java.util.Locale.ROOT);
    }

    interface Callback { void ok(JSONObject payload); }
    interface ErrorCallback { void error(String message); }

    static final class Config {
        final String gatewayUrl;
        final String bootstrapToken;
        final String gatewayToken;
        final String password;
        final String displayName;

        Config(String gatewayUrl, String bootstrapToken, String gatewayToken, String password, String displayName) {
            this.gatewayUrl = gatewayUrl;
            this.bootstrapToken = bootstrapToken;
            this.gatewayToken = gatewayToken;
            this.password = password;
            this.displayName = displayName == null || displayName.trim().isEmpty() ? "Android OpenClaw Node" : displayName.trim();
        }
    }

    private static final class Pending {
        final Callback callback;
        final ErrorCallback errorCallback;

        Pending(Callback callback, ErrorCallback errorCallback) {
            this.callback = callback;
            this.errorCallback = errorCallback;
        }

        void resolve(JSONObject payload) {
            callback.ok(payload == null ? new JSONObject() : payload);
        }

        void reject(String message) {
            errorCallback.error(message);
        }
    }
}
