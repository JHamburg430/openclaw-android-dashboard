package ai.openclaw.dashboard;

import android.Manifest;
import android.app.Activity;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ResolveInfo;
import android.content.pm.PackageManager;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.Insets;
import android.graphics.drawable.GradientDrawable;
import android.media.AudioManager;
import android.media.AudioFormat;
import android.media.AudioDeviceInfo;
import android.media.AudioRecord;
import android.media.AudioTrack;
import android.media.MediaRecorder;
import android.media.audiofx.AcousticEchoCanceler;
import android.media.audiofx.AutomaticGainControl;
import android.media.audiofx.NoiseSuppressor;
import android.net.Uri;
import android.os.BatteryManager;
import android.os.Build;
import android.os.Bundle;
import android.text.InputType;
import android.util.Log;
import android.util.Base64;
import android.webkit.ConsoleMessage;
import android.webkit.JavascriptInterface;
import android.webkit.PermissionRequest;
import android.webkit.ValueCallback;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.Window;
import android.view.WindowInsets;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import org.json.JSONObject;
import org.json.JSONArray;

import java.io.ByteArrayInputStream;
import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.FileReader;
import java.io.IOException;
import java.io.InputStream;
import java.net.InetAddress;
import java.net.UnknownHostException;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.ArrayDeque;
import java.util.Collections;
import java.util.Date;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.text.SimpleDateFormat;
import java.util.Locale;
import java.util.concurrent.atomic.AtomicBoolean;

import okhttp3.Dns;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;

public final class MainActivity extends Activity {
    private static final String PREFS = "openclaw_dashboard";
    private static final int REQUEST_RECORD_AUDIO = 2001;
    private static final int REQUEST_POST_NOTIFICATIONS = 2002;
    private static final int REQUEST_CAMERA = 2003;
    private static final int REQUEST_FILE_CHOOSER = 2004;
    private static final int REQUEST_BLUETOOTH_CONNECT = 2005;
    private static final String TAG = "OpenClawDashboard";
    private static final int APP_VERSION_CODE = 35;
    private static final String APP_VERSION_NAME = "1.0.35";
    private static final int MAX_DIAGNOSTIC_LINES = 120;
    private static final int TALK_FRAME_MS = 10;
    private static final String NOTIFICATION_CHANNEL_ID = "openclaw_updates";
    private static final int NOTIFICATION_ID = 41001;
    private static final String PREF_NOTIFICATION_COUNT = "notification_count";
    private static final int COLOR_APP_CHROME = Color.rgb(6, 10, 16);
    private static final int COLOR_PANEL = Color.rgb(20, 26, 34);
    private static final int COLOR_CONTROL = Color.rgb(30, 38, 48);
    private static final int COLOR_TEXT_PRIMARY = Color.rgb(244, 248, 252);
    private static final int COLOR_TEXT_SECONDARY = Color.rgb(198, 211, 224);
    private static final int COLOR_TEXT_MUTED = Color.rgb(154, 169, 184);
    private static final int COLOR_ACCENT = Color.rgb(0, 171, 126);

    private final OkHttpClient httpClient = new OkHttpClient.Builder()
            .dns(new HostsFileDns())
            .build();
    private final ArrayDeque<String> diagnosticsLines = new ArrayDeque<>();
    private final SimpleDateFormat diagnosticsTimeFormat = new SimpleDateFormat("HH:mm:ss.SSS", Locale.US);
    private final NativeAudioBridge nativeAudioBridge = new NativeAudioBridge();
    private final NativeAppBridge nativeAppBridge = new NativeAppBridge();
    private SharedPreferences prefs;
    private OpenClawClient nodeClient;
    private PermissionRequest pendingPermissionRequest;
    private ValueCallback<Uri[]> pendingFilePathCallback;

    private EditText setupCodeInput;
    private EditText urlInput;
    private EditText tokenInput;
    private EditText passwordInput;
    private TextView statusText;
    private TextView hintText;
    private TextView nodeStatusText;
    private TextView diagnosticsText;
    private LinearLayout chromeContainer;
    private LinearLayout topActions;
    private LinearLayout settingsPanel;
    private LinearLayout appsDrawer;
    private ScrollView controlsScroll;
    private Button openButton;
    private Button reloadButton;
    private Button connectNodeButton;
    private Button chromeToggleButton;
    private Button overlayToggleButton;
    private WebView webView;
    private boolean chromeExpanded = true;
    private boolean appsDrawerVisible = false;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        createNotificationChannel();
        configureSystemBars();
        buildUi();
        loadPrefs();
        nodeClient = new OpenClawClient(new IdentityStore(this), new DashboardNodeListener(), this::handleNativeNodeCommand);
        ensureNotificationPermission();
        if (!restoreWebViewState(savedInstanceState)) {
            maybeAutoOpenDashboard();
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        clearNativeNotifications();
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        if (webView != null) {
            webView.saveState(outState);
        }
        super.onSaveInstanceState(outState);
    }

    @Override
    protected void onDestroy() {
        if (webView != null) {
            webView.destroy();
            webView = null;
        }
        nativeAudioBridge.shutdown();
        if (nodeClient != null) {
            nodeClient.disconnect();
            nodeClient = null;
        }
        super.onDestroy();
    }

