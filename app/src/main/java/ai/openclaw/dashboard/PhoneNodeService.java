package ai.openclaw.dashboard;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.os.Binder;
import android.os.BatteryManager;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.List;
import java.util.Locale;
import java.util.concurrent.ThreadLocalRandom;

/** Keeps the custom Android node alive independently of the Control UI activity. */
public final class PhoneNodeService extends Service implements OpenClawClient.Listener {
    public static final String ACTION_START = "ai.openclaw.dashboard.node.START";
    public static final String ACTION_STOP = "ai.openclaw.dashboard.node.STOP";
    public static final String ACTION_RECONNECT = "ai.openclaw.dashboard.node.RECONNECT";
    static final String PREFS = "openclaw_dashboard";

    private static final String CHANNEL_ID = "openclaw_phone_node";
    private static final int NOTIFICATION_ID = 41002;
    private static final long MAX_RETRY_MS = 60_000L;
    private static final long CONNECT_TIMEOUT_MS = 25_000L;
    private static final long WATCHDOG_INTERVAL_MS = 15_000L;

    interface ActivityCommandHandler {
        JSONObject handle(String command, JSONObject params) throws Exception;
    }

    interface StateListener {
        void onNodeState(JSONObject state);
    }

    final class LocalBinder extends Binder {
        PhoneNodeService getService() { return PhoneNodeService.this; }
    }

    private final IBinder binder = new LocalBinder();
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final Runnable reconnectRunnable = this::connectNow;
    private final Runnable watchdogRunnable = this::runConnectionWatchdog;
    private SharedPreferences prefs;
    private OpenClawClient client;
    private AndroidCapabilityBroker broker;
    private ConnectionAuditLog audit;
    private volatile ActivityCommandHandler activityCommandHandler;
    private volatile StateListener stateListener;
    private ConnectivityManager connectivityManager;
    private ConnectivityManager.NetworkCallback networkCallback;
    private boolean manualStop;
    private boolean connecting;
    private int retryAttempt;
    private long connectStartedAtMs;
    private long nextReconnectAtMs;
    private String activeGatewayUrl = "";

    @Override public void onCreate() {
        super.onCreate();
        prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        audit = new ConnectionAuditLog(this);
        broker = new AndroidCapabilityBroker(this);
        IdentityStore identityStore = new IdentityStore(this);
        try {
            prefs.edit().putString("trace.node.id", identityStore.loadOrCreate().deviceId).apply();
        } catch (Exception error) {
            audit.record("node", "identity_error", fields("message", error.getMessage()));
        }
        client = new OpenClawClient(identityStore, this, this::handleCommand);
        createNotificationChannel();
        registerNetworkCallback();
        handler.post(watchdogRunnable);
        updateState("starting", "", false);
        audit.record("node", "service_created", new JSONObject());
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? ACTION_START : intent.getAction();
        if (ACTION_STOP.equals(action)) {
            prefs.edit().putBoolean("nodeEnabled", false).apply();
            manualStop = true;
            handler.removeCallbacks(reconnectRunnable);
            handler.removeCallbacks(watchdogRunnable);
            client.disconnect();
            updateState("stopped", "Stopped by user", false);
            audit.record("node", "service_stopped", fields("reason", "user"));
            stopForeground(STOP_FOREGROUND_REMOVE);
            stopSelf();
            return START_NOT_STICKY;
        }

        prefs.edit().putBoolean("nodeEnabled", true).apply();
        manualStop = false;
        startForeground(NOTIFICATION_ID, buildNotification("Connecting"));
        String configuredGateway = prefs.getString("url", "");
        boolean configurationChanged = !activeGatewayUrl.isEmpty()
                && !activeGatewayUrl.equals(configuredGateway == null ? "" : configuredGateway.trim());
        if (ACTION_RECONNECT.equals(action) || configurationChanged) {
            resetConnection(ACTION_RECONNECT.equals(action) ? "manual_repair" : "configuration_changed");
        }
        scheduleWatchdog();
        connectNow();
        return START_STICKY;
    }

    @Override public IBinder onBind(Intent intent) { return binder; }

    @Override public void onDestroy() {
        manualStop = true;
        handler.removeCallbacks(reconnectRunnable);
        handler.removeCallbacks(watchdogRunnable);
        if (client != null) client.disconnect();
        if (connectivityManager != null && networkCallback != null) {
            try { connectivityManager.unregisterNetworkCallback(networkCallback); } catch (Exception ignored) { }
        }
        audit.record("node", "service_destroyed", new JSONObject());
        super.onDestroy();
    }

    void setActivityCommandHandler(ActivityCommandHandler handlerValue) {
        activityCommandHandler = handlerValue;
    }

