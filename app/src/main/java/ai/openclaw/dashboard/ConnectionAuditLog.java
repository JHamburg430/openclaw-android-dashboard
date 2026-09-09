package ai.openclaw.dashboard;

import android.content.Context;
import android.content.SharedPreferences;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.util.ArrayDeque;
import java.util.Iterator;
import java.util.Locale;
import java.util.UUID;

/** A bounded, redacted JSONL trail for browser, node, session, and invoke activity. */
final class ConnectionAuditLog {
    private static final String FILE_NAME = "openclaw-connection-audit.jsonl";
    private static final String OLD_FILE_NAME = "openclaw-connection-audit.previous.jsonl";
    private static final long MAX_BYTES = 1024L * 1024L;
    private static final int MAX_READ_RECORDS = 250;
    private static final String PREFS = "openclaw_dashboard";

    private final Context context;
    private final File file;

    ConnectionAuditLog(Context context) {
        this.context = context.getApplicationContext();
        this.file = new File(this.context.getFilesDir(), FILE_NAME);
    }

    synchronized String record(String component, String event, JSONObject details) {
        String correlationId = details == null ? "" : details.optString("correlationId", "");
        if (correlationId.isEmpty()) correlationId = UUID.randomUUID().toString();
        try {
            JSONObject line = new JSONObject()
                    .put("ts", System.currentTimeMillis())
                    .put("component", safe(component))
                    .put("event", safe(event))
                    .put("correlationId", correlationId)
                    .put("details", redact(details == null ? new JSONObject() : details));
            append(line.toString() + "\n");
        } catch (Exception ignored) {
        }
        return correlationId;
    }

    synchronized JSONArray recent(int requestedLimit) {
        int limit = Math.max(1, Math.min(requestedLimit, MAX_READ_RECORDS));
        ArrayDeque<JSONObject> records = new ArrayDeque<>();
        readInto(new File(context.getFilesDir(), OLD_FILE_NAME), records, limit);
        readInto(file, records, limit);
        JSONArray result = new JSONArray();
        for (JSONObject record : records) result.put(record);
        return result;
    }

    synchronized void clear() {
        if (file.exists()) file.delete();
        File old = new File(context.getFilesDir(), OLD_FILE_NAME);
        if (old.exists()) old.delete();
    }

    synchronized JSONObject connectionSnapshot() {
        SharedPreferences prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        JSONObject result = new JSONObject();
        try {
            result.put("webViewState", prefs.getString("trace.webview.state", "not_loaded"));
            result.put("webViewSurface", prefs.getString("trace.webview.surface", "none"));
            result.put("webViewConnectionId", prefs.getString("trace.webview.connectionId", ""));
            result.put("webViewUrl", sanitizeUrl(prefs.getString("trace.webview.url", "")));
            result.put("webViewUpdatedAt", prefs.getLong("trace.webview.updatedAt", 0L));
            result.put("sessionKey", prefs.getString("trace.session.key", ""));
            result.put("sessionId", prefs.getString("trace.session.id", ""));
            result.put("sessionTitle", prefs.getString("trace.session.title", ""));
            result.put("agentId", prefs.getString("trace.session.agentId", ""));
            result.put("nodeState", prefs.getString("trace.node.state", "stopped"));
            result.put("nodeId", prefs.getString("trace.node.id", ""));
            result.put("nodeConnectionId", prefs.getString("trace.node.connectionId", ""));
            result.put("nodeConnectedAt", prefs.getLong("trace.node.connectedAt", 0L));
            result.put("nodeLastEventAt", prefs.getLong("trace.node.lastEventAt", 0L));
            result.put("nodeRetryAttempt", prefs.getInt("trace.node.retryAttempt", 0));
            result.put("nodeLastError", prefs.getString("trace.node.lastError", ""));
            result.put("clientId", "openclaw-android");
            result.put("clientProduct", "openclaw-android-dashboard");
            result.put("clientVersion", BuildConfig.VERSION_NAME);
        } catch (Exception ignored) {
        }
        return result;
    }

    private void append(String text) throws Exception {
        if (file.exists() && file.length() >= MAX_BYTES) {
            File old = new File(context.getFilesDir(), OLD_FILE_NAME);
            if (old.exists()) old.delete();
            file.renameTo(old);
        }
        try (FileOutputStream output = new FileOutputStream(file, true)) {
            output.write(text.getBytes(StandardCharsets.UTF_8));
        }
    }

    private static void readInto(File source, ArrayDeque<JSONObject> records, int limit) {
        if (!source.exists()) return;
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(new FileInputStream(source), StandardCharsets.UTF_8))) {
            String line;
            while ((line = reader.readLine()) != null) {
                if (line.trim().isEmpty()) continue;
                try {
                    records.addLast(new JSONObject(line));
                    while (records.size() > limit) records.removeFirst();
                } catch (Exception ignored) {
                }
            }
        } catch (Exception ignored) {
        }
    }

    private static JSONObject redact(JSONObject input) {
        JSONObject output = new JSONObject();
        Iterator<String> keys = input.keys();
        while (keys.hasNext()) {
            String key = keys.next();
            Object value = input.opt(key);
            try {
                if (isSensitive(key)) output.put(key, "[redacted]");
                else if (value instanceof JSONObject) output.put(key, redact((JSONObject) value));
                else if (value instanceof JSONArray) output.put(key, summarizeArray((JSONArray) value));
                else if (value instanceof String && ((String) value).length() > 240) output.put(key, ((String) value).substring(0, 240) + "…");
                else output.put(key, value == null ? JSONObject.NULL : value);
            } catch (Exception ignored) {
            }
        }
        return output;
    }

    private static Object summarizeArray(JSONArray array) {
        if (array.length() <= 12) {
            JSONArray output = new JSONArray();
            for (int i = 0; i < array.length(); i++) {
                Object value = array.opt(i);
                if (value instanceof JSONObject) output.put(redact((JSONObject) value));
                else output.put(value);
            }
            return output;
        }
        return "[array length=" + array.length() + "]";
    }

    private static boolean isSensitive(String key) {
        String lower = safe(key).toLowerCase(Locale.ROOT);
        return lower.contains("token") || lower.contains("password") || lower.contains("secret")
                || lower.equals("body") || lower.equals("text") || lower.equals("reply")
                || lower.equals("address") || lower.equals("number") || lower.equals("phone")
                || lower.equals("email") || lower.equals("contacts") || lower.equals("notifications");
    }

    private static String sanitizeUrl(String value) {
        if (value == null) return "";
        int query = value.indexOf('?');
        return query >= 0 ? value.substring(0, query) : value;
    }

    private static String safe(String value) {
        return value == null ? "" : value;
    }
}