    private void buildUi() {
        FrameLayout shellRoot = new FrameLayout(this);
        shellRoot.setBackgroundColor(COLOR_APP_CHROME);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(COLOR_APP_CHROME);
        shellRoot.addView(root, new FrameLayout.LayoutParams(-1, -1));

        chromeContainer = new LinearLayout(this);
        chromeContainer.setOrientation(LinearLayout.VERTICAL);
        chromeContainer.setBackgroundColor(COLOR_APP_CHROME);
        applyStatusBarInset(chromeContainer);
        root.addView(chromeContainer, new LinearLayout.LayoutParams(-1, -2));

        statusText = text("Configure the gateway URL and auth, then load the real Control UI.", 14, COLOR_TEXT_SECONDARY, true);
        statusText.setGravity(Gravity.CENTER_VERTICAL);
        statusText.setMinHeight(dp(44));
        statusText.setPadding(dp(14), dp(8), dp(14), dp(6));
        chromeContainer.addView(statusText, new LinearLayout.LayoutParams(-1, -2));

        topActions = new LinearLayout(this);
        topActions.setOrientation(LinearLayout.HORIZONTAL);
        topActions.setGravity(Gravity.CENTER_VERTICAL);
        topActions.setPadding(dp(14), dp(2), dp(14), dp(10));
        topActions.setBackgroundColor(COLOR_APP_CHROME);
        openButton = button("Open UI");
        reloadButton = button("Reload");
        connectNodeButton = button("Node");
        chromeToggleButton = button("-");
        chromeToggleButton.setContentDescription("Collapse controls");
        topActions.addView(openButton, new LinearLayout.LayoutParams(0, dp(38), 1));
        topActions.addView(reloadButton, new LinearLayout.LayoutParams(0, dp(38), 1));
        topActions.addView(connectNodeButton, new LinearLayout.LayoutParams(0, dp(38), 1));
        topActions.addView(chromeToggleButton, new LinearLayout.LayoutParams(dp(38), dp(38)));
        chromeContainer.addView(topActions, new LinearLayout.LayoutParams(-1, -2));

        controlsScroll = new ScrollView(this);
        settingsPanel = section();
        controlsScroll.addView(settingsPanel);
        chromeContainer.addView(controlsScroll, new LinearLayout.LayoutParams(-1, -2));

        LinearLayout controls = settingsPanel;
        setupCodeInput = input("Paste setup code from openclaw qr --json", false, false);
        setupCodeInput.setImportantForAutofill(View.IMPORTANT_FOR_AUTOFILL_NO);
        controls.addView(label("Setup code"));
        controls.addView(setupCodeInput);
        Button decode = button("Decode setup code");
        controls.addView(decode);

        urlInput = input("http://100.76.133.101:18789/ or ws://...", false, true);
        tokenInput = input("gateway token", false, true);
        tokenInput.setImportantForAutofill(View.IMPORTANT_FOR_AUTOFILL_NO);
        passwordInput = input("gateway password", true, true);
        passwordInput.setAutofillHints(View.AUTOFILL_HINT_PASSWORD);
        controls.addView(label("Gateway URL"));
        controls.addView(urlInput);
        controls.addView(label("Gateway token"));
        controls.addView(tokenInput);
        controls.addView(label("Password"));
        controls.addView(passwordInput);

        hintText = text(
                "This app now embeds the actual OpenClaw Control UI. If the gateway asks for pairing, approve the pending device request from the host once, then reload.",
                13,
                COLOR_TEXT_MUTED,
                false);
        hintText.setPadding(0, dp(12), 0, 0);
        controls.addView(hintText);

        controls.addView(label("Android node"));
        nodeStatusText = text("Custom dashboard node is not connected yet.", 13, COLOR_TEXT_SECONDARY, false);
        nodeStatusText.setPadding(dp(10), dp(8), dp(10), dp(8));
        nodeStatusText.setBackgroundColor(Color.rgb(10, 14, 20));
        controls.addView(nodeStatusText);

        LinearLayout appActions = new LinearLayout(this);
        appActions.setOrientation(LinearLayout.HORIZONTAL);
        appActions.setGravity(Gravity.CENTER_VERTICAL);
        Button appsButton = button("Apps");
        Button liveConversationButton = button("Live Conversation");
        appActions.addView(appsButton, new LinearLayout.LayoutParams(0, dp(42), 1));
        appActions.addView(liveConversationButton, new LinearLayout.LayoutParams(0, dp(42), 1));
        controls.addView(appActions);

        controls.addView(label("Diagnostics"));
        LinearLayout diagnosticsActions = new LinearLayout(this);
        diagnosticsActions.setOrientation(LinearLayout.HORIZONTAL);
        diagnosticsActions.setGravity(Gravity.CENTER_VERTICAL);
        Button copyDiagnostics = button("Copy Diagnostics");
        Button clearDiagnostics = button("Clear");
        Button probeMic = button("Probe Mic");
        diagnosticsActions.addView(copyDiagnostics, new LinearLayout.LayoutParams(0, dp(42), 1));
        diagnosticsActions.addView(clearDiagnostics, new LinearLayout.LayoutParams(0, dp(42), 1));
        diagnosticsActions.addView(probeMic, new LinearLayout.LayoutParams(0, dp(42), 1));
        controls.addView(diagnosticsActions);

        diagnosticsText = text("No diagnostics yet.", 12, COLOR_TEXT_SECONDARY, false);
        diagnosticsText.setTextIsSelectable(true);
        diagnosticsText.setPadding(dp(10), dp(8), dp(10), dp(8));
        diagnosticsText.setBackgroundColor(Color.rgb(10, 14, 20));
        controls.addView(diagnosticsText);

        FrameLayout webContainer = new FrameLayout(this);
        LinearLayout.LayoutParams webLp = new LinearLayout.LayoutParams(-1, 0, 1);
        webLp.setMargins(0, dp(4), 0, 0);
        root.addView(webContainer, webLp);

        webView = new WebView(this);
        webView.setBackgroundColor(COLOR_APP_CHROME);
        WebView.setWebContentsDebuggingEnabled(true);
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        settings.setBuiltInZoomControls(false);
        settings.setDisplayZoomControls(false);
        settings.setUseWideViewPort(true);
        settings.setLoadWithOverviewMode(true);
        settings.setMediaPlaybackRequiresUserGesture(false);
        webView.addJavascriptInterface(new DiagnosticsBridge(), "OpenClawDiag");
        webView.addJavascriptInterface(new NativeNotificationsBridge(), "OpenClawNativeNotifications");
        webView.addJavascriptInterface(nativeAudioBridge, "OpenClawNativeAudio");
        webView.addJavascriptInterface(nativeAppBridge, "OpenClawNativeApp");
        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onConsoleMessage(ConsoleMessage consoleMessage) {
                recordDiagnostic("console." + consoleMessage.messageLevel().name().toLowerCase(Locale.US),
                        consoleMessage.message());
                Log.d(TAG, "console " + consoleMessage.messageLevel() + " " + consoleMessage.sourceId() + ":" + consoleMessage.lineNumber() + " " + consoleMessage.message());
                return super.onConsoleMessage(consoleMessage);
            }

            @Override
            public void onPermissionRequest(PermissionRequest request) {
                runOnUiThread(() -> handleWebPermissionRequest(request));
            }

            @Override
            public void onPermissionRequestCanceled(PermissionRequest request) {
                runOnUiThread(() -> {
                    if (pendingPermissionRequest == request) {
                        pendingPermissionRequest = null;
                    }
                    statusText.setText("Media permission request was canceled.");
                });
            }

            @Override
            public boolean onShowFileChooser(WebView webView, ValueCallback<Uri[]> filePathCallback, FileChooserParams fileChooserParams) {
                if (pendingFilePathCallback != null) {
                    pendingFilePathCallback.onReceiveValue(null);
                }
                pendingFilePathCallback = filePathCallback;
                Intent intent;
                try {
                    intent = fileChooserParams.createIntent();
                } catch (Exception e) {
                    intent = new Intent(Intent.ACTION_OPEN_DOCUMENT);
                    intent.addCategory(Intent.CATEGORY_OPENABLE);
                    intent.setType("*/*");
                }
                try {
                    startActivityForResult(intent, REQUEST_FILE_CHOOSER);
                    return true;
                } catch (Exception e) {
                    pendingFilePathCallback = null;
                    statusText.setText("File picker is unavailable on this device.");
                    recordDiagnostic("file_chooser.failed", e.getMessage());
                    return false;
                }
            }
        });
        webView.setWebViewClient(new WebViewClient() {
            @Override
            public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                return interceptControlUiHtml(request);
            }

            @Override
            public void onPageStarted(WebView view, String url, android.graphics.Bitmap favicon) {
                recordDiagnostic("page.started", url);
                Log.d(TAG, "page started " + url);
                setConnectedUiVisible(false);
                statusText.setText("Loading Control UI");
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                recordDiagnostic("page.finished", url);
                injectRuntimeScripts(view, "page_finished");
                Log.d(TAG, "page finished " + url);
                setConnectedUiVisible(true);
            }

            @Override
            public void onReceivedError(WebView view, android.webkit.WebResourceRequest request, android.webkit.WebResourceError error) {
                if (request.isForMainFrame()) {
                    String description = error == null ? "unknown error" : String.valueOf(error.getDescription());
                    recordDiagnostic("page.error", description);
                    Log.e(TAG, "page error " + request.getUrl() + " " + description);
                    setConnectedUiVisible(false);
                    statusText.setText("Control UI load failed: " + description);
                }
            }

            @Override
            public void onReceivedHttpError(WebView view, android.webkit.WebResourceRequest request, android.webkit.WebResourceResponse errorResponse) {
                if (request.isForMainFrame()) {
                    int statusCode = errorResponse == null ? -1 : errorResponse.getStatusCode();
                    recordDiagnostic("page.http_error", String.valueOf(statusCode));
                    Log.e(TAG, "http error " + request.getUrl() + " " + statusCode);
                    setConnectedUiVisible(false);
                    statusText.setText("Control UI load failed: HTTP " + statusCode);
                }
            }
        });
        webContainer.addView(webView, new FrameLayout.LayoutParams(-1, -1));

        appsDrawer = buildAppsDrawer();
        appsDrawer.setVisibility(View.GONE);
        FrameLayout.LayoutParams appsLp = new FrameLayout.LayoutParams(-1, -2, Gravity.BOTTOM);
        appsLp.setMargins(dp(10), 0, dp(10), dp(10));
        shellRoot.addView(appsDrawer, appsLp);

        overlayToggleButton = button("+");
        overlayToggleButton.setTextSize(12);
        overlayToggleButton.setPadding(0, 0, 0, dp(1));
        overlayToggleButton.setContentDescription("Open apps");
        overlayToggleButton.setVisibility(View.GONE);
        FrameLayout.LayoutParams overlayLp = new FrameLayout.LayoutParams(dp(24), dp(22), Gravity.TOP | Gravity.RIGHT);
        overlayLp.setMargins(0, dp(2), dp(4), 0);
        shellRoot.addView(overlayToggleButton, overlayLp);
        applyStatusBarMargin(overlayToggleButton, dp(2));

        decode.setOnClickListener(v -> decodeSetupCode());
        openButton.setOnClickListener(v -> openDashboard());
        reloadButton.setOnClickListener(v -> webView.reload());
        connectNodeButton.setOnClickListener(v -> connectDashboardNode());
        chromeToggleButton.setOnClickListener(v -> setChromeExpanded(!chromeExpanded));
        overlayToggleButton.setOnClickListener(v -> setAppsDrawerVisible(!appsDrawerVisible));
        appsButton.setOnClickListener(v -> setAppsDrawerVisible(true));
        liveConversationButton.setOnClickListener(v -> openLiveConversation());
        copyDiagnostics.setOnClickListener(v -> copyDiagnostics());
        clearDiagnostics.setOnClickListener(v -> clearDiagnostics());
        probeMic.setOnClickListener(v -> runNativeMicProbe());
        setContentView(shellRoot);
        recordDiagnostic("app.ready", "Diagnostics bridge initialized version=" + APP_VERSION_NAME);
    }

    private LinearLayout buildAppsDrawer() {
        LinearLayout drawer = new LinearLayout(this);
        drawer.setOrientation(LinearLayout.VERTICAL);
        drawer.setPadding(dp(12), dp(10), dp(12), dp(12));
        drawer.setBackground(panelBackground(COLOR_PANEL, dp(8)));

        TextView title = text("Apps", 15, COLOR_TEXT_PRIMARY, true);
        title.setPadding(0, 0, 0, dp(6));
        drawer.addView(title);

        LinearLayout row1 = appButtonRow();
        row1.addView(appButton("Control UI", v -> {
            setAppsDrawerVisible(false);
            openDashboard();
        }), new LinearLayout.LayoutParams(0, dp(46), 1));
        row1.addView(appButton("Live Conversation", v -> {
            setAppsDrawerVisible(false);
            openLiveConversation();
        }), new LinearLayout.LayoutParams(0, dp(46), 1));
        drawer.addView(row1);

        LinearLayout row2 = appButtonRow();
        row2.addView(appButton("Teams Help", v -> openLocalApp(8504)), new LinearLayout.LayoutParams(0, dp(46), 1));
        row2.addView(appButton("Contacts", v -> openLocalApp(8503)), new LinearLayout.LayoutParams(0, dp(46), 1));
        drawer.addView(row2);

        LinearLayout row3 = appButtonRow();
        row3.addView(appButton("Monitor", v -> openLocalApp(8501)), new LinearLayout.LayoutParams(0, dp(46), 1));
        row3.addView(appButton("Android Native", v -> {
            setAppsDrawerVisible(false);
            openNativeToolsPage();
        }), new LinearLayout.LayoutParams(0, dp(46), 1));
        drawer.addView(row3);

        Button close = button("Close");
        close.setOnClickListener(v -> setAppsDrawerVisible(false));
        drawer.addView(close, new LinearLayout.LayoutParams(-1, dp(40)));
        return drawer;
    }

    private LinearLayout appButtonRow() {
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);
        row.setPadding(0, dp(3), 0, dp(3));
        return row;
    }

    private Button appButton(String label, View.OnClickListener listener) {
        Button button = button(label);
        button.setTextSize(13);
        button.setOnClickListener(listener);
        return button;
    }

    private void setAppsDrawerVisible(boolean visible) {
        appsDrawerVisible = visible;
        if (appsDrawer != null) {
            appsDrawer.setVisibility(visible ? View.VISIBLE : View.GONE);
        }
    }

    private void openLocalApp(int port) {
        setAppsDrawerVisible(false);
        try {
            String url = buildSiblingAppUrl(port);
            recordDiagnostic("app.open", url);
            webView.stopLoading();
            webView.loadUrl(url);
            setConnectedUiVisible(true);
            statusText.setText("Opening app on port " + port);
        } catch (Exception e) {
            statusText.setText("Could not open app: " + e.getMessage());
            recordDiagnostic("app.open.failed", e.getMessage());
        }
    }

    private void openLiveConversation() {
        try {
            connectDashboardNode();
            setConnectedUiVisible(true);
            setChromeExpanded(false);
            webView.stopLoading();
            webView.loadDataWithBaseURL(buildNativePageBaseUrl("live-conversation"), buildLiveConversationHtml(), "text/html", "UTF-8", null);
            statusText.setText("Live Conversation native page loaded.");
            recordDiagnostic("live_conversation.open", "native_page");
        } catch (Exception e) {
            statusText.setText("Live Conversation unavailable: " + e.getMessage());
            recordDiagnostic("live_conversation.failed", e.getMessage());
        }
    }

    private void openNativeToolsPage() {
        try {
            connectDashboardNode();
            setConnectedUiVisible(true);
            setChromeExpanded(false);
            webView.stopLoading();
            webView.loadDataWithBaseURL(buildNativePageBaseUrl("android-native"), buildAndroidNativeHtml(), "text/html", "UTF-8", null);
            statusText.setText("Android Native tools loaded.");
            recordDiagnostic("android_native.open", "native_page");
        } catch (Exception e) {
            statusText.setText("Android Native unavailable: " + e.getMessage());
            recordDiagnostic("android_native.failed", e.getMessage());
        }
    }

    private String buildSiblingAppUrl(int port) {
        String dashboard = buildDashboardUrl();
        java.net.URI uri = java.net.URI.create(dashboard);
        String host = uri.getHost();
        if (host == null || host.trim().isEmpty()) {
            throw new IllegalStateException("Dashboard host is missing.");
        }
        // The custom Streamlit/monitor apps are served directly by local
        // user services on plain HTTP ports, even when Control UI is reached
        // through a secure Tailscale/OpenClaw dashboard URL.
        return "http://" + host + ":" + port + "/";
    }

    private String buildNativePageBaseUrl(String pageName) {
        try {
            java.net.URI uri = java.net.URI.create(buildDashboardUrl());
            String scheme = uri.getScheme() == null ? "https" : uri.getScheme();
            String host = uri.getHost() == null ? "openclaw.local" : uri.getHost();
            return scheme + "://" + host + "/android-native/" + pageName + "/";
        } catch (Exception ignored) {
            return "https://openclaw.local/android-native/" + pageName + "/";
        }
    }

    private void connectDashboardNode() {
        if (nodeClient == null) {
            nodeClient = new OpenClawClient(new IdentityStore(this), new DashboardNodeListener(), this::handleNativeNodeCommand);
        }
        try {
            savePrefs();
            String gatewayWsUrl = toGatewayWebSocketUrl(value(urlInput));
            nodeStatusText.setText("Connecting custom dashboard node...");
            nodeClient.connect(new OpenClawClient.Config(
                    gatewayWsUrl,
                    "",
                    value(tokenInput),
                    value(passwordInput),
                    "OpenClaw Dashboard " + Build.MODEL));
        } catch (Exception e) {
            nodeStatusText.setText("Node connect failed: " + e.getMessage());
            recordDiagnostic("node.connect.failed", e.getMessage());
        }
    }

    private JSONObject handleNativeNodeCommand(String command, JSONObject params) throws Exception {
        recordDiagnostic("node.invoke", command);
        switch (command) {
            case "system.ping":
                return new JSONObject().put("pong", true).put("ts", System.currentTimeMillis());
            case "system.status":
            case "device.status":
                return nativeDeviceStatus();
            case "device.info":
                return nativeDeviceInfo();
            case "device.permissions":
                return nativePermissions();
            case "device.apps":
                return nativeInstalledApps();
            case "system.notify":
                postNativeNotification(
                        params.optString("title", "OpenClaw"),
                        new JSONObject().put("body", params.optString("body", params.optString("message", ""))).toString());
                return new JSONObject().put("ok", true);
            case "android.apps.launch":
                return launchAndroidApp(params);
            case "android.files.pick":
                openAndroidFilePicker();
                return new JSONObject().put("opened", true).put("note", "File picker opened on the device.");
            case "android.liveConversation.open":
                runOnUiThread(this::openLiveConversation);
                return new JSONObject().put("opened", true).put("surface", "live_conversation");
            case "android.mic.probe":
                runOnUiThread(this::runNativeMicProbe);
                return new JSONObject().put("started", true).put("permission", hasRecordAudioPermission());
            case "android.speaker.test":
                nativeAudioBridge.playTestTone();
                return new JSONObject().put("started", true).put("output", "AudioTrack");
            default:
                return new JSONObject()
                        .put("ok", false)
                        .put("unsupported", command);
        }
    }

    private JSONObject nativeDeviceInfo() throws Exception {
        return new JSONObject()
                .put("platform", "android")
                .put("manufacturer", Build.MANUFACTURER)
                .put("model", Build.MODEL)
                .put("device", Build.DEVICE)
                .put("sdk", Build.VERSION.SDK_INT)
                .put("release", Build.VERSION.RELEASE);
    }

    private JSONObject nativeDeviceStatus() throws Exception {
        BatteryManager batteryManager = (BatteryManager) getSystemService(Context.BATTERY_SERVICE);
        int battery = batteryManager == null ? -1 : batteryManager.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY);
        return nativeDeviceInfo()
                .put("batteryPercent", battery >= 0 ? battery : JSONObject.NULL)
                .put("notifications", hasNotificationPermission())
                .put("microphone", hasRecordAudioPermission())
                .put("bluetoothConnect", hasBluetoothConnectPermission())
                .put("camera", hasCameraPermission())
                .put("ts", System.currentTimeMillis());
    }

    private JSONObject nativePermissions() throws Exception {
        return new JSONObject()
                .put("recordAudio", hasRecordAudioPermission())
                .put("camera", hasCameraPermission())
                .put("bluetoothConnect", hasBluetoothConnectPermission())
                .put("notifications", hasNotificationPermission())
                .put("contacts", checkSelfPermission(Manifest.permission.READ_CONTACTS) == PackageManager.PERMISSION_GRANTED)
                .put("mediaAudio", Build.VERSION.SDK_INT < 33 || checkSelfPermission(Manifest.permission.READ_MEDIA_AUDIO) == PackageManager.PERMISSION_GRANTED)
                .put("mediaImages", Build.VERSION.SDK_INT < 33 || checkSelfPermission(Manifest.permission.READ_MEDIA_IMAGES) == PackageManager.PERMISSION_GRANTED)
                .put("mediaVideo", Build.VERSION.SDK_INT < 33 || checkSelfPermission(Manifest.permission.READ_MEDIA_VIDEO) == PackageManager.PERMISSION_GRANTED);
    }

    private JSONObject nativeInstalledApps() throws Exception {
        PackageManager packageManager = getPackageManager();
        Intent launcherIntent = new Intent(Intent.ACTION_MAIN, null);
        launcherIntent.addCategory(Intent.CATEGORY_LAUNCHER);
        List<ResolveInfo> launchables = packageManager.queryIntentActivities(launcherIntent, 0);
        JSONArray apps = new JSONArray();
        for (ResolveInfo info : launchables) {
            if (info == null || info.activityInfo == null) continue;
            JSONObject app = new JSONObject()
                    .put("label", String.valueOf(info.loadLabel(packageManager)))
                    .put("packageName", info.activityInfo.packageName)
                    .put("activityName", info.activityInfo.name);
            apps.put(app);
            if (apps.length() >= 250) break;
        }
        return new JSONObject().put("apps", apps).put("count", apps.length());
    }

    private JSONObject launchAndroidApp(JSONObject params) throws Exception {
        String packageName = firstNonEmpty(params.optString("packageName", ""), params.optString("package", ""));
        if (packageName.isEmpty()) {
            throw new IllegalArgumentException("packageName is required.");
        }
        Intent launchIntent = getPackageManager().getLaunchIntentForPackage(packageName);
        if (launchIntent == null) {
            throw new IllegalArgumentException("No launcher activity found for " + packageName);
        }
        launchIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        startActivity(launchIntent);
        return new JSONObject().put("launched", true).put("packageName", packageName);
    }

    private void openAndroidFilePicker() {
        runOnUiThread(() -> {
            Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT);
            intent.addCategory(Intent.CATEGORY_OPENABLE);
            intent.setType("*/*");
            try {
                startActivityForResult(intent, REQUEST_FILE_CHOOSER);
            } catch (Exception e) {
                recordDiagnostic("file_picker.failed", e.getMessage());
                statusText.setText("File picker is unavailable on this device.");
            }
        });
    }

    private String buildLiveConversationHtml() {
        return nativePageShell(
                "Live Conversation",
                "<p class=\"muted\">Native microphone capture is available here. Use the mic monitor to confirm Android audio capture, or open Control UI Talk for the full assistant conversation surface.</p>"
                        + "<div class=\"grid\">"
                        + "<button onclick=\"openControlUi()\">Open Control UI Talk</button>"
                        + "<button onclick=\"startMic()\">Start Mic Monitor</button>"
                        + "<button onclick=\"stopMic()\">Stop Mic</button>"
                        + "<button onclick=\"micProbe()\">Run Native Mic Probe</button>"
                        + "<button onclick=\"nativeSpeakerTest()\">Native Speaker Test</button>"
                        + "<button onclick=\"webAudioTest()\">Web UI Speaker Test</button>"
                        + "</div>"
                        + "<div class=\"meter\"><div id=\"bar\"></div></div>"
                        + "<pre id=\"out\">Ready.</pre>",
                "var micTimer=null;"
                        + "function log(v){document.getElementById('out').textContent=typeof v==='string'?v:JSON.stringify(v,null,2);}"
                        + "function b64bytes(s){var bin=atob(s||'');var arr=[];for(var i=0;i<bin.length;i++)arr.push(bin.charCodeAt(i)&255);return arr;}"
                        + "function rmsFromPcm16(s){var bytes=b64bytes(s);if(bytes.length<2)return 0;var sum=0,n=0;for(var i=0;i+1<bytes.length;i+=2){var v=bytes[i]|(bytes[i+1]<<8);if(v&32768)v-=65536;var f=v/32768;sum+=f*f;n++;}return n?Math.sqrt(sum/n):0;}"
                        + "function startMic(){try{OpenClawNativeAudio.startCapture(16000,20);log('Listening locally. Speak and watch the level.');if(micTimer)clearInterval(micTimer);micTimer=setInterval(function(){var c=OpenClawNativeAudio.readChunkBase64();if(!c)return;var r=rmsFromPcm16(c);document.getElementById('bar').style.width=Math.min(100,Math.round(r*700))+'%';},40);}catch(e){log('Mic start failed: '+e);}}"
                        + "function stopMic(){try{OpenClawNativeAudio.stopCapture();}catch(e){}if(micTimer)clearInterval(micTimer);micTimer=null;document.getElementById('bar').style.width='0%';log('Mic stopped.');}"
                        + "function micProbe(){log(OpenClawNativeApp.micProbe());}"
                        + "function nativeSpeakerTest(){log(OpenClawNativeApp.speakerTest());}"
                        + "function webAudioTest(){try{log(OpenClawNativeApp.webUiSpeakerTest());}catch(e){log('Web UI speaker test failed: '+e);}}"
                        + "function openControlUi(){OpenClawNativeApp.openControlUi();}"
        );
    }

    private String buildAndroidNativeHtml() {
        return nativePageShell(
                "Android Native",
                "<p class=\"muted\">Device features exposed by the custom dashboard app. These are also available to the gateway through the custom node connection.</p>"
                        + "<div class=\"grid\">"
                        + "<button onclick=\"showStatus()\">Device Status</button>"
                        + "<button onclick=\"showPerms()\">Permissions</button>"
                        + "<button onclick=\"showApps()\">Installed Apps</button>"
                        + "<button onclick=\"filePicker()\">File Picker</button>"
                        + "<button onclick=\"notifyTest()\">Test Notification</button>"
                        + "<button onclick=\"micProbe()\">Mic Probe</button>"
                        + "<button onclick=\"speakerTest()\">Speaker Test</button>"
                        + "<button onclick=\"webAudioTest()\">Web UI Speaker Test</button>"
                        + "<button onclick=\"openControlUi()\">Control UI</button>"
                        + "<button onclick=\"openLive()\">Live Conversation</button>"
                        + "</div>"
                        + "<pre id=\"out\">Ready.</pre>",
                "function log(v){document.getElementById('out').textContent=typeof v==='string'?v:JSON.stringify(v,null,2);}"
                        + "function parse(s){try{return JSON.parse(s);}catch(e){return s;}}"
                        + "function showStatus(){log(parse(OpenClawNativeApp.deviceStatus()));}"
                        + "function showPerms(){log(parse(OpenClawNativeApp.permissions()));}"
                        + "function showApps(){var data=parse(OpenClawNativeApp.apps());if(data&&data.apps){data.apps=data.apps.slice(0,60);}log(data);}"
                        + "function filePicker(){log(OpenClawNativeApp.filePicker());}"
                        + "function notifyTest(){log(OpenClawNativeApp.notifyTest());}"
                        + "function micProbe(){log(OpenClawNativeApp.micProbe());}"
                        + "function speakerTest(){log(OpenClawNativeApp.speakerTest());}"
                        + "function webAudioTest(){try{log(OpenClawNativeApp.webUiSpeakerTest());}catch(e){log('Web UI speaker test failed: '+e);}}"
                        + "function openControlUi(){OpenClawNativeApp.openControlUi();}"
                        + "function openLive(){OpenClawNativeApp.openLiveConversation();}"
        );
    }

    private String nativePageShell(String title, String body, String script) {
        return "<!doctype html><html><head><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
                + "<style>"
                + "html,body{margin:0;min-height:100%;background:#060a10;color:#f4f8fc;font-family:system-ui,-apple-system,Segoe UI,sans-serif;}"
                + "main{padding:18px 14px 28px;}"
                + "h1{font-size:22px;margin:0 0 10px;letter-spacing:0;}"
                + ".muted{color:#c6d3e0;font-size:14px;line-height:1.35;margin:0 0 14px;}"
                + ".grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin:12px 0;}"
                + "button{min-height:44px;border:1px solid #00ab7e;border-radius:6px;background:#1e2630;color:#f4f8fc;font-size:14px;padding:8px;}"
                + "pre{white-space:pre-wrap;word-break:break-word;background:#0a0e14;color:#c6d3e0;border-radius:6px;padding:12px;min-height:140px;font-size:12px;}"
                + ".meter{height:18px;background:#101820;border:1px solid #304050;border-radius:6px;overflow:hidden;margin:12px 0;}"
                + "#bar{height:100%;width:0%;background:#00ab7e;transition:width 80ms linear;}"
                + "</style></head><body><main><h1>" + title + "</h1>" + body + "</main><script>" + script + "</script></body></html>";
    }

    private void configureSystemBars() {
        Window window = getWindow();
        if (window == null) return;
        window.setStatusBarColor(COLOR_APP_CHROME);
        window.setNavigationBarColor(COLOR_APP_CHROME);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            window.setStatusBarContrastEnforced(false);
            window.setNavigationBarContrastEnforced(false);
        }
        View decorView = window.getDecorView();
        if (decorView == null) return;
        int flags = decorView.getSystemUiVisibility();
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            flags &= ~View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR;
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            flags &= ~View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR;
        }
        decorView.setSystemUiVisibility(flags);
    }

    private boolean restoreWebViewState(Bundle savedInstanceState) {
        if (savedInstanceState == null || webView == null) return false;
        webView.restoreState(savedInstanceState);
        String restoredUrl = webView.getUrl();
        if (restoredUrl == null || restoredUrl.trim().isEmpty()) return false;
        recordDiagnostic("webview.restored", restoredUrl);
        setConnectedUiVisible(true);
        return true;
    }

    private void applyStatusBarInset(View view) {
        final int baseLeft = view.getPaddingLeft();
        final int baseTop = view.getPaddingTop();
        final int baseRight = view.getPaddingRight();
        final int baseBottom = view.getPaddingBottom();
        view.setOnApplyWindowInsetsListener((target, insets) -> {
            Insets statusBars = insets.getInsets(WindowInsets.Type.statusBars());
            target.setPadding(
                    baseLeft,
                    baseTop + statusBars.top,
                    baseRight,
                    baseBottom);
            return insets;
        });
        view.requestApplyInsets();
    }

    private void applyStatusBarMargin(View view, int topOffset) {
        view.setOnApplyWindowInsetsListener((target, insets) -> {
            Insets statusBars = insets.getInsets(WindowInsets.Type.statusBars());
            ViewGroup.MarginLayoutParams params = (ViewGroup.MarginLayoutParams) target.getLayoutParams();
            params.topMargin = statusBars.top + topOffset;
            target.setLayoutParams(params);
            return insets;
        });
        view.requestApplyInsets();
    }

    private void loadPrefs() {
        urlInput.setText(prefs.getString("url", ""));
        tokenInput.setText(prefs.getString("token", ""));
        passwordInput.setText(prefs.getString("password", ""));
    }

    private void maybeAutoOpenDashboard() {
        String rawUrl = value(urlInput);
        if (rawUrl.isEmpty()) return;
        String dashboardUrl = toDashboardUrl(rawUrl);
        if (!isDashboardUrl(dashboardUrl)) return;
        openDashboard();
    }

    private void savePrefs() {
        prefs.edit()
                .putString("url", value(urlInput))
                .putString("token", value(tokenInput))
                .putString("password", value(passwordInput))
                .apply();
    }

    private void decodeSetupCode() {
        try {
            IdentityStore.Setup setup = IdentityStore.parseSetupCode(value(setupCodeInput));
            String preferredUrl = firstNonEmpty(setup.publicUrl, setup.url);
            urlInput.setText(toDashboardUrl(preferredUrl));
            statusText.setText(isSecureDashboardUrl(toDashboardUrl(preferredUrl))
                    ? "Setup code decoded. Secure dashboard URL loaded."
                    : "Setup code decoded. For Android Talk, use a secure https:// dashboard URL before opening the UI.");
            savePrefs();
        } catch (Exception e) {
            statusText.setText("Setup decode failed: " + e.getMessage());
        }
    }

    private void openDashboard() {
        savePrefs();
        try {
            String dashboardUrl = buildDashboardUrl();
            clearDiagnostics();
            recordDiagnostic("open_dashboard", dashboardUrl);
            Log.d(TAG, "opening " + dashboardUrl);
            setConnectedUiVisible(false);
            connectDashboardNode();
            webView.stopLoading();
            webView.loadUrl(dashboardUrl);
            statusText.setText("Opening Control UI");
        } catch (Exception e) {
            setConnectedUiVisible(false);
            statusText.setText(e.getMessage());
        }
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == REQUEST_RECORD_AUDIO) {
            if (hasRecordAudioPermission()) {
                grantPendingMediaCapture();
            } else {
                denyPendingPermissionRequest();
                recordDiagnostic("android.permission.denied", "RECORD_AUDIO");
                statusText.setText("Microphone access was denied by Android. Enable it in app permissions and try again.");
            }
            return;
        }
        if (requestCode == REQUEST_CAMERA) {
            if (hasCameraPermission()) {
                grantPendingMediaCapture();
            } else {
                denyPendingPermissionRequest();
                recordDiagnostic("android.permission.denied", "CAMERA");
                statusText.setText("Camera access was denied by Android. Enable it in app permissions and try again.");
            }
            return;
        }
        if (requestCode == REQUEST_BLUETOOTH_CONNECT) {
            recordDiagnostic(
                    hasBluetoothConnectPermission() ? "android.permission.granted" : "android.permission.denied",
                    "BLUETOOTH_CONNECT");
            return;
        }
        if (requestCode == REQUEST_POST_NOTIFICATIONS) {
            recordDiagnostic(
                    hasNotificationPermission() ? "android.permission.granted" : "android.permission.denied",
                    "POST_NOTIFICATIONS");
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        if (requestCode == REQUEST_FILE_CHOOSER) {
            ValueCallback<Uri[]> callback = pendingFilePathCallback;
            pendingFilePathCallback = null;
            if (callback != null) {
                callback.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(resultCode, data));
            }
            return;
        }
        super.onActivityResult(requestCode, resultCode, data);
    }

    private void handleWebPermissionRequest(PermissionRequest request) {
        if (!requestsSupportedMediaCapture(request)) {
            recordDiagnostic("web.permission.denied", "Unsupported permission request");
            request.deny();
            return;
        }
        if (canGrantMediaCapture(request)) {
            grantMediaCapture(request);
            recordDiagnostic("android.permission.already_granted", describeRequestedMediaPermissions(request));
            statusText.setText("Media capture access granted.");
            return;
        }
        pendingPermissionRequest = request;
        if (requestsAudioCapture(request) && !hasRecordAudioPermission()) {
            recordDiagnostic("android.permission.requested", "RECORD_AUDIO");
            statusText.setText("OpenClaw needs microphone access for Talk. Approve the Android permission prompt.");
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, REQUEST_RECORD_AUDIO);
            return;
        }
        if (requestsVideoCapture(request) && !hasCameraPermission()) {
            recordDiagnostic("android.permission.requested", "CAMERA");
            statusText.setText("OpenClaw needs camera access. Approve the Android permission prompt.");
            requestPermissions(new String[]{Manifest.permission.CAMERA}, REQUEST_CAMERA);
        }
    }

    private boolean hasRecordAudioPermission() {
        return checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED;
    }

    private boolean hasCameraPermission() {
        return checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED;
    }

    private boolean hasBluetoothConnectPermission() {
        return Build.VERSION.SDK_INT < Build.VERSION_CODES.S
                || checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) == PackageManager.PERMISSION_GRANTED;
    }

    private void requestBluetoothConnectPermission() {
        if (hasBluetoothConnectPermission() || Build.VERSION.SDK_INT < Build.VERSION_CODES.S) return;
        runOnUiThread(() -> {
            if (hasBluetoothConnectPermission()) return;
            recordDiagnostic("android.permission.requested", "BLUETOOTH_CONNECT");
            requestPermissions(new String[]{Manifest.permission.BLUETOOTH_CONNECT}, REQUEST_BLUETOOTH_CONNECT);
        });
    }

    private boolean requestsAudioCapture(PermissionRequest request) {
        for (String resource : request.getResources()) {
            if (PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(resource)) {
                return true;
            }
        }
        return false;
    }

    private boolean requestsVideoCapture(PermissionRequest request) {
        for (String resource : request.getResources()) {
            if (PermissionRequest.RESOURCE_VIDEO_CAPTURE.equals(resource)) {
                return true;
            }
        }
        return false;
    }

    private boolean requestsSupportedMediaCapture(PermissionRequest request) {
        for (String resource : request.getResources()) {
            if (!PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(resource)
                    && !PermissionRequest.RESOURCE_VIDEO_CAPTURE.equals(resource)) {
                return false;
            }
        }
        return request.getResources().length > 0;
    }

    private boolean canGrantMediaCapture(PermissionRequest request) {
        return (!requestsAudioCapture(request) || hasRecordAudioPermission())
                && (!requestsVideoCapture(request) || hasCameraPermission());
    }

    private void grantPendingMediaCapture() {
        PermissionRequest request = pendingPermissionRequest;
        pendingPermissionRequest = null;
        if (request != null) {
            if (canGrantMediaCapture(request)) {
                grantMediaCapture(request);
                recordDiagnostic("android.permission.granted", describeRequestedMediaPermissions(request));
                statusText.setText("Media capture access granted.");
            } else {
                pendingPermissionRequest = request;
                handleWebPermissionRequest(request);
            }
        }
    }

    private void denyPendingPermissionRequest() {
        PermissionRequest request = pendingPermissionRequest;
        pendingPermissionRequest = null;
        if (request != null) {
            request.deny();
        }
    }

    private void grantMediaCapture(PermissionRequest request) {
        List<String> granted = new ArrayList<>();
        for (String resource : request.getResources()) {
            if (PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(resource)
                    || PermissionRequest.RESOURCE_VIDEO_CAPTURE.equals(resource)) {
                granted.add(resource);
            }
        }
        if (granted.isEmpty()) {
            recordDiagnostic("web.permission.denied", "Media capture resources missing");
            request.deny();
            return;
        }
        recordDiagnostic("web.permission.granted", String.join(",", granted));
        request.grant(granted.toArray(new String[0]));
    }

    private String describeRequestedMediaPermissions(PermissionRequest request) {
        List<String> permissions = new ArrayList<>();
        if (requestsAudioCapture(request)) permissions.add("RECORD_AUDIO");
        if (requestsVideoCapture(request)) permissions.add("CAMERA");
        return String.join(",", permissions);
    }

    private void setConnectedUiVisible(boolean connected) {
        if (chromeContainer == null || settingsPanel == null || hintText == null) return;
        chromeContainer.setVisibility(View.VISIBLE);
        setChromeExpanded(!connected);
    }

    private void setChromeExpanded(boolean expanded) {
        if (statusText == null || topActions == null || controlsScroll == null
                || openButton == null || reloadButton == null || chromeToggleButton == null
                || overlayToggleButton == null || chromeContainer == null) return;
        chromeExpanded = expanded;
        chromeContainer.setVisibility(expanded ? View.VISIBLE : View.GONE);
        overlayToggleButton.setVisibility(expanded ? View.GONE : View.VISIBLE);
        statusText.setVisibility(expanded ? View.VISIBLE : View.GONE);
        controlsScroll.setVisibility(expanded ? View.VISIBLE : View.GONE);
        openButton.setVisibility(expanded ? View.VISIBLE : View.GONE);
        reloadButton.setVisibility(expanded ? View.VISIBLE : View.GONE);
        topActions.setGravity(expanded ? Gravity.CENTER_VERTICAL : Gravity.RIGHT);
        topActions.setPadding(
                dp(expanded ? 14 : 0),
                dp(expanded ? 2 : 0),
                dp(expanded ? 14 : 4),
                dp(expanded ? 10 : 2));
        chromeToggleButton.setText(expanded ? "-" : "+");
        chromeToggleButton.setContentDescription(expanded ? "Collapse controls" : "Expand controls");
        ViewGroup.MarginLayoutParams layoutParams = (ViewGroup.MarginLayoutParams) webView.getLayoutParams();
        if (layoutParams != null) {
            layoutParams.topMargin = expanded ? dp(4) : 0;
            webView.setLayoutParams(layoutParams);
        }
    }

    private String buildDashboardUrl() {
        String raw = value(urlInput);
        if (raw.isEmpty()) throw new IllegalStateException("Gateway URL is required.");
        String dashboardUrl = toDashboardUrl(raw);
        if (!isDashboardUrl(dashboardUrl)) {
            throw new IllegalStateException("Gateway URL must be an http:// or https:// dashboard URL.");
        }
        return dashboardUrl;
    }

    private WebResourceResponse interceptControlUiHtml(WebResourceRequest request) {
        try {
            if (request == null || !request.isForMainFrame()) return null;
            if (!"GET".equalsIgnoreCase(request.getMethod())) return null;
            Uri uri = request.getUrl();
            if (uri == null) return null;
            String scheme = uri.getScheme();
            if (!"https".equalsIgnoreCase(scheme) && !"http".equalsIgnoreCase(scheme)) return null;

            Request.Builder builder = new Request.Builder().url(uri.toString()).get();
            Map<String, String> requestHeaders = request.getRequestHeaders();
            if (requestHeaders != null) {
                for (Map.Entry<String, String> entry : requestHeaders.entrySet()) {
                    if (entry.getKey() != null && entry.getValue() != null) {
                        builder.header(entry.getKey(), entry.getValue());
                    }
                }
            }

            Response response = httpClient.newCall(builder.build()).execute();
            if (response.body() == null) {
                response.close();
                return null;
            }
            String contentType = response.header("Content-Type", "");
            if (!contentType.toLowerCase().contains("text/html")) {
                response.close();
                return null;
            }

            String html = response.body().string();
            response.close();
            String injected = injectNativeAuth(html);
            byte[] bytes = injected.getBytes(StandardCharsets.UTF_8);
            InputStream stream = new ByteArrayInputStream(bytes);
            Map<String, String> headers = new HashMap<>();
            headers.put("Cache-Control", "no-store");
            return new WebResourceResponse("text/html", "UTF-8", 200, "OK", headers, stream);
        } catch (Exception e) {
            recordDiagnostic("html.intercept.failed", e.getMessage());
            Log.w(TAG, "html intercept failed: " + e.getMessage());
            return null;
        }
    }

    private String injectNativeAuth(String html) {
        String script = "<script>" + buildInjectedScript() + "</script>";
        int headIndex = html.indexOf("<head>");
        if (headIndex >= 0) {
            return html.substring(0, headIndex + 6) + script + html.substring(headIndex + 6);
        }
        int htmlIndex = html.indexOf(">");
        if (htmlIndex >= 0) {
            return html.substring(0, htmlIndex + 1) + script + html.substring(htmlIndex + 1);
        }
        return script + html;
    }

    private String buildInjectedScript() {
        return buildNativeAuthScript()
                + buildNotificationBridgeScript()
                + buildTalkGatewayRelayPatchScript()
                + buildDiagnosticsScript()
                + buildNativeAudioBridgeScript()
                + buildViewportTighteningScript();
    }

    private String buildNotificationBridgeScript() {
        return "(function(){"
                + "var bridge=window.OpenClawNativeNotifications;"
                + "if(!bridge||typeof bridge.isAvailable!=='function'||!bridge.isAvailable())return;"
                + "if(window.__OPENCLAW_NATIVE_NOTIFICATIONS_PATCHED__)return;"
                + "window.__OPENCLAW_NATIVE_NOTIFICATIONS_PATCHED__=true;"
                + "function permission(){try{return String(bridge.getPermissionStatus()||'default');}catch(_){return 'default';}}"
                + "function notify(title,options){options=options||{};try{bridge.notify(String(title||''),JSON.stringify(options));}catch(_){}}"
                + "function NativeNotification(title,options){"
                + "this.title=String(title||'');"
                + "this.options=options||{};"
                + "this.data=this.options.data;"
                + "this.tag=this.options.tag||'';"
                + "this.body=this.options.body||'';"
                + "notify(this.title,this.options);"
                + "}"
                + "NativeNotification.permission=permission();"
                + "NativeNotification.requestPermission=function(callback){"
                + "var value=permission();"
                + "if(value!=='granted'){value=String(bridge.requestPermission()||value||'default');}"
                + "NativeNotification.permission=value;"
                + "if(typeof callback==='function'){try{callback(value);}catch(_){}}"
                + "return Promise.resolve(value);"
                + "};"
                + "NativeNotification.prototype.close=function(){try{bridge.clearAll();}catch(_){}};"
                + "Object.defineProperty(window,'Notification',{configurable:true,writable:true,value:NativeNotification});"
                + "if(typeof ServiceWorkerRegistration!=='undefined'&&ServiceWorkerRegistration.prototype&&typeof ServiceWorkerRegistration.prototype.showNotification==='function'){"
                + "ServiceWorkerRegistration.prototype.showNotification=function(title,options){notify(title,options);return Promise.resolve();};"
                + "}"
                + "if(navigator&&typeof navigator.setAppBadge==='undefined'){navigator.setAppBadge=function(){return Promise.resolve();};}"
                + "if(navigator&&typeof navigator.clearAppBadge==='undefined'){navigator.clearAppBadge=function(){try{bridge.clearAll();}catch(_){ }return Promise.resolve();};}"
                + "})();";
    }

    private String buildNativeAuthScript() {
        String token = value(tokenInput);
        String password = value(passwordInput);
        StringBuilder auth = new StringBuilder();
        auth.append("{\"gatewayUrl\":").append(JSONObject.quote(buildDashboardUrl()));
        if (!token.isEmpty()) auth.append(",\"token\":").append(JSONObject.quote(token));
        if (!password.isEmpty()) auth.append(",\"password\":").append(JSONObject.quote(password));
        auth.append("}");
        return "window.__OPENCLAW_NATIVE_CONTROL_AUTH__=" + auth + ";"
                + "window.__OPENCLAW_ANDROID_DASHBOARD__={versionName:" + JSONObject.quote(APP_VERSION_NAME)
                + ",versionCode:" + APP_VERSION_CODE + "};";
    }

    private String buildDiagnosticsScript() {
        return "(function(){"
                + "var bridge=window.OpenClawDiag;"
                + "function send(kind,payload){try{if(bridge&&typeof bridge.emit==='function'){bridge.emit(String(kind||'diag'),typeof payload==='string'?payload:JSON.stringify(payload||{}));}}catch(_){}}"
                + "window.__OPENCLAW_DIAG__={emit:send};"
                + "send('bootstrap',{href:String(location.href||''),userAgent:String(navigator.userAgent||'')});"
                + "if(window.__OPENCLAW_DIAG_INSTALLED__){return;}"
                + "window.__OPENCLAW_DIAG_INSTALLED__=true;"
                + "window.addEventListener('error',function(event){send('window.error',{message:String(event.message||''),filename:String(event.filename||''),line:event.lineno||0,column:event.colno||0});});"
                + "window.addEventListener('unhandledrejection',function(event){var reason=event&&event.reason;send('window.unhandledrejection',{reason:reason&&reason.message?String(reason.message):String(reason)});});"
                + "var mediaDevices=navigator.mediaDevices;"
                + "if(mediaDevices&&typeof mediaDevices.getUserMedia==='function'){"
                + "var originalGetUserMedia=mediaDevices.getUserMedia.bind(mediaDevices);"
                + "mediaDevices.getUserMedia=function(constraints){send('gum.request',constraints||{});return originalGetUserMedia(constraints).then(function(stream){var track=stream&&stream.getAudioTracks&&stream.getAudioTracks()[0];send('gum.success',{trackState:track&&track.readyState?String(track.readyState):'unknown',sampleRate:track&&track.getSettings&&track.getSettings().sampleRate||null});return stream;}).catch(function(error){send('gum.error',{name:error&&error.name?String(error.name):'',message:error&&error.message?String(error.message):String(error)});throw error;});};"
                + "}"
                + "var AudioContextCtor=window.AudioContext||window.webkitAudioContext;"
                + "if(AudioContextCtor&& !window.__OPENCLAW_DIAG_AUDIO_PATCHED__){"
                + "window.__OPENCLAW_DIAG_AUDIO_PATCHED__=true;"
                + "function WrappedAudioContext(){var args=Array.prototype.slice.call(arguments);var ctx=new (Function.prototype.bind.apply(AudioContextCtor,[null].concat(args)))();send('audio.context.created',{state:String(ctx.state||''),sampleRate:ctx.sampleRate||null});"
                + "var originalResume=ctx.resume&&ctx.resume.bind(ctx);if(originalResume){ctx.resume=function(){send('audio.context.resume.request',{state:String(ctx.state||'')});return originalResume().then(function(result){send('audio.context.resume.success',{state:String(ctx.state||'')});return result;}).catch(function(error){send('audio.context.resume.error',{message:error&&error.message?String(error.message):String(error)});throw error;});};}"
                + "var originalCreateMediaStreamSource=ctx.createMediaStreamSource&&ctx.createMediaStreamSource.bind(ctx);if(originalCreateMediaStreamSource){ctx.createMediaStreamSource=function(stream){send('audio.media_stream_source.request',{});try{var node=originalCreateMediaStreamSource(stream);send('audio.media_stream_source.success',{});return node;}catch(error){send('audio.media_stream_source.error',{message:error&&error.message?String(error.message):String(error)});throw error;}};}"
                + "var originalCreateScriptProcessor=ctx.createScriptProcessor&&ctx.createScriptProcessor.bind(ctx);if(originalCreateScriptProcessor){ctx.createScriptProcessor=function(){var node=originalCreateScriptProcessor.apply(ctx,arguments);send('audio.script_processor.created',{bufferSize:arguments[0]||null});var seen=false;var previous=node.onaudioprocess;Object.defineProperty(node,'onaudioprocess',{configurable:true,enumerable:true,get:function(){return previous;},set:function(handler){previous=function(event){if(!seen){seen=true;send('audio.script_processor.first_frame',{inputChannels:event&&event.inputBuffer?event.inputBuffer.numberOfChannels:null,length:event&&event.inputBuffer?event.inputBuffer.length:null});}return handler&&handler.call(this,event);};}});return node;};}"
                + "return ctx;}"
                + "WrappedAudioContext.prototype=AudioContextCtor.prototype;"
                + "window.AudioContext=WrappedAudioContext;"
                + "if(window.webkitAudioContext){window.webkitAudioContext=WrappedAudioContext;}"
                + "}"
                + "var levels=['debug','log','info','warn','error'];"
                + "for(var i=0;i<levels.length;i++){(function(level){var original=console[level];if(typeof original!=='function')return;console[level]=function(){var parts=[];for(var j=0;j<arguments.length;j++){var value=arguments[j];if(typeof value==='string'){parts.push(value);}else{try{parts.push(JSON.stringify(value));}catch(_){parts.push(String(value));}}}send('console.'+level,parts.join(' '));return original.apply(console,arguments);};})(levels[i]);}"
                + "})();";
    }

    private String buildTalkGatewayRelayPatchScript() {
        return "(function(){"
                + "if(window.__OPENCLAW_TALK_GATEWAY_RELAY_PATCHED__)return;"
                + "window.__OPENCLAW_TALK_GATEWAY_RELAY_PATCHED__=true;"
                + "var diag=function(kind,payload){try{var d=window.__OPENCLAW_DIAG__;if(d&&typeof d.emit==='function'){d.emit(kind,payload||{});}}catch(_){}};"
                + "var nativeAudio=window.OpenClawNativeAudio;"
                + "var WebSocketCtor=window.WebSocket;"
                + "if(typeof WebSocketCtor!=='function')return;"
                + "var relayAudioChunks=0;"
                + "function parse(data){if(typeof data!=='string')return null;try{return JSON.parse(data);}catch(_){return null;}}"
                + "function firstString(){for(var i=0;i<arguments.length;i++){var value=arguments[i];if(typeof value==='string'&&value.trim())return value.trim();}return '';}"
                + "function audioBase64(payload){if(!payload||typeof payload!=='object')return '';var nested=payload.payload&&typeof payload.payload==='object'?payload.payload:{};var audio=payload.audio&&typeof payload.audio==='object'?payload.audio:{};var value=firstString(payload.audioBase64,payload.base64,payload.delta,payload.audio,nested.audioBase64,nested.base64,nested.delta,audio.audioBase64,audio.base64,audio.delta,audio.data);if(value.indexOf('base64,')>=0)value=value.substring(value.indexOf('base64,')+7);return value;}"
                + "function audioSampleRate(payload){if(!payload||typeof payload!=='object')return 24000;var nested=payload.payload&&typeof payload.payload==='object'?payload.payload:{};var audio=payload.audio&&typeof payload.audio==='object'?payload.audio:{};return payload.sampleRate||payload.sampleRateHz||nested.sampleRate||nested.sampleRateHz||audio.sampleRate||audio.sampleRateHz||24000;}"
                + "function cleanGatewayRelayParams(params){"
                + "params=params&&typeof params==='object'?params:{};"
                + "var clean={mode:'realtime',transport:'gateway-relay',brain:'agent-consult'};"
                + "if(typeof params.sessionKey==='string'&&params.sessionKey.trim())clean.sessionKey=params.sessionKey;"
                + "if(typeof params.provider==='string'&&params.provider.trim())clean.provider=params.provider;"
                + "if(typeof params.model==='string'&&params.model.trim())clean.model=params.model;"
                + "if(typeof params.voice==='string'&&params.voice.trim())clean.voice=params.voice;"
                + "if(typeof params.instructions==='string'&&params.instructions.trim())clean.instructions=params.instructions;"
                + "if(typeof params.vadThreshold==='number')clean.vadThreshold=params.vadThreshold;"
                + "if(typeof params.silenceDurationMs==='number')clean.silenceDurationMs=params.silenceDurationMs;"
                + "if(typeof params.prefixPaddingMs==='number')clean.prefixPaddingMs=params.prefixPaddingMs;"
                + "if(typeof params.ttlMs==='number')clean.ttlMs=params.ttlMs;"
                + "return clean;"
                + "}"
                + "function handleRelayMessage(data){"
                + "var message=parse(data);"
                + "if(!message||message.type!=='event'||message.event!=='talk.event'||!message.payload)return;"
                + "var payload=message.payload;"
                + "var kind=String(payload.type||'');"
                + "var base64=audioBase64(payload);"
                + "if(base64&&(kind==='audio'||kind==='output.audio.delta'||kind.indexOf('audio')>=0)){"
                + "relayAudioChunks++;"
                + "var sampleRate=audioSampleRate(payload);"
                + "if(relayAudioChunks===1||relayAudioChunks%20===0){diag('talk.relay.audio',{'type':kind,'chunks':relayAudioChunks,'sampleRate':sampleRate,'bytes':String(base64).length});}"
                + "try{if(nativeAudio&&typeof nativeAudio.playAgentResponsePcm16Base64==='function'){nativeAudio.playAgentResponsePcm16Base64(String(base64),sampleRate);}else if(nativeAudio&&typeof nativeAudio.playPcm16Base64==='function'){nativeAudio.playPcm16Base64(String(base64),sampleRate);}else{diag('talk.relay.native_play.unavailable',{});}}catch(error){diag('talk.relay.native_play.error',{'message':String(error)});}"
                + "}else if(kind==='audio'||kind==='output.audio.delta'||kind.indexOf('audio')>=0){"
                + "diag('talk.relay.audio_missing',{'type':kind,'keys':Object.keys(payload).join(',')});"
                + "}else if(kind){"
                + "diag('talk.relay.event',{'type':kind});"
                + "}"
                + "}"
                + "function patchSocket(socket){"
                + "try{socket.addEventListener('message',function(event){handleRelayMessage(event&&event.data);});}catch(_){ }"
                + "var originalSend=socket.send;"
                + "socket.send=function(data){"
                + "try{"
                + "var message=parse(data);"
                + "if(message&&message.type==='req'&&message.method==='talk.client.create'){"
                + "message.method='talk.session.create';"
                + "message.params=cleanGatewayRelayParams(message.params);"
                + "data=JSON.stringify(message);"
                + "diag('talk.patch.rewrite',{'method':'talk.session.create','transport':message.params.transport,'brain':message.params.brain});"
                + "}"
                + "}catch(error){diag('talk.patch.send.error',{'message':String(error)});}"
                + "return originalSend.call(socket,data);"
                + "};"
                + "return socket;"
                + "}"
                + "function PatchedWebSocket(url,protocols){"
                + "return patchSocket(arguments.length>1?new WebSocketCtor(url,protocols):new WebSocketCtor(url));"
                + "}"
                + "PatchedWebSocket.prototype=WebSocketCtor.prototype;"
                + "PatchedWebSocket.CONNECTING=WebSocketCtor.CONNECTING;"
                + "PatchedWebSocket.OPEN=WebSocketCtor.OPEN;"
                + "PatchedWebSocket.CLOSING=WebSocketCtor.CLOSING;"
                + "PatchedWebSocket.CLOSED=WebSocketCtor.CLOSED;"
                + "window.WebSocket=PatchedWebSocket;"
                + "diag('talk.patch.ready',{'mode':'gateway-relay'});"
                + "})();";
    }

    private String buildNativeAudioBridgeScript() {
        return "(function(){"
                + "var bridge=window.OpenClawNativeAudio;"
                + "var diag=window.__OPENCLAW_DIAG__&&typeof window.__OPENCLAW_DIAG__.emit==='function'?window.__OPENCLAW_DIAG__.emit:function(){};"
                + "if(!bridge||typeof bridge.isAvailable!=='function'||!bridge.isAvailable())return;"
                + "if(window.__OPENCLAW_NATIVE_AUDIO_PATCHED__)return;"
                + "window.__OPENCLAW_NATIVE_AUDIO_PATCHED__=true;"
                + "diag('native_audio.patch',{'status':'enabled'});"
                + "function decodePcm16(base64){var binary=atob(base64||'');var bytes=binary.length;var samples=new Float32Array(bytes/2);for(var i=0,j=0;i<bytes;i+=2,j++){var value=(binary.charCodeAt(i)&255)|((binary.charCodeAt(i+1)&255)<<8);if(value&32768)value-=65536;samples[j]=Math.max(-1,Math.min(1,value/32768));}return samples;}"
                + "function resampleLinear(input,fromRate,toRate){if(!input||!input.length||!fromRate||!toRate||fromRate===toRate)return input;var ratio=toRate/fromRate;var outLength=Math.max(1,Math.round(input.length*ratio));var output=new Float32Array(outLength);for(var i=0;i<outLength;i++){var position=i/ratio;var index=Math.floor(position);var next=Math.min(index+1,input.length-1);var weight=position-index;output[i]=input[index]+(input[next]-input[index])*weight;}return output;}"
                + "var mediaDevices=navigator.mediaDevices||(navigator.mediaDevices={});"
                + "var nativeStreamFactory=function(){return Promise.resolve({__openclawNativeAudio:true,getAudioTracks:function(){return[{readyState:'live',stop:function(){try{bridge.stopCapture();}catch(_){}}}];},getTracks:function(){return this.getAudioTracks();}});};"
                + "mediaDevices.getUserMedia=function(constraints){diag('native_audio.gum_override',constraints||{});return nativeStreamFactory();};"
                + "var AudioContextCtor=window.AudioContext||window.webkitAudioContext;"
                + "if(!AudioContextCtor)return;"
                + "var originalCreateMediaStreamSource=AudioContextCtor.prototype.createMediaStreamSource;"
                + "var originalCreateScriptProcessor=AudioContextCtor.prototype.createScriptProcessor;"
                + "AudioContextCtor.prototype.createMediaStreamSource=function(stream){if(!stream||!stream.__openclawNativeAudio)return originalCreateMediaStreamSource.call(this,stream);var ctx=this;return{connect:function(node){node&&typeof node.__openclawNativeAttach==='function'&&node.__openclawNativeAttach(ctx);},disconnect:function(){try{bridge.stopCapture();}catch(_){}}};};"
                + "AudioContextCtor.prototype.createScriptProcessor=function(bufferSize,inputChannels,outputChannels){var node=originalCreateScriptProcessor.call(this,bufferSize,inputChannels,outputChannels);var originalConnect=node.connect?node.connect.bind(node):null;var originalDisconnect=node.disconnect?node.disconnect.bind(node):null;node.__openclawNativeTimer=null;node.__openclawNativeCtx=null;node.__openclawNativeAttach=function(ctx){node.__openclawNativeCtx=ctx;try{bridge.startCapture(16000,10);diag('native_audio.capture.start',{'bufferSize':bufferSize||0,'ctxSampleRate':ctx&&ctx.sampleRate||null});}catch(error){diag('native_audio.capture.error',{'message':String(error)});}if(node.__openclawNativeTimer)return;node.__openclawNativeTimer=window.setInterval(function(){if(typeof node.onaudioprocess!=='function')return;var chunk='';try{chunk=bridge.readChunkBase64()||'';}catch(error){diag('native_audio.read.error',{'message':String(error)});return;}if(!chunk)return;var floats=decodePcm16(chunk);var targetRate=node.__openclawNativeCtx&&node.__openclawNativeCtx.sampleRate?node.__openclawNativeCtx.sampleRate:16000;var resampled=resampleLinear(floats,16000,targetRate);var inputBuffer={length:resampled.length,numberOfChannels:1,getChannelData:function(){return resampled;}};node.onaudioprocess({inputBuffer:inputBuffer});},10);};"
                + "node.disconnect=function(){if(node.__openclawNativeTimer){window.clearInterval(node.__openclawNativeTimer);node.__openclawNativeTimer=null;}try{bridge.stopCapture();diag('native_audio.capture.stop',{});}catch(_){ }return originalDisconnect?originalDisconnect():void 0;};"
                + "node.connect=function(){return originalConnect?originalConnect.apply(node,arguments):void 0;};"
                + "return node;};"
                + "})();";
    }

    private String buildViewportTighteningScript() {
        return "(function(){"
                + "function apply(){"
                + "try{"
                + "var root=document.documentElement;"
                + "if(root){root.style.setProperty('--safe-area-bottom','0px','important');root.style.setProperty('--safe-area-top','0px','important');}"
                + "var body=document.body;"
                + "if(body){body.style.paddingBottom='0px';body.style.marginBottom='0px';body.style.minHeight='100dvh';}"
                + "var app=document.querySelector('openclaw-app');"
                + "if(app){app.style.height='100dvh';app.style.minHeight='100dvh';app.style.paddingBottom='0px';app.style.marginBottom='0px';}"
                + "var style=document.getElementById('openclaw-android-tighten');"
                + "if(!style){style=document.createElement('style');style.id='openclaw-android-tighten';style.textContent='html,body,openclaw-app,.shell,.shell--chat{height:100dvh!important;min-height:100dvh!important;max-height:100dvh!important;}body,openclaw-app{padding-bottom:0!important;margin-bottom:0!important;}[class*=composer],[class*=chat]{padding-bottom:0!important;margin-bottom:0!important;}';document.head&&document.head.appendChild(style);}"
                + "}catch(_){}}"
                + "if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',apply,{once:true});}"
                + "apply();"
                + "})();";
    }

    private void injectRuntimeScripts(WebView view, String reason) {
        if (view == null) return;
        String script = buildInjectedScript();
        recordDiagnostic("js.inject.request", reason);
        view.post(() -> view.evaluateJavascript(script, value ->
                recordDiagnostic("js.inject.result", reason + " " + String.valueOf(value))));
    }

    private static String toDashboardUrl(String raw) {
        String value = raw == null ? "" : raw.trim();
        if (value.isEmpty()) return "";
        if (value.startsWith("ws://")) value = "http://" + value.substring(5);
        else if (value.startsWith("wss://")) value = "https://" + value.substring(6);
        else if (!value.startsWith("http://") && !value.startsWith("https://")) value = "https://" + value;
        if (!value.endsWith("/")) value = value + "/";
        return value;
    }

    private static boolean isSecureDashboardUrl(String raw) {
        try {
            java.net.URI uri = java.net.URI.create(raw);
            String scheme = uri.getScheme();
            String host = uri.getHost();
            if (scheme == null || host == null) return false;
            if ("https".equalsIgnoreCase(scheme)) return true;
            if (!"http".equalsIgnoreCase(scheme)) return false;
            String normalized = host.trim().toLowerCase();
            return "localhost".equals(normalized) || "127.0.0.1".equals(normalized) || "::1".equals(normalized);
        } catch (Exception ignored) {
            return false;
        }
    }

    private static boolean isDashboardUrl(String raw) {
        try {
            java.net.URI uri = java.net.URI.create(raw);
            String scheme = uri.getScheme();
            String host = uri.getHost();
            if (scheme == null || host == null) return false;
            return "https".equalsIgnoreCase(scheme) || "http".equalsIgnoreCase(scheme);
        } catch (Exception ignored) {
            return false;
        }
    }

    private static String toGatewayWebSocketUrl(String raw) {
        String value = raw == null ? "" : raw.trim();
        if (value.isEmpty()) throw new IllegalStateException("Gateway URL is required.");
        if (value.startsWith("ws://") || value.startsWith("wss://")) return value;
        if (value.startsWith("https://")) value = "wss://" + value.substring(8);
        else if (value.startsWith("http://")) value = "ws://" + value.substring(7);
        else value = "wss://" + value;
        if (!value.endsWith("/")) value = value + "/";
        return value;
    }

    private static String firstNonEmpty(String... values) {
        for (String value : values) {
            if (value != null && !value.trim().isEmpty()) return value;
        }
        return "";
    }

    private void recordDiagnostic(String kind, String message) {
        String safeMessage = message == null ? "" : message;
        String line = diagnosticsTimeFormat.format(new Date()) + "  " + kind + "  " + safeMessage;
        Log.d(TAG, "diag " + line);
        runOnUiThread(() -> {
            diagnosticsLines.addLast(line);
            while (diagnosticsLines.size() > MAX_DIAGNOSTIC_LINES) {
                diagnosticsLines.removeFirst();
            }
            StringBuilder builder = new StringBuilder();
            for (String entry : diagnosticsLines) {
                if (builder.length() > 0) builder.append('\n');
                builder.append(entry);
            }
            diagnosticsText.setText(builder.length() == 0 ? "No diagnostics yet." : builder.toString());
        });
    }

    private void clearDiagnostics() {
        diagnosticsLines.clear();
        diagnosticsText.setText("No diagnostics yet.");
    }

    private void copyDiagnostics() {
        ClipboardManager clipboard = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
        if (clipboard == null) return;
        CharSequence text = diagnosticsText.getText();
        clipboard.setPrimaryClip(ClipData.newPlainText("OpenClaw diagnostics", text));
        statusText.setText("Diagnostics copied.");
    }

    private void runNativeMicProbe() {
        if (!hasRecordAudioPermission()) {
            recordDiagnostic("native_mic_probe.skipped", "RECORD_AUDIO permission missing");
            statusText.setText("Grant microphone permission first.");
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, REQUEST_RECORD_AUDIO);
            return;
        }
        statusText.setText("Running native mic probe");
        recordDiagnostic("native_mic_probe.start", "AudioRecord");
        new Thread(() -> {
            AudioRecord recorder = null;
            try {
                int sampleRate = 16000;
                int channelConfig = AudioFormat.CHANNEL_IN_MONO;
                int encoding = AudioFormat.ENCODING_PCM_16BIT;
                int minBuffer = AudioRecord.getMinBufferSize(sampleRate, channelConfig, encoding);
                if (minBuffer <= 0) {
                    throw new IllegalStateException("getMinBufferSize returned " + minBuffer);
                }
                int bufferSize = Math.max(minBuffer, sampleRate);
                recordDiagnostic("native_mic_probe.config",
                        "sampleRate=" + sampleRate + " minBuffer=" + minBuffer + " bufferSize=" + bufferSize);
                recorder = new AudioRecord(
                        MediaRecorder.AudioSource.MIC,
                        sampleRate,
                        channelConfig,
                        encoding,
                        bufferSize);
                recordDiagnostic("native_mic_probe.record_state",
                        "state=" + recorder.getState() + " sessionId=" + recorder.getAudioSessionId());
                if (recorder.getState() != AudioRecord.STATE_INITIALIZED) {
                    throw new IllegalStateException("AudioRecord failed to initialize: state=" + recorder.getState());
                }
                recorder.startRecording();
                recordDiagnostic("native_mic_probe.recording_state",
                        "state=" + recorder.getRecordingState());
                if (recorder.getRecordingState() != AudioRecord.RECORDSTATE_RECORDING) {
                    throw new IllegalStateException("AudioRecord failed to start: state=" + recorder.getRecordingState());
                }
                byte[] buffer = new byte[bufferSize];
                int bytesRead = recorder.read(buffer, 0, buffer.length);
                recordDiagnostic("native_mic_probe.read",
                        "bytesRead=" + bytesRead);
                if (bytesRead <= 0) {
                    throw new IllegalStateException("AudioRecord read failed: " + bytesRead);
                }
                runOnUiThread(() -> statusText.setText("Native mic probe passed."));
                recordDiagnostic("native_mic_probe.success", "AudioRecord captured audio");
            } catch (Exception e) {
                recordDiagnostic("native_mic_probe.error",
                        e.getClass().getSimpleName() + ": " + e.getMessage());
                runOnUiThread(() -> statusText.setText("Native mic probe failed: " + e.getMessage()));
            } finally {
                if (recorder != null) {
                    try {
                        recorder.stop();
                    } catch (Exception ignored) {
                    }
                    recorder.release();
                }
            }
        }, "openclaw-native-mic-probe").start();
    }

    private final class DiagnosticsBridge {
        @JavascriptInterface
        public void emit(String kind, String payload) {
            recordDiagnostic("js." + kind, payload);
        }
    }

    private final class NativeNotificationsBridge {
        @JavascriptInterface
        public boolean isAvailable() {
            return true;
        }

        @JavascriptInterface
        public String getPermissionStatus() {
            return hasNotificationPermission() ? "granted" : "default";
        }

        @JavascriptInterface
        public String requestPermission() {
            ensureNotificationPermission();
            return getPermissionStatus();
        }

        @JavascriptInterface
        public void notify(String title, String optionsJson) {
            runOnUiThread(() -> postNativeNotification(title, optionsJson));
        }

        @JavascriptInterface
        public void clearAll() {
            runOnUiThread(MainActivity.this::clearNativeNotifications);
        }
    }

    private final class NativeAppBridge {
        @JavascriptInterface
        public String deviceStatus() {
            try {
                return nativeDeviceStatus().toString();
            } catch (Exception e) {
                return errorJson(e);
            }
        }

        @JavascriptInterface
        public String permissions() {
            try {
                return nativePermissions().toString();
            } catch (Exception e) {
                return errorJson(e);
            }
        }

        @JavascriptInterface
        public String apps() {
            try {
                return nativeInstalledApps().toString();
            } catch (Exception e) {
                return errorJson(e);
            }
        }

        @JavascriptInterface
        public String filePicker() {
            openAndroidFilePicker();
            return "File picker opened.";
        }

        @JavascriptInterface
        public String notifyTest() {
            runOnUiThread(() -> postNativeNotification(
                    "OpenClaw Dashboard",
                    "{\"body\":\"Native notification bridge is working.\"}"));
            return "Notification requested.";
        }

        @JavascriptInterface
        public String micProbe() {
            runOnUiThread(MainActivity.this::runNativeMicProbe);
            return "Native mic probe started. Check diagnostics for capture results.";
        }

        @JavascriptInterface
        public String speakerTest() {
            nativeAudioBridge.playTestTone();
            return "Native speaker test requested. You should hear a short tone.";
        }

        @JavascriptInterface
        public String webUiSpeakerTest() {
            nativeAudioBridge.playWebUiTestTone();
            return "Web UI speaker test requested through the native audio route.";
        }

        @JavascriptInterface
        public void openControlUi() {
            runOnUiThread(MainActivity.this::openDashboard);
        }

        @JavascriptInterface
        public void openLiveConversation() {
            runOnUiThread(MainActivity.this::openLiveConversation);
        }

        private String errorJson(Exception e) {
            try {
                return new JSONObject()
                        .put("ok", false)
                        .put("message", e.getMessage() == null ? e.toString() : e.getMessage())
                        .toString();
            } catch (Exception ignored) {
                return "{\"ok\":false}";
            }
        }
    }

    private final class NativeAudioBridge {
        private static final int NATIVE_SAMPLE_RATE = 16000;
        private static final int OUTPUT_SAMPLE_RATE = 24000;
        private static final int MAX_QUEUED_CHUNKS = 64;
        private static final int MAX_OUTPUT_QUEUED_CHUNKS = 96;
        private static final double OUTPUT_GAIN = 5.0;
        private static final double TEST_TONE_AMPLITUDE = 30000.0;
        private static final double MIN_PLAYBACK_VOLUME_RATIO = 1.0;
        private final Object lock = new Object();
        private final Object outputLock = new Object();
        private final ArrayDeque<String> chunkQueue = new ArrayDeque<>();
        private final ArrayDeque<byte[]> outputQueue = new ArrayDeque<>();
        private final AtomicBoolean running = new AtomicBoolean(false);
        private final AtomicBoolean outputRunning = new AtomicBoolean(false);
        private AudioRecord recorder;
        private Thread readerThread;
        private Thread outputThread;
        private Integer previousAudioMode;
        private AudioDeviceInfo previousCommunicationDevice;
        private boolean bluetoothRouteActive;
        private AcousticEchoCanceler acousticEchoCanceler;
        private NoiseSuppressor noiseSuppressor;
        private AutomaticGainControl automaticGainControl;

        @JavascriptInterface
        public boolean isAvailable() {
            return true;
        }

        @JavascriptInterface
        public void startCapture(int sampleRateHz, int frameMs) {
            synchronized (lock) {
                stopCaptureLocked();
                int requestedFrameMs = frameMs > 0 ? Math.max(TALK_FRAME_MS, frameMs) : TALK_FRAME_MS;
                int samplesPerChunk = Math.max(160, NATIVE_SAMPLE_RATE * requestedFrameMs / 1000);
                int minBuffer = AudioRecord.getMinBufferSize(
                        NATIVE_SAMPLE_RATE,
                        AudioFormat.CHANNEL_IN_MONO,
                        AudioFormat.ENCODING_PCM_16BIT);
                if (minBuffer <= 0) {
                    recordDiagnostic("native_audio_bridge.error", "getMinBufferSize=" + minBuffer);
                    return;
                }
                int bufferSize = Math.max(minBuffer, samplesPerChunk * 2);
                setCommunicationAudioMode();
                AudioDeviceInfo bluetoothInput = preferBluetoothAudioRoute(true, "capture");
                recorder = new AudioRecord(
                        MediaRecorder.AudioSource.VOICE_COMMUNICATION,
                        NATIVE_SAMPLE_RATE,
                        AudioFormat.CHANNEL_IN_MONO,
                        AudioFormat.ENCODING_PCM_16BIT,
                        bufferSize);
                if (recorder.getState() != AudioRecord.STATE_INITIALIZED) {
                    recordDiagnostic("native_audio_bridge.error", "AudioRecord state=" + recorder.getState());
                    recorder.release();
                    recorder = null;
                    restoreAudioMode();
                    return;
                }
                if (bluetoothInput != null && recorder.setPreferredDevice(bluetoothInput)) {
                    recordDiagnostic("native_audio_bridge.input_route", describeAudioDevice(bluetoothInput));
                }
                enableVoiceProcessingEffects(recorder.getAudioSessionId());
                recorder.startRecording();
                running.set(true);
                recordDiagnostic("native_audio_bridge.start",
                        "sampleRate=" + sampleRateHz
                                + " nativeRate=" + NATIVE_SAMPLE_RATE
                                + " frameMs=" + requestedFrameMs
                                + " source=VOICE_COMMUNICATION");
                readerThread = new Thread(() -> readNativeAudioLoop(samplesPerChunk), "openclaw-native-audio");
                readerThread.start();
            }
        }

        @JavascriptInterface
        public void stopCapture() {
            synchronized (lock) {
                stopCaptureLocked();
            }
        }

        @JavascriptInterface
        public String readChunkBase64() {
            synchronized (lock) {
                return chunkQueue.pollFirst();
            }
        }

        @JavascriptInterface
        public void playPcm16Base64(String base64Pcm16, int sampleRateHz) {
            playPcm16Base64(base64Pcm16, sampleRateHz, "playback");
        }

        @JavascriptInterface
        public void playAgentResponsePcm16Base64(String base64Pcm16, int sampleRateHz) {
            playPcm16Base64(base64Pcm16, sampleRateHz, "agent_response");
        }

        private void playPcm16Base64(String base64Pcm16, int sampleRateHz, String routeReason) {
            if (base64Pcm16 == null || base64Pcm16.isEmpty()) return;
            int outputSampleRate = sampleRateHz > 0 ? sampleRateHz : OUTPUT_SAMPLE_RATE;
            byte[] pcm;
            try {
                pcm = Base64.decode(base64Pcm16, Base64.DEFAULT);
            } catch (Exception e) {
                recordDiagnostic("native_audio_output.decode_error", e.getClass().getSimpleName() + ": " + e.getMessage());
                return;
            }
            if (pcm.length == 0) return;
            int clippedSamples = applyPcm16Gain(pcm, OUTPUT_GAIN);
            recordDiagnostic("native_audio_output.enqueue",
                    "reason=" + routeReason
                            + " sampleRate=" + outputSampleRate
                            + " bytes=" + pcm.length
                            + " gain=" + OUTPUT_GAIN
                            + " clipped=" + clippedSamples);
            synchronized (outputLock) {
                outputQueue.addLast(pcm);
                while (outputQueue.size() > MAX_OUTPUT_QUEUED_CHUNKS) {
                    outputQueue.removeFirst();
                }
                outputLock.notifyAll();
            }
            if (outputRunning.compareAndSet(false, true)) {
                outputThread = new Thread(() -> playPcmOutputLoop(outputSampleRate, routeReason), "openclaw-native-pcm-output");
                outputThread.start();
            }
        }

        @JavascriptInterface
        public void playTestTone() {
            playTone(880.0, "test_tone", "openclaw-native-speaker-test");
        }

        @JavascriptInterface
        public void playWebUiTestTone() {
            playTone(660.0, "webui_test", "openclaw-webui-speaker-test");
        }

        private void playTone(double frequency, String routeReason, String threadName) {
            new Thread(() -> {
                AudioTrack track = null;
                try {
                    AudioDeviceInfo bluetoothOutput = preferBluetoothAudioRoute(false, routeReason);
                    AudioManager audioManager = getAudioManager();
                    if (audioManager != null) {
                        ensurePlaybackStreamVolume(audioManager);
                    }
                    int durationMs = 700;
                    int samples = OUTPUT_SAMPLE_RATE * durationMs / 1000;
                    byte[] pcm = new byte[samples * 2];
                    for (int i = 0; i < samples; i++) {
                        double envelope = Math.min(1.0, Math.min(i / 1200.0, (samples - i) / 1200.0));
                        short value = (short) Math.round(Math.sin(2.0 * Math.PI * frequency * i / OUTPUT_SAMPLE_RATE) * TEST_TONE_AMPLITUDE * envelope);
                        pcm[i * 2] = (byte) (value & 0xff);
                        pcm[i * 2 + 1] = (byte) ((value >> 8) & 0xff);
                    }
                    int minBuffer = AudioTrack.getMinBufferSize(
                            OUTPUT_SAMPLE_RATE,
                            AudioFormat.CHANNEL_OUT_MONO,
                            AudioFormat.ENCODING_PCM_16BIT);
                    int bufferSize = Math.max(minBuffer, pcm.length);
                    track = new AudioTrack(
                            AudioManager.STREAM_MUSIC,
                            OUTPUT_SAMPLE_RATE,
                            AudioFormat.CHANNEL_OUT_MONO,
                            AudioFormat.ENCODING_PCM_16BIT,
                            bufferSize,
                            AudioTrack.MODE_STREAM);
                    if (track.getState() != AudioTrack.STATE_INITIALIZED) {
                        throw new IllegalStateException("AudioTrack failed to initialize: state=" + track.getState());
                    }
                    if (bluetoothOutput != null && track.setPreferredDevice(bluetoothOutput)) {
                        recordDiagnostic("native_audio_output.route", describeAudioDevice(bluetoothOutput));
                    }
                    recordDiagnostic("native_audio_output.start",
                            "AudioTrack tone reason=" + routeReason
                                    + " frequency=" + frequency
                                    + " sampleRate=" + OUTPUT_SAMPLE_RATE
                                    + " bytes=" + pcm.length);
                    track.play();
                    int written = track.write(pcm, 0, pcm.length);
                    recordDiagnostic("native_audio_output.write", "bytes=" + written + " playState=" + track.getPlayState());
                    Thread.sleep(durationMs + 100L);
                    recordDiagnostic("native_audio_output.done", "tone complete");
                } catch (Exception e) {
                    recordDiagnostic("native_audio_output.error", e.getClass().getSimpleName() + ": " + e.getMessage());
                    runOnUiThread(() -> statusText.setText("Speaker test failed: " + e.getMessage()));
                } finally {
                    if (track != null) {
                        try {
                            track.stop();
                        } catch (Exception ignored) {
                        }
                        track.release();
                    }
                    restoreBluetoothAudioRouteIfIdle();
                }
            }, threadName).start();
        }

        private void playPcmOutputLoop(int sampleRateHz, String routeReason) {
            AudioTrack track = null;
            try {
                AudioDeviceInfo bluetoothOutput = preferBluetoothAudioRoute(false, routeReason);
                AudioManager audioManager = getAudioManager();
                if (audioManager != null) {
                    ensurePlaybackStreamVolume(audioManager);
                }
                int minBuffer = AudioTrack.getMinBufferSize(
                        sampleRateHz,
                        AudioFormat.CHANNEL_OUT_MONO,
                        AudioFormat.ENCODING_PCM_16BIT);
                int bufferSize = Math.max(minBuffer, sampleRateHz / 2);
                track = new AudioTrack(
                        AudioManager.STREAM_MUSIC,
                        sampleRateHz,
                        AudioFormat.CHANNEL_OUT_MONO,
                        AudioFormat.ENCODING_PCM_16BIT,
                        bufferSize,
                        AudioTrack.MODE_STREAM);
                if (track.getState() != AudioTrack.STATE_INITIALIZED) {
                    throw new IllegalStateException("AudioTrack failed to initialize: state=" + track.getState());
                }
                if (bluetoothOutput != null && track.setPreferredDevice(bluetoothOutput)) {
                    recordDiagnostic("native_audio_output.route", describeAudioDevice(bluetoothOutput));
                }
                recordDiagnostic("native_audio_output.stream_start", "reason=" + routeReason + " sampleRate=" + sampleRateHz + " buffer=" + bufferSize);
                track.play();
                int chunks = 0;
                while (true) {
                    byte[] chunk;
                    synchronized (outputLock) {
                        chunk = outputQueue.pollFirst();
                        if (chunk == null) {
                            try {
                                outputLock.wait(500);
                            } catch (InterruptedException e) {
                                Thread.currentThread().interrupt();
                                break;
                            }
                            chunk = outputQueue.pollFirst();
                            if (chunk == null) break;
                        }
                    }
                    int offset = 0;
                    while (offset < chunk.length) {
                        int written = track.write(chunk, offset, chunk.length - offset);
                        if (written <= 0) {
                            recordDiagnostic("native_audio_output.write_error", "bytes=" + written);
                            break;
                        }
                        offset += written;
                    }
                    chunks++;
                    if (chunks == 1 || chunks % 20 == 0) {
                        recordDiagnostic("native_audio_output.stream_write", "chunks=" + chunks + " lastBytes=" + chunk.length);
                    }
                }
                recordDiagnostic("native_audio_output.stream_done", "complete");
            } catch (Exception e) {
                recordDiagnostic("native_audio_output.stream_error", e.getClass().getSimpleName() + ": " + e.getMessage());
            } finally {
                if (track != null) {
                    try {
                        track.stop();
                    } catch (Exception ignored) {
                    }
                    track.release();
                }
                synchronized (outputLock) {
                    outputQueue.clear();
                }
                outputRunning.set(false);
                outputThread = null;
                restoreBluetoothAudioRouteIfIdle();
            }
        }

        private void ensurePlaybackStreamVolume(AudioManager audioManager) {
            int current = audioManager.getStreamVolume(AudioManager.STREAM_MUSIC);
            int max = audioManager.getStreamMaxVolume(AudioManager.STREAM_MUSIC);
            recordDiagnostic("native_audio_output.volume", "music=" + current + "/" + max + " mode=" + audioManager.getMode());
            if (max <= 0) return;
            int minimum = Math.max(1, (int) Math.ceil(max * MIN_PLAYBACK_VOLUME_RATIO));
            if (current < minimum) {
                audioManager.setStreamVolume(AudioManager.STREAM_MUSIC, minimum, 0);
                recordDiagnostic("native_audio_output.volume_adjusted", "music=" + current + "/" + max + " -> " + minimum + "/" + max);
            }
        }

        private int applyPcm16Gain(byte[] pcm, double gain) {
            if (gain <= 0.0 || gain == 1.0) return 0;
            int clipped = 0;
            for (int i = 0; i + 1 < pcm.length; i += 2) {
                int sample = (pcm[i] & 0xff) | (pcm[i + 1] << 8);
                int amplified = (int) Math.round(sample * gain);
                if (amplified > Short.MAX_VALUE) {
                    amplified = Short.MAX_VALUE;
                    clipped++;
                } else if (amplified < Short.MIN_VALUE) {
                    amplified = Short.MIN_VALUE;
                    clipped++;
                }
                pcm[i] = (byte) (amplified & 0xff);
                pcm[i + 1] = (byte) ((amplified >> 8) & 0xff);
            }
            return clipped;
        }

        private void readNativeAudioLoop(int samplesPerChunk) {
            byte[] buffer = new byte[samplesPerChunk * 2];
            while (running.get()) {
                AudioRecord activeRecorder;
                synchronized (lock) {
                    activeRecorder = recorder;
                }
                if (activeRecorder == null) return;
                int bytesRead = activeRecorder.read(buffer, 0, buffer.length);
                if (bytesRead <= 0) {
                    recordDiagnostic("native_audio_bridge.read_error", "bytesRead=" + bytesRead);
                    continue;
                }
                String encoded = Base64.encodeToString(copyOf(buffer, bytesRead), Base64.NO_WRAP);
                synchronized (lock) {
                    chunkQueue.addLast(encoded);
                    while (chunkQueue.size() > MAX_QUEUED_CHUNKS) {
                        chunkQueue.removeFirst();
                    }
                }
            }
        }

        private byte[] copyOf(byte[] source, int length) {
            ByteArrayOutputStream out = new ByteArrayOutputStream(length);
            out.write(source, 0, length);
            return out.toByteArray();
        }

        private void stopCaptureLocked() {
            running.set(false);
            chunkQueue.clear();
            releaseVoiceProcessingEffects();
            if (recorder != null) {
                try {
                    recorder.stop();
                } catch (Exception ignored) {
                }
                recorder.release();
                recorder = null;
            }
            if (readerThread != null) {
                try {
                    readerThread.join(100);
                } catch (InterruptedException ignored) {
                    Thread.currentThread().interrupt();
                }
                readerThread = null;
            }
            restoreAudioMode();
            restoreBluetoothAudioRouteIfIdle();
            recordDiagnostic("native_audio_bridge.stop", "stopped");
        }

        private void setCommunicationAudioMode() {
            AudioManager audioManager = getAudioManager();
            if (audioManager == null) return;
            if (previousAudioMode == null) {
                previousAudioMode = audioManager.getMode();
            }
            audioManager.setMode(AudioManager.MODE_IN_COMMUNICATION);
            recordDiagnostic("native_audio_bridge.audio_mode", "mode=MODE_IN_COMMUNICATION");
        }

        private void restoreAudioMode() {
            AudioManager audioManager = getAudioManager();
            if (audioManager == null || previousAudioMode == null) return;
            audioManager.setMode(previousAudioMode);
            recordDiagnostic("native_audio_bridge.audio_mode", "mode=" + previousAudioMode);
            previousAudioMode = null;
        }

        private AudioDeviceInfo preferBluetoothAudioRoute(boolean needsInput, String reason) {
            if (!hasBluetoothConnectPermission()) {
                requestBluetoothConnectPermission();
                recordDiagnostic("native_audio_route.bluetooth", "permission_missing reason=" + reason);
                return null;
            }
            AudioManager audioManager = getAudioManager();
            if (audioManager == null) return null;
            try {
                AudioDeviceInfo preferred = findPreferredBluetoothDevice(audioManager, needsInput);
                if (preferred == null) {
                    recordDiagnostic("native_audio_route.bluetooth", "none_connected reason=" + reason);
                    return null;
                }
                if (previousCommunicationDevice == null) {
                    previousCommunicationDevice = audioManager.getCommunicationDevice();
                }
                boolean selected = audioManager.setCommunicationDevice(preferred);
                bluetoothRouteActive = selected || bluetoothRouteActive;
                recordDiagnostic("native_audio_route.bluetooth",
                        (selected ? "selected " : "select_failed ")
                                + describeAudioDevice(preferred)
                                + " reason=" + reason);
                return selected ? preferred : null;
            } catch (SecurityException e) {
                recordDiagnostic("native_audio_route.bluetooth", "security=" + e.getMessage());
                requestBluetoothConnectPermission();
                return null;
            } catch (Exception e) {
                recordDiagnostic("native_audio_route.bluetooth", e.getClass().getSimpleName() + ": " + e.getMessage());
                return null;
            }
        }

        private AudioDeviceInfo findPreferredBluetoothDevice(AudioManager audioManager, boolean needsInput) {
            AudioDeviceInfo[] devices = audioManager.getDevices(
                    needsInput ? AudioManager.GET_DEVICES_INPUTS : AudioManager.GET_DEVICES_OUTPUTS);
            AudioDeviceInfo fallback = null;
            for (AudioDeviceInfo device : devices) {
                if (!isBluetoothAudioDevice(device)) continue;
                if (needsInput && !device.isSource()) continue;
                if (!needsInput && !device.isSink()) continue;
                int type = device.getType();
                if (type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO
                        || type == AudioDeviceInfo.TYPE_BLE_HEADSET) {
                    return device;
                }
                if (!needsInput && (type == AudioDeviceInfo.TYPE_BLUETOOTH_A2DP
                        || type == AudioDeviceInfo.TYPE_BLE_SPEAKER
                        || type == AudioDeviceInfo.TYPE_BLE_BROADCAST)) {
                    return device;
                }
                if (fallback == null) fallback = device;
            }
            return fallback;
        }

        private boolean isBluetoothAudioDevice(AudioDeviceInfo device) {
            int type = device.getType();
            return type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO
                    || type == AudioDeviceInfo.TYPE_BLUETOOTH_A2DP
                    || type == AudioDeviceInfo.TYPE_BLE_HEADSET
                    || type == AudioDeviceInfo.TYPE_BLE_SPEAKER
                    || type == AudioDeviceInfo.TYPE_BLE_BROADCAST;
        }

        private String describeAudioDevice(AudioDeviceInfo device) {
            String name = device.getProductName() == null ? "unknown" : device.getProductName().toString();
            return "id=" + device.getId()
                    + " type=" + device.getType()
                    + " name=" + name;
        }

        private void restoreBluetoothAudioRouteIfIdle() {
            if (running.get() || outputRunning.get() || !bluetoothRouteActive) return;
            if (!hasBluetoothConnectPermission()) return;
            AudioManager audioManager = getAudioManager();
            if (audioManager == null) return;
            try {
                if (previousCommunicationDevice != null) {
                    audioManager.setCommunicationDevice(previousCommunicationDevice);
                    recordDiagnostic("native_audio_route.restore", describeAudioDevice(previousCommunicationDevice));
                } else {
                    audioManager.clearCommunicationDevice();
                    recordDiagnostic("native_audio_route.restore", "default");
                }
            } catch (Exception e) {
                recordDiagnostic("native_audio_route.restore", e.getClass().getSimpleName() + ": " + e.getMessage());
            } finally {
                previousCommunicationDevice = null;
                bluetoothRouteActive = false;
            }
        }

        private void shutdown() {
            synchronized (lock) {
                stopCaptureLocked();
            }
            synchronized (outputLock) {
                outputQueue.clear();
                outputLock.notifyAll();
            }
            restoreBluetoothAudioRouteIfIdle();
        }

        private AudioManager getAudioManager() {
            return (AudioManager) MainActivity.this.getSystemService(Context.AUDIO_SERVICE);
        }

        private void enableVoiceProcessingEffects(int audioSessionId) {
            acousticEchoCanceler = enableAcousticEchoCanceler(audioSessionId);
            noiseSuppressor = enableNoiseSuppressor(audioSessionId);
            automaticGainControl = enableAutomaticGainControl(audioSessionId);
        }

        private AcousticEchoCanceler enableAcousticEchoCanceler(int audioSessionId) {
            if (!AcousticEchoCanceler.isAvailable()) {
                recordDiagnostic("native_audio_bridge.effect", "aec=unavailable");
                return null;
            }
            AcousticEchoCanceler effect = AcousticEchoCanceler.create(audioSessionId);
            if (effect == null) {
                recordDiagnostic("native_audio_bridge.effect", "aec=create_failed");
                return null;
            }
            effect.setEnabled(true);
            recordDiagnostic("native_audio_bridge.effect", "aec=" + effect.getEnabled());
            return effect;
        }

        private NoiseSuppressor enableNoiseSuppressor(int audioSessionId) {
            if (!NoiseSuppressor.isAvailable()) {
                recordDiagnostic("native_audio_bridge.effect", "ns=unavailable");
                return null;
            }
            NoiseSuppressor effect = NoiseSuppressor.create(audioSessionId);
            if (effect == null) {
                recordDiagnostic("native_audio_bridge.effect", "ns=create_failed");
                return null;
            }
            effect.setEnabled(true);
            recordDiagnostic("native_audio_bridge.effect", "ns=" + effect.getEnabled());
            return effect;
        }

        private AutomaticGainControl enableAutomaticGainControl(int audioSessionId) {
            if (!AutomaticGainControl.isAvailable()) {
                recordDiagnostic("native_audio_bridge.effect", "agc=unavailable");
                return null;
            }
            AutomaticGainControl effect = AutomaticGainControl.create(audioSessionId);
            if (effect == null) {
                recordDiagnostic("native_audio_bridge.effect", "agc=create_failed");
                return null;
            }
            effect.setEnabled(true);
            recordDiagnostic("native_audio_bridge.effect", "agc=" + effect.getEnabled());
            return effect;
        }

        private void releaseVoiceProcessingEffects() {
            if (acousticEchoCanceler != null) {
                acousticEchoCanceler.release();
                acousticEchoCanceler = null;
            }
            if (noiseSuppressor != null) {
                noiseSuppressor.release();
                noiseSuppressor = null;
            }
            if (automaticGainControl != null) {
                automaticGainControl.release();
                automaticGainControl = null;
            }
        }
    }

    private boolean hasNotificationPermission() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return true;
        return checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED;
    }

    private void ensureNotificationPermission() {
        if (hasNotificationPermission()) return;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, REQUEST_POST_NOTIFICATIONS);
        }
    }

    private void createNotificationChannel() {
        NotificationManager manager = getNotificationManager();
        if (manager == null || Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return;
        NotificationChannel channel = new NotificationChannel(
                NOTIFICATION_CHANNEL_ID,
                "OpenClaw updates",
                NotificationManager.IMPORTANCE_DEFAULT);
        channel.setDescription("Notifications from the OpenClaw dashboard");
        channel.setShowBadge(true);
        manager.createNotificationChannel(channel);
    }

    private void postNativeNotification(String title, String optionsJson) {
        if (!hasNotificationPermission()) {
            ensureNotificationPermission();
            recordDiagnostic("native_notification.skipped", "POST_NOTIFICATIONS permission missing");
            return;
        }
        String safeTitle = title == null || title.trim().isEmpty() ? "OpenClaw" : title.trim();
        String body = "";
        try {
            if (optionsJson != null && !optionsJson.isEmpty()) {
                JSONObject options = new JSONObject(optionsJson);
                body = options.optString("body", "");
            }
        } catch (Exception e) {
            recordDiagnostic("native_notification.parse_error", e.getMessage());
        }
        int count = prefs.getInt(PREF_NOTIFICATION_COUNT, 0) + 1;
        prefs.edit().putInt(PREF_NOTIFICATION_COUNT, count).apply();
        NotificationManager manager = getNotificationManager();
        if (manager == null) return;
        Intent intent = new Intent(this, MainActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent contentIntent = PendingIntent.getActivity(
                this,
                0,
                intent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification notification = new Notification.Builder(this, NOTIFICATION_CHANNEL_ID)
                .setSmallIcon(android.R.drawable.stat_notify_more)
                .setContentTitle(safeTitle)
                .setContentText(body.isEmpty() ? "OpenClaw has a new update." : body)
                .setStyle(new Notification.BigTextStyle().bigText(body.isEmpty() ? safeTitle : body))
                .setAutoCancel(true)
                .setContentIntent(contentIntent)
                .setNumber(count)
                .setBadgeIconType(Notification.BADGE_ICON_SMALL)
                .build();
        manager.notify(NOTIFICATION_ID, notification);
        recordDiagnostic("native_notification.posted", safeTitle + " count=" + count);
    }

    private void clearNativeNotifications() {
        prefs.edit().putInt(PREF_NOTIFICATION_COUNT, 0).apply();
        NotificationManager manager = getNotificationManager();
        if (manager != null) {
            manager.cancel(NOTIFICATION_ID);
        }
        recordDiagnostic("native_notification.cleared", "count=0");
    }

    private NotificationManager getNotificationManager() {
        return (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
    }

    private static final class HostsFileDns implements Dns {
        @Override
        public List<InetAddress> lookup(String hostname) throws UnknownHostException {
            try {
                return Dns.SYSTEM.lookup(hostname);
            } catch (UnknownHostException firstError) {
                InetAddress mapped = lookupHostsFile(hostname);
                if (mapped != null) {
                    return Collections.singletonList(mapped);
                }
                throw firstError;
            }
        }

        private InetAddress lookupHostsFile(String hostname) {
            try (BufferedReader reader = new BufferedReader(new FileReader("/etc/hosts"))) {
                String normalizedHost = hostname == null ? "" : hostname.trim().toLowerCase();
                String line;
                while ((line = reader.readLine()) != null) {
                    String trimmed = line.trim();
                    if (trimmed.isEmpty() || trimmed.startsWith("#")) continue;
                    int commentIndex = trimmed.indexOf('#');
                    if (commentIndex >= 0) {
                        trimmed = trimmed.substring(0, commentIndex).trim();
                    }
                    String[] parts = trimmed.split("\\s+");
                    if (parts.length < 2) continue;
                    for (int i = 1; i < parts.length; i++) {
                        if (normalizedHost.equals(parts[i].trim().toLowerCase())) {
                            return InetAddress.getByName(parts[0]);
                        }
                    }
                }
            } catch (IOException ignored) {
                return null;
            }
            return null;
        }
    }

    private LinearLayout section() {
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        layout.setPadding(dp(14), dp(10), dp(14), dp(10));
        layout.setBackgroundColor(COLOR_PANEL);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(-1, -2);
        lp.setMargins(dp(14), dp(4), dp(14), 0);
        layout.setLayoutParams(lp);
        return layout;
    }

    private TextView label(String value) {
        TextView view = text(value, 12, COLOR_TEXT_MUTED, true);
        view.setPadding(0, dp(6), 0, dp(3));
        return view;
    }

    private EditText input(String hint, boolean password, boolean singleLine) {
        EditText input = new EditText(this);
        input.setHint(hint);
        input.setSingleLine(singleLine);
        input.setMinLines(singleLine ? 1 : 3);
        input.setMaxLines(singleLine ? 1 : 6);
        input.setTextSize(14);
        if (password) input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        else if (singleLine) input.setInputType(InputType.TYPE_CLASS_TEXT);
        else input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_MULTI_LINE);
        input.setPadding(dp(10), dp(8), dp(10), dp(8));
        input.setTextColor(COLOR_TEXT_PRIMARY);
        input.setHintTextColor(COLOR_TEXT_MUTED);
        input.setBackgroundColor(COLOR_CONTROL);
        return input;
    }

    private Button button(String text) {
        Button button = new Button(this);
        button.setText(text);
        button.setAllCaps(false);
        button.setTextColor(COLOR_TEXT_PRIMARY);
        button.setTextSize(13);
        button.setPadding(dp(8), 0, dp(8), 0);
        button.setMinWidth(0);
        button.setMinimumWidth(0);
        button.setMinHeight(0);
        button.setMinimumHeight(0);
        GradientDrawable background = new GradientDrawable();
        background.setColor(COLOR_CONTROL);
        background.setCornerRadius(dp(6));
        background.setStroke(dp(1), COLOR_ACCENT);
        button.setBackground(background);
        return button;
    }

    private GradientDrawable panelBackground(int color, int radiusPx) {
        GradientDrawable background = new GradientDrawable();
        background.setColor(color);
        background.setCornerRadius(radiusPx);
        background.setStroke(dp(1), Color.rgb(48, 60, 72));
        return background;
    }

    private TextView text(String value, int sp, int color, boolean bold) {
        TextView view = new TextView(this);
        view.setText(value);
        view.setTextSize(sp);
        view.setTextColor(color);
        if (bold) view.setTypeface(android.graphics.Typeface.DEFAULT_BOLD);
        return view;
    }

    private String value(EditText input) {
        return input.getText().toString().trim();
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    private final class DashboardNodeListener implements OpenClawClient.Listener {
        @Override
        public void onStatus(String status) {
            runOnUiThread(() -> {
                if (nodeStatusText != null) nodeStatusText.setText(status);
                recordDiagnostic("node.status", status);
            });
        }

        @Override
        public void onConnected(JSONObject hello) {
            runOnUiThread(() -> {
                if (nodeStatusText != null) {
                    nodeStatusText.setText("Custom dashboard node connected. Approve pairing if the gateway requested it.");
                }
                recordDiagnostic("node.connected", hello == null ? "{}" : hello.toString());
            });
        }

        @Override
        public void onDashboard(JSONObject dashboard) {
            recordDiagnostic("node.dashboard", dashboard == null ? "{}" : dashboard.toString());
        }

        @Override
        public void onLog(String message) {
            recordDiagnostic("node.log", message);
        }

        @Override
        public void onError(String message) {
            runOnUiThread(() -> {
                if (nodeStatusText != null) nodeStatusText.setText("Node: " + message);
                recordDiagnostic("node.error", message);
            });
        }
    }
}