    void clearActivityCommandHandler(ActivityCommandHandler expected) {
        if (activityCommandHandler == expected) activityCommandHandler = null;
    }

    void setStateListener(StateListener listener) {
        stateListener = listener;
        dispatchState();
    }

    void clearStateListener(StateListener expected) {
        if (stateListener == expected) stateListener = null;
    }

    JSONObject snapshot() { return audit.connectionSnapshot(); }
    JSONArray recentAudit(int limit) { return audit.recent(limit); }
    void clearAudit() { audit.clear(); }

    private synchronized void connectNow() {
        if (manualStop) return;
        if (!hasUsableNetwork()) {
            updateState("network_wait", "Network unavailable", false);
            return;
        }
        String rawUrl = prefs.getString("url", "");
        if (rawUrl == null || rawUrl.trim().isEmpty()) {
            updateState("waiting_for_configuration", "Gateway URL is missing", false);
            return;
        }
        String configuredGateway = rawUrl.trim();
        if (client.isConnected() && configuredGateway.equals(activeGatewayUrl)) return;
        long now = System.currentTimeMillis();
        if (connecting && now - connectStartedAtMs < CONNECT_TIMEOUT_MS) return;
        if (connecting || client.isConnected()) resetConnection("stale_or_changed_connection");
        try {
            connecting = true;
            connectStartedAtMs = now;
            nextReconnectAtMs = 0L;
            activeGatewayUrl = configuredGateway;
            updateState("connecting", "", false);
            audit.record("node", "connect_attempt", new JSONObject()
                    .put("attempt", retryAttempt + 1)
                    .put("gateway", sanitizeGateway(rawUrl)));
            client.connect(new OpenClawClient.Config(
                    toGatewayWebSocketUrl(rawUrl),
                    prefs.getString("bootstrapToken", ""),
                    prefs.getString("token", ""),
                    prefs.getString("password", ""),
                    "John's S25 Ultra — Dashboard"));
        } catch (Exception error) {
            connecting = false;
            onError(error.getMessage() == null ? error.toString() : error.getMessage());
            scheduleReconnect("configuration_or_connect_error");
        }
    }

    private JSONObject handleCommand(String command, JSONObject params) throws Exception {
        ActivityCommandHandler foregroundHandler = activityCommandHandler;
        if (foregroundHandler != null) return foregroundHandler.handle(command, params);

        JSONObject result = broker.handle(command, params);
        if (result != null) return result;
        switch (command) {
            case "system.ping":
                return new JSONObject().put("pong", true).put("ts", System.currentTimeMillis());
            case "system.status":
            case "device.status":
                return deviceStatus();
            case "device.info":
                return deviceInfo();
            case "device.permissions":
                return permissions();
            case "device.apps":
                return installedApps();
            case "android.apps.launch":
                return launchApp(params);
            case "android.files.pick":
            case "android.permissions.request":
            case "android.liveConversation.open":
            case "android.mic.probe":
            case "android.speaker.test":
            case "talk.ptt.start":
            case "talk.ptt.stop":
            case "talk.ptt.cancel":
            case "talk.ptt.once":
                openDashboardActivity();
                return new JSONObject()
                        .put("foregroundRequired", true)
                        .put("openedApp", true)
                        .put("command", command);
            default:
                throw new IllegalArgumentException("Unsupported Android node command: " + command);
        }
    }

    private JSONObject deviceInfo() throws Exception {
        return new JSONObject()
                .put("platform", "android")
                .put("manufacturer", Build.MANUFACTURER)
                .put("model", Build.MODEL)
                .put("device", Build.DEVICE)
                .put("sdk", Build.VERSION.SDK_INT)
                .put("release", Build.VERSION.RELEASE)
                .put("nodeClient", "openclaw-android-dashboard")
                .put("nodeClientVersion", BuildConfig.VERSION_NAME);
    }

    private JSONObject deviceStatus() throws Exception {
        BatteryManager batteryManager = (BatteryManager) getSystemService(BATTERY_SERVICE);
        int battery = batteryManager == null ? -1 : batteryManager.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY);
        return deviceInfo()
                .put("batteryPercent", battery < 0 ? JSONObject.NULL : battery)
                .put("node", snapshot())
                .put("ts", System.currentTimeMillis());
    }

    private JSONObject permissions() throws Exception {
        boolean postNotifications = Build.VERSION.SDK_INT < 33 || granted(Manifest.permission.POST_NOTIFICATIONS);
        boolean appNotificationsEnabled = PhoneNotificationListenerService.areAppNotificationsEnabled(this);
        boolean notificationAccess = PhoneNotificationListenerService.isAccessEnabled(this);
        return new JSONObject()
                .put("recordAudio", granted(Manifest.permission.RECORD_AUDIO))
                .put("camera", granted(Manifest.permission.CAMERA))
                .put("bluetoothConnect", granted(Manifest.permission.BLUETOOTH_CONNECT))
                .put("notifications", notificationAccess)
                .put("canPostNotifications", postNotifications && appNotificationsEnabled)
                .put("postNotifications", postNotifications)
                .put("appNotificationsEnabled", appNotificationsEnabled)
                .put("contacts", granted(Manifest.permission.READ_CONTACTS))
                .put("contactsWrite", granted(Manifest.permission.WRITE_CONTACTS))
                .put("calendarRead", granted(Manifest.permission.READ_CALENDAR))
                .put("calendarWrite", granted(Manifest.permission.WRITE_CALENDAR))
                .put("callLogRead", granted(Manifest.permission.READ_CALL_LOG))
                .put("smsRead", granted(Manifest.permission.READ_SMS))
                .put("smsSend", granted(Manifest.permission.SEND_SMS))
                .put("callPhone", granted(Manifest.permission.CALL_PHONE))
                .put("notificationAccess", notificationAccess)
                .put("notificationAccessConnected", PhoneNotificationListenerService.isConnected())
                .put("accessibilityControl", PhoneAccessibilityService.isConnected());
    }

    private JSONObject installedApps() throws Exception {
        PackageManager manager = getPackageManager();
        Intent launcher = new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER);
        List<ResolveInfo> activities = manager.queryIntentActivities(launcher, 0);
        JSONArray apps = new JSONArray();
        for (ResolveInfo info : activities) {
            if (info == null || info.activityInfo == null) continue;
            apps.put(new JSONObject()
                    .put("label", String.valueOf(info.loadLabel(manager)))
                    .put("packageName", info.activityInfo.packageName)
                    .put("activityName", info.activityInfo.name));
            if (apps.length() >= 250) break;
        }
        return new JSONObject().put("apps", apps).put("count", apps.length());
    }

    private JSONObject launchApp(JSONObject params) throws Exception {
        String packageName = params.optString("packageName", params.optString("package", "")).trim();
        if (packageName.isEmpty()) throw new IllegalArgumentException("packageName is required.");
        Intent launch = getPackageManager().getLaunchIntentForPackage(packageName);
        if (launch == null) throw new IllegalArgumentException("App is not launchable: " + packageName);
        launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        startActivity(launch);
        return new JSONObject().put("launched", true).put("packageName", packageName);
    }

    private void openDashboardActivity() {
        Intent launch = new Intent(this, MainActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        startActivity(launch);
    }

    @Override public void onStatus(String status) {
        if (status != null && (status.startsWith("Connection failed") || status.startsWith("Disconnected"))) connecting = false;
        updateState(statusToState(status), "", client.isConnected());
    }

    @Override public void onConnected(JSONObject hello) {
        connecting = false;
        connectStartedAtMs = 0L;
        nextReconnectAtMs = 0L;
        retryAttempt = 0;
        handler.removeCallbacks(reconnectRunnable);
        String nodeId = client.nodeId();
        prefs.edit()
                .putString("trace.node.id", nodeId)
                .putString("trace.node.connectionId", client.connectionId())
                .putLong("trace.node.connectedAt", System.currentTimeMillis())
                .putString("trace.node.lastError", "")
                .putInt("trace.node.retryAttempt", 0)
                .apply();
        updateState("connected", "", true);
        audit.record("node", "connected", fields(
                "nodeId", nodeId,
                "nodeConnectionId", client.connectionId(),
                "clientVersion", BuildConfig.VERSION_NAME));
    }

    @Override public void onDashboard(JSONObject dashboard) {
        audit.record("node", "gateway_snapshot", fields("sections", dashboard == null ? 0 : dashboard.length()));
    }

    @Override public void onLog(String message) {
        audit.record("node", "protocol_event", fields("message", message == null ? "" : message));
    }

    @Override public void onError(String message) {
        connecting = false;
        prefs.edit().putString("trace.node.lastError", message == null ? "" : message).apply();
        updateState("error", message, false);
        audit.record("node", "error", fields("message", message == null ? "" : message));
        scheduleReconnect("client_error");
    }

    @Override public void onDisconnected(String reason) {
        connecting = false;
        updateState("disconnected", reason, false);
        audit.record("node", "disconnected", fields("reason", reason));
        scheduleReconnect(reason);
    }

    @Override public void onInvokeStarted(String invokeId, String correlationId, String command, JSONObject params) {
        JSONObject details = sessionTrace();
        try {
            details.put("invokeId", invokeId)
                    .put("correlationId", correlationId)
                    .put("command", command)
                    .put("params", params);
        } catch (Exception ignored) { }
        audit.record("invoke", "started", details);
    }

    @Override public void onInvokeFinished(String invokeId, String correlationId, String command, boolean ok, long durationMs, JSONObject result) {
        JSONObject details = sessionTrace();
        try {
            details.put("invokeId", invokeId)
                    .put("correlationId", correlationId)
                    .put("command", command)
                    .put("ok", ok)
                    .put("durationMs", durationMs)
                    .put("resultSummary", summarizeResult(result));
        } catch (Exception ignored) { }
        audit.record("invoke", "finished", details);
    }

    private void scheduleReconnect(String reason) {
        if (manualStop || !prefs.getBoolean("nodeEnabled", true)) return;
        long now = System.currentTimeMillis();
        if (nextReconnectAtMs > now) return;
        handler.removeCallbacks(reconnectRunnable);
        retryAttempt++;
        long base = Math.min(MAX_RETRY_MS, 1000L << Math.min(retryAttempt - 1, 6));
        long jitter = ThreadLocalRandom.current().nextLong(Math.max(1L, base / 4L));
        long delay = Math.min(MAX_RETRY_MS, base + jitter);
        nextReconnectAtMs = now + delay;
        prefs.edit().putInt("trace.node.retryAttempt", retryAttempt).apply();
        updateState("retry_wait", reason, false);
        audit.record("node", "reconnect_scheduled", fields(
                "attempt", retryAttempt,
                "delayMs", delay,
                "reason", reason));
        handler.postDelayed(reconnectRunnable, delay);
    }

    private synchronized void resetConnection(String reason) {
        handler.removeCallbacks(reconnectRunnable);
        client.disconnect();
        connecting = false;
        connectStartedAtMs = 0L;
        nextReconnectAtMs = 0L;
        retryAttempt = 0;
        audit.record("node", "connection_reset", fields("reason", reason));
    }

    private void scheduleWatchdog() {
        handler.removeCallbacks(watchdogRunnable);
        handler.postDelayed(watchdogRunnable, WATCHDOG_INTERVAL_MS);
    }

    private boolean hasUsableNetwork() {
        if (connectivityManager == null) return true;
        Network network = connectivityManager.getActiveNetwork();
        if (network == null) return false;
        NetworkCapabilities capabilities = connectivityManager.getNetworkCapabilities(network);
        return capabilities != null
                && capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET);
    }

    private synchronized void runConnectionWatchdog() {
        if (manualStop || !prefs.getBoolean("nodeEnabled", false)) return;
        long now = System.currentTimeMillis();
        String configured = prefs.getString("url", "");
        configured = configured == null ? "" : configured.trim();
        if (!activeGatewayUrl.isEmpty() && !activeGatewayUrl.equals(configured)) {
            resetConnection("watchdog_configuration_changed");
            connectNow();
        } else if (connecting && now - connectStartedAtMs >= CONNECT_TIMEOUT_MS) {
            audit.record("node", "connect_timeout", fields("elapsedMs", now - connectStartedAtMs));
            resetConnection("connect_timeout");
            connectNow();
        } else if (!client.isConnected() && !connecting
                && (nextReconnectAtMs == 0L || now >= nextReconnectAtMs)) {
            audit.record("node", "watchdog_reconnect", new JSONObject());
            connectNow();
        }
        scheduleWatchdog();
    }

    private void updateState(String state, String error, boolean connected) {
        long now = System.currentTimeMillis();
        SharedPreferences.Editor edit = prefs.edit()
                .putString("trace.node.state", state == null ? "unknown" : state)
                .putLong("trace.node.lastEventAt", now)
                .putInt("trace.node.retryAttempt", retryAttempt);
        if (error != null && !error.isEmpty()) edit.putString("trace.node.lastError", error);
        if (!connected && "stopped".equals(state)) edit.putLong("trace.node.connectedAt", 0L);
        edit.apply();
        updateNotification(state);
        dispatchState();
    }

    private void dispatchState() {
        StateListener listener = stateListener;
        if (listener == null) return;
        JSONObject state = snapshot();
        handler.post(() -> listener.onNodeState(state));
    }

    private JSONObject sessionTrace() {
        JSONObject result = new JSONObject();
        try {
            result.put("sessionKey", prefs.getString("trace.session.key", ""));
            result.put("sessionId", prefs.getString("trace.session.id", ""));
            result.put("agentId", prefs.getString("trace.session.agentId", ""));
            result.put("nodeConnectionId", client.connectionId());
        } catch (Exception ignored) { }
        return result;
    }

    private static JSONObject summarizeResult(JSONObject result) {
        JSONObject summary = new JSONObject();
        if (result == null) return summary;
        try {
            JSONArray keys = new JSONArray();
            java.util.Iterator<String> iterator = result.keys();
            while (iterator.hasNext()) keys.put(iterator.next());
            summary.put("keys", keys);
            if (result.has("count")) summary.put("count", result.optInt("count"));
            if (result.has("created")) summary.put("created", result.optBoolean("created"));
            if (result.has("submitted")) summary.put("submitted", result.optBoolean("submitted"));
            if (result.has("opened")) summary.put("opened", result.optBoolean("opened"));
        } catch (Exception ignored) { }
        return summary;
    }

    private boolean granted(String permission) {
        return checkSelfPermission(permission) == PackageManager.PERMISSION_GRANTED;
    }

    private void registerNetworkCallback() {
        connectivityManager = (ConnectivityManager) getSystemService(CONNECTIVITY_SERVICE);
        if (connectivityManager == null) return;
        networkCallback = new ConnectivityManager.NetworkCallback() {
            @Override public void onAvailable(Network network) {
                audit.record("network", "available", new JSONObject());
                resetConnection("network_available");
                handler.post(reconnectRunnable);
            }

            @Override public void onLost(Network network) {
                audit.record("network", "lost", new JSONObject());
                resetConnection("network_lost");
                updateState("network_lost", "Network unavailable", false);
            }
        };
        try { connectivityManager.registerDefaultNetworkCallback(networkCallback); }
        catch (Exception error) { audit.record("network", "callback_error", fields("message", error.getMessage())); }
    }

    private void createNotificationChannel() {
        NotificationManager manager = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (manager == null) return;
        NotificationChannel channel = new NotificationChannel(CHANNEL_ID, "OpenClaw phone node", NotificationManager.IMPORTANCE_LOW);
        channel.setDescription("Keeps Android phone capabilities connected to OpenClaw");
        manager.createNotificationChannel(channel);
    }

    private Notification buildNotification(String state) {
        Intent open = new Intent(this, MainActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent contentIntent = PendingIntent.getActivity(this, 0, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Intent reconnect = new Intent(this, PhoneNodeService.class).setAction(ACTION_RECONNECT);
        PendingIntent reconnectIntent = PendingIntent.getService(this, 1, reconnect,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this, CHANNEL_ID)
                .setSmallIcon(android.R.drawable.stat_notify_sync)
                .setContentTitle("OpenClaw phone node")
                .setContentText(state == null ? "Starting" : state.replace('_', ' '))
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .setContentIntent(contentIntent)
                .addAction(new Notification.Action.Builder(null, "Reconnect", reconnectIntent).build())
                .build();
    }

    private void updateNotification(String state) {
        NotificationManager manager = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (manager != null && prefs.getBoolean("nodeEnabled", false)) manager.notify(NOTIFICATION_ID, buildNotification(state));
    }

    private static String statusToState(String status) {
        if (status == null) return "unknown";
        String normalized = status.toLowerCase(Locale.ROOT);
        if (normalized.contains("connected as")) return "connected";
        if (normalized.contains("challenge")) return "authenticating";
        if (normalized.contains("connecting")) return "connecting";
        if (normalized.contains("pairing") || normalized.contains("auth required")) return "pairing_required";
        if (normalized.contains("failed")) return "error";
        if (normalized.contains("disconnected")) return "disconnected";
        return normalized.replace(' ', '_');
    }

    private static String sanitizeGateway(String value) {
        if (value == null) return "";
        int query = value.indexOf('?');
        return query >= 0 ? value.substring(0, query) : value;
    }

    private static JSONObject fields(Object... pairs) {
        JSONObject result = new JSONObject();
        for (int i = 0; i + 1 < pairs.length; i += 2) {
            try { result.put(String.valueOf(pairs[i]), pairs[i + 1]); }
            catch (Exception ignored) { }
        }
        return result;
    }

    private static String toGatewayWebSocketUrl(String raw) {
        String value = raw == null ? "" : raw.trim();
        if (value.isEmpty()) throw new IllegalStateException("Gateway URL is required.");
        if (value.startsWith("https://")) value = "wss://" + value.substring(8);
        else if (value.startsWith("http://")) value = "ws://" + value.substring(7);
        else if (!value.startsWith("ws://") && !value.startsWith("wss://")) value = "wss://" + value;
        if (!value.endsWith("/")) value += "/";
        return value;
    }
}
